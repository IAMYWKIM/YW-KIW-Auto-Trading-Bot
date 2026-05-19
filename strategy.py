"""
strategy.py v1.4.3 — 키움 조건검색식 기반 종가베팅 스캔 (배포용 최신본)

[v1.4.2 → v1.4.3 변경]
  surge_lookback_days 확장에 따른 로그 강화
  전일종가 없음 오류(주성엔지니어링 케이스) 방어 코드 추가
  check_recent_surge_strong: DEBUG → INFO 레벨 탈락 사유 출력

[v1.3.2 버그 수정 반영]
  BUG2: 수급 데이터 항상 수집 (use_*_buy 플래그와 분리)
  BUG3: analyze_candidate()에서 pullback_pct 실제 계산
  BUG4: volume_ratio 필터 추가
  DESIGN1: scan_candidates() max_positions 제한 제거

[v1.4.0 핵심 변경 — 키움 조건검색식 코드 재현]
  기존 3소스(ka10023/ka10016/ka10024) 방식 → 조건식 A~G 직접 구현으로 대체

  조건검색식 재현:
    조건 A: [일]0봉전 Envelope(20,20) 중가가 상한선이상, 30봉이내
           → 최근 30봉 이내에 종가 >= Envelope 상한선(MA20×1.2)인 날 존재
    조건 B: 기간내 거래대금 [일]0봉전 30봉이내 거래대금
           → 30봉 이내 거래대금 유효 + strategy_config min_trading_value 이상
    조건 C: [일]0봉전 Envelope(20,20) 중가가 상한선이상
           → 오늘 현재가 >= Envelope 상한선
    조건 D: 가격-이동평균 비교:[일]0봉전 (종가 3이평) < 종가
           → MA3 < 현재가 (극단기 정배열)
    조건 E: 기간내 등락률:[일]0봉전 30봉이내에서 전일종가대비증가 9% 이상
           → 30봉 이내 일간 등락률 +9% 이상인 날 존재 (필수)
    조건 F: 기간내 등락률:[일]0봉전 30봉이내에서 전일종가대비증가 29.5% 이상
           → 30봉 이내 일간 등락률 +29.5% 이상인 날 존재 (가산점)
    조건 G: [일]거래대금 300억 이상 (가산점)
           ※ 원래 조건식 300000(백만단위)=3000억이나 실운용 의도에 맞게 300억으로 수정

  필수 조건: B AND E AND D AND (C OR A)
  가산점 조건: G(+15점) F(+10점) A(+5점)
"""

import logging
import time
from datetime import datetime
from typing import Optional

from broker import KiwoomBroker
from strategy_config import StrategyConfig

logger = logging.getLogger(__name__)


def _clean_code(code: str) -> str:
    """종목코드 정제 — _AL(SOR), _NX(NXT) 접미사 제거"""
    return code.split("_")[0] if code else code


