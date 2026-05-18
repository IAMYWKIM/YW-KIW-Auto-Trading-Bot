"""
market_filter.py — 시장 상황 모니터링 및 매매 허용 판단
v1.0

[핵심 기능]
  1. 지수 상태 모니터링 (KOSPI/KOSDAQ)
     - KODEX 200 ETF(069500) → KOSPI 대리 지표
     - KODEX KOSDAQ150(229200) → KOSDAQ 대리 지표
     - 지수 -0.5% 이하 하락 시 단타 진입 중단
     - 지수 +0.5% 이상 상승 시 적극 매매 허용

  2. 외인/기관 매수 강도 측정
     - ka10004 등락률 상위 종목의 외인 순매수 집계
     - 외인 대량 매수 시 추가 점수 부여

  3. 시장 상태 점수 (0~100)
     STOP (0~30)   → 신규 진입 완전 중단
     CAUTION (31~60) → 소극적 매매 (포지션 1개 이하)
     NORMAL (61~80)  → 일반 매매
     BULLISH (81~100) → 적극 매매 (최대 포지션 허용)

[사용 예시]
    mf = MarketFilter(broker)
    state = mf.get_market_state()
    if not state["allow_entry"]:
        logger.info(f"시장 필터 차단: {state['reason']}")
        return
"""

import logging
import time
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
KST    = pytz.timezone("Asia/Seoul")

# ── 지수 대리 ETF 코드 ──────────────────────────────────────
KOSPI_PROXY  = "069500"   # KODEX 200 (코스피200 ETF)
KOSDAQ_PROXY = "229200"   # KODEX KOSDAQ150 (코스닥150 ETF)


