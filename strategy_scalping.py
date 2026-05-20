"""
strategy_scalping.py — 단타 진입·청산·포지션 관리 전략 엔진
v1.2: VWAP 점수화 전환 + 종목별 손절 쿨다운 + Fib 눌림 보너스

[v1.1 → v1.2 핵심 변경]
  문제1: VWAP 하회 → 즉시 차단 → 고점 이후 모든 눌림 매매 불가
         → VWAP을 차단 대신 점수(+10/+5/-5/-15)로 활용
  문제2: 연속 손절 3회 → 전략 전체 멈춤 → 새 종목도 진입 불가
         → 손절 종목만 개별 쿨다운 (기본 60분), 전략은 계속 실행
  문제3: 당일 고점 대비 Fib 눌림 패턴 미감지
         → _calc_fib_pullback_score(): Fib 0.236/0.382 구간 보너스 +20/+10점
  문제4: broker.py에 get_volume_surge/get_new_high 없어서 limit_scanner 0개
         → broker.py v1.2에서 해결

[v1.1 변경]
  - 매도 API 오류 코드별 자동 처리
  - sync_with_account(), force_remove_position()
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

from broker import KiwoomBroker
from scalp_config import ScalpConfig

logger = logging.getLogger(__name__)

KST      = pytz.timezone("Asia/Seoul")
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# 포지션 영속화 파일
POSITIONS_FILE  = DATA_DIR / "scalp_positions.json"
# 당일 매매 이력 (쿨다운/손절 이력)
DAILY_LOG_FILE  = DATA_DIR / "scalp_daily_log.json"
# [v1.3] 하이브리드 모드 수동 감시 목록
WATCHLIST_FILE  = DATA_DIR / "scalp_watchlist.json"


def _write_json_atomic(path: Path, data) -> bool:
    """원자적 JSON 쓰기 — config.py와 동일 패턴"""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
        return True
    except Exception as e:
        logger.error(f"JSON 쓰기 실패 {path.name}: {e}")
        return False


def _read_json_safe(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"JSON 읽기 실패 {path.name}: {e}")
        return default


# ──────────────────────────────────────────────────────────────
# 포지션 데이터 클래스
# ──────────────────────────────────────────────────────────────

class ScalpPosition:
    """단타 포지션 단일 종목 상태"""
    def __init__(self, code: str, name: str, qty: int, buy_price: int,
                 buy_time: str, vwap_at_buy: float = 0.0):
        self.code        = code
        self.name        = name
        self.qty         = qty
        self.buy_price   = buy_price
        self.buy_time    = buy_time           # HH:MM 형식
        self.day_high    = buy_price          # 매수 이후 장중 고가 추적
        self.vwap_at_buy = vwap_at_buy        # 진입 시점 VWAP
        self.partial_done = False             # 부분 익절 완료 여부

    def to_dict(self) -> dict:
        return {
            "code":         self.code,
            "name":         self.name,
            "qty":          self.qty,
            "buy_price":    self.buy_price,
            "buy_time":     self.buy_time,
            "day_high":     self.day_high,
            "vwap_at_buy":  self.vwap_at_buy,
            "partial_done": self.partial_done,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScalpPosition":
        pos              = cls(d["code"], d["name"], d["qty"],
                               d["buy_price"], d["buy_time"],
                               d.get("vwap_at_buy", 0.0))
        pos.day_high     = d.get("day_high",     d["buy_price"])
        pos.partial_done = d.get("partial_done", False)
        return pos

    def profit_pct(self, cur_price: int) -> float:
        if self.buy_price <= 0:
            return 0.0
        return (cur_price - self.buy_price) / self.buy_price * 100

    def minutes_held(self) -> int:
        """보유 경과 시간 (분)"""
        try:
            now  = datetime.now(KST)
            h, m = map(int, self.buy_time.split(":"))
            buy  = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return max(0, int((now - buy).total_seconds() / 60))
        except Exception:
            return 0


# ──────────────────────────────────────────────────────────────
# 단타 전략 엔진
# ──────────────────────────────────────────────────────────────

class ScalpStrategy:
    """
    단타 진입·청산 신호 생성 + 포지션 상태 관리

    [사용법]
        strat = ScalpStrategy(broker, scalp_cfg)
        strat.load_positions()          # 서버 재시작 후 복원

        # 진입 판단
        entry = strat.check_entry(candidate, available_cash)
        if entry["signal"]:
            result = broker.buy_order(...)
            strat.add_position(...)

        # 청산 판단 (30초 주기)
        for pos in strat.get_positions():
            exit = strat.check_exit(pos, cur_price)
            if exit["signal"] != "HOLD":
                broker.sell_order(...)
                strat.remove_position(pos.code, ...)
    """

    def __init__(self, broker: KiwoomBroker, scalp_cfg: ScalpConfig):
        self.broker = broker
        self.cfg    = scalp_cfg

        # 메모리 포지션 딕셔너리 {code: ScalpPosition}
        self._positions: dict[str, ScalpPosition] = {}

        # 당일 쿨다운 딕셔너리 {code: last_exit_timestamp}
        self._cooldown: dict[str, float] = {}

        # [v1.2] FibReentryManager — 손절 종목 피보나치 재진입 감시
        self.fib_mgr = None   # main.py에서 fib_mgr 주입

        # [v1.3] 하이브리드 모드 — 유저 수동 감시 목록
        # {code: {"code", "name", "added_at", "active": bool}}
        # JSON 영속화 → 봇 재시작 후에도 유지
        self._watchlist: dict[str, dict] = _read_json_safe(WATCHLIST_FILE, {})

        # 당일 손익 추적
        self._daily_pnl: int     = 0
        self._daily_start_cash: int = 0

        # 연속 손절 카운터 (전체 — 로그·알림용으로만 유지)
        self._consecutive_loss: int = 0

        # [v1.2] 종목별 손절 쿨다운 {code: 만료_timestamp}
        # 손절 후 해당 종목만 N분 재진입 금지 (전략 전체는 계속 실행)
        self._loss_cooldown: dict[str, float] = {}

        # [v1.1] 매도 실패 카운터 {code: 연속실패횟수}
        self._sell_fail_count: dict[str, int] = {}

        # 일일 매매 이력 로드
        self._daily_log: dict = _read_json_safe(DAILY_LOG_FILE, {})

    # ──────────────────────────────────────────────────────────
    # A. 진입 신호 판단
    # ──────────────────────────────────────────────────────────

    def check_entry(
        self,
        candidate: dict,
        available_cash: int,
    ) -> dict:
        """
        진입 신호 종합 판단

        Args:
            candidate: scanner.scan()이 반환한 종목 딕셔너리
            available_cash: 현재 주문 가능 금액 (원)

        Returns:
            {
              "signal": bool,
              "reason": str,
              "qty": int,        # 매수 수량 (signal=True일 때)
              "price": int,      # 매수 기준 가격 (시장가 시 0)
            }
        """
        cfg_entry = self.cfg.get_entry()
        cfg_scan  = self.cfg.get_scan()
        cfg_risk  = self.cfg.get_risk()
        now_str   = datetime.now(KST).strftime("%H:%M")

        code      = candidate["code"]
        name      = candidate["name"]
        cur_price = candidate["cur_price"]
        vwap      = candidate.get("vwap", 0.0)

        # ① 신규 진입 마감 시각
        if now_str >= cfg_scan["entry_end_time"]:
            return self._no_signal(f"진입 마감 ({now_str})")

        # ② 최대 보유 종목 수
        if len(self._positions) >= cfg_entry["max_positions"]:
            return self._no_signal(
                f"최대 보유 도달 ({len(self._positions)}/{cfg_entry['max_positions']})"
            )

        # ③ 이미 보유 중인 종목
        if code in self._positions:
            return self._no_signal("이미 보유 중")

        # ③-2 [v1.2] Fib 감시 중인 종목 — 직접 재진입 차단
        #   (Fib 신호가 나면 fib_mgr.check_all() 통해 별도 진입)
        #   단, Fib 재진입 candidate에는 _reentry=True 플래그가 있음
        is_reentry = candidate.get("_reentry", False)
        if not is_reentry and self.fib_mgr and self.fib_mgr.is_watching(code):
            return self._no_signal(
                f"Fib 조정대 대기 중 — {code} (Fib 반등 신호 대기)"
            )

        # ④ 쿨다운 체크 (동일 종목 재진입 방지)
        if self._is_in_cooldown(code):
            return self._no_signal(f"쿨다운 중 ({code})")

        # ⑤ 일일 손실 한도
        if self._daily_start_cash > 0:
            loss_pct = self._daily_pnl / self._daily_start_cash * 100
            if loss_pct <= cfg_risk["daily_loss_limit_pct"]:
                return self._no_signal(
                    f"일일 손실 한도 도달 ({loss_pct:.1f}%)"
                )

        # ⑥ 연속 손절 전체 한도 (완전 멈춤 임계값 — 기본 10회)
        global_loss_limit = cfg_risk.get("max_consecutive_loss", 10)
        if self._consecutive_loss >= global_loss_limit:
            return self._no_signal(
                f"연속 손절 {self._consecutive_loss}회 — 전략 일시정지"
            )

        # ⑥-2 [v1.2] 종목별 손절 쿨다운 체크
        import time as _time
        if code in self._loss_cooldown:
            if _time.time() < self._loss_cooldown[code]:
                remain = int((self._loss_cooldown[code] - _time.time()) / 60)
                return self._no_signal(f"손절 쿨다운 {remain}분 남음 ({code})")
            del self._loss_cooldown[code]

        # ⑦ [v2.0] 순수 모멘텀 점수 판단 (VWAP 완전 제거)
        #    scanner._score()가 이미 5신호 점수를 계산해서 candidate["score"]에 넣음
        #    여기선 Fib 눌림 보너스만 추가로 반영
        fib_bonus  = self._calc_fib_pullback_score(candidate)
        base_score = candidate.get("score", 50)
        total_score = base_score + fib_bonus
        min_score   = cfg_entry.get("min_entry_score", 30)

        logger.info(
            f"[ScalpStrategy] {name}({code}) 진입점수: "
            f"스캔기본{base_score} + Fib{fib_bonus:+} = {total_score} "
            f"(기준 {min_score}점)"
        )

        if total_score < min_score:
            return self._no_signal(
                f"진입 점수 미달 {total_score} < {min_score} (Fib:{fib_bonus:+})"
            )

        # ⑧ 매수 수량 계산
        qty = self.calculate_qty(cur_price, available_cash)
        if qty <= 0:
            return self._no_signal(
                f"매수 수량 0 (가용현금:{available_cash:,}원, 주가:{cur_price:,}원)"
            )

        reason = (
            f"진입 조건 충족 "
            f"(상승:{candidate['rise_pct']:+.1f}% "
            f"TV:{candidate['trading_value']//100_000_000}억 "
            f"거래량:{candidate['volume_ratio']:.1f}배)"
        )
        return {
            "signal": True,
            "reason": reason,
            "qty":    qty,
            "price":  0,        # 0 = 시장가 (단타는 시장가 매수 권장)
        }

    def _calc_fib_pullback_score(self, candidate: dict) -> int:
        """
        [v1.2] 당일 고점 대비 피보나치 눌림 구간 점수
        코스모로보틱스·주성엔지니어링처럼 고점 후 Fib 눌림 매매 포착용

        Fib 0.236 ~ 0.382 구간 (황금구간): +20점
        Fib 0.382 ~ 0.618 구간 (깊은눌림): +10점
        Fib 0.618 이하 (과도한눌림):        0점 (패널티 없음)
        고점 근처 (0 ~ 0.236):              +5점
        """
        day_high = candidate.get("day_high", 0)
        day_low  = candidate.get("day_low",  0)
        cur      = candidate.get("cur_price", 0)

        if day_high <= day_low or cur <= 0 or day_high <= 0:
            return 0

        # 당일 움직임이 너무 작으면 Fib 의미 없음 (3% 미만)
        move_pct = (day_high - day_low) / day_low * 100 if day_low > 0 else 0
        if move_pct < 3.0:
            return 0

        fib_range = day_high - day_low
        fib236    = day_high - fib_range * 0.236
        fib382    = day_high - fib_range * 0.382
        fib618    = day_high - fib_range * 0.618

        if fib236 <= cur <= day_high:          return +5   # 얕은 눌림
        if fib382 <= cur < fib236:             return +20  # 황금구간 0.236~0.382
        if fib618 <= cur < fib382:             return +10  # 깊은 눌림 0.382~0.618
        return 0

    def calculate_qty(self, cur_price: int, available_cash: int) -> int:
        """매수 수량 계산"""
        if cur_price <= 0 or available_cash <= 0:
            return 0
        cfg = self.cfg.get_entry()
        risk_cfg    = self.cfg.get_risk()
        reserve_pct = risk_cfg.get("reserve_cash_pct", 10)
        usable_cash = int(available_cash * (1 - reserve_pct / 100))
        target_amt  = int(usable_cash * cfg["position_size_pct"] / 100)
        qty         = target_amt // cur_price
        logger.info(
            f"[ScalpStrategy] 수량 계산: {target_amt:,}원 ÷ "
            f"@{cur_price:,}원 = {qty}주"
        )
        return qty

    @staticmethod
    def calc_real_cost(buy_price: int, sell_price: int, qty: int,
                       market: str = "KOSDAQ") -> dict:
        """
        [v1.1] 실제 거래 비용 계산 — 세금 + 수수료

        [키움증권 기준 비용]
          수수료: 0.015% (매수/매도 각각)
          증권거래세: 0.18% (코스닥/코스피 동일, 매도 시만)
          → 총 비용: 약 0.21% per round trip

        Args:
            buy_price : 매수 체결가
            sell_price: 매도 체결가
            qty       : 수량
            market    : "KOSPI" | "KOSDAQ" (기본 KOSDAQ)

        Returns:
            {
              "buy_commission" : 매수 수수료,
              "sell_commission": 매도 수수료,
              "sell_tax"       : 증권거래세,
              "total_cost"     : 총 비용,
              "cost_pct"       : 투자금 대비 비용 비율 (%),
            }
        """
        COMMISSION_RATE = 0.00015   # 0.015% (키움 기준)
        TAX_RATE        = 0.0018    # 0.18% (코스피/코스닥 동일)

        buy_amt  = buy_price  * qty
        sell_amt = sell_price * qty

        buy_commission  = int(buy_amt  * COMMISSION_RATE)
        sell_commission = int(sell_amt * COMMISSION_RATE)
        sell_tax        = int(sell_amt * TAX_RATE)

        total_cost = buy_commission + sell_commission + sell_tax
        cost_pct   = round(total_cost / buy_amt * 100, 3) if buy_amt > 0 else 0

        return {
            "buy_commission":  buy_commission,
            "sell_commission": sell_commission,
            "sell_tax":        sell_tax,
            "total_cost":      total_cost,
            "cost_pct":        cost_pct,
        }

    # ──────────────────────────────────────────────────────────
    # B. 청산 신호 판단
    # ──────────────────────────────────────────────────────────

    def check_exit(
        self,
        pos: ScalpPosition,
        cur_price: int,
    ) -> dict:
        """
        청산 신호 판단 (보유 종목별 30초 주기 호출)

        Returns:
            {
              "signal": "HOLD" | "PARTIAL" | "FULL",
              "reason": str,
              "qty": int,        # 매도 수량
            }
        """
        cfg_exit = self.cfg.get_exit()
        now_str  = datetime.now(KST).strftime("%H:%M")

        if pos.buy_price <= 0 or pos.qty <= 0:
            return {"signal": "HOLD", "reason": "포지션 없음", "qty": 0}

        profit_pct = pos.profit_pct(cur_price)

        # 장중 고가 갱신
        if cur_price > pos.day_high:
            pos.day_high = cur_price
            self._save_positions()  # 고가 갱신 시 즉시 저장

        # ── 1순위: 손절선 ────────────────────────────────────
        if profit_pct <= cfg_exit["stop_loss_pct"]:
            return self._exit_signal(
                "FULL", pos.qty,
                f"손절 ({profit_pct:.1f}% ≤ {cfg_exit['stop_loss_pct']}%)"
            )

        # ── 2순위: 트레일링 스탑 (수익 달성 후 하락 방어) ────
        if cfg_exit["trailing_stop"] and pos.day_high > pos.buy_price:
            activate_pct = cfg_exit.get("trailing_activate_pct", 1.0)
            high_profit  = (pos.day_high - pos.buy_price) / pos.buy_price * 100
            if high_profit >= activate_pct:   # 최소 수익 달성 후 활성화
                trail_pct = (cur_price - pos.day_high) / pos.day_high * 100
                if trail_pct <= -cfg_exit["trailing_gap_pct"]:
                    return self._exit_signal(
                        "FULL", pos.qty,
                        f"트레일링 스탑 (고점:{pos.day_high:,} → 현재:{cur_price:,}, "
                        f"{trail_pct:.1f}%)"
                    )

        # ── 3순위: 강제 청산 시각 ────────────────────────────
        if now_str >= cfg_exit["force_exit_time"]:
            return self._exit_signal(
                "FULL", pos.qty,
                f"강제 청산 ({now_str} ≥ {cfg_exit['force_exit_time']})"
            )

        # ── 4순위: 전량 익절선 ───────────────────────────────
        if profit_pct >= cfg_exit["take_profit_pct"]:
            return self._exit_signal(
                "FULL", pos.qty,
                f"익절 ({profit_pct:.1f}% ≥ {cfg_exit['take_profit_pct']}%)"
            )

        # ── 5순위: 부분 익절 (1회만) ─────────────────────────
        partial_pct = cfg_exit.get("partial_profit_pct", 1.5)
        if (not pos.partial_done
                and profit_pct >= partial_pct):
            ratio = cfg_exit.get("partial_ratio", 50)
            qty   = max(int(pos.qty * ratio / 100), 1)
            if qty < pos.qty:
                return self._exit_signal(
                    "PARTIAL", qty,
                    f"부분 익절 {ratio}% ({profit_pct:.1f}%)"
                )

        # ── 6순위: 시간 손절 ─────────────────────────────────
        time_stop_min = cfg_exit.get("time_stop_minutes", 60)
        min_profit    = cfg_exit.get("time_stop_min_profit", 0.0)
        if pos.minutes_held() >= time_stop_min and profit_pct < min_profit:
            return self._exit_signal(
                "FULL", pos.qty,
                f"시간 손절 ({pos.minutes_held()}분 보유, {profit_pct:.1f}%)"
            )

        return {"signal": "HOLD",
                "reason": f"보유 유지 ({profit_pct:.1f}%)", "qty": 0}

    @staticmethod
    def _exit_signal(signal: str, qty: int, reason: str) -> dict:
        return {"signal": signal, "reason": reason, "qty": qty}

    @staticmethod
    def _no_signal(reason: str) -> dict:
        return {"signal": False, "reason": reason, "qty": 0, "price": 0}

    # ──────────────────────────────────────────────────────────
    # C. 포지션 관리 (CRUD)
    # ──────────────────────────────────────────────────────────

    def add_position(
        self,
        code: str, name: str, qty: int, buy_price: int,
        vwap_at_buy: float = 0.0
    ):
        """매수 완료 후 포지션 추가"""
        buy_time = datetime.now(KST).strftime("%H:%M")
        pos = ScalpPosition(code, name, qty, buy_price, buy_time, vwap_at_buy)
        self._positions[code] = pos
        self._save_positions()
        self._log_trade(code, "BUY", qty, buy_price)
        logger.info(
            f"[ScalpStrategy] 포지션 추가: {name}({code}) "
            f"{qty}주 @{buy_price:,}원"
        )

    def remove_position(
        self,
        code: str,
        sell_price: int,
        sell_qty: int,
        reason: str = "",
    ) -> Optional[dict]:
        """
        매도 완료 후 포지션 제거/축소
        [v1.1] 실제 손익(세금+수수료) 계산 + 손절 시 당일 블랙리스트 등록

        Returns: 손익 정보 dict (실제 손익 포함)
        """
        pos = self._positions.get(code)
        if not pos:
            return None

        gross_profit = (sell_price - pos.buy_price) * sell_qty
        profit_pct   = pos.profit_pct(sell_price)

        # ── [v1.1] 실제 손익: 세금 + 수수료 차감 ─────────────
        cost_info    = self.calc_real_cost(pos.buy_price, sell_price, sell_qty)
        net_profit   = gross_profit - cost_info["total_cost"]

        is_stop_loss = "손절" in reason or "트레일링" in reason

        # 부분 매도
        if sell_qty < pos.qty:
            pos.qty         -= sell_qty
            pos.partial_done = True
            self._save_positions()
        else:
            # 전량 매도 → 포지션 제거
            del self._positions[code]
            self._save_positions()

            if is_stop_loss:
                # ── [v1.2] 손절 시 종목별 쿨다운 등록 ───────────────
                import time as _time
                cooldown_min = self.cfg.get_entry().get("loss_cooldown_min", 60)
                self._loss_cooldown[code] = _time.time() + cooldown_min * 60
                logger.info(
                    f"[ScalpStrategy] {code} 손절 쿨다운 {cooldown_min}분 등록"
                )
                # Fib 재진입 감시 등록 (fib_mgr 있을 때)
                if self.fib_mgr is not None:
                    try:
                        from fib_reentry import FibWatcher
                        info       = self.broker.get_stock_info(code)
                        today_low  = info.get("low",       sell_price)
                        prev_close = info.get("prev_close", pos.buy_price)
                        today_open = info.get("open",       pos.buy_price)
                        watcher = FibWatcher.from_position(
                            code       = code,
                            name       = pos.name,
                            stop_price = sell_price,
                            day_high   = pos.day_high,
                            today_low  = today_low,
                            prev_close = prev_close,
                            today_open = today_open,
                        )
                        self.fib_mgr.add(watcher)
                        logger.info(
                            f"[ScalpStrategy] {code} Fib 재진입 감시 등록 "
                            f"(고점:{pos.day_high:,})"
                        )
                    except Exception as e:
                        logger.warning(f"[ScalpStrategy] Fib 감시 등록 실패: {e}")
            else:
                # 익절/강제청산 → 일반 쿨다운만
                import time as _time
                self._cooldown[code] = _time.time()

        # 손익 집계 (실제 손익 기준)
        self._daily_pnl += net_profit
        if net_profit < 0:
            self._consecutive_loss += 1
        else:
            self._consecutive_loss = 0

        self._log_trade(
            code, "SELL", sell_qty, sell_price,
            net_profit, reason
        )

        result = {
            "code":        code,
            "name":        pos.name,
            "qty":         sell_qty,
            "buy_price":   pos.buy_price,
            "sell_price":  sell_price,
            "gross_profit": gross_profit,
            "total_cost":  cost_info["total_cost"],
            "profit":      net_profit,           # 실제 손익 (세금+수수료 차감)
            "profit_pct":  round(profit_pct, 2),
            "reason":      reason,
            "is_blacklisted": is_stop_loss,
        }
        logger.info(
            f"[ScalpStrategy] 포지션 제거: {pos.name}({code}) "
            f"{sell_qty}주 "
            f"총손익:{gross_profit:+,}원 "
            f"비용:{cost_info['total_cost']:,}원 "
            f"실손익:{net_profit:+,}원 ({profit_pct:+.1f}%) "
            f"— {reason}"
        )
        return result

    def get_positions(self) -> list[ScalpPosition]:
        """현재 보유 포지션 리스트"""
        return list(self._positions.values())

    def get_position(self, code: str) -> Optional[ScalpPosition]:
        return self._positions.get(code)

    def held_codes(self) -> list[str]:
        return list(self._positions.keys())

    # ──────────────────────────────────────────────────────────
    # D. 쿨다운 관리
    # ──────────────────────────────────────────────────────────

    def _is_in_cooldown(self, code: str) -> bool:
        import time as _time
        last = self._cooldown.get(code, 0)
        if last <= 0:
            return False
        cfg        = self.cfg.get_entry()
        cooldown   = cfg.get("cooldown_sec", 300)
        elapsed    = _time.time() - last
        return elapsed < cooldown

    def add_to_blacklist_today(self, code: str):
        """당일 블랙리스트 (손절 또는 수동 차단)"""
        import time as _time
        # 9999초 쿨다운 = 사실상 당일 차단
        self._cooldown[code] = _time.time() - (
            self.cfg.get_entry().get("cooldown_sec", 300) - 9999
        )

    # ──────────────────────────────────────────────────────────
    # E. 영속화 (JSON 저장/로드)
    # ──────────────────────────────────────────────────────────

    def _save_positions(self):
        data = {code: pos.to_dict()
                for code, pos in self._positions.items()}
        _write_json_atomic(POSITIONS_FILE, data)

    def load_positions(self):
        """서버 재시작 후 포지션 복원"""
        data = _read_json_safe(POSITIONS_FILE, {})
        today = datetime.now(KST).strftime("%Y-%m-%d")
        loaded = 0
        for code, d in data.items():
            # 당일 매수 포지션만 복원 (전일 미청산 방지)
            buy_time_full = d.get("buy_time", "")
            if not buy_time_full:
                continue
            self._positions[code] = ScalpPosition.from_dict(d)
            loaded += 1
        if loaded:
            logger.info(f"[ScalpStrategy] 포지션 복원: {loaded}개")
        else:
            logger.info("[ScalpStrategy] 복원할 포지션 없음")

    def _log_trade(self, code: str, side: str, qty: int, price: int,
                   profit: int = 0, reason: str = ""):
        """당일 매매 이력 기록"""
        log   = _read_json_safe(DAILY_LOG_FILE, {})
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if today not in log:
            log[today] = []
        log[today].append({
            "time":   datetime.now(KST).strftime("%H:%M:%S"),
            "code":   code,
            "side":   side,
            "qty":    qty,
            "price":  price,
            "profit": profit,
            "reason": reason,
        })
        _write_json_atomic(DAILY_LOG_FILE, log)

    # ──────────────────────────────────────────────────────────
    # F. 일일 초기화 / 결산
    # ──────────────────────────────────────────────────────────

    def init_daily(self, start_cash: int):
        """장 시작 전 일일 상태 초기화"""
        self._daily_pnl        = 0
        self._daily_start_cash = start_cash
        self._consecutive_loss = 0
        if self.fib_mgr:
            self.fib_mgr.init_daily()   # Fib 감시 목록 초기화
        logger.info(
            f"[ScalpStrategy] 일일 초기화 완료 "
            f"(시작 현금: {start_cash:,}원)"
        )

    def init_daily(self, start_cash: int):
        """장 시작 전 일일 상태 초기화"""
        self._daily_pnl        = 0
        self._daily_start_cash = start_cash
        self._consecutive_loss = 0
        if self.fib_mgr:
            self.fib_mgr.init_daily()   # Fib 감시 목록 초기화
        logger.info(
            f"[ScalpStrategy] 일일 초기화 완료 "
            f"(시작 현금: {start_cash:,}원)"
        )

    # ──────────────────────────────────────────────────────────
    # [v1.3] 하이브리드 모드 — 수동 감시 목록 관리
    # ──────────────────────────────────────────────────────────

    def watchlist_add(self, code: str, name: str = "") -> dict:
        """
        수동 감시 목록에 종목 추가.
        이미 있으면 active=True 로 재활성화.
        이름 미입력 시 broker API로 자동 조회.

        Returns: {"ok": bool, "msg": str, "item": dict}
        """
        now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

        if code in self._watchlist:
            item = self._watchlist[code]
            if item["active"]:
                return {"ok": False, "msg": f"이미 감시 중: {item['name']}({code})", "item": item}
            item["active"]     = True
            item["added_at"]   = now_str
            item["stopped_at"] = ""
            if name:
                item["name"] = name
        else:
            if not name:
                try:
                    info = self.broker.get_stock_info(code)
                    name = info.get("name", code)
                except Exception:
                    name = code
            item = {
                "code": code, "name": name,
                "added_at": now_str, "stopped_at": "",
                "active": True, "note": "수동 추가",
            }
            self._watchlist[code] = item

        _write_json_atomic(WATCHLIST_FILE, self._watchlist)
        logger.info(f"[ScalpStrategy] 감시 추가: {name}({code})")
        return {"ok": True, "msg": f"✅ {name}({code}) 감시 시작", "item": item}

    def watchlist_remove(self, code: str) -> dict:
        """
        수동 감시 목록에서 종목 비활성화 (이력 보존).
        보유 중이면 포지션은 유지하고 신규 진입만 차단.

        Returns: {"ok": bool, "msg": str}
        """
        if code not in self._watchlist:
            return {"ok": False, "msg": f"❌ 감시 목록에 없음: {code}"}
        item = self._watchlist[code]
        if not item["active"]:
            return {"ok": False, "msg": f"이미 중지됨: {item['name']}({code})"}
        item["active"]     = False
        item["stopped_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        _write_json_atomic(WATCHLIST_FILE, self._watchlist)
        name = item["name"]
        if code in self._positions:
            msg = (
                f"⏸ {name}({code}) 신규 진입 중단\n"
                f"현재 보유 포지션은 기존 청산 로직으로 계속 관리됩니다"
            )
        else:
            msg = f"⏹ {name}({code}) 감시 중단"
        logger.info(f"[ScalpStrategy] 감시 중단: {name}({code})")
        return {"ok": True, "msg": msg}

    def watchlist_get_active(self) -> list[dict]:
        """현재 활성화된 수동 감시 종목 목록"""
        return [v for v in self._watchlist.values() if v.get("active", False)]

    def watchlist_all(self) -> list[dict]:
        """전체 목록 (비활성 포함, 최신순)"""
        return sorted(
            self._watchlist.values(),
            key=lambda x: x.get("added_at", ""), reverse=True
        )

    def is_in_watchlist(self, code: str) -> bool:
        """활성화된 수동 감시 종목인지 확인"""
        return code in self._watchlist and self._watchlist[code].get("active", False)

    def build_watchlist_candidate(self, code: str) -> dict | None:
        """
        수동 감시 종목을 scanner candidate 형식으로 변환.
        현재 시세를 broker에서 조회하여 채움.
        source="MANUAL" 로 마킹 → 스캐너 필터 우회.

        Returns: candidate dict or None
        """
        if not self.is_in_watchlist(code):
            return None
        try:
            info = self.broker.get_stock_info(code)
            if not info or info.get("cur_price", 0) <= 0:
                return None
            item       = self._watchlist[code]
            cur_price  = info["cur_price"]
            prev_close = info.get("prev_close", 0)
            rise_pct   = (
                (cur_price - prev_close) / prev_close * 100
                if prev_close > 0 else 0.0
            )
            return {
                "code":          code,
                "name":          item["name"],
                "cur_price":     cur_price,
                "prev_close":    prev_close,
                "rise_pct":      round(rise_pct, 2),
                "volume":        info.get("volume", 0),
                "volume_ratio":  0,
                "trading_value": info.get("trading_value", 0),
                "vwap":          0,         # main.py에서 선택적으로 계산
                "source":        "MANUAL",  # 수동 추가 식별자
                "score":         70,        # 기본 점수
                "scan_time":     datetime.now(KST).strftime("%H:%M"),
                "_manual":       True,      # 스캐너 필터 우회 플래그
            }
        except Exception as e:
            logger.warning(f"[ScalpStrategy] 수동 종목 시세 조회 실패 {code}: {e}")
            return None

    def format_watchlist_message(self) -> str:
        """수동 감시 목록 텔레그램 메시지 포맷"""
        active  = self.watchlist_get_active()
        stopped = [v for v in self._watchlist.values() if not v.get("active")]

        lines = ["⚡ <b>[ 하이브리드 감시 목록 ]</b>\n"]

        if not active and not stopped:
            lines.append("감시 중인 종목 없음\n/scalp_add [종목코드] 로 추가")
            return "\n".join(lines)

        if active:
            lines.append(f"<b>▶ 감시 중 ({len(active)}개)</b>")
            for item in active:
                # 현재 보유 중이면 표시
                held = "📌 보유중" if item["code"] in self._positions else ""
                lines.append(
                    f"  • <b>{item['name']}({item['code']})</b> {held}\n"
                    f"    추가: {item['added_at'][11:16]}"
                )

        if stopped:
            lines.append(f"\n<b>⏹ 중단됨 ({len(stopped)}개)</b>")
            for item in list(stopped)[:3]:   # 최근 3개만
                lines.append(
                    f"  • {item['name']}({item['code']}) "
                    f"— {item.get('stopped_at','')[:16]}"
                )

        lines.append(
            "\n<i>/scalp_add [코드] — 추가\n"
            "/scalp_remove [코드] — 중단</i>"
        )
        return "\n".join(lines)

    def daily_summary(self) -> str:
        """당일 결산 요약 텍스트 (텔레그램 전송용)"""
        now_str = datetime.now(KST).strftime("%Y-%m-%d")
        log     = _read_json_safe(DAILY_LOG_FILE, {})
        trades  = log.get(now_str, [])

        sells   = [t for t in trades if t["side"] == "SELL"]
        total_p = sum(t.get("profit", 0) for t in sells)
        wins    = sum(1 for t in sells if t.get("profit", 0) > 0)
        total   = len(sells)
        wr      = round(wins / total * 100, 1) if total > 0 else 0.0

        lines = [
            f"📊 <b>[ 단타 당일 결산 ]</b> {now_str}",
            f"",
            f"총 매도: {total}회 | 승률: {wr}% ({wins}/{total})",
            f"당일 손익: <b>{total_p:+,}원</b>",
        ]
        if self._daily_start_cash > 0:
            pnl_pct = total_p / self._daily_start_cash * 100
            lines.append(f"수익률: <b>{pnl_pct:+.2f}%</b>")

        if sells:
            lines.append("\n<b>[ 매매 이력 ]</b>")
            for t in sells[-10:]:   # 최근 10건
                sign = "✅" if t.get("profit", 0) >= 0 else "❌"
                lines.append(
                    f"{sign} {t['code']} "
                    f"{t.get('profit', 0):+,}원 "
                    f"({t['reason'][:20]})"
                )
        return "\n".join(lines)

    def force_exit_all(self) -> list[str]:
        """
        15:20 강제 청산 — 보유 중인 모든 종목 시장가 매도
        Returns: 매도 주문 완료된 코드 리스트
        """
        if not self._positions:
            return []

        logger.info(
            f"[ScalpStrategy] 강제 청산 시작 — "
            f"{len(self._positions)}개 종목"
        )
        exited = []
        for code, pos in list(self._positions.items()):
            try:
                result = self.broker.sell_order(
                    code, pos.qty, 0, "3"   # "3" = 시장가
                )
                if result["success"]:
                    # cur_price 조회 없이 일단 제거
                    cur_info = self.broker.get_stock_info(code)
                    cur_price = cur_info["cur_price"] if cur_info else pos.buy_price
                    self.remove_position(code, cur_price, pos.qty, "강제 청산 15:20")
                    exited.append(code)
                    logger.info(f"[ScalpStrategy] 강제 청산 완료: {code}")
                else:
                    logger.error(f"[ScalpStrategy] 강제 청산 실패: {code}")
            except Exception as e:
                logger.error(f"[ScalpStrategy] 강제 청산 오류 {code}: {e}")
        return exited

    # ──────────────────────────────────────────────────────────
    # [v1.1] 매도 실패 처리 + 포지션 불일치 자동 정리
    # ──────────────────────────────────────────────────────────

    # 포지션 강제 삭제 트리거 오류 코드
    _FORCE_REMOVE_ERROR_CODES = {
        "800033",  # 모의투자 매도 가능수량 부족
        "800034",  # 매도 가능 수량 0
        "800012",  # 주문 가능 수량 없음
    }
    # 연속 실패 허용 횟수 (초과 시 포지션 강제 삭제)
    _MAX_SELL_FAIL = 3

    def handle_sell_failure(
        self,
        code      : str,
        error_code: str = "",
        error_msg : str = "",
    ) -> dict:
        """
        매도 실패 시 호출. 오류 코드에 따라 자동 처리.

        Returns
        -------
        {"action": "force_removed" | "retry" | "ignore", "msg": str}
        """
        pos = self._positions.get(code)
        if not pos:
            return {"action": "ignore", "msg": "포지션 없음"}

        # ── 즉시 강제 삭제 오류 코드 ────────────────────────────
        if error_code in self._FORCE_REMOVE_ERROR_CODES:
            msg = (
                f"[ScalpStrategy] {code} 매도 오류 {error_code} "
                f"({error_msg}) → 포지션 강제 삭제"
            )
            logger.warning(msg)
            self.force_remove_position(code, reason=f"매도불가 {error_code}")
            return {"action": "force_removed", "msg": msg}

        # ── 연속 실패 카운터 누적 ────────────────────────────────
        self._sell_fail_count[code] = self._sell_fail_count.get(code, 0) + 1
        fail_cnt = self._sell_fail_count[code]

        if fail_cnt >= self._MAX_SELL_FAIL:
            msg = (
                f"[ScalpStrategy] {code} 연속 매도 실패 {fail_cnt}회 "
                f"→ 포지션 강제 삭제"
            )
            logger.warning(msg)
            self.force_remove_position(code, reason=f"연속매도실패 {fail_cnt}회")
            self._sell_fail_count.pop(code, None)
            return {"action": "force_removed", "msg": msg}

        logger.info(
            f"[ScalpStrategy] {code} 매도 실패 {fail_cnt}/{self._MAX_SELL_FAIL} "
            f"— 다음 사이클 재시도"
        )
        return {"action": "retry", "msg": f"매도 재시도 대기 ({fail_cnt}회)"}

    def force_remove_position(
        self,
        code  : str,
        reason: str = "강제삭제",
    ) -> bool:
        """
        API 주문 없이 포지션 기록만 삭제.
        실제 계좌와 불일치 해소용.

        Returns: 삭제 성공 여부
        """
        pos = self._positions.pop(code, None)
        if not pos:
            logger.warning(f"[ScalpStrategy] force_remove: {code} 포지션 없음")
            return False

        self._save_positions()
        self._sell_fail_count.pop(code, None)

        # 쿨다운 등록 (재진입 방지)
        import time as _time
        self._cooldown[code] = _time.time()

        logger.info(
            f"[ScalpStrategy] 포지션 강제 삭제: {pos.name}({code}) "
            f"{pos.qty}주 @{pos.buy_price:,}원 — {reason}"
        )
        return True

    def sync_with_account(self) -> list[str]:
        """
        실제 계좌 보유 종목과 봇 포지션을 비교하여
        불일치 종목(봇에만 있고 계좌에 없는 종목)을 자동 삭제.

        Returns: 삭제된 종목 코드 리스트
        """
        if not self._positions:
            return []
        try:
            # 실제 계좌 보유 종목 조회
            balance  = self.broker.get_balance()
            holdings = balance.get("holdings", [])
            held_codes = {h["code"] for h in holdings}
        except Exception as e:
            logger.error(f"[ScalpStrategy] sync_with_account 잔고 조회 실패: {e}")
            return []

        removed = []
        for code in list(self._positions.keys()):
            if code not in held_codes:
                logger.warning(
                    f"[ScalpStrategy] 불일치 감지: {code} "
                    f"봇 포지션 있음 / 실제 계좌 없음 → 강제 삭제"
                )
                self.force_remove_position(code, reason="계좌불일치 자동정리")
                removed.append(code)

        if removed:
            logger.info(
                f"[ScalpStrategy] sync 완료 — {len(removed)}개 불일치 정리: "
                f"{', '.join(removed)}"
            )
        return removed
        return exited

    # ──────────────────────────────────────────────────────────
    # G. 포지션 현황 포맷 (텔레그램 메시지용)
    # ──────────────────────────────────────────────────────────

    def format_positions_message(self) -> str:
        """현재 보유 포지션 텔레그램 메시지 포맷"""
        if not self._positions:
            return "📭 보유 포지션 없음"

        lines = [
            f"📊 <b>[ 단타 포지션 현황 ]</b> "
            f"{datetime.now(KST).strftime('%H:%M')}\n"
        ]
        total_profit = 0

        for pos in self._positions.values():
            try:
                info      = self.broker.get_stock_info(pos.code)
                cur_price = info["cur_price"] if info else pos.buy_price
            except Exception:
                cur_price = pos.buy_price

            profit     = (cur_price - pos.buy_price) * pos.qty
            profit_pct = pos.profit_pct(cur_price)
            total_profit += profit

            sign = "📈" if profit >= 0 else "📉"
            lines.append(
                f"{sign} <b>{pos.name}({pos.code})</b>\n"
                f"   {pos.buy_price:,}원 → <b>{cur_price:,}원</b> "
                f"(<b>{profit_pct:+.1f}%</b>) {pos.qty}주\n"
                f"   고점:{pos.day_high:,} | 보유:{pos.minutes_held()}분"
            )

        lines.append(f"\n💰 평가손익 합계: <b>{total_profit:+,}원</b>")
        return "\n".join(lines)
