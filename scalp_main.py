"""
scalp_main.py — 키움 국내주식 단타 자동매매 메인 실행 파일
v1.0: 기존 main.py(종가베팅)와 독립적으로 별도 프로세스로 실행

[기존 main.py와의 관계]
  main.py       → 종가베팅 봇 (14:30 스캔, 15:10 매수, D+1 청산)
  scalp_main.py → 단타 봇   (09:00 시작, 30초 스캔/매매, 15:20 전량청산)
  → 두 프로세스를 동시에 실행하거나, 전략 선택 시 하나만 실행

[자동 스케줄 (KST 평일)]
  08:50   장전 준비 (토큰 갱신 + 스캐너 전일 거래량 캐시 + 잔고 확인)
  09:00   단타 스캔/매매 루프 시작 (30초 주기)
  13:00   신규 진입 중단 (보유 포지션 청산 감시만 계속)
  15:10   강제 청산 경고 텔레그램 알림
  15:20   전량 강제 청산 실행
  15:30   당일 결산 리포트 + 잠금 초기화

[텔레그램 명령어 — 단타 전용]
  /scalp_status        — 현재 포지션 현황
  /scalp_scan          — 즉시 스캔 실행
  /scalp_config show   — 단타 설정 조회
  /scalp_config set    — 단타 설정 변경
  /scalp_stop          — 당일 단타 매매 중단 (포지션 유지)
  /scalp_exit_all      — 전체 포지션 즉시 청산

[GCP 배포 방법]
  # scalp_main.py 전용 systemd 서비스 등록
  sudo tee /etc/systemd/system/scalp_bot.service > /dev/null << 'EOF'
  [Unit]
  Description=키움 단타 자동매매 봇
  After=network.target

  [Service]
  Type=simple
  User=iamywkim
  WorkingDirectory=/home/iamywkim/kiw_trader
  Environment="TZ=Asia/Seoul"
  ExecStart=/home/iamywkim/kiw_trader/kiw_venv/bin/python3 scalp_main.py
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=multi-user.target
  EOF
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from broker import KiwoomBroker
from scalp_config import ScalpConfig
from scanner import DayTradingScanner
from strategy_scalping import ScalpStrategy
from telegram.ext import (
    Application, CommandHandler, Defaults
)
from telegram import Bot

# ──────────────────────────────────────────────────────────────
# 로그 설정 (기존 main.py와 동일 패턴, 별도 로그 파일)
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
        LOG_DIR / "scalp_trader.log",
        when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    eh = TimedRotatingFileHandler(
        LOG_DIR / "scalp_error.log",
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
# 환경 변수 로드
# ──────────────────────────────────────────────────────────────
import os
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID")

# ──────────────────────────────────────────────────────────────
# 전역 컴포넌트 초기화
# ──────────────────────────────────────────────────────────────
broker   = KiwoomBroker()
scalp_cfg = ScalpConfig()
scanner  = DayTradingScanner(broker, scalp_cfg)
strategy = ScalpStrategy(broker, scalp_cfg)

# 단타 일시 중단 플래그 (텔레그램 /scalp_stop 명령어로 제어)
_scalp_paused: bool = False


# ──────────────────────────────────────────────────────────────
# 텔레그램 헬퍼 — 기존 telegram_bot.py와 독립적
# ──────────────────────────────────────────────────────────────

async def _send(text: str):
    """텔레그램 메시지 전송"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        # 4096자 제한 분할 전송
        for i in range(0, len(text), 4000):
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text[i:i+4000],
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"[Main] 텔레그램 전송 실패: {e}")


async def _send_error(msg: str):
    await _send(f"🚨 <b>[단타봇 오류]</b>\n{msg}")


# ──────────────────────────────────────────────────────────────
# 스케줄 작업 정의
# ──────────────────────────────────────────────────────────────

