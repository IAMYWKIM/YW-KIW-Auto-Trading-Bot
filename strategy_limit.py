"""
strategy_limit.py — 상한가 선진입(전략C) 핵심 로직  v1.0
──────────────────────────────────────────────────────────
구조: 기존 strategy_scalping.py 패턴과 동일하게 작성.
     broker.py / limit_config.py 에만 의존한다.

핵심 알고리즘
─────────────
1. 25 % 이상 급등 → 7대 신호 복합 점수화 (0~100)
2. 점수 ≥ 임계치 & 상한가 굳히기 확인 → 매수
3. 익일 갭 구간별 매도 or 당일 손절
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import json

import pytz

from limit_config import LimitConfig

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

_DATA_DIR = Path("data")
_DATA_DIR.mkdir(exist_ok=True)
_POSITIONS_PATH = _DATA_DIR / "limit_positions.json"

# ── 테마 클러스터 DB (간소화 버전 — 정기 업데이트 필요) ──────────────────────
THEME_CLUSTERS: dict[str, list[str]] = {
    "2차전지"  : ["247540","096770","086520","003670","012330","051910","006400"],
    "AI반도체" : ["000660","005930","042700","079550","042270"],
    "방산"     : ["012450","047810","064350","267270"],
    "바이오"   : ["068270","207940","005090","009290","196170"],
    "로봇"     : ["090430","215090","336260","454910"],
    "원전"     : ["034020","105560","298040","082640"],
}

# 역방향 조회: 종목코드 → 테마명
_CODE_TO_THEME: dict[str, str] = {
    code: theme
    for theme, codes in THEME_CLUSTERS.items()
    for code in codes
}


# ── 포지션 데이터클래스 ────────────────────────────────────────────────────────
@dataclass
class LimitPosition:
    code        : str
    name        : str
    qty         : int
    buy_price   : int
    buy_time    : datetime = field(default_factory=lambda: datetime.now(KST))
    high_price  : int = 0         # 트레일링 스탑용 고점 추적
    score       : float = 0.0     # 매수 시점 복합 점수
    theme       : str = ""

    def profit_pct(self, cur_price: int) -> float:
        if self.buy_price <= 0:
            return 0.0
        return (cur_price - self.buy_price) / self.buy_price * 100

    def profit_won(self, cur_price: int) -> int:
        return (cur_price - self.buy_price) * self.qty


class LimitStrategy:
    """
    상한가 선진입 전략 실행 엔진

    외부에서 호출하는 주요 메서드
    ─────────────────────────────
    score_candidate(candidate)   → 복합 점수 계산 (0~100)
    check_buy(candidate, cash)   → 매수 진입 신호
    check_exit(pos, cur_price)   → 매도/손절 신호
    add_position(...)            → 포지션 등록
    remove_position(...)         → 포지션 청산
    get_positions()              → 현재 보유 목록
    force_exit_all()             → 강제 전량 청산
    daily_summary()              → 당일 결산 문자열
    """

    def __init__(self, broker, cfg: LimitConfig) -> None:
        self.broker  = broker
        self.cfg     = cfg
        self._positions : dict[str, LimitPosition] = {}
        self._trades    : list[dict] = []        # 당일 체결 기록
        self._daily_cash_start = 0               # 당일 시작 자금 (손실 한도 기준)
        self._consec_loss = 0                    # 연속 손절 횟수
        self._paused  = False                    # 연속 손절 시 일시 정지
        self.load_positions()

    # ──────────────────────────────────────────────────────────────────────────
    # 초기화
    # ──────────────────────────────────────────────────────────────────────────

    def init_daily(self, cash: int) -> None:
        """08:50 장전 준비 시 호출"""
        self._daily_cash_start = cash
        self._consec_loss = 0
        self._paused = False
        self._trades.clear()
        logger.info(f"[LimitStrategy] 일일 초기화 — 기준 현금 {cash:,}원")

    def load_positions(self) -> None:
        """재시작 시 기존 포지션 복원"""
        if not _POSITIONS_PATH.exists():
            return
        try:
            data = json.loads(_POSITIONS_PATH.read_text(encoding="utf-8"))
            for d in data:
                pos = LimitPosition(
                    code=d["code"], name=d["name"],
                    qty=d["qty"], buy_price=d["buy_price"],
                    buy_time=datetime.fromisoformat(d["buy_time"]),
                    high_price=d.get("high_price", d["buy_price"]),
                    score=d.get("score", 0),
                    theme=d.get("theme", ""),
                )
                self._positions[pos.code] = pos
            logger.info(f"[LimitStrategy] 포지션 복원 {len(self._positions)}개")
        except Exception as e:
            logger.warning(f"[LimitStrategy] 포지션 복원 실패: {e}")

    def _save_positions(self) -> None:
        data = [
            {
                "code": p.code, "name": p.name,
                "qty": p.qty, "buy_price": p.buy_price,
                "buy_time": p.buy_time.isoformat(),
                "high_price": p.high_price,
                "score": p.score, "theme": p.theme,
            }
            for p in self._positions.values()
        ]
        _POSITIONS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 복합 스코어링 — 7대 신호 (0~100)
    # ──────────────────────────────────────────────────────────────────────────

    def score_candidate(
        self,
        candidate   : dict,
        peer_states : dict[str, float] | None = None,
    ) -> float:
        """
        7대 신호 복합 점수 (0~100) v1.1

        변경 내역 (v1.0 → v1.1):
          - 미구현 news/social 가중치를 volume/supply로 재분배
          - vol_ratio=1.0(캐시 미구축) 시 패널티 없이 중간점수 처리
          - strength 기준 완화 (200→150, 150→120)
          - 상한가 종목 change_pct 가산점 추가 (+30%)
          - RSI 과매도(30 이하) 패널티 제거 (상한가 초기는 RSI 낮음)

        가중치:
          테마  20% / 수급  20% / 거래량  25% / 기술  25% / 시장  10%
        """
        weights = {
            "theme"  : 0.20,   # 테마 동료 상한가 비율
            "supply" : 0.20,   # 기관·외인 수급
            "volume" : 0.25,   # 거래량 폭발 + 매수잔량
            "tech"   : 0.25,   # MA·RSI·잔량·체결강도
            "market" : 0.10,   # 시장 전체 흐름
        }

        # 1) 테마 모멘텀
        theme_score = self._calc_theme_score(
            candidate["code"], peer_states or {}
        )

        # 2) 수급 (기관+외인)
        supply_score = self._calc_supply_score(
            candidate.get("inst_net", 0),
            candidate.get("foreign_net", 0),
        )

        # 3) 거래량 (v1.1: vol_ratio=1.0 패널티 제거)
        volume_score = self._calc_volume_score(
            candidate.get("vol_ratio", 1.0),
            candidate.get("buy_ratio", 0.5),
        )

        # 4) 기술적 지표 (v1.1: 상한가 종목 특성 반영)
        tech_score = self._calc_tech_score(candidate)

        # 5) 시장 흐름
        market_score = candidate.get("market_score", 60)

        # 6) 상한가 도달 가산점 (change_pct ≥ 29.5%)
        limit_bonus = 10.0 if candidate.get("change_pct", 0) >= 29.5 else 0.0

        scores = {
            "theme"  : theme_score,
            "supply" : supply_score,
            "volume" : volume_score,
            "tech"   : tech_score,
            "market" : market_score,
        }
        total = sum(scores[k] * weights[k] for k in weights) + limit_bonus
        return round(min(100, max(0, total)), 1)

    # ── 개별 신호 계산 ────────────────────────────────────────────────────────

    @staticmethod
    def _calc_theme_score(
        code: str,
        peer_states: dict[str, float],   # code → change_pct
    ) -> float:
        theme = _CODE_TO_THEME.get(code)
        if not theme:
            return 40.0   # 테마 미등록 → 중립

        peers = [
            c for c in THEME_CLUSTERS.get(theme, [])
            if c != code and c in peer_states
        ]
        if not peers:
            return 40.0

        limit_count = sum(
            1 for c in peers if peer_states[c] >= 29.0
        )
        surge_count = sum(
            1 for c in peers if 15.0 <= peer_states[c] < 29.0
        )
        score = (limit_count / len(peers)) * 100 + (surge_count / len(peers)) * 30
        return min(100, score)

    @staticmethod
    def _calc_supply_score(inst_net: int, foreign_net: int) -> float:
        score = 50.0
        if inst_net > 0:
            score += min(25, inst_net / 10000 * 5)
        else:
            score -= min(20, abs(inst_net) / 10000 * 4)
        if foreign_net > 0:
            score += min(25, foreign_net / 10000 * 5)
        else:
            score -= min(20, abs(foreign_net) / 10000 * 4)
        return min(100, max(0, score))

    @staticmethod
    def _calc_volume_score(vol_ratio: float, buy_ratio: float) -> float:
        """
        거래량 점수 v1.1

        vol_ratio = 1.0: 장전 캐시 미구축 상태 → 패널티 없이 중간값(50점)
        vol_ratio > 1.0: 실제 배수로 계산
        """
        if vol_ratio <= 1.0:
            # 캐시 없음 or 전일과 동일 → 중간값 처리 (패널티 없음)
            vol_score = 50.0
        else:
            # 5배 → 70점, 10배 → 100점 상한
            vol_score = min(100, vol_ratio * 12)

        # 매수잔량 비율 보너스 (호가 기반, 0~30점)
        buy_score = max(0, (buy_ratio - 0.5) * 60)
        return min(100, vol_score + buy_score)

    @staticmethod
    def _calc_tech_score(c: dict) -> float:
        """
        기술적 지표 점수 v1.1

        변경:
          - strength 기준 완화: 200→150점, 150→120점 (모의투자 API 특성 반영)
          - RSI 과매도(30 이하) 패널티 제거: 상한가 초기 종목은 RSI가 낮음
          - MA 역배열이어도 change_pct 높으면 일부 점수
        """
        score = 0.0

        # 이평선 정배열 (ma5 > ma20 > ma60)
        ma5  = c.get("ma5", 0)
        ma20 = c.get("ma20", 0)
        ma60 = c.get("ma60", 0)
        if ma5 > 0 and ma20 > 0 and ma60 > 0:
            if ma5 > ma20 > ma60:
                score += 30
            elif ma5 > ma20:
                score += 15   # 단기 정배열만
        elif ma5 > 0 and ma20 > 0:
            if ma5 > ma20:
                score += 15

        # RSI (v1.1: 과매도 패널티 제거 — 상한가 초기 RSI는 낮을 수 있음)
        rsi = c.get("rsi", 50)
        if 60 <= rsi <= 80:
            score += 30
        elif 50 <= rsi < 60:
            score += 20
        elif 40 <= rsi < 50:
            score += 10
        elif rsi > 80:
            score += 10   # 과열이지만 모멘텀 유지
        # rsi < 40: 패널티 없음 (0점만, 감점 안함)

        # 상한가 굳히기 잔량 (sell_remain_ratio)
        sell_rem = c.get("sell_remain_ratio", 1.0)
        if sell_rem < 0.05:
            score += 25
        elif sell_rem < 0.10:
            score += 15
        elif sell_rem < 0.20:
            score += 5

        # 체결강도 (v1.1: 기준 완화)
        strength = c.get("strength", 100)
        if strength >= 200:
            score += 15
        elif strength >= 150:
            score += 10
        elif strength >= 120:
            score += 5    # v1.1 신규: 120 이상도 부분 점수

        return min(100, score)

    # ──────────────────────────────────────────────────────────────────────────
    # 진입 신호
    # ──────────────────────────────────────────────────────────────────────────

    def check_buy(self, candidate: dict, cash: int) -> dict:
        """
        Returns
        ───────
        {"signal": True/False, "qty": int, "reason": str}
        """
        cfg_entry  = self.cfg.get_entry()
        cfg_sig    = self.cfg.get_signal()

        # 전략 일시 정지 중
        if self._paused:
            return {"signal": False, "qty": 0, "reason": "연속손절 일시정지"}

        # 최대 포지션 수
        if len(self._positions) >= cfg_entry["max_positions"]:
            return {"signal": False, "qty": 0, "reason": "최대 포지션 도달"}

        # 이미 보유 중
        if candidate["code"] in self._positions:
            return {"signal": False, "qty": 0, "reason": "이미 보유 중"}

        # 복합 점수 확인
        score = candidate.get("score", 0)
        if score < cfg_sig["composite_score_min"]:
            return {
                "signal": False, "qty": 0,
                "reason": f"점수 미달 {score:.0f} < {cfg_sig['composite_score_min']}",
            }

        # 체결강도 확인
        if candidate.get("strength", 0) < cfg_sig["strength_min"]:
            return {
                "signal": False, "qty": 0,
                "reason": f"체결강도 {candidate.get('strength',0)} < {cfg_sig['strength_min']}",
            }

        # 매도 잔량 비율 확인
        if candidate.get("sell_remain_ratio", 1.0) > cfg_sig["sell_remain_ratio_max"]:
            return {
                "signal": False, "qty": 0,
                "reason": (
                    f"매도잔량 {candidate.get('sell_remain_ratio',1)*100:.1f}% "
                    f"> {cfg_sig['sell_remain_ratio_max']*100:.0f}%"
                ),
            }

        # 테마 선행 필수 조건
        if cfg_sig["theme_lead_required"] and candidate.get("theme_score", 0) < 30:
            return {"signal": False, "qty": 0, "reason": "테마 모멘텀 부족"}

        # 매수 수량 계산
        size_ratio = cfg_entry["position_size_pct"] / 100
        alloc      = cash * size_ratio * cfg_entry["partial_buy_ratio"]
        price      = candidate.get("cur_price", 0)
        if price <= 0:
            return {"signal": False, "qty": 0, "reason": "가격 정보 없음"}

        qty = int(alloc / price)
        if qty < 1:
            return {"signal": False, "qty": 0, "reason": f"자금 부족 (배분 {alloc:,.0f}원)"}

        return {"signal": True, "qty": qty, "reason": "매수 신호"}

    # ──────────────────────────────────────────────────────────────────────────
    # 청산 신호
    # ──────────────────────────────────────────────────────────────────────────

    def check_exit(self, pos: LimitPosition, cur_price: int) -> dict:
        """
        Returns
        ───────
        {"signal": "SELL"/"HOLD", "qty": int, "reason": str}
        """
        cfg_exit = self.cfg.get_exit()
        pnl_pct  = pos.profit_pct(cur_price)
        now_str  = datetime.now(KST).strftime("%H:%M")

        # 고점 갱신 (트레일링 스탑용)
        if cur_price > pos.high_price:
            pos.high_price = cur_price

        # 당일 매수 종목 손절
        if pnl_pct <= cfg_exit["stop_loss_pct"]:
            return {
                "signal": "SELL", "qty": pos.qty,
                "reason": f"손절 {pnl_pct:+.1f}%",
            }

        # 익절
        if pnl_pct >= cfg_exit["take_profit_pct"]:
            return {
                "signal": "SELL", "qty": pos.qty,
                "reason": f"익절 {pnl_pct:+.1f}%",
            }

        # 트레일링 스탑
        if cfg_exit["trailing_stop"] and pos.high_price > 0:
            trail_line = pos.high_price * (1 - cfg_exit["trailing_gap_pct"] / 100)
            if cur_price <= trail_line:
                drop_from_high = (cur_price - pos.high_price) / pos.high_price * 100
                return {
                    "signal": "SELL", "qty": pos.qty,
                    "reason": f"트레일링 스탑 {drop_from_high:+.1f}% (고점 {pos.high_price:,}원)",
                }

        # 익일 강제 청산 시각 도달
        if now_str >= cfg_exit["force_sell_time"]:
            return {
                "signal": "SELL", "qty": pos.qty,
                "reason": f"강제청산 {now_str} — 손익 {pnl_pct:+.1f}%",
            }

        return {"signal": "HOLD", "qty": 0, "reason": ""}

    def check_exit_next_day(self, pos: LimitPosition, cur_price: int) -> dict:
        """
        익일 매도 전략 (장전 동시호가 포함).

        expected_gap_pct: 예상 갭 등락률 (동시호가 집계 전 추정값)
        """
        cfg_exit = self.cfg.get_exit()
        gap_pct  = pos.profit_pct(cur_price)    # 전날 매수가 기준

        if gap_pct >= cfg_exit["next_day_gap_full_pct"]:
            return {
                "signal": "SELL_ALL", "qty": pos.qty,
                "reason": f"갭상승 전량 {gap_pct:+.1f}% ≥ {cfg_exit['next_day_gap_full_pct']}%",
            }
        if gap_pct >= cfg_exit["next_day_gap_half_pct"]:
            half_qty = max(1, pos.qty // 2)
            return {
                "signal": "SELL_HALF", "qty": half_qty,
                "reason": f"갭상승 절반 {gap_pct:+.1f}% ≥ {cfg_exit['next_day_gap_half_pct']}%",
            }
        if gap_pct <= cfg_exit["next_day_stop_loss_pct"]:
            return {
                "signal": "SELL_ALL", "qty": pos.qty,
                "reason": f"익일 손절 {gap_pct:+.1f}% ≤ {cfg_exit['next_day_stop_loss_pct']}%",
            }
        return {"signal": "HOLD", "qty": 0, "reason": ""}

    # ──────────────────────────────────────────────────────────────────────────
    # 포지션 관리
    # ──────────────────────────────────────────────────────────────────────────

    def add_position(
        self, code: str, name: str, qty: int, price: int,
        score: float = 0, theme: str = "",
    ) -> None:
        pos = LimitPosition(
            code=code, name=name, qty=qty, buy_price=price,
            high_price=price, score=score, theme=theme,
        )
        self._positions[code] = pos
        self._save_positions()
        logger.info(
            f"[LimitStrategy] 포지션 추가: {name}({code}) "
            f"{qty}주 @{price:,}원 | 점수 {score:.1f}"
        )

    def remove_position(
        self, code: str, cur_price: int, qty: int, reason: str
    ) -> dict | None:
        pos = self._positions.pop(code, None)
        if not pos:
            return None

        pnl     = pos.profit_won(cur_price)
        pnl_pct = pos.profit_pct(cur_price)
        hold_h  = (datetime.now(KST) - pos.buy_time).total_seconds() / 3600

        # 연속 손절 추적
        if "손절" in reason:
            self._consec_loss += 1
            risk = self.cfg.get_risk()
            if self._consec_loss >= risk["max_consecutive_loss"]:
                self._paused = True
                logger.warning(
                    f"[LimitStrategy] 연속 손절 {self._consec_loss}회 "
                    "→ 전략C 일시 정지"
                )
        else:
            self._consec_loss = 0

        trade = {
            "code": code, "name": pos.name,
            "buy_price": pos.buy_price, "sell_price": cur_price,
            "qty": qty, "pnl": pnl, "pnl_pct": round(pnl_pct, 2),
            "reason": reason, "hold_hours": round(hold_h, 1),
            "score": pos.score,
        }
        self._trades.append(trade)
        self._save_positions()

        logger.info(
            f"[LimitStrategy] 포지션 제거: {pos.name}({code}) "
            f"손익 {pnl:+,}원 ({pnl_pct:+.1f}%) — {reason}"
        )
        return trade

    def get_positions(self) -> list[LimitPosition]:
        return list(self._positions.values())

    def held_codes(self) -> set[str]:
        return set(self._positions.keys())

    def force_exit_all(self) -> list[str]:
        """강제 전량 청산 — broker 호출은 main.py 에서 처리"""
        codes = list(self._positions.keys())
        for code in codes:
            pos = self._positions.get(code)
            if not pos:
                continue
            try:
                info = self.broker.get_stock_info(code)
                cur  = info["cur_price"] if info else pos.buy_price
                result = self.broker.sell_order(code, pos.qty, 0, "3")
                if result["success"]:
                    self.remove_position(code, cur, pos.qty, "강제청산")
            except Exception as e:
                logger.error(f"[LimitStrategy] 강제청산 실패 {code}: {e}")
        return codes

    # ──────────────────────────────────────────────────────────────────────────
    # 당일 결산
    # ──────────────────────────────────────────────────────────────────────────

    def daily_summary(self) -> str:
        if not self._trades:
            return (
                "<b>[ 상한가선진입 C 당일 결산 ]</b>\n"
                "체결 없음"
            )

        wins  = [t for t in self._trades if t["pnl"] > 0]
        loses = [t for t in self._trades if t["pnl"] <= 0]
        total_pnl   = sum(t["pnl"] for t in self._trades)
        win_rate    = len(wins) / len(self._trades) * 100

        lines = [
            f"<b>[ 상한가선진입 C 당일 결산 ]</b>",
            f"총 {len(self._trades)}회 | 승률 {win_rate:.0f}% "
            f"({len(wins)}승 {len(loses)}패)",
            f"총 손익: <b>{total_pnl:+,}원</b>",
            "──────────────────",
        ]
        for t in sorted(self._trades, key=lambda x: x["pnl"], reverse=True):
            icon = "📈" if t["pnl"] > 0 else "📉"
            lines.append(
                f"{icon} {t['name']}({t['code']}) "
                f"{t['pnl_pct']:+.1f}% {t['pnl']:+,}원 "
                f"[{t['reason']}]"
            )
        return "\n".join(lines)

    @property
    def is_paused(self) -> bool:
        return self._paused

    def resume(self) -> None:
        """텔레그램 명령어로 수동 재개"""
        self._paused = False
        self._consec_loss = 0
        logger.info("[LimitStrategy] 전략C 재개")