class Strategy:

    def __init__(self, broker: KiwoomBroker, strategy_cfg: StrategyConfig):
        self.broker = broker
        self.scfg   = strategy_cfg

    # ──────────────────────────────────────────────────────────
    # 1. 일봉 데이터 (MA + 거래량 + 거래대금)
    # ──────────────────────────────────────────────────────────

    def get_daily_data(self, code: str) -> dict:
        """ka10081 — 주식일봉차트"""
        try:
            data = self.broker._post(
                "ka10081",
                "/api/dostk/chart",
                {
                    "stk_cd":       code,
                    "base_dt":      datetime.now().strftime("%Y%m%d"),
                    "upd_stkpc_tp": "1",
                }
            )
            candles = data.get("stk_dt_pole_chart_qry", [])
            if not candles:
                return {}

            closes, volumes, trading_values, highs, lows = [], [], [], [], []
            for c in candles:
                try:
                    closes.append(abs(float(c.get("cur_prc",    "0").lstrip("+-") or "0")))
                    volumes.append(abs(int(  c.get("trde_qty",  "0").lstrip("+-") or "0")))
                    tv = abs(int(c.get("trde_prica", "0").lstrip("+-") or "0")) * 1_000_000
                    trading_values.append(tv)
                    highs.append(abs(float(c.get("high_pric", "0").lstrip("+-") or "0")))
                    lows.append( abs(float(c.get("low_pric",  "0").lstrip("+-") or "0")))
                except ValueError:
                    continue

            if len(closes) < 20:
                return {}

            cfg = self.scfg.get_scan()
            s, m, l = cfg["ma_short"], cfg["ma_mid"], cfg["ma_long"]
            ma3  = sum(closes[:3]) / 3 if len(closes) >= 3 else closes[0]
            ma5  = sum(closes[:s]) / s
            ma20 = sum(closes[:m]) / m
            ma60 = sum(closes[:min(l, len(closes))]) / min(l, len(closes))

            return {
                "ma3":            round(ma3, 2),
                "ma5":            round(ma5, 2),
                "ma20":           round(ma20, 2),
                "ma60":           round(ma60, 2),
                "closes":         closes,
                "volumes":        volumes,
                "trading_values": trading_values,
                "highs":          highs,
                "lows":           lows,
            }
        except Exception as e:
            logger.error(f"[Strategy] {code} 일봉 조회 실패: {e}")
            return {}

    def get_moving_averages(self, code: str) -> dict:
        return self.get_daily_data(code)

    # ──────────────────────────────────────────────────────────
    # 2. RSI 계산
    # ──────────────────────────────────────────────────────────

    def calculate_rsi(self, closes: list, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(period):
            diff = closes[i] - closes[i + 1]
            gains.append(diff if diff > 0 else 0)
            losses.append(-diff if diff < 0 else 0)
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - 100 / (1 + rs), 1)

    # ──────────────────────────────────────────────────────────
    # 3. 수급 조회 (항상 수집 — v1.3.2 BUG2 수정)
    # ──────────────────────────────────────────────────────────

    def get_institution_foreign_flow(self, code: str) -> dict:
        """ka10009 — 기관/외국인 순매수 (필터 여부와 무관하게 항상 수집)"""
        try:
            data = self.broker._post(
                "ka10009", "/api/dostk/frgnistt", {"stk_cd": code}
            )
            def to_int(s):
                s = (s or "0").strip()
                if not s or s in ("-", "+"): return 0
                sign = -1 if s.startswith("-") else 1
                return sign * int(s.lstrip("+-0") or "0")
            return {
                "institution_net": to_int(data.get("orgn_daly_nettrde", "0")),
                "foreign_net":     to_int(data.get("frgnr_daly_nettrde", "0")),
            }
        except Exception as e:
            logger.debug(f"[Strategy] {code} 수급 조회 실패: {e}")
            return {"institution_net": 0, "foreign_net": 0}

    # ──────────────────────────────────────────────────────────
    # 4. Envelope 계산 — 조건 A, C 공통
    # ──────────────────────────────────────────────────────────

    def calc_envelope(self, closes: list, period: int = 20,
                      band_pct: float = 20.0) -> list:
        """
        Envelope(period, band_pct) 상한선 시계열 계산
        반환: [{"upper", "mid", "lower"}, ...] — index 0 = 가장 최근 봉
        """
        if len(closes) < period:
            return []
        result = []
        for i in range(len(closes) - period + 1):
            ma = sum(closes[i:i + period]) / period
            result.append({
                "upper": round(ma * (1 + band_pct / 100), 2),
                "mid":   round(ma, 2),
                "lower": round(ma * (1 - band_pct / 100), 2),
            })
        return result

    # ──────────────────────────────────────────────────────────
    # 5. 조건 A — 30봉 이내 Envelope 상한선 이상 경험
    # ──────────────────────────────────────────────────────────

    def check_cond_A(self, closes: list, lookback: int = 30,
                     period: int = 20, band_pct: float = 20.0) -> dict:
        """
        조건 A: [일]0봉전 Envelope(20,20) 중가가 상한선이상, 30봉이내
        → 최근 30봉 이내에 종가 >= Envelope 상한선인 날이 1일 이상 존재
        """
        envelopes = self.calc_envelope(closes, period, band_pct)
        if not envelopes:
            return {"pass": False, "touch_days_ago": -1, "max_touch_pct": 0.0}

        touch_days_ago = -1
        max_touch_pct  = 0.0
        check_range    = min(lookback, len(envelopes), len(closes))

        for i in range(check_range):
            env   = envelopes[i]
            close = closes[i]
            if env["upper"] <= 0:
                continue
            if close >= env["upper"]:
                touch_pct = (close - env["upper"]) / env["upper"] * 100
                if touch_days_ago == -1:
                    touch_days_ago = i
                if touch_pct > max_touch_pct:
                    max_touch_pct = touch_pct

        return {
            "pass":           touch_days_ago >= 0,
            "touch_days_ago": touch_days_ago,
            "max_touch_pct":  round(max_touch_pct, 2),
        }

    # ──────────────────────────────────────────────────────────
    # 6. 조건 C — 현재 Envelope 상한선 이상
    # ──────────────────────────────────────────────────────────

    def check_cond_C(self, closes: list,
                     period: int = 20, band_pct: float = 20.0) -> dict:
        """
        조건 C: [일]0봉전 Envelope(20,20) 중가가 상한선이상
        → 오늘 종가(closes[0]) >= Envelope 상한선
        """
        envelopes = self.calc_envelope(closes, period, band_pct)
        if not envelopes or len(closes) == 0:
            return {"pass": False, "upper": 0, "pct_from_upper": 0.0}

        env   = envelopes[0]
        close = closes[0]
        pct   = round((close - env["upper"]) / env["upper"] * 100, 2) \
                if env["upper"] > 0 else 0.0
        return {
            "pass":           close >= env["upper"],
            "upper":          env["upper"],
            "pct_from_upper": pct,
        }

    # ──────────────────────────────────────────────────────────
    # 7. 조건 D — MA3 < 현재가 (극단기 정배열)
    # ──────────────────────────────────────────────────────────

    def check_cond_D(self, daily: dict) -> dict:
        """
        조건 D: 가격-이동평균 비교:[일]0봉전 (종가 3이평) < 종가
        → closes[0] > MA3
        """
        ma3    = daily.get("ma3", 0)
        closes = daily.get("closes", [])
        if not closes or ma3 <= 0:
            return {"pass": False, "ma3": 0, "gap_pct": 0.0}

        cur     = closes[0]
        gap_pct = round((cur - ma3) / ma3 * 100, 2) if ma3 > 0 else 0.0
        return {
            "pass":    cur > ma3,
            "ma3":     round(ma3, 2),
            "gap_pct": gap_pct,   # 양수 = 현재가가 MA3 위
        }

    # ──────────────────────────────────────────────────────────
    # 8. 조건 E / F — 30봉 이내 기간 등락률
    # ──────────────────────────────────────────────────────────

    def check_cond_EF(self, closes: list, lookback: int = 30,
                      threshold_e: float = 9.0,
                      threshold_f: float = 29.5) -> dict:
        """
        조건 E: 30봉이내 전일종가대비 +9% 이상인 날 존재  (필수)
        조건 F: 30봉이내 전일종가대비 +29.5% 이상인 날 존재 (가산점)
        """
        if len(closes) < 2:
            return {"pass_E": False, "pass_F": False,
                    "max_gain": 0.0, "best_days_ago": -1}

        max_gain    = 0.0
        best_day    = -1
        check_range = min(lookback, len(closes) - 1)

        for i in range(check_range):
            prev = closes[i + 1]
            cur  = closes[i]
            if prev <= 0:
                continue
            gain = (cur - prev) / prev * 100
            if gain > max_gain:
                max_gain = gain
                best_day = i   # 0=오늘, 1=어제 ...

        return {
            "pass_E":        max_gain >= threshold_e,
            "pass_F":        max_gain >= threshold_f,
            "max_gain":      round(max_gain, 2),
            "best_days_ago": best_day,
        }

    # ──────────────────────────────────────────────────────────
    # 9. 조건 B — 30봉 내 거래대금 유효성
    # ──────────────────────────────────────────────────────────

    def check_cond_B(self, trading_values: list, lookback: int = 30) -> dict:
        """
        조건 B: 기간내 거래대금 [일]0봉전 30봉이내
        → 30봉 이내 유효 거래대금 존재 + 전일 거래대금 min_trading_value 이상
        """
        cfg         = self.scfg.get_scan()
        min_tv_cfg  = cfg.get("min_trading_value", 10_000_000_000)
        check_range = min(lookback, len(trading_values))
        valid_days  = sum(1 for tv in trading_values[:check_range] if tv > 0)
        prev_tv     = trading_values[1] if len(trading_values) > 1 else 0

        return {
            "pass":       valid_days > 0 and prev_tv >= min_tv_cfg,
            "valid_days": valid_days,
            "prev_tv":    prev_tv,
        }

    # ──────────────────────────────────────────────────────────
    # 10. 조건 G — 당일 거래대금 300억 이상 (가산점)
    # ──────────────────────────────────────────────────────────

    def check_cond_G(self, trading_values: list) -> dict:
        """
        조건 G: 당일 거래대금 300억 이상
        trading_values[0] = 오늘 현재까지 누적 거래대금 (원 단위)
        strategy_config의 cond_g_min_tv 로 기준 조정 가능 (기본 300억)
        """
        cfg      = self.scfg.get_scan()
        min_tv_g = cfg.get("cond_g_min_tv", 30_000_000_000)  # 기본 300억
        today_tv = trading_values[0] if trading_values else 0
        return {
            "pass":     today_tv >= min_tv_g,
            "today_tv": today_tv,
        }

    # ──────────────────────────────────────────────────────────
    # 11. 전체 조건검색식 실행 (A~G 통합)
    # ──────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────
    # 11. 핵심 전략 — 급등 후 눌림 포착
    # ──────────────────────────────────────────────────────────

    def check_recent_surge_strong(self, closes: list, trading_values: list) -> dict:
        """
        최근 N일 이내 강한 급등일 확인
        조건: 거래대금 surge_min_tv 이상 AND surge_min_pct% 이상 인 날이 존재
        v1.4.3: INFO 레벨 탈락 사유 출력 추가
        반환: {
          "pass": bool,
          "surge_pct": float,      # 급등일의 등락률
          "surge_days_ago": int,   # 며칠 전 (0=오늘)
          "surge_tv": float,       # 급등일 거래대금(억)
          "is_upper_limit": bool,  # 상한가 여부 (+29.5% 이상)
        }
        """
        cfg          = self.scfg.get_scan()
        lookback     = cfg.get("surge_lookback_days", 10)
        min_surge    = cfg.get("surge_min_pct", 15.0)
        min_tv_surge = cfg.get("surge_min_tv", 30_000_000_000)  # 300억

        check_range = min(lookback + 1, len(closes) - 1, len(trading_values))
        best = {"pass": False, "surge_pct": 0.0, "surge_days_ago": -1,
                "surge_tv": 0.0, "is_upper_limit": False}

        best_pct_found = 0.0   # 거래대금 미달이라도 최대 등락률 추적 (로그용)
        best_tv_found  = 0.0   # 등락률 미달이라도 최대 거래대금 추적 (로그용)

        for i in range(check_range):
            prev = closes[i + 1] if i + 1 < len(closes) else 0
            cur  = closes[i]
            tv   = trading_values[i] if i < len(trading_values) else 0
            if prev <= 0:
                continue
            pct = (cur - prev) / prev * 100
            best_pct_found = max(best_pct_found, pct)
            best_tv_found  = max(best_tv_found, tv)
            if pct >= min_surge and tv >= min_tv_surge:
                if pct > best["surge_pct"]:
                    best = {
                        "pass":           True,
                        "surge_pct":      round(pct, 2),
                        "surge_days_ago": i,
                        "surge_tv":       round(tv / 100_000_000, 1),
                        "is_upper_limit": pct >= 29.5,
                    }

        # v1.4.3: 탈락 시 INFO 로그로 원인 출력
        if not best["pass"]:
            if best_pct_found >= min_surge:
                # 등락률은 충족했으나 거래대금 미달
                logger.debug(
                    f"[Strategy] 급등조건 실패(거래대금미달) — "
                    f"최대등락:{best_pct_found:+.1f}% 최대TV:{best_tv_found/100_000_000:.0f}억 "
                    f"(기준: {min_tv_surge/100_000_000:.0f}억↑)"
                )
            elif best_tv_found >= min_tv_surge:
                # 거래대금은 충족했으나 등락률 미달
                logger.debug(
                    f"[Strategy] 급등조건 실패(등락률미달) — "
                    f"최대등락:{best_pct_found:+.1f}% (기준: {min_surge:.0f}%↑)"
                )
            else:
                logger.debug(
                    f"[Strategy] 급등조건 실패 — "
                    f"최근 {lookback}일 내 {min_surge:.0f}%+/{min_tv_surge/100_000_000:.0f}억+ 없음 "
                    f"(최대등락:{best_pct_found:+.1f}% 최대TV:{best_tv_found/100_000_000:.0f}억)"
                )
        return best

    def check_pullback_condition(self, closes: list, daily: dict) -> dict:
        """
        오늘 눌림/조정 조건 확인
        조건:
          1. 오늘 등락률 pullback_max_pct ~ 0% (조정 중, 상한가 아님)
          2. 현재가 > MA5 (5일선 이탈 없음)
          3. 현재가 > Envelope 상한선 (상한선 위에서 눌림)
        반환: {"pass": bool, "today_pct": float, "above_ma5": bool,
               "above_envelope": bool, "envelope_upper": float}
        """
        cfg          = self.scfg.get_scan()
        env_period   = cfg.get("envelope_period", 20)
        env_band_pct = cfg.get("envelope_band_pct", 20.0)
        pb_max       = cfg.get("pullback_max_pct_scan", 0.0)    # 오늘 최대 등락률 (0% = 상승 불가)
        pb_min       = cfg.get("pullback_min_pct_scan", -5.0)   # 오늘 최소 등락률 (-5% = 과도한 하락 제외)

        if len(closes) < 2:
            return {"pass": False}

        prev_close = closes[1]
        today_pct  = round((closes[0] - prev_close) / prev_close * 100, 2) \
                     if prev_close > 0 else 0.0

        # 조건 1: 오늘 조정 중 (상한가 아님, 너무 많이 빠지지도 않음)
        if not (pb_min <= today_pct <= pb_max):
            return {
                "pass": False,
                "today_pct": today_pct,
                "reason": f"오늘 등락률 {today_pct:+.1f}% — 눌림 범위({pb_min}~{pb_max}%) 벗어남"
            }

        ma5         = daily.get("ma5", 0)
        cur         = closes[0]
        above_ma5   = cur > ma5 if ma5 > 0 else False

        # 조건 2: MA5 이탈 없음
        if not above_ma5:
            return {
                "pass": False,
                "today_pct": today_pct,
                "reason": f"MA5({ma5:,.0f}) 이탈 — 현재가({cur:,.0f})"
            }

        # 조건 3: Envelope 상한선 위에서 눌림
        envelopes = self.calc_envelope(closes, env_period, env_band_pct)
        env_upper = envelopes[0]["upper"] if envelopes else 0
        above_env = cur >= env_upper if env_upper > 0 else False

        if not above_env:
            return {
                "pass": False,
                "today_pct": today_pct,
                "reason": f"Envelope 상한선({env_upper:,.0f}) 아래 — 현재가({cur:,.0f})"
            }

        return {
            "pass":           True,
            "today_pct":      today_pct,
            "above_ma5":      above_ma5,
            "above_envelope": above_env,
            "envelope_upper": round(env_upper, 2),
            "ma5":            round(ma5, 2),
            "pct_from_env":   round((cur - env_upper) / env_upper * 100, 2),
        }

    def apply_condition_search(self, code: str, basic_info: dict,
                               daily: Optional[dict] = None) -> Optional[dict]:
        """
        종가베팅 후보 선정 — v1.5.0 급등 후 눌림 포착 전략

        필수 조건 (모두 AND):
          1. 최근 N일 이내 거래대금 1000억+ AND (+20% 이상 or 상한가) 급등일 존재
          2. 오늘 등락률 -5% ~ 0% (눌림/조정 중, 상한가 아님)
          3. 현재가 > MA5 (5일선 이탈 없음)
          4. 현재가 > Envelope(20,20) 상한선 (강세 구간 유지)
          5. 거래대금 기준 (전일 or 당일 중 하나 이상)
        """
        cfg_scan  = self.scfg.get_scan()
        cfg_entry = self.scfg.get_entry()
        cur_price = basic_info.get("cur_price", 0)

        # 주가 범위 필터
        if cur_price > 0 and not (cfg_scan["min_price"] <= cur_price <= cfg_scan["max_price"]):
            return None

        daily = daily or self.get_daily_data(code)
        if not daily:
            return None

        closes         = daily.get("closes", [])
        trading_values = daily.get("trading_values", [])
        volumes        = daily.get("volumes", [])

        if len(closes) < 22:
            return None

        # ── 조건 1: 최근 N일 강한 급등일 확인 ───────────────
        surge = self.check_recent_surge_strong(closes, trading_values)
        if not surge["pass"]:
            logger.debug(
                f"[Strategy] {code} 급등조건 실패 — "
                f"최근 {cfg_scan.get('surge_lookback_days', 5)}일 내 "
                f"1000억+/{cfg_scan.get('surge_min_pct', 20.0)}%+ 급등일 없음"
            )
            return None

        # ── 조건 2~4: 오늘 눌림 + MA5 유지 + Envelope 위 ────
        pb = self.check_pullback_condition(closes, daily)
        if not pb["pass"]:
            reason = pb.get('reason', '')
            # v1.4.3: 전일종가 없음 → INFO 레벨로 기록 (주성엔지니어링 케이스 방어)
            if "전일 종가 없음" in reason or closes[1] <= 0 if len(closes) > 1 else True:
                logger.info(f"[Strategy] {code} 전일종가 없음 또는 눌림조건 실패 — {reason}")
            else:
                logger.debug(f"[Strategy] {code} 눌림조건 실패 — {reason}")
            return None

        # ── 조건 5: 거래대금 (전일 OR 당일) ─────────────────
        min_tv   = cfg_scan.get("min_trading_value", 10_000_000_000)
        today_tv = trading_values[0] if trading_values else 0
        prev_tv  = trading_values[1] if len(trading_values) > 1 else 0
        if prev_tv < min_tv and today_tv < min_tv:
            logger.debug(
                f"[Strategy] {code} 거래대금 실패 — "
                f"전일TV {prev_tv//100_000_000}억 / 당일TV {today_tv//100_000_000}억"
            )
            return None

        # ── 부가 정보 수집 ────────────────────────────────────
        rsi          = self.calculate_rsi(closes, cfg_entry["rsi_period"])
        flow         = self.get_institution_foreign_flow(code)
        today_volume = volumes[0] if volumes else 0
        prev_volume  = volumes[1] if len(volumes) > 1 else 1
        volume_ratio = round(today_volume / max(prev_volume, 1), 1)
        cond_G       = self.check_cond_G(trading_values)
        cond_A       = self.check_cond_A(
            closes,
            cfg_scan.get("envelope_lookback", 30),
            cfg_scan.get("envelope_period", 20),
            cfg_scan.get("envelope_band_pct", 20.0),
        )
        # scan_logger 호환용 cond_EF 구조
        cond_EF = {
            "pass_E":        True,
            "pass_F":        surge["is_upper_limit"],
            "max_gain":      surge["surge_pct"],
            "best_days_ago": surge["surge_days_ago"],
        }

        logger.info(
            f"[Strategy] ✅ {basic_info.get('name','')}({code}) "
            f"급등:{surge['surge_pct']:+.1f}%(D-{surge['surge_days_ago']}) "
            f"{'상한가' if surge['is_upper_limit'] else ''} "
            f"TV:{surge['surge_tv']}억 "
            f"오늘:{pb['today_pct']:+.1f}% "
            f"Env상한:{pb['envelope_upper']:,.0f} "
            f"MA5:{pb['ma5']:,.0f} RSI:{rsi}"
        )

        return {
            **basic_info,
            "code":            code,
            # 조건 결과
            "surge_info":      surge,
            "pullback_info":   pb,
            "cond_A":          cond_A,
            "cond_B":          {"pass": True, "prev_tv": prev_tv, "today_tv": today_tv},
            "cond_C":          {"pass": True, "upper": pb["envelope_upper"],
                                "pct_from_upper": pb["pct_from_env"]},
            "cond_D":          {"pass": True, "ma3": daily.get("ma3", 0)},
            "cond_EF":         cond_EF,
            "cond_G":          cond_G,
            # scan_logger 호환 필드
            "trading_value":   max(prev_tv, today_tv),
            "volume":          today_volume,
            "volume_ratio":    volume_ratio,
            "ma3":             daily.get("ma3", 0),
            "ma5":             daily.get("ma5", 0),
            "ma20":            daily.get("ma20", 0),
            "ma60":            daily.get("ma60", 0),
            "rsi":             rsi,
            "pct_from_high":   pb["pct_from_env"],
            "surge_max_gain":  surge["surge_pct"],
            "surge_days_ago":  surge["surge_days_ago"],
            "pullback_pct":    pb["today_pct"],
            "prev_close":      int(closes[1]) if len(closes) > 1 else 0,
            "institution_net": flow["institution_net"],
            "foreign_net":     flow["foreign_net"],
            "envelope_upper":            pb["envelope_upper"],
            "envelope_touch_days_ago":   cond_A["touch_days_ago"],
        }

    # ──────────────────────────────────────────────────────────
    # 12. 점수화 — 급등 후 눌림 전략 기준
    # ──────────────────────────────────────────────────────────

    def score_candidate(self, c: dict) -> float:
        """
        급등강도(40) + 거래대금(30) + 수급(20) + 가산점(10)
        급등강도: 상한가 > +30% > +20% 순
        거래대금: 급등일 거래대금 기준
        수급: 기관/외국인 순매수
        가산점: 눌림이 얕을수록 (Envelope 상한선 근처)
        """
        score      = 0.0
        surge      = c.get("surge_info", {})
        pb         = c.get("pullback_info", {})
        cond_G     = c.get("cond_G", {})

        # 급등 강도 (최대 40점)
        surge_pct = surge.get("surge_pct", 0)
        if surge.get("is_upper_limit"):    score += 40  # 상한가
        elif surge_pct >= 25:              score += 30
        elif surge_pct >= 20:              score += 20

        # 급등일 거래대금 (최대 30점) — 1000억=0점, 5000억+=30점
        surge_tv_억 = surge.get("surge_tv", 0)
        tv_score = min((surge_tv_억 - 1000) / 4000 * 30, 30)
        score += max(tv_score, 0)

        # 수급 (최대 20점)
        if c.get("institution_net", 0) > 0: score += 10
        if c.get("foreign_net",     0) > 0: score += 10

        # 눌림 얕을수록 가산점 (최대 10점) — 오늘 등락률이 0에 가까울수록
        today_pct = pb.get("today_pct", -5)
        pullback_score = max(0, (today_pct + 5) / 5 * 10)  # -5%=0점, 0%=10점
        score += pullback_score

        # 당일 거래대금 300억 이상 가산
        if cond_G.get("pass"):
            score += 5

        return round(score, 2)

    # ──────────────────────────────────────────────────────────
    # 13. 후보 풀 수집 소스
    # [v1.4.2] 소스 전면 개편 — 전체 종목 커버리지 확보
    #
    # 기존 문제: ka10023(거래량급증)/ka10024(거래량갱신)은
    #           오늘 거래량이 특별히 많은 종목만 수집 → 누락 다수
    # 해결:    등락률 상위(ka10004) + 거래대금 상위(ka10005) 조합으로
    #           키움 조건검색 대상(전체 종목)에 최대한 근접
    # ──────────────────────────────────────────────────────────

    def _source_price_rise(self) -> list[dict]:
        """
        소스1: ka10023 거래대금 기준 상위
        sort_tp=3 → 거래대금 많은 종목 (현대오토에버, 현대무벡스 등 포함)
        """
        try:
            data = self.broker._post(
                "ka10023", "/api/dostk/rkinfo",
                {
                    "mrkt_tp":     "000",
                    "sort_tp":     "3",    # 거래대금 기준
                    "tm_tp":       "2",
                    "trde_qty_tp": "0",    # 수량 무관
                    "tm":          "",
                    "stk_cnd":     "20",
                    "pric_tp":     "0",
                    "stex_tp":     "3",
                }
            )
            result = []
            for item in data.get("trde_qty_sdnin", []):
                code  = _clean_code(item.get("stk_cd", "").lstrip("A"))
                name  = item.get("stk_nm", "")
                price = abs(int(item.get("cur_prc", "0").lstrip("+-") or "0"))
                if code and price > 0:
                    result.append({"code": code, "name": name,
                                   "cur_price": price, "source": "TV_RANK"})
            logger.info(f"[Strategy] 소스1(거래대금순): {len(result)}개")
            return result
        except Exception as e:
            logger.warning(f"[Strategy] 소스1 실패: {e}")
            return []

    def _source_trading_value(self) -> list[dict]:
        """
        소스2: ka10023 등락률 기준 상위
        sort_tp=2 → 오늘 많이 오른 종목
        """
        try:
            data = self.broker._post(
                "ka10023", "/api/dostk/rkinfo",
                {
                    "mrkt_tp":     "000",
                    "sort_tp":     "2",    # 등락률 기준
                    "tm_tp":       "2",
                    "trde_qty_tp": "0",
                    "tm":          "",
                    "stk_cnd":     "20",
                    "pric_tp":     "0",
                    "stex_tp":     "3",
                }
            )
            result = []
            for item in data.get("trde_qty_sdnin", []):
                code  = _clean_code(item.get("stk_cd", "").lstrip("A"))
                name  = item.get("stk_nm", "")
                price = abs(int(item.get("cur_prc", "0").lstrip("+-") or "0"))
                if code and price > 0:
                    result.append({"code": code, "name": name,
                                   "cur_price": price, "source": "PRICE_RISE"})
            logger.info(f"[Strategy] 소스2(등락률순): {len(result)}개")
            return result
        except Exception as e:
            logger.warning(f"[Strategy] 소스2 실패: {e}")
            return []

    def _source_volume_surge(self) -> list[dict]:
        """
        소스3: ka10023 거래량 기준 상위
        sort_tp=1 → 거래량 많은 종목
        """
        try:
            data = self.broker._post(
                "ka10023", "/api/dostk/rkinfo",
                {
                    "mrkt_tp":     "000",
                    "sort_tp":     "1",    # 거래량 기준
                    "tm_tp":       "2",
                    "trde_qty_tp": "0",
                    "tm":          "",
                    "stk_cnd":     "20",
                    "pric_tp":     "0",
                    "stex_tp":     "3",
                }
            )
            result = []
            for item in data.get("trde_qty_sdnin", []):
                code  = _clean_code(item.get("stk_cd", "").lstrip("A"))
                name  = item.get("stk_nm", "")
                price = abs(int(item.get("cur_prc", "0").lstrip("+-") or "0"))
                if code and price > 0:
                    result.append({"code": code, "name": name,
                                   "cur_price": price, "source": "VOL_SURGE"})
            logger.info(f"[Strategy] 소스3(거래량순): {len(result)}개")
            return result
        except Exception as e:
            logger.warning(f"[Strategy] 소스3 실패: {e}")
            return []

    # ──────────────────────────────────────────────────────────
    # 14. 전체 스캔 실행 (메인)
    # ──────────────────────────────────────────────────────────

    def _get_daily_data_with_retry(self, code: str,
                                    delay: float = 0.5,
                                    max_retry: int = 3) -> dict:
        """
        get_daily_data() 래퍼 — 429 발생 시 지수 백오프 재시도
        delay: 기본 호출 간격 (초)
        max_retry: 429 시 최대 재시도 횟수
        """
        time.sleep(delay)   # 매 호출마다 기본 딜레이
        for attempt in range(max_retry):
            try:
                result = self.get_daily_data(code)
                return result
            except Exception as e:
                err_str = str(e)
                if "429" in err_str:
                    wait = 2.0 * (attempt + 1)   # 2초 → 4초 → 6초
                    logger.warning(
                        f"[Strategy] {code} 429 발생 "
                        f"({attempt+1}/{max_retry}) — {wait:.0f}초 대기"
                    )
                    time.sleep(wait)
                else:
                    raise
        logger.error(f"[Strategy] {code} 일봉 조회 {max_retry}회 실패 — 스킵")
        return {}

    def scan_candidates(self) -> list[dict]:
        """
        [v1.4.2] 키움 조건검색식 A~G 기반 종가베팅 후보 선정
        1. 후보 풀 수집 — 등락률상위 + 거래대금상위 + 거래량급증 3소스
        2. 조건 B → E → D → (C or A) 순서로 필터
        3. 통과 종목 점수화 후 전체 반환 (max_positions는 main.py에서 적용)

        [v1.4.2 소스 개편]
        기존 ka10023/ka10024(거래량 기준)는 오늘 거래량이 많은 종목만 포함
        → 키움 조건검색 전체 대상 종목을 커버하지 못하는 근본 문제
        해결: ka10004(등락률상위) + ka10005(거래대금상위) + ka10023(거래량급증) 조합
        """
        logger.info("[Strategy] 조건검색식 스캔 시작 (v1.4.2)...")
        cfg_scan  = self.scfg.get_scan()
        api_delay = cfg_scan.get("api_delay_sec", 0.5)

        # ── 후보 풀 수집 (3소스 중복 제거) ──────────────────
        pool: dict[str, dict] = {}
        for item in (
            self._source_price_rise()    +   # 소스1: 등락률 상위 (핵심)
            self._source_trading_value() +   # 소스2: 거래대금 상위
            self._source_volume_surge()      # 소스3: 거래량급증 (보조)
        ):
            code = item["code"]
            if code and code not in pool:
                pool[code] = item

        logger.info(f"[Strategy] 후보 풀: {len(pool)}개 / 딜레이: {api_delay}초")

        # ── 조건검색식 A~G 적용 ──────────────────────────────
        passed: dict[str, dict] = {}
        for i, (code, item) in enumerate(list(pool.items())[:100]):

            # 매 호출마다 딜레이 + 429 재시도
            daily  = self._get_daily_data_with_retry(code, delay=api_delay)
            if not daily:
                continue

            result = self.apply_condition_search(code, item, daily)
            if result:
                result["score"] = self.score_candidate(result)
                passed[code]    = result
                logger.info(
                    f"[Strategy] ✅ [{i+1}/{len(pool)}] "
                    f"{result['name']}({code}) 점수:{result['score']}"
                )
            else:
                logger.debug(f"[Strategy] ❌ [{i+1}/{len(pool)}] {code} 조건 미통과")

        # ── 점수 정렬 후 전체 반환 ───────────────────────────
        sorted_list = sorted(passed.values(), key=lambda x: x["score"], reverse=True)
        logger.info(
            f"[Strategy] 최종 후보: {len(sorted_list)}개 "
            f"(풀 {len(pool)}개 → 조건통과 {len(passed)}개)"
        )
        return sorted_list

    # ──────────────────────────────────────────────────────────
    # 15. 진입 신호 확인 (15:10~15:20 눌림 확인)
    # ──────────────────────────────────────────────────────────

    def check_entry_signal(self, code: str, cur_price: int,
                           prev_close: int) -> dict:
        cfg = self.scfg.get_entry()
        now = datetime.now().strftime("%H:%M")

        if not (cfg["entry_start_time"] <= now <= cfg["entry_end_time"]):
            return {"signal": False,
                    "reason": f"진입 시각 아님 ({now})", "pullback_pct": 0}
        if prev_close <= 0:
            return {"signal": False, "reason": "전일 종가 없음", "pullback_pct": 0}

        pullback_pct = (cur_price - prev_close) / prev_close * 100

        if pullback_pct < cfg["pullback_min_pct"]:
            return {"signal": False,
                    "reason": f"눌림 과다 ({pullback_pct:.1f}%)",
                    "pullback_pct": round(pullback_pct, 2)}
        if pullback_pct > cfg["pullback_max_pct"]:
            return {"signal": False,
                    "reason": f"눌림 부족/상승 중 ({pullback_pct:.1f}%)",
                    "pullback_pct": round(pullback_pct, 2)}
        return {"signal": True,
                "reason": f"눌림 정상 ({pullback_pct:.1f}%)",
                "pullback_pct": round(pullback_pct, 2)}

    # ──────────────────────────────────────────────────────────
    # 16. 매도 신호 확인 (D+1)
    # ──────────────────────────────────────────────────────────

    def check_exit_signal(self, code: str, cur_price: int,
                          buy_price: int, held_qty: int,
                          day_high: int = 0) -> dict:
        cfg_risk = self.scfg.get_risk()
        cfg_sell = self.scfg.get_sell()
        now      = datetime.now().strftime("%H:%M")

        if buy_price <= 0 or held_qty <= 0:
            return {"signal": "HOLD", "reason": "포지션 없음", "qty": 0}

        profit_pct = (cur_price - buy_price) / buy_price * 100

        if profit_pct <= cfg_risk["stop_loss_pct"]:
            return {"signal": "FULL",
                    "reason": f"손절 ({profit_pct:.1f}%)", "qty": held_qty}

        if (cfg_sell["use_nxt_premarket"] and "08:00" <= now <= "08:50"
                and profit_pct >= cfg_sell["nxt_gap_target_pct"]):
            return {"signal": "FULL",
                    "reason": f"NXT 갭 익절 (+{profit_pct:.1f}%)", "qty": held_qty}

        if (now <= cfg_sell["morning_sell_end"]
                and profit_pct >= cfg_sell["morning_target_pct"]):
            qty = max(int(held_qty * cfg_risk["partial_sell_pct"] / 100), 1)
            return {"signal": "PARTIAL",
                    "reason": f"오전 1차 익절 (+{profit_pct:.1f}%)", "qty": qty}

        if profit_pct >= cfg_risk["take_profit_pct"]:
            return {"signal": "FULL",
                    "reason": f"목표 익절 (+{profit_pct:.1f}%)", "qty": held_qty}

        if cfg_risk["trailing_stop"] and day_high > 0:
            trail = (cur_price - day_high) / day_high * 100
            if trail <= -cfg_risk["trailing_gap_pct"]:
                return {"signal": "FULL",
                        "reason": f"트레일링 스탑 ({trail:.1f}%)", "qty": held_qty}

        if now >= cfg_sell["afternoon_cut_time"] and profit_pct < 0:
            qty = max(int(held_qty * cfg_sell["afternoon_cut_ratio"] / 100), 1)
            if qty < held_qty:
                return {"signal": "PARTIAL",
                        "reason": f"오후 손실 축소 ({profit_pct:.1f}%)", "qty": qty}

        if cfg_sell["eod_force_sell"] and now >= cfg_risk["force_sell_time"]:
            return {"signal": "FULL",
                    "reason": f"강제 청산 {now}", "qty": held_qty}

        return {"signal": "HOLD",
                "reason": f"보유 유지 ({profit_pct:.1f}%)", "qty": 0}

    # ──────────────────────────────────────────────────────────
    # 17. 매수 수량 계산
    # ──────────────────────────────────────────────────────────

    def calculate_buy_qty(self, code: str, cur_price: int,
                          available_cash: int) -> int:
        if cur_price <= 0:
            return 0
        cfg           = self.scfg.get_entry()
        target_amount = int(available_cash * cfg["position_size_pct"] / 100)
        qty           = target_amount // cur_price
        logger.info(f"[Strategy] {code} 매수: {target_amount:,}원 / @{cur_price:,}원 = {qty}주")
        return qty

    # ──────────────────────────────────────────────────────────
    # 18. 하위 호환 메서드
    # ──────────────────────────────────────────────────────────

    def is_ma_aligned(self, code: str, ma_data: Optional[dict] = None) -> bool:
        if ma_data is None:
            ma_data = self.get_daily_data(code)
        if not ma_data:
            return False
        return ma_data["ma5"] > ma_data["ma20"]

    def is_near_high(self, code: str, ma_data: Optional[dict] = None) -> dict:
        if ma_data is None:
            ma_data = self.get_daily_data(code)
        if not ma_data:
            return {"is_high": False, "high_20d": 0, "pct_from_high": 0}
        cfg       = self.scfg.get_scan()
        n         = cfg.get("use_recent_high_days", 20)
        closes    = ma_data.get("closes", [])
        if len(closes) < n:
            return {"is_high": False, "high_20d": 0, "pct_from_high": 0}
        high_nd   = max(closes[:n])
        cur       = closes[0]
        pct       = (cur - high_nd) / high_nd * 100 if high_nd > 0 else 0
        threshold = cfg.get("near_high_threshold_pct", -20.0)
        return {
            "is_high":       pct >= threshold,
            "high_20d":      high_nd,
            "pct_from_high": round(pct, 2),
        }

    def check_recent_surge(self, daily: dict) -> dict:
        """하위 호환용 — check_cond_EF로 대체됨"""
        closes = daily.get("closes", [])
        result = self.check_cond_EF(closes)
        return {
            "has_surge":      result["pass_E"],
            "max_gain":       result["max_gain"],
            "surge_days_ago": result["best_days_ago"],
        }