async def job_pre_market():
    """08:50 — 장전 준비: 토큰 갱신 + 스캐너 초기화 + 잔고 확인"""
    logger.info("[Scalp] 08:50 장전 준비 시작")
    try:
        broker._get_token(force=True)
        logger.info("[Scalp] 토큰 갱신 완료")
    except Exception as e:
        logger.error(f"[Scalp] 토큰 갱신 실패: {e}")
        await _send_error(f"토큰 갱신 실패: {e}")
        return

    try:
        balance = broker.get_balance()
        cash    = balance["cash"]
        strategy.init_daily(cash)
        strategy.load_positions()
        logger.info(f"[Scalp] 잔고 확인 완료 — 가용 현금: {cash:,}원")
    except Exception as e:
        logger.error(f"[Scalp] 잔고 조회 실패: {e}")

    try:
        await asyncio.to_thread(scanner.init_daily)
        logger.info("[Scalp] 스캐너 초기화 완료")
    except Exception as e:
        logger.error(f"[Scalp] 스캐너 초기화 실패: {e}")

    await _send(
        f"<b>[ 단타봇 장전 준비 완료 ]</b>\n"
        f"{datetime.now(KST).strftime('%H:%M')}\n\n"
        f"가용 현금: <b>{balance.get('cash', 0):,}원</b>\n"
        f"스캔 시작: 09:00 / 진입 마감: "
        f"{scalp_cfg.get_scan()['entry_end_time']}\n"
        f"강제 청산: {scalp_cfg.get_exit()['force_exit_time']}"
    )


async def job_scalp_loop():
    """
    09:00~15:20 매분(or 30초) 실행 — 핵심 단타 루프
    1. 신규 진입 스캔 (13:00 이전)
    2. 보유 포지션 청산 감시 (항상)
    """
    global _scalp_paused

    now_str = datetime.now(KST).strftime("%H:%M")

    # 장 시간 외 스킵
    if not ("09:00" <= now_str <= "15:25"):
        return

    # ── 보유 포지션 청산 감시 (항상 실행) ─────────────────────
    positions = strategy.get_positions()
    for pos in positions:
        try:
            info = await asyncio.to_thread(broker.get_stock_info, pos.code)
            if not info:
                continue
            cur_price = info["cur_price"]
            exit_sig  = strategy.check_exit(pos, cur_price)

            if exit_sig["signal"] == "HOLD":
                continue

            sell_qty = exit_sig["qty"]
            result   = await asyncio.to_thread(
                broker.sell_order, pos.code, sell_qty, 0, "3"  # 시장가
            )

            if result["success"]:
                trade_result = strategy.remove_position(
                    pos.code, cur_price, sell_qty, exit_sig["reason"]
                )
                sign = "✅" if trade_result and trade_result["profit"] >= 0 else "❌"
                await _send(
                    f"{sign} <b>[단타 청산]</b> "
                    f"{pos.name}({pos.code})\n"
                    f"{pos.buy_price:,}원 → {cur_price:,}원 "
                    f"(<b>{trade_result['profit_pct']:+.1f}%</b>)\n"
                    f"손익: {trade_result['profit']:+,}원\n"
                    f"사유: {exit_sig['reason']}"
                )
            else:
                logger.error(f"[Scalp] {pos.code} 매도 실패")

        except Exception as e:
            logger.error(f"[Scalp] 포지션 감시 오류 {pos.code}: {e}")

    # ── 신규 진입 스캔 (진입 마감 전 + 일시 중단 아닐 때) ────
    if _scalp_paused or now_str >= scalp_cfg.get_scan()["entry_end_time"]:
        return

    # 최대 포지션 이미 달성 시 스캔 스킵
    cfg_entry = scalp_cfg.get_entry()
    if len(strategy.held_codes()) >= cfg_entry["max_positions"]:
        return

    try:
        candidates = await asyncio.to_thread(
            scanner.scan,
            strategy.held_codes(),
            set()
        )

        if not candidates:
            return

        # 상위 후보부터 진입 시도
        balance = await asyncio.to_thread(broker.get_balance)
        cash    = balance["cash"]

        for candidate in candidates[:3]:   # 상위 3개 시도
            entry_sig = strategy.check_entry(candidate, cash)
            if not entry_sig["signal"]:
                logger.debug(
                    f"[Scalp] {candidate['code']} 진입 거부: "
                    f"{entry_sig['reason']}"
                )
                continue

            code  = candidate["code"]
            name  = candidate["name"]
            qty   = entry_sig["qty"]
            price = candidate["cur_price"]

            result = await asyncio.to_thread(
                broker.buy_order, code, qty, 0, "3"  # 시장가 매수
            )

            if result["success"]:
                strategy.add_position(
                    code, name, qty, price,
                    vwap_at_buy=candidate.get("vwap", 0.0)
                )
                await _send(
                    f"🟢 <b>[단타 매수]</b> {name}({code})\n"
                    f"현재가: <b>{price:,}원</b> "
                    f"({candidate['rise_pct']:+.1f}%)\n"
                    f"수량: {qty}주 | 점수: {candidate['score']}점\n"
                    f"TV:{candidate['trading_value']//100_000_000}억 "
                    f"거래량:{candidate['volume_ratio']:.1f}배\n"
                    f"목표: +{scalp_cfg.get_exit()['take_profit_pct']}% "
                    f"/ 손절: {scalp_cfg.get_exit()['stop_loss_pct']}%"
                )
                cash -= price * qty   # 현금 차감 (다음 종목 계산용)
                logger.info(
                    f"[Scalp] 매수 완료: {name}({code}) "
                    f"{qty}주 @{price:,}원"
                )
                break   # 1 사이클에 1종목만 매수

            else:
                logger.error(
                    f"[Scalp] 매수 실패: {code} — {result['raw']}"
                )

    except Exception as e:
        logger.error(f"[Scalp] 신규 진입 스캔 오류: {e}")
        await _send_error(f"스캔 오류: {e}")


