"""
limit_scanner.py — 상한가 선진입(전략C) 종목 스캐너  v1.0
────────────────────────────────────────────────────────────
기존 scanner.py (DayTradingScanner) 와 동일한 패턴으로 구현.
broker.py 의 기존 API (ka10016/ka10023/ka10024/ka10081/ka10009/ka10001) 만 사용한다.

스캔 흐름
──────────
1. ka10023(거래량급증) + ka10016(신고가) 로 후보 풀 수집
2. 등락률 ≥ 24 % 1차 필터
3. 전일 대비 거래량 배수 계산
4. 관리종목·단기과열 블랙리스트 제외
5. 상한가 잔량·체결강도 조회 (ka10001)
6. 기관·외인 수급 조회 (ka10009)
7. 일봉 차트 → MA / RSI 계산 (ka10081)
8. 테마 동료 등락률 집계
9. LimitStrategy.score_candidate() 로 종합 점수 계산
10. 점수 상위 종목 반환
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime
from typing import Optional

import pytz

from limit_config import LimitConfig
from strategy_limit import LimitStrategy, THEME_CLUSTERS, _CODE_TO_THEME

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")


class LimitScanner:
    """
    상한가 선진입 후보 스캐너

    Parameters
    ──────────
    broker        : KiwoomBroker 인스턴스
    cfg           : LimitConfig 인스턴스
    limit_strategy: LimitStrategy 인스턴스 (점수 계산용)
    """

    def __init__(
        self,
        broker,
        cfg           : LimitConfig,
        limit_strategy: LimitStrategy,
    ) -> None:
        self.broker          = broker
        self.cfg             = cfg
        self.limit_strategy  = limit_strategy

        self._vol_cache      : dict[str, int]   = {}   # 전일 거래량 캐시
        self._blacklist      : set[str]          = set() # 관리종목/단기과열

    # ──────────────────────────────────────────────────────────────────────────
    # 일일 초기화
    # ──────────────────────────────────────────────────────────────────────────

    def init_daily(self) -> None:
        """
        08:50 장전 준비 시 호출.
        전일 거래량 캐시 구축 + 블랙리스트 초기화.
        """
        logger.info("[LimitScanner] 장전 초기화 시작")
        self._blacklist.clear()
        self._vol_cache.clear()

        # 신고가 / 거래량 급증 풀에서 관심 종목 사전 캐시
        try:
            pool = self._collect_pool()
            for code in pool:
                try:
                    chart = self.broker.get_daily_chart(code, count=6)
                    if chart and len(chart) >= 2:
                        prev_vol = chart[-2].get("volume", 0)   # 전일 거래량
                        self._vol_cache[code] = max(1, prev_vol)
                    _time.sleep(0.05)
                except Exception:
                    pass
            logger.info(
                f"[LimitScanner] 초기화 완료 — 풀 {len(pool)}개 / "
                f"거래량 캐시 {len(self._vol_cache)}개"
            )
        except Exception as e:
            logger.error(f"[LimitScanner] 초기화 실패: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # 메인 스캔
    # ──────────────────────────────────────────────────────────────────────────

    def scan(
        self,
        held_codes    : set[str],
        exclude_codes : set[str] | None = None,
    ) -> list[dict]:
        """
        25% 이상 급등 종목 스캔 후 종합 점수 상위 순으로 반환.

        v1.1 수정:
          - 후보 빌드를 한 번만 수행 (기존 2번 → 1번, API 호출 50% 절감)
          - 시간 가드 추가 (09:00~14:30 외 즉시 반환)
          - 소형주 진입 기준 완화 옵션 (min_trading_value_small)
          - 디버그 로그 강화 (왜 탈락했는지 파악 가능)
        """
        # ── 시간 가드 ────────────────────────────────────────────
        now_str = datetime.now(KST).strftime("%H:%M")
        if not ("09:00" <= now_str <= "14:30"):
            return []

        cfg_scan     = self.cfg.get_scan()
        cfg_entry    = self.cfg.get_entry()
        entry_pct    = cfg_scan.get("entry_pct_min", 23)
        vol_min      = cfg_scan.get("vol_ratio_min", 1.5)
        score_min    = cfg_entry.get("composite_score_min", 70)
        max_pos      = cfg_entry.get("max_positions", 5)
        exclude      = (exclude_codes or set()) | set(held_codes) | self._blacklist

        # 1) 후보 풀 수집 ────────────────────────────────────────
        raw_pool = self._collect_pool()
        logger.info(f"[LimitScanner] 풀 수집 {len(raw_pool)}개")
        if not raw_pool:
            return []

        # 2) 후보 빌드 (1번만) + 1차 필터 ────────────────────────
        built: list[dict] = []
        peer_states: dict[str, float] = {}   # 테마 점수 계산용

        for code in raw_pool:
            if code in exclude:
                continue
            try:
                candidate = self._build_candidate(code)
                if candidate is None:
                    continue

                change_pct = candidate["change_pct"]

                # 등락률 1차 필터
                if change_pct < entry_pct:
                    logger.debug(
                        f"[LimitScanner] {code} 등락률 탈락: "
                        f"{change_pct:.1f}% < {entry_pct}%"
                    )
                    continue

                # 거래량 배수 계산
                prev_vol  = self._vol_cache.get(code, 1)
                vol_ratio = candidate["volume"] / prev_vol if prev_vol > 0 else 1.0
                candidate["vol_ratio"] = round(vol_ratio, 1)

                if vol_ratio < vol_min:
                    logger.debug(
                        f"[LimitScanner] {code} 거래량 탈락: "
                        f"{vol_ratio:.1f}배 < {vol_min}배"
                    )
                    continue

                peer_states[code] = change_pct
                built.append(candidate)
                _time.sleep(0.05)

            except Exception as e:
                logger.debug(f"[LimitScanner] {code} 빌드 실패: {e}")

        logger.info(f"[LimitScanner] 1차 필터 통과: {len(built)}개")

        if not built:
            return []

        # 3) 점수 계산 ─────────────────────────────────────────────
        scored: list[dict] = []
        for candidate in built:
            try:
                score = self.limit_strategy.score_candidate(candidate, peer_states)
                candidate["score"] = score
                candidate["theme"] = _CODE_TO_THEME.get(candidate["code"], "")
                candidate["theme_score"] = LimitStrategy._calc_theme_score(
                    candidate["code"], peer_states
                )

                if score < score_min:
                    logger.info(
                        f"[LimitScanner] {candidate['name']}({candidate['code']}) "
                        f"점수 탈락: {score:.1f} < {score_min}"
                    )
                    continue

                scored.append(candidate)
                logger.info(
                    f"[LimitScanner] ✅ {candidate['name']}({candidate['code']}) "
                    f"등락:{candidate['change_pct']:.1f}% "
                    f"거래량:{candidate['vol_ratio']:.1f}배 "
                    f"점수:{score:.1f}"
                )
            except Exception as e:
                logger.debug(f"[LimitScanner] 점수 계산 실패 {candidate.get('code')}: {e}")

        # 4) 점수 내림차순 정렬 ────────────────────────────────────
        scored.sort(key=lambda x: x["score"], reverse=True)
        logger.info(f"[LimitScanner] 최종 후보 {len(scored)}개")
        return scored

    # ──────────────────────────────────────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────────────────────────────────────

    def _collect_pool(self) -> list[str]:
        """
        거래량급증(ka10023) + 신고가(ka10016) 조합으로 후보 코드 풀을 수집.
        기존 strategy.py 의 소스 수집 패턴과 동일하게 broker API 호출.
        """
        codes: set[str] = set()
        try:
            # 거래량 급증 (ka10023 래퍼)
            vol_list = self.broker.get_volume_surge()   # [{"code":..,"name":..}, ...]
            for item in (vol_list or []):
                codes.add(item["code"])
        except Exception as e:
            logger.warning(f"[LimitScanner] ka10023 실패: {e}")

        try:
            # 신고가 (ka10016 래퍼)
            high_list = self.broker.get_new_high()
            for item in (high_list or []):
                codes.add(item["code"])
        except Exception as e:
            logger.warning(f"[LimitScanner] ka10016 실패: {e}")

        return list(codes)

    def _build_candidate(self, code: str) -> dict | None:
        """
        단일 종목의 현재가·호가·수급·차트 정보를 통합하여
        candidate dict 로 반환한다.
        """
        try:
            # 현재가 / 등락률 (ka10001)
            info = self.broker.get_stock_info(code)
            if not info:
                return None

            cur_price  = info.get("cur_price", 0)
            change_pct = info.get("change_pct", 0.0)
            volume     = info.get("volume", 0)

            if cur_price <= 0:
                return None

            # 호가 (잔량 정보)
            orderbook  = self.broker.get_orderbook(code) or {}
            sell_rem   = orderbook.get("sell_remain", 0)
            buy_rem    = orderbook.get("buy_remain", 1)
            strength   = orderbook.get("strength", 100.0)
            sell_ratio = sell_rem / buy_rem if buy_rem > 0 else 1.0
            buy_ratio  = orderbook.get("buy_ratio", 0.5)

            # 수급 (ka10009)
            supply     = {}
            try:
                supply = self.broker.get_investor_info(code) or {}
            except Exception:
                pass
            inst_net    = supply.get("inst_net", 0)
            foreign_net = supply.get("foreign_net", 0)

            # 일봉 차트 → MA / RSI (ka10081)
            ma5 = ma20 = ma60 = rsi = 0.0
            try:
                chart = self.broker.get_daily_chart(code, count=65)
                if chart and len(chart) >= 20:
                    closes = [c["close"] for c in chart[-65:]]
                    ma5    = sum(closes[-5:])  / 5
                    ma20   = sum(closes[-20:]) / 20
                    ma60   = sum(closes[-60:]) / min(60, len(closes))
                    rsi    = self._calc_rsi(closes[-15:])
            except Exception:
                pass

            # 상한가 유지 시간 (broker 에서 제공 가능한 경우)
            limit_hold_min = info.get("limit_hold_min", 0)

            return {
                "code"              : code,
                "name"              : info.get("name", code),
                "cur_price"         : cur_price,
                "change_pct"        : change_pct,
                "volume"            : volume,
                "vol_ratio"         : 1.0,          # 이후 계산으로 덮어씀
                "sell_remain_ratio" : sell_ratio,
                "buy_ratio"         : buy_ratio,
                "strength"          : strength,
                "inst_net"          : inst_net,
                "foreign_net"       : foreign_net,
                "ma5"               : ma5,
                "ma20"              : ma20,
                "ma60"              : ma60,
                "rsi"               : rsi,
                "limit_hold_min"    : limit_hold_min,
                # 기본값 (외부 신호 미구현)
                "news_score"        : 50,
                "social_score"      : 50,
                "market_score"      : 60,
            }

        except Exception as e:
            logger.debug(f"[LimitScanner] {code} 빌드 예외: {e}")
            return None

    @staticmethod
    def _calc_rsi(closes: list[float], period: int = 14) -> float:
        """단순 RSI 계산 (Wilder 방식)"""
        if len(closes) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - 100 / (1 + rs), 1)

    def add_blacklist(self, code: str) -> None:
        """수동으로 블랙리스트 추가 (텔레그램 명령 등)"""
        self._blacklist.add(code)

    def remove_blacklist(self, code: str) -> None:
        self._blacklist.discard(code)