class MarketFilter:
    """
    시장 상황 필터 — 지수 + 외인 매수 종합 판단

    [캐시 설계]
    30초마다 갱신 (job_scalp_loop 주기와 동일)
    → 매 스캔마다 API 호출 없이 캐시 활용
    """

    def __init__(self, broker):
        self.broker = broker
        self._cache: Optional[dict] = None
        self._cache_ts: float       = 0.0
        self._cache_ttl: int        = 30   # 초

    # ──────────────────────────────────────────────────────────
    # 메인 메서드 — 시장 상태 반환
    # ──────────────────────────────────────────────────────────

    def get_market_state(self, force: bool = False) -> dict:
        """
        시장 상태 종합 판단

        Returns:
            {
              "score":        시장 점수 (0~100),
              "grade":        "STOP" | "CAUTION" | "NORMAL" | "BULLISH",
              "allow_entry":  True/False — 신규 진입 허용 여부,
              "max_positions": 허용 최대 포지션 수,
              "kospi_pct":    코스피 ETF 등락률 (%),
              "kosdaq_pct":   코스닥 ETF 등락률 (%),
              "foreign_score": 외인 매수 점수 (0~40),
              "reason":       진입 차단 사유 (allow_entry=False 시),
              "summary":      요약 텍스트,
            }
        """
        now = time.time()
        if not force and self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        try:
            result = self._calc_state()
        except Exception as e:
            logger.error(f"[MarketFilter] 상태 계산 실패: {e}")
            # 오류 시 CAUTION (안전 우선)
            result = self._default_caution(str(e))

        self._cache    = result
        self._cache_ts = now
        return result

    def _calc_state(self) -> dict:
        """지수 + 외인 점수 계산"""

        # ── 1. KOSPI ETF 등락률 ───────────────────────────────
        kospi_pct  = self._get_flu_rt(KOSPI_PROXY,  "KOSPI ETF")
        kosdaq_pct = self._get_flu_rt(KOSDAQ_PROXY, "KOSDAQ ETF")

        # ── 2. 외인 매수 강도 ─────────────────────────────────
        foreign_score = self._calc_foreign_score()

        # ── 3. 지수 점수 계산 (최대 60점) ────────────────────
        # KOSPI 40점 + KOSDAQ 20점
        kospi_score  = self._index_score(kospi_pct,  weight=40)
        kosdaq_score = self._index_score(kosdaq_pct, weight=20)
        index_score  = kospi_score + kosdaq_score

        total_score  = min(100, index_score + foreign_score)

        # ── 4. 등급 판정 ──────────────────────────────────────
        if total_score <= 30:
            grade        = "STOP"
            allow_entry  = False
            max_pos      = 0
        elif total_score <= 55:
            grade        = "CAUTION"
            allow_entry  = True
            max_pos      = 1
        elif total_score <= 75:
            grade        = "NORMAL"
            allow_entry  = True
            max_pos      = 2
        else:
            grade        = "BULLISH"
            allow_entry  = True
            max_pos      = 3   # 설정의 max_positions 와 min 적용

        # ── 5. 차단 사유 ──────────────────────────────────────
        reason = ""
        if not allow_entry:
            parts = []
            if kospi_pct <= -0.5:
                parts.append(f"KOSPI {kospi_pct:+.2f}%")
            if kosdaq_pct <= -0.5:
                parts.append(f"KOSDAQ {kosdaq_pct:+.2f}%")
            if not parts:
                parts.append(f"종합 점수 {total_score}점")
            reason = "지수 하락 차단: " + ", ".join(parts)

        now_str = datetime.now(KST).strftime("%H:%M")
        grade_emoji = {"STOP": "🛑", "CAUTION": "⚠️", "NORMAL": "🟢", "BULLISH": "🚀"}

        summary = (
            f"{grade_emoji.get(grade,'?')} 시장 [{grade}] {now_str}\n"
            f"KOSPI ETF: {kospi_pct:+.2f}%  KOSDAQ ETF: {kosdaq_pct:+.2f}%\n"
            f"외인점수: {foreign_score}/40  종합: {total_score}점"
        )

        return {
            "score":         total_score,
            "grade":         grade,
            "allow_entry":   allow_entry,
            "max_positions": max_pos,
            "kospi_pct":     kospi_pct,
            "kosdaq_pct":    kosdaq_pct,
            "foreign_score": foreign_score,
            "reason":        reason,
            "summary":       summary,
        }

    # ──────────────────────────────────────────────────────────
    # 지수 ETF 등락률 조회
    # ──────────────────────────────────────────────────────────

    def _get_flu_rt(self, code: str, label: str) -> float:
        """ka10001로 ETF 등락률 조회"""
        try:
            info = self.broker.get_stock_info(code)
            flu  = float(info.get("flu_rt", "0"))
            logger.debug(f"[MarketFilter] {label}({code}): {flu:+.2f}%")
            return flu
        except Exception as e:
            logger.warning(f"[MarketFilter] {label} 조회 실패: {e}")
            return 0.0

    # ──────────────────────────────────────────────────────────
    # 외인 매수 강도 점수 계산 (0~40점)
    # ──────────────────────────────────────────────────────────

    def _calc_foreign_score(self) -> int:
        """
        등락률 상위 10개 종목의 외인 순매수 집계
        외인이 강하게 사고 있으면 시장에 돈이 들어오는 신호

        [점수 기준]
          외인 순매수 상위 10개 중 7개+ 순매수: 40점
          5~6개 순매수: 30점
          3~4개 순매수: 20점
          1~2개 순매수: 10점
          0개:          0점
        """
        try:
            # 등락률 상위 종목 조회
            data = self.broker._post(
                "ka10023", "/api/dostk/rkinfo",
                {
                    "mrkt_tp":     "000",
                    "sort_tp":     "2",   # 등락률 기준
                    "tm_tp":       "2",
                    "trde_qty_tp": "0",
                    "tm":          "",
                    "stk_cnd":     "20",
                    "pric_tp":     "0",
                    "stex_tp":     "3",
                }
            )
            items = data.get("trde_qty_sdnin", [])[:10]
            if not items:
                return 20   # 데이터 없으면 중간 점수

            # 외인 순매수 집계
            # ka10023 응답에 외인 필드가 있으면 사용, 없으면 기본 20점
            foreign_buy_cnt = 0
            for item in items:
                # 외인 순매수 필드 탐색 (frgnr_ntby, frgn_ntby 등 키 다를 수 있음)
                frgn = (
                    item.get("frgnr_ntby_qty", 0) or
                    item.get("frgn_ntby_qty",  0) or
                    item.get("frgnr_ord_qty",  0) or
                    0
                )
                try:
                    frgn_val = int(str(frgn).lstrip("+-") or "0")
                    is_buy   = str(frgn).startswith("+") or frgn_val > 0
                except (ValueError, TypeError):
                    is_buy = False

                if is_buy:
                    foreign_buy_cnt += 1

            # 외인 필드가 없는 경우 상위 종목 상승률로 대리 판단
            if foreign_buy_cnt == 0:
                # 상위 10개 중 5개 이상이 +5% 넘으면 외인 유입 가능성
                # flu_rt는 '+3.87' 같은 소수점 문자열 → float 변환 필요
                surging = 0
                for item in items:
                    try:
                        flu_str = str(item.get("flu_rt", "0")).strip()
                        flu_val = abs(float(flu_str))
                        if flu_val >= 5:
                            surging += 1
                    except (ValueError, TypeError):
                        pass
                if surging >= 7:   return 35
                elif surging >= 5: return 25
                elif surging >= 3: return 15
                else:              return 10

            if foreign_buy_cnt >= 7:   return 40
            elif foreign_buy_cnt >= 5: return 30
            elif foreign_buy_cnt >= 3: return 20
            elif foreign_buy_cnt >= 1: return 10
            else:                      return 0

        except Exception as e:
            logger.warning(f"[MarketFilter] 외인 점수 계산 실패: {e}")
            return 20   # 오류 시 중간 점수

    # ──────────────────────────────────────────────────────────
    # 지수 점수 계산
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _index_score(pct: float, weight: int) -> int:
        """
        지수 등락률 → 점수 변환

        +1.0% 이상:  100% (weight 전부)
        +0.5% 이상:  80%
        +0.0% 이상:  60%
        -0.5% 이상:  30%
        -0.5% 미만:  0%   → 사실상 진입 중단

        Args:
            pct:    등락률 (%)
            weight: 최대 점수
        """
        if pct >= 1.0:
            ratio = 1.0
        elif pct >= 0.5:
            ratio = 0.80
        elif pct >= 0.0:
            ratio = 0.60
        elif pct >= -0.5:
            ratio = 0.30
        else:
            ratio = 0.0   # 하락 차단
        return int(weight * ratio)

    @staticmethod
    def _default_caution(reason: str) -> dict:
        """오류 시 기본 CAUTION 상태"""
        return {
            "score":         50,
            "grade":         "CAUTION",
            "allow_entry":   True,
            "max_positions": 1,
            "kospi_pct":     0.0,
            "kosdaq_pct":    0.0,
            "foreign_score": 20,
            "reason":        f"조회 오류 — 소극적 매매: {reason}",
            "summary":       f"⚠️ 시장 [CAUTION] — 조회 오류",
        }

    # ──────────────────────────────────────────────────────────
    # 텔레그램용 상태 메시지
    # ──────────────────────────────────────────────────────────

    def format_for_telegram(self) -> str:
        """시장 상태 텔레그램 메시지"""
        s = self.get_market_state(force=True)
        bar_w = 10
        filled = round(s["score"] / 100 * bar_w)
        bar    = "█" * filled + "░" * (bar_w - filled)

        grade_map = {
            "STOP":    ("🛑", "매매 중단"),
            "CAUTION": ("⚠️", "소극적 매매"),
            "NORMAL":  ("🟢", "정상 매매"),
            "BULLISH": ("🚀", "적극 매매"),
        }
        icon, desc = grade_map.get(s["grade"], ("?", ""))

        lines = [
            f"{icon} <b>[ 시장 상황 — {desc} ]</b>",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"종합 점수: <b>{s['score']}/100</b>  {bar}",
            f"KOSPI ETF: <b>{s['kospi_pct']:+.2f}%</b>",
            f"KOSDAQ ETF: <b>{s['kosdaq_pct']:+.2f}%</b>",
            f"외인 강도: {s['foreign_score']}/40점",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"허용 포지션: <b>최대 {s['max_positions']}개</b>",
        ]
        if not s["allow_entry"]:
            lines.append(f"\n⛔ <b>진입 차단</b>: {s['reason']}")
        return "\n".join(lines)
