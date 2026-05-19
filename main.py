"""
main.py — 키움 국내주식 자동매매 통합 메인 스케줄러  v3.0.0
종가베팅 + 단타 + 상한가선진입 3전략 단일 프로세스 운영

[종가베팅 스케줄 (KST 평일)]
  09:00        — API 토큰 갱신
  14:30        — 후보 종목 사전 스캔 (텔레그램 알림)
  15:10        — 눌림 확인 + 자동 매수
  15:30        — 잠금 초기화
  08:00~08:50  — D+1 NXT 프리마켓 매도 감시 (매분)
  09:00~10:00  — D+1 오전 익절/손절 감시 (매분)
  15:00        — D+1 미청산 강제 청산
  06:00        — 로그 7일 초과분 삭제

[단타 스케줄 (KST 평일)]
  08:50        — 단타 장전 준비 (거래량 캐시 + 잔고 확인)
  09:00~15:20  — 30초 주기 스캔/매매 루프
  13:00        — 신규 진입 중단 (포지션 감시만 계속)
  15:10        — 강제 청산 경고 알림
  15:20        — 전량 강제 청산
  15:30        — 단타 당일 결산 리포트

[상한가선진입 스케줄 (KST 평일) — v3.0 NEW]
  08:50        — 전략C 장전 초기화 (거래량 캐시 + 포지션 복원)
  08:00~09:30  — 익일 아침 갭 매도 감시 (매분)
  09:00~14:30  — 장중 스캔·진입·청산 루프 (30초)
  09:25        — 강제청산 5분 전 경고
  09:30        — 익일 미청산 전량 강제청산
  15:40        — 전략C 당일 결산 리포트

[v3.0 변경]
  전략C 상한가선진입 추가 — 25%+ 구간 7대 신호 복합분석
  PositionRegistry — 3전략 충돌·자금 경합 방지
  TelegramBot — limit_strategy / limit_cfg / limit_scanner / registry 주입
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from broker import KiwoomBroker
from config import Config
from strategy import Strategy
from strategy_config import StrategyConfig
from telegram_bot import TelegramBot

# ── 단타 전략 모듈 ──────────────────────────────────────────
from scalp_config import ScalpConfig
from scanner import DayTradingScanner
from strategy_scalping import ScalpStrategy
from market_filter import MarketFilter
from fib_reentry import FibReentryManager

# ── 전략C 상한가선진입 모듈 (v3.0 NEW) ─────────────────────
from position_registry import PositionRegistry, Strategy as Strat
from limit_config      import LimitConfig
from limit_scanner     import LimitScanner
from strategy_limit    import LimitStrategy

# ── 성과 추적 모듈 (v3.1 NEW) ──────────────────────────────
from performance_tracker import PerformanceTracker


# ──────────────────────────────────────────────────────────────
# 거래일 판별 — 주말 + 한국 공휴일 체크
# ──────────────────────────────────────────────────────────────

# 한국 증권시장 공휴일 (매년 초 업데이트 필요)
# 출처: 한국거래소(KRX) 공식 휴장일 기준
_KRX_HOLIDAYS: set[str] = {
    # 2025년
    "20250101","20250128","20250129","20250130","20250301",
    "20250505","20250506","20250506","20250606","20250815",
    "20251003","20251009","20251007","20251008","20251009","20251225",
    # 2026년
    "20260101","20260127","20260128","20260129","20260301",
    "20260505","20260606","20260815","20260924","20260925","20260926",
    "20261009","20261225","20261231",
}


def is_trading_day(dt: datetime | None = None) -> bool:
    """
    주어진 날짜가 국내 증권시장 거래일인지 판별

    거래일 조건:
      - 평일 (월~금, weekday 0~4)
      - KRX 공휴일 아님

    Args:
        dt: 판별할 datetime (None 이면 현재 KST 기준)

    Returns:
        True = 거래일, False = 비거래일 (주말/공휴일)
    """
    if dt is None:
        dt = datetime.now(KST)

    # 주말 체크 (5=토, 6=일)
    if dt.weekday() >= 5:
        return False

    # 공휴일 체크
    date_str = dt.strftime("%Y%m%d")
    if date_str in _KRX_HOLIDAYS:
        return False

    return True


def assert_trading_day(job_name: str = "") -> bool:
    """
    비거래일이면 로그를 남기고 False 반환
    스케줄 작업 최상단에서 호출하는 가드 함수
    """
    now = datetime.now(KST)
    if not is_trading_day(now):
        weekday_str = ["월","화","수","목","금","토","일"][now.weekday()]
        reason      = "주말" if now.weekday() >= 5 else "공휴일"
        logger.debug(
            f"[Scheduler] {job_name} 스킵 — "
            f"{now.strftime('%Y-%m-%d')}({weekday_str}) {reason}"
        )
        return False
    return True

# ──────────────────────────────────────────────────────────────
# 로그 설정
# ──────────────────────────────────────────────────────────────
KST     = pytz.timezone("Asia/Seoul")
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def setup_logging():
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = TimedRotatingFileHandler(
        LOG_DIR / "kiwoom_trader.log",
        when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    eh = TimedRotatingFileHandler(
        LOG_DIR / "kiwoom_error.log",
        when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    eh.setFormatter(fmt)
    eh.setLevel(logging.ERROR)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(eh)
    root.addHandler(ch)


setup_logging()
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# 전역 컴포넌트 초기화
# ──────────────────────────────────────────────────────────────

# 종가베팅 컴포넌트
broker   = KiwoomBroker()
cfg      = Config()
scfg     = StrategyConfig()
strategy = Strategy(broker, scfg)

# 단타 컴포넌트
scalp_cfg       = ScalpConfig()
scalp_scanner   = DayTradingScanner(broker, scalp_cfg)
scalp_strategy  = ScalpStrategy(broker, scalp_cfg)
market_filter   = MarketFilter(broker)
fib_mgr         = FibReentryManager()
scalp_strategy.fib_mgr = fib_mgr   # Fib 재진입 매니저 주입

# ── 전략C 컴포넌트 (v3.0 NEW) ───────────────────────────────
registry       = PositionRegistry()
limit_cfg      = LimitConfig()
limit_strategy = LimitStrategy(broker, limit_cfg)
limit_scanner  = LimitScanner(broker, limit_cfg, limit_strategy)

# ── 성과 추적기 (v3.1 NEW) ───────────────────────────────────
tracker = PerformanceTracker()

# 텔레그램 봇 (3전략 컴포넌트 모두 주입)
bot = TelegramBot(
    broker, cfg, scfg, strategy,
    scanner         = scalp_scanner,
    scalp_strategy  = scalp_strategy,
    scalp_cfg       = scalp_cfg,
    limit_strategy  = limit_strategy,
    limit_cfg       = limit_cfg,
    limit_scanner   = limit_scanner,
    registry        = registry,
    tracker         = tracker,
)


# ──────────────────────────────────────────────────────────────
# ① 종가베팅 스케줄 작업
# ──────────────────────────────────────────────────────────────

async def job_token_refresh():
    """09:00 — API 토큰 갱신 (거래일에만)"""
    if not assert_trading_day("token_refresh"):
        return
    try:
        broker._get_token(force=True)
        logger.info("[Scheduler] 토큰 갱신 완료")
    except Exception as e:
        logger.error(f"[Scheduler] 토큰 갱신 실패: {e}")
        await bot.notify_error(f"토큰 갱신 실패: {e}")


async def job_pre_scan():
    """14:30 — 종가베팅 후보 종목 사전 스캔 (거래일에만)"""
    if not assert_trading_day("pre_scan"):
        return
    logger.info("[Scheduler] 14:30 종가베팅 사전 스캔 시작")
    try:
        candidates = strategy.scan_candidates()
        if candidates:
            await bot.notify_scan_result(candidates)
            logger.info(f"[Scheduler] 사전 스캔 완료 — {len(candidates)}개")
        else:
            await bot.send("📡 <b>[ 14:30 종가베팅 스캔 ]</b>\n조건 충족 종목 없음")
    except Exception as e:
        logger.error(f"[Scheduler] 사전 스캔 오류: {e}")
        await bot.notify_error(f"사전 스캔 오류: {e}")


async def job_auto_buy():
    """15:10 — 종가베팅 눌림 확인 후 자동 매수 (거래일에만)"""
    if not assert_trading_day("auto_buy"):
        return
    logger.info("[Scheduler] 15:10 종가베팅 자동 매수 시작")
    try:
        balance    = broker.get_balance()
        held_count = len(balance["holdings"])
        max_pos    = scfg.get_entry()["max_positions"]
        available  = balance["cash"]

        if held_count >= max_pos:
            msg = f"⛔ 종가베팅 자동 매수 스킵 — 최대 보유({max_pos}개) 달성"
            logger.info(f"[Scheduler] {msg}")
            await bot.send(msg)
            return

        all_candidates = strategy.scan_candidates()
        if not all_candidates:
            await bot.send("📡 <b>[ 15:10 종가베팅 ]</b>\n조건 충족 종목 없음")
            return

        buy_slots  = max_pos - held_count
        candidates = all_candidates[:buy_slots * 2]
        bought     = 0

        for c in candidates:
            if bought >= buy_slots:
                break
            code = c["code"]
            name = c["name"]
            if cfg.check_lock(code):
                continue

            cur_price  = c.get("cur_price", 0)
            prev_close = c.get("prev_close", 0)

            # ── [v3.0] 레지스트리 충돌 방지 ──────────────────
            _ok, _reason = registry.can_buy(
                code, Strat.CLOSE_BET, cur_price * 10, available
            )
            if not _ok:
                logger.info(f"[CloseBet] {name}({code}) 레지스트리 거부: {_reason}")
                continue
            # ──────────────────────────────────────────────────

            if prev_close <= 0:
                for attempt in range(3):
                    try:
                        import time as _t
                        _t.sleep(0.5 * (attempt + 1))
                        prev_close = broker.get_prev_close(code)
                        break
                    except Exception as e:
                        if "429" in str(e) and attempt < 2:
                            logger.warning(f"[Scheduler] {code} 429 재시도 {attempt+1}")
                        else:
                            prev_close = 0
                            break

            if cur_price <= 0 or prev_close <= 0:
                continue

            entry = strategy.check_entry_signal(code, cur_price, prev_close)
            if not entry["signal"]:
                continue

            qty = strategy.calculate_buy_qty(code, cur_price, available)
            if qty <= 0:
                continue

            result = broker.buy_order(code, qty, cur_price, "0")
            if result["success"]:
                cfg.add_ledger_record(code, "BUY", cur_price, qty)
                cfg.set_lock(code)
                registry.register(code, Strat.CLOSE_BET, qty, cur_price)  # v3.0
                await bot.notify_buy(
                    code, name, qty, cur_price,
                    f"눌림 {entry['pullback_pct']:+.1f}% / 점수:{c['score']}"
                )
                available -= cur_price * qty
                bought    += 1
                logger.info(f"[Scheduler] 종가베팅 매수: {name}({code}) {qty}주 @{cur_price:,}원")
            else:
                logger.error(f"[Scheduler] 매수 실패: {code}")

        if bought == 0:
            await bot.send("📡 <b>[ 15:10 종가베팅 ]</b>\n눌림 조건 충족 종목 없음")

    except Exception as e:
        logger.error(f"[Scheduler] 자동 매수 오류: {e}")
        await bot.notify_error(f"자동 매수 오류: {e}")


async def job_monitor_exit():
    """매분 — 종가베팅 D+1 익절/손절 감시 (거래일에만)"""
    if not assert_trading_day("monitor_exit"):
        return
    now = datetime.now(KST).strftime("%H:%M")
    in_nxt     = "08:00" <= now <= "08:50"
    in_morning = "09:00" <= now <= "10:00"
    in_force   = now >= "15:00"
    if not (in_nxt or in_morning or in_force):
        return
    try:
        balance  = broker.get_balance()
        holdings = balance["holdings"]
        if not holdings:
            return
        for h in holdings:
            code      = h["code"]
            name      = h["name"]
            cur_price = h["cur_price"]
            qty       = h["qty"]
            pos       = cfg.get_position(code)
            buy_price = pos["avg_price"] if pos["avg_price"] > 0 else h["avg_price"]
            exit_sig  = strategy.check_exit_signal(code, cur_price, buy_price, qty)
            if exit_sig["signal"] == "HOLD":
                continue
            sell_qty = exit_sig["qty"]
            result   = broker.sell_order(code, sell_qty, 0, "3")
            if result["success"]:
                cfg.add_ledger_record(code, "SELL", cur_price, sell_qty)
                pnl = (cur_price - buy_price) * sell_qty
                tracker.record_sell(          # v3.1
                    strategy="A", code=code, name=name,
                    buy_price=buy_price, sell_price=cur_price,
                    qty=sell_qty, reason=exit_sig["reason"],
                    hold_hours=(datetime.now(KST) - datetime.fromisoformat(
                        pos.get("buy_time", datetime.now(KST).isoformat())
                    ).replace(tzinfo=KST)).total_seconds() / 3600
                    if pos.get("buy_time") else 0,
                )
                await bot.notify_sell(code, name, sell_qty, cur_price,
                                      buy_price, exit_sig["reason"])
    except Exception as e:
        logger.error(f"[Scheduler] 종가베팅 매도 감시 오류: {e}")


async def job_reset_locks():
    """15:30 — 종가베팅 전체 잠금 초기화"""
    cfg.release_all_locks()
    registry.reset_daily()   # v3.0: 일일 손익 리셋
    logger.info("[Scheduler] 종가베팅 잠금 초기화 + 레지스트리 일일 리셋 완료")


async def job_cleanup_logs():
    """06:00 — 7일 초과 로그 파일 삭제"""
    import time
    now_ts  = time.time()
    deleted = 0
    for f in LOG_DIR.glob("*.log.*"):
        if now_ts - f.stat().st_mtime > 7 * 86400:
            f.unlink()
            deleted += 1
    if deleted:
        logger.info(f"[Scheduler] 로그 {deleted}개 삭제")


# ──────────────────────────────────────────────────────────────
# ② 단타 스케줄 작업
# ──────────────────────────────────────────────────────────────

async def job_scalp_pre_market():
    """08:50 — 단타 장전 준비 (거래일에만 실행)"""
    if not assert_trading_day("scalp_pre_market"):
        return
    logger.info("[Scalp] 08:50 단타 장전 준비 시작")
    try:
        balance = broker.get_balance()
        cash    = balance["cash"]
        scalp_strategy.init_daily(cash)
        scalp_strategy.load_positions()
        logger.info(f"[Scalp] 잔고 확인 — 가용 현금: {cash:,}원")

        # ── [v1.1] 계좌 불일치 자동 정리 ─────────────────────
        removed = scalp_strategy.sync_with_account()
        if removed:
            logger.warning(f"[Scalp] 불일치 정리: {removed}")
            await bot.send(
                f"⚠️ <b>[단타 포지션 불일치 정리]</b>\n"
                f"실제 계좌에 없는 포지션 {len(removed)}개 삭제\n"
                f"종목: {', '.join(removed)}"
            )
    except Exception as e:
        logger.error(f"[Scalp] 잔고 조회 실패: {e}")
        cash = 0

    try:
        # 전일 거래량 캐시 구축 (blocking → thread로)
        await asyncio.to_thread(scalp_scanner.init_daily)
        logger.info("[Scalp] 스캐너 초기화 완료")
    except Exception as e:
        logger.error(f"[Scalp] 스캐너 초기화 실패: {e}")

    await bot.send(
        f"<b>[ 단타봇 장전 준비 완료 ]</b> "
        f"{datetime.now(KST).strftime('%H:%M')}\n\n"
        f"가용 현금: <b>{cash:,}원</b>\n"
        f"진입 마감: {scalp_cfg.get_scan()['entry_end_time']} "
        f"| 강제 청산: {scalp_cfg.get_exit()['force_exit_time']}\n"
        f"⚡ /scalp_scan 으로 즉시 스캔 가능"
    )


async def job_scalp_loop():
    """
    30초 주기 — 단타 핵심 루프 (거래일 09:00~15:25만 실행)
    1. 보유 포지션 청산 감시
    2. 신규 진입 스캔
    """
    # 주말/공휴일 스킵
    if not assert_trading_day("scalp_loop"):
        return

    now_str = datetime.now(KST).strftime("%H:%M")

    # 장 시간 외 스킵
    if not ("09:00" <= now_str <= "15:25"):
        return

    # ── 보유 포지션 청산 감시 ─────────────────────────────────
    positions = scalp_strategy.get_positions()
    for pos in positions:
        try:
            info = await asyncio.to_thread(broker.get_stock_info, pos.code)
            if not info:
                continue
            cur_price = info["cur_price"]
            exit_sig  = scalp_strategy.check_exit(pos, cur_price)

            if exit_sig["signal"] == "HOLD":
                continue

            sell_qty = exit_sig["qty"]
            result   = await asyncio.to_thread(
                broker.sell_order, pos.code, sell_qty, 0, "3"
            )
            if result["success"]:
                # 매도 성공 시 실패 카운터 초기화
                scalp_strategy._sell_fail_count.pop(pos.code, None)
                trade = scalp_strategy.remove_position(
                    pos.code, cur_price, sell_qty, exit_sig["reason"]
                )
                if trade:
                    await bot.notify_scalp_sell(
                        pos.code, pos.name, sell_qty,
                        cur_price, pos.buy_price,
                        reason   = exit_sig["reason"],
                        buy_time = pos.buy_time,
                        source   = getattr(pos, "source", ""),
                        score    = getattr(pos, "score",  0),
                    )
                    logger.info(
                        f"[Scalp] 청산: {pos.name}({pos.code}) "
                        f"{sell_qty}주 {trade['profit']:+,}원 — {exit_sig['reason']}"
                    )
            else:
                # ── [v1.1] 매도 실패 처리 ─────────────────────────
                error_code = str(result.get("error_code", ""))
                error_msg  = str(result.get("error_msg",  ""))
                handle = scalp_strategy.handle_sell_failure(
                    pos.code, error_code, error_msg
                )
                logger.error(
                    f"[Scalp] 매도 실패: {pos.code} "
                    f"[{error_code}] {error_msg} → {handle['action']}"
                )
                # 강제 삭제된 경우 텔레그램 알림
                if handle["action"] == "force_removed":
                    await bot.send(
                        f"⚠️ <b>[단타 포지션 강제 삭제]</b>\n"
                        f"{pos.name}({pos.code})\n"
                        f"사유: 매도불가 {error_code or '연속실패'}\n"
                        f"<i>실제 계좌와 불일치 → 포지션 기록 삭제</i>"
                    )

        except Exception as e:
            logger.error(f"[Scalp] 포지션 감시 오류 {pos.code}: {e}")

    # ── 신규 진입 스캔 ────────────────────────────────────────
    # bot.scalp_paused 플래그 또는 진입 마감 시각 이후면 스킵
    if bot.scalp_paused or now_str >= scalp_cfg.get_scan()["entry_end_time"]:
        return

    cfg_entry = scalp_cfg.get_entry()
    if len(scalp_strategy.held_codes()) >= cfg_entry["max_positions"]:
        return

    # ── [v1.1] 시장 상황 필터 ────────────────────────────────
    mkt = market_filter.get_market_state()
    if not mkt["allow_entry"]:
        logger.info(f"[Scalp] 시장 필터 차단 [{mkt['grade']}]: {mkt['reason']}")
        return

    # 시장 등급에 따라 최대 포지션 수 동적 조정
    effective_max = min(cfg_entry["max_positions"], mkt["max_positions"])
    if len(scalp_strategy.held_codes()) >= effective_max:
        logger.info(
            f"[Scalp] 시장 [{mkt['grade']}] 제한: "
            f"현재 {len(scalp_strategy.held_codes())}개 ≥ 허용 {effective_max}개"
        )
        return

    try:
        # [v1.2] Fib 감시 중인 종목 스캔에서 제외 (Fib 반등 전까지)
        fib_watching = set(fib_mgr.get_watching_codes())
        if fib_watching:
            logger.info(
                f"[Scalp] Fib 대기 {len(fib_watching)}개 제외: {fib_watching}"
            )

        # ── Fib 재진입 신호 체크 (30초마다) ─────────────────────
        fib_signals = await asyncio.to_thread(
            fib_mgr.check_all, broker,
            10,   # 손절 후 최소 대기 시간 (분) — fib_min_wait_min 기본값
        )
        for fib_candidate in fib_signals:
            if len(scalp_strategy.held_codes()) >= effective_max:
                break
            code = fib_candidate["code"]
            name = fib_candidate["name"]
            fib_ratio = fib_candidate.get("_fib_ratio", "?")
            fib_low   = fib_candidate.get("_fib_zone_low", 0)
            bounce    = fib_candidate.get("_bounce_pct", 0)
            is_gap    = fib_candidate.get("_is_gap_up", False)

            balance = await asyncio.to_thread(broker.get_balance)
            cash    = balance["cash"]
            if cash <= 0:
                cash = await asyncio.to_thread(broker.get_deposit)

            entry_sig = scalp_strategy.check_entry(fib_candidate, cash)
            if not entry_sig["signal"]:
                logger.info(
                    f"[Scalp] Fib 재진입 거부 {name}({code}): "
                    f"{entry_sig['reason']}"
                )
                continue

            qty   = entry_sig["qty"]
            price = fib_candidate["cur_price"]
            result = await asyncio.to_thread(
                broker.buy_order, code, qty, 0, "3"
            )
            if result["success"]:
                scalp_strategy.add_position(code, name, qty, price)
                await bot.send(
                    f"🔄 <b>[Fib 재진입]</b> {name}({code})\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 Fib {fib_ratio} 레벨 반등 확인\n"
                    f"📉 저점: {fib_low:,}원 → "
                    f"📈 반등: <b>+{bounce:.2f}%</b>\n"
                    f"💰 체결가: <b>{price:,}원</b>  {qty}주\n"
                    f"{'📊 갭상승 기준 Fib' if is_gap else '📊 저점 기준 Fib'}"
                )
                logger.info(
                    f"[Scalp] Fib 재진입 완료: {name}({code}) "
                    f"Fib{fib_ratio} +{bounce:.2f}% {qty}주 @{price:,}원"
                )

        # ── [v1.3] 하이브리드 — 수동 감시 종목 진입 시도 ───────
        # 스캐너 필터를 우회하고 유저가 직접 지정한 종목을 단타 로직으로 매매
        watchlist_active = scalp_strategy.watchlist_get_active()
        if watchlist_active:
            # 가용 현금 조회 (수동 종목도 동일하게 필요)
            try:
                wl_balance = await asyncio.to_thread(broker.get_balance)
                wl_cash    = wl_balance["cash"] or await asyncio.to_thread(broker.get_deposit)
            except Exception:
                wl_cash = 0

            for wl_item in watchlist_active:
                wl_code = wl_item["code"]
                wl_name = wl_item["name"]

                # 이미 보유 중이면 신규 진입 스킵 (청산 감시는 위쪽 루프에서 처리됨)
                if wl_code in scalp_strategy.held_codes():
                    continue

                # Fib 감시 중이면 스킵 (Fib 신호 대기)
                if fib_mgr.is_watching(wl_code):
                    logger.debug(f"[Scalp][수동] {wl_code} Fib 대기 중 — 스킵")
                    continue

                # 최대 포지션 체크
                if len(scalp_strategy.held_codes()) >= effective_max:
                    break

                # 실시간 시세 조회 → candidate 형식으로 변환
                wl_candidate = await asyncio.to_thread(
                    scalp_strategy.build_watchlist_candidate, wl_code
                )
                if not wl_candidate:
                    logger.debug(f"[Scalp][수동] {wl_code} 시세 조회 실패")
                    continue

                # 수동 종목은 스캐너 필터(상승률/거래대금)를 우회하고
                # 진입 조건(포지션 수/현금/쿨다운/시간)만 체크
                entry_sig = scalp_strategy.check_entry(wl_candidate, wl_cash)
                if not entry_sig["signal"]:
                    logger.info(
                        f"[Scalp][수동] {wl_name}({wl_code}) "
                        f"진입 거부: {entry_sig['reason']}"
                    )
                    continue

                qty   = entry_sig["qty"]
                price = wl_candidate["cur_price"]

                result = await asyncio.to_thread(
                    broker.buy_order, wl_code, qty, 0, "3"
                )
                if result["success"]:
                    scalp_strategy.add_position(
                        wl_code, wl_name, qty, price,
                        vwap_at_buy=wl_candidate.get("vwap", 0.0)
                    )
                    await bot.notify_scalp_buy(
                        wl_code, wl_name, qty, price, wl_candidate
                    )
                    wl_cash -= price * qty
                    logger.info(
                        f"[Scalp][수동] 매수 완료: {wl_name}({wl_code}) "
                        f"{qty}주 @{price:,}원"
                    )
                else:
                    logger.error(
                        f"[Scalp][수동] 매수 실패: {wl_code} "
                        f"— {result['raw'].get('return_msg','')}"
                    )

        # ── 일반 신규 진입 스캔 ───────────────────────────────────
        candidates = await asyncio.to_thread(
            scalp_scanner.scan,
            scalp_strategy.held_codes(),
            fib_watching,   # Fib 감시 종목 제외
            False
        )

        if not candidates:
            logger.info("[Scalp] 스캔 결과 후보 없음 — 진입 스킵")
            return

        # ── 가용 현금 조회 ────────────────────────────────────
        balance = await asyncio.to_thread(broker.get_balance)
        cash    = balance["cash"]

        # [버그수정] MOCK 모드에서 cash=0 반환 시 deposit으로 보완
        if cash <= 0:
            try:
                cash = await asyncio.to_thread(broker.get_deposit)
                logger.info(f"[Scalp] cash=0 → deposit 조회: {cash:,}원")
            except Exception:
                pass

        if cash <= 0:
            logger.warning(
                f"[Scalp] 가용 현금 0원 — 매수 불가 "
                f"(MOCK 모드는 모의 잔고가 없을 수 있음)"
            )
            await bot.send(
                f"⚠️ <b>[단타]</b> 후보 {len(candidates)}개 발견됐으나\n"
                f"가용 현금 0원으로 매수 불가\n"
                f"<i>MOCK 모드 잔고 확인 필요</i>"
            )
            return

        logger.info(
            f"[Scalp] 후보 {len(candidates)}개 / 가용현금 {cash:,}원 — 진입 검토"
        )

        # ── 상위 3개 진입 시도 ────────────────────────────────
        for candidate in candidates[:3]:
            code = candidate["code"]
            name = candidate["name"]

            entry_sig = scalp_strategy.check_entry(candidate, cash)

            # [버그수정] 거부 이유를 INFO로 로깅 (기존 debug → 안 보였음)
            if not entry_sig["signal"]:
                logger.info(
                    f"[Scalp] {name}({code}) 진입 거부: {entry_sig['reason']}"
                )
                continue

            qty   = entry_sig["qty"]
            price = candidate["cur_price"]

            result = await asyncio.to_thread(
                broker.buy_order, code, qty, 0, "3"   # 시장가 매수
            )

            if result["success"]:
                scalp_strategy.add_position(
                    code, name, qty, price,
                    vwap_at_buy=candidate.get("vwap", 0.0)
                )
                await bot.notify_scalp_buy(code, name, qty, price, candidate)
                cash -= price * qty
                logger.info(
                    f"[Scalp] 매수 완료: {name}({code}) {qty}주 @{price:,}원"
                )
                break   # 1 사이클 1종목

            else:
                logger.error(
                    f"[Scalp] 매수 주문 실패: {code} — "
                    f"{result['raw'].get('return_msg', '알 수 없는 오류')}"
                )

    except Exception as e:
        logger.error(f"[Scalp] 신규 진입 오류: {e}")
        await bot.notify_error(f"단타 스캔 오류: {e}")


async def job_scalp_force_exit_warn():
    """15:10 — 단타 강제 청산 10분 전 경고 (거래일에만)"""
    if not assert_trading_day("scalp_force_exit_warn"):
        return
    positions = scalp_strategy.get_positions()
    if not positions:
        return
    lines = ["⚠️ <b>[단타] 15:20 강제 청산 10분 전!</b>\n"]
    for pos in positions:
        try:
            info      = broker.get_stock_info(pos.code)
            cur_price = info["cur_price"] if info else pos.buy_price
            pct       = pos.profit_pct(cur_price)
            sign      = "📈" if pct >= 0 else "📉"
            lines.append(
                f"{sign} {pos.name}({pos.code}): {pct:+.1f}% ({pos.qty}주)"
            )
        except Exception:
            lines.append(f"• {pos.code}: 조회 실패")
    await bot.send("\n".join(lines))


async def job_scalp_force_exit():
    """15:20 — 단타 전량 강제 청산 (거래일에만)"""
    if not assert_trading_day("scalp_force_exit"):
        return
    logger.info("[Scalp] 15:20 강제 청산 실행")
    exited = await asyncio.to_thread(scalp_strategy.force_exit_all)
    if exited:
        await bot.send(
            f"⛔ <b>[단타 강제 청산]</b>\n청산 완료: {', '.join(exited)}"
        )
    else:
        logger.info("[Scalp] 강제 청산 대상 없음")


async def job_scalp_daily_report():
    """15:35 — 단타 당일 결산 (거래일에만)"""
    if not assert_trading_day("scalp_daily_report"):
        return
    report = scalp_strategy.daily_summary()
    await bot.send(report)
    logger.info("[Scalp] 당일 결산 완료")


# ──────────────────────────────────────────────────────────────
# ③ 전략C — 상한가선진입 스케줄 작업  v3.0 NEW
# ──────────────────────────────────────────────────────────────

async def job_limit_pre_market():
    """08:50 — 전략C 장전 초기화 (거래일에만)"""
    if not assert_trading_day("limit_pre_market"):
        return
    logger.info("[Limit] 08:50 장전 초기화 시작")
    try:
        balance = broker.get_balance()
        cash    = balance["cash"]
        limit_strategy.init_daily(cash)
        await asyncio.to_thread(limit_scanner.init_daily)
        registry.reset_daily()
        # 복원된 포지션을 레지스트리에 재등록
        for pos in limit_strategy.get_positions():
            registry.register(pos.code, Strat.PRE_LIMIT, pos.qty, pos.buy_price)
        logger.info(f"[Limit] 초기화 완료 — 기준 현금 {cash:,}원")
        await bot.send(
            f"<b>[ 전략C 장전 준비 완료 ]</b> "
            f"{datetime.now(KST).strftime('%H:%M')}\n\n"
            f"현금: <b>{cash:,}원</b>\n"
            f"스캔 중단: {limit_cfg.get_scan()['scan_stop_time']}\n"
            f"강제청산: {limit_cfg.get_exit()['force_sell_time']}"
        )
    except Exception as e:
        logger.error(f"[Limit] 장전 초기화 실패: {e}")
        await bot.notify_error(f"전략C 초기화 실패: {e}")


async def job_limit_morning_exit():
    """08:00~09:30 매분 — 전략C 익일 아침 갭 매도 감시"""
    if not assert_trading_day("limit_morning_exit"):
        return
    now_str = datetime.now(KST).strftime("%H:%M")
    if not ("08:00" <= now_str <= "09:30"):
        return
    for pos in limit_strategy.get_positions():
        try:
            info      = await asyncio.to_thread(broker.get_stock_info, pos.code)
            if not info:
                continue
            cur_price = info["cur_price"]
            exit_sig  = limit_strategy.check_exit_next_day(pos, cur_price)
            if exit_sig["signal"] == "HOLD":
                continue
            result = await asyncio.to_thread(
                broker.sell_order, pos.code, exit_sig["qty"], 0, "3"
            )
            if result["success"]:
                pnl   = pos.profit_won(cur_price)
                limit_strategy.remove_position(
                    pos.code, cur_price, exit_sig["qty"], exit_sig["reason"]
                )
                registry.close(pos.code, reason=exit_sig["reason"], pnl=pnl)
                await bot.notify_limit_sell(
                    pos.code, pos.name, exit_sig["qty"],
                    cur_price, pos.buy_price, exit_sig["reason"]
                )
        except Exception as e:
            logger.error(f"[Limit] 아침 매도 오류 {pos.code}: {e}")


async def job_limit_scan_loop():
    """30초 주기 — 전략C 장중 스캔·진입·청산 루프 (09:00~14:30)"""
    if not assert_trading_day("limit_scan_loop"):
        return
    now_str  = datetime.now(KST).strftime("%H:%M")
    scan_cfg = limit_cfg.get_scan()

    # ── 보유 포지션 장중 청산 감시 ──────────────────────────
    for pos in limit_strategy.get_positions():
        try:
            info = await asyncio.to_thread(broker.get_stock_info, pos.code)
            if not info:
                continue
            cur_price = info["cur_price"]
            exit_sig  = limit_strategy.check_exit(pos, cur_price)
            if exit_sig["signal"] == "HOLD":
                continue
            result = await asyncio.to_thread(
                broker.sell_order, pos.code, exit_sig["qty"], 0, "3"
            )
            if result["success"]:
                pnl = pos.profit_won(cur_price)
                limit_strategy.remove_position(
                    pos.code, cur_price, exit_sig["qty"], exit_sig["reason"]
                )
                registry.close(pos.code, reason=exit_sig["reason"], pnl=pnl)
                await bot.notify_limit_sell(
                    pos.code, pos.name, exit_sig["qty"],
                    cur_price, pos.buy_price, exit_sig["reason"]
                )
        except Exception as e:
            logger.error(f"[Limit] 장중 청산 오류 {pos.code}: {e}")

    # ── 신규 진입 스캔 ────────────────────────────────────
    if now_str >= scan_cfg["scan_stop_time"]:
        return
    if limit_strategy.is_paused:
        return
    if len(limit_strategy.held_codes()) >= limit_cfg.get_entry()["max_positions"]:
        return
    if not ("09:00" <= now_str <= "14:30"):
        return

    try:
        candidates = await asyncio.to_thread(
            limit_scanner.scan, limit_strategy.held_codes(), set()
        )
        if not candidates:
            return
        balance = await asyncio.to_thread(broker.get_balance)
        cash    = balance["cash"]
        if cash <= 0:
            return

        for candidate in candidates[:3]:
            code  = candidate["code"]
            score = candidate.get("score", 0)

            ok, reason = registry.can_buy(
                code, Strat.PRE_LIMIT, candidate["cur_price"] * 10, cash
            )
            if not ok:
                logger.info(f"[Limit] 레지스트리 거부 {code}: {reason}")
                continue

            entry_sig = limit_strategy.check_buy(candidate, cash)
            if not entry_sig["signal"]:
                logger.info(
                    f"[Limit] 진입 거부 {candidate['name']}({code}): "
                    f"{entry_sig['reason']}"
                )
                continue

            qty   = entry_sig["qty"]
            price = candidate["cur_price"]

            ok, reason = registry.can_buy(code, Strat.PRE_LIMIT, price * qty, cash)
            if not ok:
                continue

            result = await asyncio.to_thread(
                broker.buy_order, code, qty, price, "0"
            )
            if result["success"]:
                limit_strategy.add_position(
                    code, candidate["name"], qty, price,
                    score=score, theme=candidate.get("theme", "")
                )
                registry.register(code, Strat.PRE_LIMIT, qty, price)
                await bot.notify_limit_buy(code, candidate["name"], qty, price, candidate)
                cash -= price * qty
                logger.info(
                    f"[Limit] 매수: {candidate['name']}({code}) "
                    f"{qty}주 @{price:,}원 | 점수 {score:.1f}"
                )
                break
    except Exception as e:
        logger.error(f"[Limit] 스캔 루프 오류: {e}")
        await bot.notify_error(f"전략C 오류: {e}")


async def job_limit_force_exit_warn():
    """09:25 — 전략C 강제청산 5분 전 경고"""
    if not assert_trading_day("limit_force_exit_warn"):
        return
    positions = limit_strategy.get_positions()
    if not positions:
        return
    lines = ["⚠️ <b>[전략C] 09:30 강제청산 5분 전!</b>"]
    for pos in positions:
        try:
            info      = broker.get_stock_info(pos.code)
            cur_price = info["cur_price"] if info else pos.buy_price
            pct       = pos.profit_pct(cur_price)
            icon      = "📈" if pct >= 0 else "📉"
            lines.append(f"{icon} {pos.name}: {pct:+.1f}% ({pos.qty}주)")
        except Exception:
            lines.append(f"• {pos.code}: 조회 실패")
    await bot.send("\n".join(lines))


async def job_limit_force_exit():
    """09:30 — 전략C 익일 미청산 전량 강제청산"""
    if not assert_trading_day("limit_force_exit"):
        return
    positions = limit_strategy.get_positions()
    if not positions:
        return
    logger.info("[Limit] 09:30 강제청산 실행")
    exited = []
    for pos in positions:
        try:
            result = await asyncio.to_thread(
                broker.sell_order, pos.code, pos.qty, 0, "3"
            )
            if result["success"]:
                info = broker.get_stock_info(pos.code)
                cur  = info["cur_price"] if info else pos.buy_price
                pnl  = pos.profit_won(cur)
                limit_strategy.remove_position(pos.code, cur, pos.qty, "강제청산 09:30")
                registry.close(pos.code, reason="강제청산", pnl=pnl)
                exited.append(pos.name)
        except Exception as e:
            logger.error(f"[Limit] 강제청산 실패 {pos.code}: {e}")
    if exited:
        await bot.send(f"⛔ <b>[전략C 강제청산]</b>\n청산: {', '.join(exited)}")


async def job_limit_daily_report():
    """15:40 — 전략C 당일 결산"""
    if not assert_trading_day("limit_daily_report"):
        return
    report = limit_strategy.daily_summary()
    await bot.send(report)
    logger.info("[Limit] 당일 결산 완료")


# ──────────────────────────────────────────────────────────────
# ④ 성과 리포트 스케줄 작업  v3.1 NEW
# ──────────────────────────────────────────────────────────────

async def job_daily_performance():
    """15:50 — 3전략 통합 일별 성과 리포트 (거래일에만)"""
    if not assert_trading_day("daily_performance"):
        return
    try:
        now    = datetime.now(KST)
        report = tracker.daily_report(now)
        await bot.send(report)

        # 개선 힌트 (거래가 3건 이상일 때만)
        if tracker.total_count() >= 3:
            hints = tracker._quick_hints(
                now.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=KST),
                now.replace(hour=23, minute=59, second=59, microsecond=0).replace(tzinfo=KST),
            )
            if hints:
                await bot.send(
                    "💡 <b>[ 오늘의 개선 힌트 ]</b>\n" + "\n".join(hints)
                )
        logger.info("[Performance] 일별 리포트 전송 완료")
    except Exception as e:
        logger.error(f"[Performance] 일별 리포트 오류: {e}")


async def job_weekly_performance():
    """금요일 16:00 — 주별 성과 리포트 + 개선 분석"""
    if not assert_trading_day("weekly_performance"):
        return
    now = datetime.now(KST)
    if now.weekday() != 4:   # 금요일(4)만 실행
        return
    try:
        report = tracker.weekly_report(now)
        await bot.send(report)

        # 주별 심층 분석 (거래 5건 이상)
        if tracker.total_count() >= 5:
            analysis = tracker.improvement_hints(days=7)
            await bot.send(
                "🔬 <b>[ 주간 알고리즘 분석 ]</b>\n\n" + analysis
            )
        logger.info("[Performance] 주별 리포트 전송 완료")
    except Exception as e:
        logger.error(f"[Performance] 주별 리포트 오류: {e}")


async def job_monthly_performance():
    """매월 마지막 거래일 16:10 — 월별 성과 리포트 + 심층 분석"""
    if not assert_trading_day("monthly_performance"):
        return
    now      = datetime.now(KST)
    tomorrow = now + timedelta(days=1)
    # 다음날이 거래일이 아닌 경우(주말/공휴일) = 이번달 마지막 거래일
    next_trading = is_trading_day(tomorrow) or is_trading_day(tomorrow + timedelta(days=1))
    if now.month == (now + timedelta(days=1)).month and next_trading:
        return  # 마지막 거래일 아님
    try:
        report = tracker.monthly_report(now)
        await bot.send(report)

        # 월별 심층 개선 분석
        analysis = tracker.improvement_hints(days=30)
        await bot.send(
            "📊 <b>[ 월간 알고리즘 개선 분석 ]</b>\n\n" + analysis
        )
        logger.info("[Performance] 월별 리포트 전송 완료")
    except Exception as e:
        logger.error(f"[Performance] 월별 리포트 오류: {e}")


# ──────────────────────────────────────────────────────────────
# 스케줄러 설정
# ──────────────────────────────────────────────────────────────

def setup_scheduler(scheduler: AsyncIOScheduler):
    def cron(hour, minute=0, day_of_week="mon-fri"):
        return CronTrigger(hour=hour, minute=minute,
                           day_of_week=day_of_week, timezone=KST)

    # ── 종가베팅 스케줄 ──────────────────────────────────────
    scheduler.add_job(job_token_refresh,  cron(9, 0),   id="token_refresh")
    scheduler.add_job(job_pre_scan,       cron(14, 30), id="pre_scan")
    scheduler.add_job(job_auto_buy,       cron(15, 10), id="auto_buy")
    scheduler.add_job(job_reset_locks,    cron(15, 30), id="reset_locks")
    scheduler.add_job(
        job_monitor_exit,
        CronTrigger(minute="*", hour="8,9,10,15",
                    day_of_week="mon-fri", timezone=KST),
        id="monitor_exit"
    )
    scheduler.add_job(
        job_cleanup_logs,
        CronTrigger(hour=6, minute=0, timezone=KST),
        id="cleanup_logs"
    )

    # ── 단타 스케줄 ──────────────────────────────────────────
    scheduler.add_job(job_scalp_pre_market,     cron(8, 50),  id="scalp_pre_market")
    scheduler.add_job(job_scalp_force_exit_warn, cron(15, 10), id="scalp_exit_warn")
    scheduler.add_job(job_scalp_force_exit,      cron(15, 20), id="scalp_force_exit")
    scheduler.add_job(job_scalp_daily_report,    cron(15, 35), id="scalp_daily_report")

    # 단타 핵심 루프 — 30초 주기
    scheduler.add_job(
        job_scalp_loop,
        "interval", seconds=30,
        id="scalp_loop"
    )

    # ── 전략C 상한가선진입 스케줄 (v3.0 NEW) ─────────────────
    scheduler.add_job(job_limit_pre_market,      cron(8, 50),  id="limit_pre_market")
    scheduler.add_job(job_limit_force_exit_warn, cron(9, 25),  id="limit_exit_warn")
    scheduler.add_job(job_limit_force_exit,      cron(9, 30),  id="limit_force_exit")
    scheduler.add_job(job_limit_daily_report,    cron(15, 40), id="limit_daily_report")

    # 아침 매도 감시 — 08:00~09:30 매분
    scheduler.add_job(
        job_limit_morning_exit,
        CronTrigger(minute="*", hour="8,9", day_of_week="mon-fri", timezone=KST),
        id="limit_morning_exit"
    )
    # 전략C 장중 루프 — 30초 주기
    scheduler.add_job(
        job_limit_scan_loop,
        "interval", seconds=30,
        id="limit_scan_loop"
    )

    # ── 성과 리포트 스케줄 (v3.1 NEW) ────────────────────────
    scheduler.add_job(job_daily_performance,   cron(15, 50), id="daily_performance")
    scheduler.add_job(job_weekly_performance,  cron(16,  0), id="weekly_performance")
    scheduler.add_job(job_monthly_performance, cron(16, 10), id="monthly_performance")

    logger.info("[Scheduler] 모든 스케줄 등록 완료")
    for job in scheduler.get_jobs():
        logger.info(f"  {job.id} 등록")


# ──────────────────────────────────────────────────────────────
# 메인 진입점
# ──────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 55)
    logger.info("  키움 국내주식 자동매매 봇 v3.0.0 (종가베팅 + 단타 + 상한가선진입)")
    logger.info(f"  시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}")
    logger.info(f"  모드: {broker.mode}")
    logger.info("=" * 55)

    # 토큰 확인
    try:
        broker._get_token()
        logger.info("[Main] API 토큰 발급 성공")
    except Exception as e:
        logger.error(f"[Main] API 토큰 발급 실패: {e}")
        logger.error("  → .env 파일의 앱키/시크릿키를 확인하세요")
        sys.exit(1)

    # 기존 포지션 복원
    scalp_strategy.load_positions()
    limit_strategy.load_positions()   # v3.0: 전략C 포지션 복원

    # 텔레그램 봇 빌드
    application = bot.build()

    # 스케줄러 시작
    scheduler = AsyncIOScheduler(timezone=KST)
    setup_scheduler(scheduler)
    scheduler.start()

    # 시작 알림
    await bot.send(
        f"<b>[ 자동매매 봇 v3.0.0 시작 ]</b>\n"
        f"<i>{datetime.now(KST).strftime('%Y-%m-%d %H:%M')}</i>\n\n"
        f"모드: <b>{broker.mode}</b>\n\n"
        f"<b>[ 종가베팅 A ]</b>\n"
        f"스캔: 14:30 / 매수: 15:10\n"
        f"감시: 08:00~08:50, 09:00~10:00\n\n"
        f"<b>[ 단타 B ]</b>\n"
        f"스캔/매매: 09:00~{scalp_cfg.get_scan()['entry_end_time']} (30초 주기)\n"
        f"익절: +{scalp_cfg.get_exit()['take_profit_pct']}% "
        f"/ 손절: {scalp_cfg.get_exit()['stop_loss_pct']}%\n"
        f"강제 청산: {scalp_cfg.get_exit()['force_exit_time']}\n\n"
        f"<b>[ 상한가선진입 C ]</b> ★ v3.0 NEW\n"
        f"스캔: 09:00~{limit_cfg.get_scan()['scan_stop_time']} (30초 주기)\n"
        f"점수기준: {limit_cfg.get_signal()['composite_score_min']}점↑  "
        f"최대 {limit_cfg.get_entry()['max_positions']}종목\n"
        f"강제청산: {limit_cfg.get_exit()['force_sell_time']}\n\n"
        f"/start 로 전체 명령어 확인"
    )

    logger.info("[Main] 텔레그램 봇 polling 시작")
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            logger.info("[Main] 종료 신호 수신")
        finally:
            scheduler.shutdown()
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
            logger.info("[Main] 봇 정상 종료")


if __name__ == "__main__":
    asyncio.run(main())