async def job_force_exit_warn():
    """15:10 — 강제 청산 10분 전 경고"""
    positions = strategy.get_positions()
    if not positions:
        return

    lines = ["⚠️ <b>[단타봇 경고]</b> 15:20 강제 청산 10분 전!\n"]
    for pos in positions:
        try:
            info      = broker.get_stock_info(pos.code)
            cur_price = info["cur_price"] if info else pos.buy_price
            pct       = pos.profit_pct(cur_price)
            sign      = "📈" if pct >= 0 else "📉"
            lines.append(
                f"{sign} {pos.name}({pos.code}): "
                f"{pct:+.1f}% ({pos.qty}주)"
            )
        except Exception:
            lines.append(f"• {pos.code}: 조회 실패")

    await _send("\n".join(lines))


async def job_force_exit():
    """15:20 — 전량 강제 청산"""
    logger.info("[Scalp] 15:20 강제 청산 실행")
    exited = await asyncio.to_thread(strategy.force_exit_all)
    if exited:
        await _send(
            f"⛔ <b>[단타봇 강제 청산]</b>\n"
            f"청산 종목: {', '.join(exited)}"
        )
    else:
        logger.info("[Scalp] 강제 청산 대상 없음")


async def job_daily_report():
    """15:30 — 당일 결산 리포트"""
    report = strategy.daily_summary()
    await _send(report)
    logger.info("[Scalp] 당일 결산 완료")


# ──────────────────────────────────────────────────────────────
# 텔레그램 명령어 핸들러
# ──────────────────────────────────────────────────────────────

async def cmd_scalp_status(update, context):
    """/scalp_status — 포지션 현황"""
    msg = strategy.format_positions_message()
    await update.message.reply_html(msg)


async def cmd_scalp_scan(update, context):
    """/scalp_scan — 즉시 스캔 실행 (장외 시간에도 동작)"""
    await update.message.reply_text("🔍 스캔 중... (약 15초 소요)")
    try:
        # force_time=True: 장외 시간에도 수동으로 스캔 가능
        candidates = await asyncio.to_thread(
            scanner.scan, strategy.held_codes(), set(), True
        )
        msg = scanner.format_scan_message(candidates)
        await update.message.reply_html(msg)
    except Exception as e:
        logger.error(f"[Main] /scalp_scan 오류: {e}")
        await update.message.reply_text(f"❌ 스캔 오류: {e}")


async def cmd_scalp_config(update, context):
    """/scalp_config [show|set|reset] [그룹] [키] [값]"""
    args = context.args or []
    if not args or args[0] == "show":
        group = args[1] if len(args) > 1 else "all"
        await update.message.reply_html(scalp_cfg.format_for_telegram(group))
    elif args[0] == "set" and len(args) == 3:
        try:
            scalp_cfg.set(args[1], args[2])
            await update.message.reply_text(
                f"✅ 설정 변경 완료\n{args[1]} = {args[2]}"
            )
        except KeyError as e:
            await update.message.reply_text(f"❌ {e}")
    elif args[0] == "reset":
        scalp_cfg.reset_to_defaults()
        await update.message.reply_text("✅ 기본값으로 초기화")
    elif args[0] == "help":
        await update.message.reply_html(scalp_cfg.format_help())
    else:
        await update.message.reply_text(
            "/scalp_config show\n"
            "/scalp_config set [키] [값]\n"
            "/scalp_config reset\n"
            "/scalp_config help"
        )


async def cmd_scalp_stop(update, context):
    """/scalp_stop — 신규 진입 중단 (보유 포지션 유지)"""
    global _scalp_paused
    _scalp_paused = not _scalp_paused
    status = "일시 중단" if _scalp_paused else "재개"
    await update.message.reply_text(f"⏸ 단타 신규 진입 {status}")


async def cmd_scalp_exit_all(update, context):
    """/scalp_exit_all — 전체 포지션 즉시 청산"""
    positions = strategy.get_positions()
    if not positions:
        await update.message.reply_text("📭 보유 포지션 없음")
        return
    await update.message.reply_text(
        f"⛔ {len(positions)}개 포지션 강제 청산 실행..."
    )
    exited = await asyncio.to_thread(strategy.force_exit_all)
    await update.message.reply_text(
        f"완료: {', '.join(exited) if exited else '없음'}"
    )


# ──────────────────────────────────────────────────────────────
# 스케줄러 설정
# ──────────────────────────────────────────────────────────────

def setup_scheduler(scheduler: AsyncIOScheduler):
    def cron(hour, minute=0, second=0, day_of_week="mon-fri"):
        return CronTrigger(
            hour=hour, minute=minute, second=second,
            day_of_week=day_of_week, timezone=KST
        )

    # 장전 준비
    scheduler.add_job(job_pre_market,       cron(8, 50),  id="pre_market")

    # 핵심 루프 — 30초 주기 (장 시간 내부에서 체크)
    # start_date를 now()로 설정해 즉시 등록 (특정 시각 지정 시 과거 오류 발생 방지)
    scheduler.add_job(
        job_scalp_loop,
        "interval", seconds=30,
        id="scalp_loop",
    )

    # 강제 청산 관련
    scheduler.add_job(job_force_exit_warn,  cron(15, 10), id="force_exit_warn")
    scheduler.add_job(job_force_exit,       cron(15, 20), id="force_exit")
    scheduler.add_job(job_daily_report,     cron(15, 30), id="daily_report")

    logger.info("[Scalp] 모든 스케줄 등록 완료")
    for job in scheduler.get_jobs():
        logger.info(f"  {job.id} 등록")


# ──────────────────────────────────────────────────────────────
# 메인 진입점
# ──────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 55)
    logger.info("  키움 국내주식 단타 자동매매 봇 v1.0")
    logger.info(f"  시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}")
    logger.info(f"  모드: {broker.mode}")
    logger.info("=" * 55)

    # 토큰 확인
    try:
        broker._get_token()
        logger.info("[Main] API 토큰 발급 성공")
    except Exception as e:
        logger.error(f"[Main] API 토큰 발급 실패: {e}")
        sys.exit(1)

    # 기존 포지션 복원
    strategy.load_positions()

    # 텔레그램 봇 설정
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .defaults(Defaults(tzinfo=KST))
        .build()
    )

    # 명령어 핸들러 등록
    for cmd, handler in [
        ("scalp_status",   cmd_scalp_status),
        ("scalp_scan",     cmd_scalp_scan),
        ("scalp_config",   cmd_scalp_config),
        ("scalp_stop",     cmd_scalp_stop),
        ("scalp_exit_all", cmd_scalp_exit_all),
    ]:
        application.add_handler(CommandHandler(cmd, handler))

    # 스케줄러 시작
    scheduler = AsyncIOScheduler(timezone=KST)
    setup_scheduler(scheduler)
    scheduler.start()

    await _send(
        f"<b>[ 단타봇 시작 ]</b> "
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M')}\n\n"
        f"모드: <b>{broker.mode}</b>\n"
        f"스캔 주기: 30초 | 진입 마감: "
        f"{scalp_cfg.get_scan()['entry_end_time']}\n"
        f"익절: +{scalp_cfg.get_exit()['take_profit_pct']}% "
        f"| 손절: {scalp_cfg.get_exit()['stop_loss_pct']}%\n"
        f"강제 청산: {scalp_cfg.get_exit()['force_exit_time']}\n\n"
        f"명령어: /scalp_status /scalp_scan /scalp_stop"
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
            logger.info("[Main] 단타봇 정상 종료")


if __name__ == "__main__":
    asyncio.run(main())
