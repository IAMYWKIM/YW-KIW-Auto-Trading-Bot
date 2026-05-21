"""
scanner.py — 키움 국내주식 단타 자동매매 종목 스캐너
v2.0: VWAP 완전 제거 + 급등 신호 기반 순수 모멘텀 스캐너

[v1.x → v2.0 전면 개편]
  VWAP 제거 이유:
    VWAP은 장중 누적 평균이라 고점 이후 항상 현재가 < VWAP
    → 눌림 매매 자체가 구조적으로 불가능 (코스모로보틱스·주성엔지니어링 미포착 원인)

  새 점수 체계 (최대 100점):
    ① 거래대금 폭발  30점: 오늘 거래대금이 기준 이상
    ② 거래량 폭발    25점: 전일 대비 거래량 배수
    ③ 가격 모멘텀    20점: 전일종가 대비 상승률 구간
    ④ 고점 눌림 Fib  15점: 당일 고가 대비 눌림 구간 (Fib 0.236~0.382)
    ⑤ 시간대         10점: 09:00~09:30 장초반 가중

  환기종목 제외:
    - 거래정지 예고 / 관리종목 / 투자주의 환기종목
    - ka10016 조회 시 관련 플래그 체크
    - 주가 이상: 상한가(+29.9%)·하한가(-29.9%) 근접 제외

[API 호출 횟수]
  소스4회 + 상세30회 = 34회 (VWAP 계산 완전 제거로 절감)
  소요시간: 약 10~12초
"""

import logging
import time
from datetime import datetime, date as _date
from typing import Optional

import pytz

from broker import KiwoomBroker
from scalp_config import ScalpConfig

logger = logging.getLogger(__name__)
KST    = pytz.timezone("Asia/Seoul")


def _clean_code(code: str) -> str:
    return code.split("_")[0] if code else code


def _safe_int(s, default: int = 0) -> int:
    try:
        return abs(int(str(s or "0").strip().lstrip("+-0") or "0"))
    except (ValueError, TypeError):
        return default


class DayTradingScanner:
    """
    단타 종목 스캐너 v2.0 — 순수 모멘텀 기반, VWAP 없음

    [권장 사용법]
      scanner = DayTradingScanner(broker, scalp_cfg)
      scanner.init_daily()          # 08:50 장전 — 전일 거래량 캐시
      candidates = scanner.scan()   # 30초 주기 호출
    """

    def __init__(self, broker: KiwoomBroker, scalp_cfg: ScalpConfig):
        self.broker            = broker
        self.cfg               = scalp_cfg
        self._prev_vol_cache:  dict[str, int] = {}   # code → 전일 거래량
        self._listing_date:    dict[str, str] = {}   # code → 상장일 YYYYMMDD
        self._caution_cache:   set[str]       = set()  # 환기종목 캐시
        self.last_scan_result: list[dict]     = []

    # ──────────────────────────────────────────────────────────
    # 1. 장전 초기화
    # ──────────────────────────────────────────────────────────

    def init_daily(self) -> None:
        """
        08:50 장전 — 전일 거래량 캐시 구축 + 환기종목 캐시 갱신
        """
        logger.info("[Scanner] 장전 초기화 시작")
        self._prev_vol_cache.clear()
        self._caution_cache.clear()

        try:
            cfg        = self.cfg.get_scan()
            candidates = self._source_rise()[:50]

            for i, item in enumerate(candidates):
                if i % 10 == 0:
                    logger.info(f"[Scanner] 초기화 {i+1}/{len(candidates)}")
                time.sleep(cfg.get("api_delay_sec", 0.3))
                prev = self._get_prev_day_volumes(item["code"])
                if prev and prev["prev_volume"] > 0:
                    self._prev_vol_cache[item["code"]] = prev["prev_volume"]

            # 환기종목 캐시 (ka10016 기반 — 관리종목 플래그)
            self._refresh_caution_cache()

            logger.info(
                f"[Scanner] init_daily 완료 — "
                f"거래량 캐시 {len(self._prev_vol_cache)}개 / "
                f"환기종목 {len(self._caution_cache)}개"
            )
        except Exception as e:
            logger.error(f"[Scanner] init_daily 실패: {e}")

    def _refresh_caution_cache(self) -> None:
        """환기종목(관리종목·투자주의) 캐시 갱신"""
        try:
            data = self.broker._post(
                "ka10016", "/api/dostk/stkinfo",
                {
                    "mrkt_tp": "000",
                    "sort_tp": "1",
                    "ntl_tp":  "1",   # 필수 파라미터
                    "stex_tp": "3",
                }
            )
            for item in data.get("stk_new_high_qry", []):
                if (item.get("adm_yn",   "N") == "Y" or
                    item.get("atten_yn", "N") == "Y" or
                    item.get("halt_yn",  "N") == "Y"):
                    code = _clean_code(item.get("stk_cd", "").lstrip("A"))
                    if code:
                        self._caution_cache.add(code)
        except Exception as e:
            logger.debug(f"[Scanner] 환기종목 캐시 갱신 실패 (무시): {e}")

    # ──────────────────────────────────────────────────────────
    # 2. 메인 스캔
    # ──────────────────────────────────────────────────────────

    def scan(
        self,
        held_codes: Optional[list[str]] = None,
        blacklist:  Optional[set[str]]  = None,
        force_time: bool                = False,
    ) -> list[dict]:
        """
        장중 급등 종목 실시간 스캔 v2.0

        변경: VWAP 제거, 환기종목 제외, 순수 모멘텀 점수
        소요시간: 약 10~12초
        """
        cfg     = self.cfg.get_scan()
        now_str = datetime.now(KST).strftime("%H:%M")

        if not force_time and now_str >= cfg["entry_end_time"]:
            logger.info(f"[Scanner] 진입마감 ({now_str}) 스킵")
            return []

        # 진입 시작 시각 체크 (장초반 변동성 구간 제외)
        entry_start = cfg.get("entry_start_time", "09:00")
        if not force_time and now_str < entry_start:
            logger.info(f"[Scanner] 진입시작 전 ({now_str} < {entry_start}) 스킵")
            return []

        held_codes = set(held_codes or [])
        blacklist  = blacklist or set()

        # ── Step 1: 후보 풀 수집 ────────────────────────────
        src1 = self._source_rise()
        time.sleep(1.0)
        src2 = self._source_trading_val()
        time.sleep(1.0)
        src3 = self._source_volume()
        time.sleep(1.0)
        src4 = self._source_new_listing()

        pool: dict[str, dict] = {}
        for item in src1 + src2 + src3 + src4:
            code = item["code"]
            if code and code not in pool:
                pool[code] = item

        logger.info(
            f"[Scanner] 풀 {len(pool)}개 "
            f"(등락률:{len(src1)} 거래대금:{len(src2)} "
            f"거래량:{len(src3)} 신규상장:{len(src4)})"
        )

        # ── Step 2: 1차 빠른 필터 ────────────────────────────
        pre: list[dict] = []
        for code, item in pool.items():
            if code in held_codes or code in blacklist:
                continue
            # 환기종목 제외 (v2.0)
            if code in self._caution_cache:
                logger.debug(f"[Scanner] {code} 환기/관리종목 제외")
                continue
            price = item.get("cur_price", 0)
            if not (cfg["min_price"] <= price <= cfg["max_price"]):
                continue
            pre.append(item)

        pre = pre[:cfg.get("max_candidates", 40)]
        logger.info(f"[Scanner] 1차 필터: {len(pre)}개")

        # ── Step 3: 상세 필터 ────────────────────────────────
        api_delay = cfg.get("api_delay_sec", 0.3)
        passed: list[dict] = []

        for item in pre:
            code = item["code"]
            time.sleep(api_delay)

            detail = self._get_stock_detail_with_retry(code)
            if not detail:
                continue

            cur_price  = detail["cur_price"]
            prev_close = detail["prev_close"]
            volume     = detail["volume"]
            today_tv   = detail["trading_value"]
            day_high   = detail.get("day_high", cur_price)
            day_low    = detail.get("day_low",  cur_price)

            if prev_close <= 0 or cur_price <= 0:
                continue

            rise_pct = (cur_price - prev_close) / prev_close * 100

            # ── 상승률 필터 ────────────────────────────────────
            if not (cfg["min_rise_pct"] <= rise_pct <= cfg["max_rise_pct"]):
                logger.debug(f"[Scanner] {code} 상승률 {rise_pct:.1f}% 제외")
                continue

            # ── 상한가 근접 제외 ───────────────────────────────
            if cfg.get("exclude_upper_limit", True) and rise_pct >= 29.0:
                logger.debug(f"[Scanner] {code} 상한가 근접 {rise_pct:.1f}% 제외")
                continue

            # ── 거래대금 필터 ──────────────────────────────────
            if today_tv < cfg["min_trading_value"]:
                logger.debug(
                    f"[Scanner] {code} 거래대금 "
                    f"{today_tv//100_000_000}억 미달"
                )
                continue

            # ── 거래량 비율 ────────────────────────────────────
            prev_vol     = self._prev_vol_cache.get(code, 0)
            volume_ratio = round(volume / prev_vol, 1) if prev_vol > 0 else 0.0
            if prev_vol > 0 and volume_ratio < cfg["volume_ratio_min"]:
                logger.debug(f"[Scanner] {code} 거래량 {volume_ratio}배 미달")
                continue

            # ── 환기종목 실시간 2차 체크 (detail에 플래그 있을 경우) ──
            if detail.get("adm_yn") == "Y" or detail.get("atten_yn") == "Y":
                logger.debug(f"[Scanner] {code} 환기종목 실시간 제외")
                self._caution_cache.add(code)
                continue

            passed.append({
                "code":           code,
                "name":           detail["name"],
                "cur_price":      cur_price,
                "prev_close":     prev_close,
                "rise_pct":       round(rise_pct, 2),
                "volume":         volume,
                "volume_ratio":   volume_ratio,
                "trading_value":  today_tv,
                "day_high":       day_high,
                "day_low":        day_low,
                "source":         item.get("source", ""),
                "scan_time":      now_str,
                "is_new_listing": item.get("is_new_listing", False),
                "vwap":           0.0,   # 하위 호환 (사용 안 함)
            })

        # ── Step 4: 점수화 및 정렬 ──────────────────────────
        for c in passed:
            c["score"] = self._score(c, now_str)

        passed.sort(key=lambda x: x["score"], reverse=True)

        logger.info(
            f"[Scanner] 최종 후보: {len(passed)}개 "
            f"(최고점: {passed[0]['score']}점/{passed[0]['name']} "
            f"if passed else '')"
            if passed else
            f"[Scanner] 최종 후보: 0개"
        )
        self.last_scan_result = passed
        return passed

    # ──────────────────────────────────────────────────────────
    # 3. 소스 수집
    # ──────────────────────────────────────────────────────────

    def _fetch_rank_list(self, sort_tp: str) -> list[dict]:
        """ka10023 랭킹 API 공통 호출"""
        try:
            data = self.broker._post(
                "ka10023", "/api/dostk/rkinfo",
                {
                    "mrkt_tp":     "000",
                    "sort_tp":     sort_tp,
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
                price = _safe_int(item.get("cur_prc", "0"))
                if code and price > 0:
                    result.append({"code": code, "name": name, "cur_price": price})
            return result
        except Exception as e:
            logger.warning(f"[Scanner] 랭킹 조회 실패 sort_tp={sort_tp}: {e}")
            return []

    def _source_rise(self) -> list[dict]:
        result = self._fetch_rank_list(sort_tp="2")
        for item in result: item["source"] = "RISE_RANK"
        logger.info(f"[Scanner] 소스1(등락률): {len(result)}개")
        return result

    def _source_trading_val(self) -> list[dict]:
        result = self._fetch_rank_list(sort_tp="3")
        for item in result: item["source"] = "TV_RANK"
        logger.info(f"[Scanner] 소스2(거래대금): {len(result)}개")
        return result

    def _source_volume(self) -> list[dict]:
        result = self._fetch_rank_list(sort_tp="1")
        for item in result: item["source"] = "VOL_RANK"
        logger.info(f"[Scanner] 소스3(거래량): {len(result)}개")
        return result

    def _source_new_listing(self) -> list[dict]:
        """신규상장 종목 소스 (상장 N일 이내)"""
        cfg            = self.cfg.get_scan()
        new_days_limit = cfg.get("new_listing_days", 60)
        result         = []

        try:
            data  = self.broker._post(
                "ka10016", "/api/dostk/stkinfo",
                {
                    "mrkt_tp": "000",
                    "sort_tp": "1",
                    "new_tp":  "1",
                    "ntl_tp":  "1",   # 필수 파라미터 추가
                    "stex_tp": "3",
                }
            )
            items = data.get("stk_new_high_qry", [])
            for item in items[:30]:
                code    = _clean_code(item.get("stk_cd", "").lstrip("A"))
                name    = item.get("stk_nm", "")
                price   = _safe_int(item.get("cur_prc", "0"))
                list_dt = item.get("list_dt", "")

                if not code or price <= 0:
                    continue
                if list_dt:
                    self._listing_date[code] = list_dt

                if list_dt and len(list_dt) == 8:
                    try:
                        listed       = _date(int(list_dt[:4]),
                                             int(list_dt[4:6]),
                                             int(list_dt[6:8]))
                        elapsed_days = (_date.today() - listed).days
                        if elapsed_days > new_days_limit:
                            continue
                    except Exception:
                        pass

                result.append({
                    "code":            code,
                    "name":            name,
                    "cur_price":       price,
                    "source":          "NEW_LISTING",
                    "is_new_listing":  True,
                })
        except Exception as e:
            logger.warning(f"[Scanner] 신규상장 소스 실패 (무시하고 계속): {e}")

        logger.info(f"[Scanner] 소스4(신규상장): {len(result)}개")
        return result

    # ──────────────────────────────────────────────────────────
    # 4. 상세 조회 (429 재시도)
    # ──────────────────────────────────────────────────────────

    def _get_stock_detail_with_retry(self, code: str,
                                      max_retry: int = 3) -> Optional[dict]:
        for attempt in range(max_retry):
            try:
                info = self.broker.get_stock_info(code)
                if not info or info.get("cur_price", 0) <= 0:
                    return None
                return info
            except Exception as e:
                err_str = str(e)
                if "429" in err_str and attempt < max_retry - 1:
                    wait = 2.0 * (attempt + 1)
                    logger.warning(
                        f"[Scanner] {code} 429 {attempt+1}/{max_retry}회 "
                        f"({wait:.0f}초 대기)"
                    )
                    time.sleep(wait)
                else:
                    logger.debug(f"[Scanner] {code} 조회 실패: {e}")
                    return None
        return None

    def _get_prev_day_volumes(self, code: str) -> Optional[dict]:
        """ka10081 일봉 — 전일 거래량 조회 (init_daily 전용)"""
        try:
            data = self.broker._post(
                "ka10081", "/api/dostk/chart",
                {
                    "stk_cd":       code,
                    "base_dt":      datetime.now(KST).strftime("%Y%m%d"),
                    "upd_stkpc_tp": "1",
                }
            )
            candles = data.get("stk_dt_pole_chart_qry", [])
            if len(candles) < 2:
                return None
            prev     = candles[1]
            prev_vol = _safe_int(prev.get("trde_qty",   "0"))
            prev_tv  = _safe_int(prev.get("trde_prica", "0")) * 1_000_000
            return {"prev_volume": prev_vol, "prev_tv": prev_tv}
        except Exception as e:
            logger.debug(f"[Scanner] {code} 전일 거래량 조회 실패: {e}")
            return None

    # ──────────────────────────────────────────────────────────
    # 5. 점수화 v2.0 — VWAP 제거, 순수 모멘텀 5신호
    # ──────────────────────────────────────────────────────────

    def _score(self, item: dict, now_str: str) -> int:
        """
        단타 후보 순수 모멘텀 점수 (최대 100점)

        ① 거래대금 폭발 (30점): 오늘 얼마나 거래됐나
           200억+ =30 / 100억+ =25 / 50억+ =18 / 10억+ =10 / 그 이하 =3

        ② 거래량 폭발 (25점): 전일 대비 몇 배나 터졌나
           10배+ =25 / 5배+ =20 / 3배+ =13 / 2배+ =8 / 캐시없음 =12

        ③ 가격 모멘텀 (20점): 전일 대비 상승률 구간
           10~15% =20 / 7~10% =16 / 5~7% =12 / 15~20% =10 / 20~29% =8 / 3~5% =6

        ④ 당일 고가 눌림 Fib (15점): 고점 이후 눌림 깊이
           Fib 0.236~0.382 황금구간 =15 / 0.382~0.618 =10 / 0~0.236 =5
           (고점 = 현재가: 눌림 없음 = 0점 / 당일 움직임 3% 미만 = 0점)

        ⑤ 시간대 (10점): 장 초반일수록 모멘텀 강함
           09:00~09:30 =10 / 09:30~10:00 =8 / 10:00~11:30 =5 / 그 이후 =2

        보너스:
           신규상장 60일 이내 +5점
        """
        score = 0

        # ① 거래대금
        tv = item.get("trading_value", 0)
        if   tv >= 20_000_000_000: score += 30
        elif tv >= 10_000_000_000: score += 25
        elif tv >=  5_000_000_000: score += 18
        elif tv >=  1_000_000_000: score += 10
        else:                      score += 3

        # ② 거래량 배수
        vr = item.get("volume_ratio", 0)
        if   vr >= 10: score += 25
        elif vr >= 5:  score += 20
        elif vr >= 3:  score += 13
        elif vr >= 2:  score += 8
        elif vr == 0:  score += 12   # 캐시 없음 → 중간값 (패널티 없음)

        # ③ 가격 모멘텀
        rise = item.get("rise_pct", 0)
        if   10.0 <= rise < 15.0: score += 20
        elif  7.0 <= rise < 10.0: score += 16
        elif  5.0 <= rise <  7.0: score += 12
        elif 15.0 <= rise < 20.0: score += 10
        elif 20.0 <= rise < 29.0: score += 8
        elif  3.0 <= rise <  5.0: score += 6

        # ④ 당일 고가 대비 눌림 — Fib 구간 점수
        score += self._fib_pullback_score(item)

        # ⑤ 시간대
        # 09:30 이전은 entry_start_time으로 스킵되므로 09:30부터 점수 계산
        if   "09:30" <= now_str < "10:00": score += 10   # 장초반 모멘텀 (변동성 안정 후)
        elif "10:00" <= now_str < "11:30": score += 7    # 오전 주 시간대
        elif "11:30" <= now_str < "13:00": score += 4    # 점심 전후
        else:                              score += 2    # 오후

        # 신규상장 보너스
        if item.get("is_new_listing", False):
            score += 5

        return score

    @staticmethod
    def _fib_pullback_score(item: dict) -> int:
        """
        당일 고가 대비 현재가 위치로 Fib 눌림 점수 계산

        핵심 아이디어:
          코스모로보틱스처럼 11:08 고점 후 Fib 0.382 눌림 → 재상승 패턴
          VWAP 없이 순수 고가/저가 기반으로 눌림 구간을 감지

        Returns: 0~15점
        """
        high = item.get("day_high", 0)
        low  = item.get("day_low",  0)
        cur  = item.get("cur_price", 0)

        if high <= 0 or low <= 0 or cur <= 0:
            return 0

        # 당일 움직임이 3% 미만이면 Fib 의미 없음
        move_pct = (high - low) / low * 100 if low > 0 else 0
        if move_pct < 3.0:
            return 0

        # 현재가가 고점 = 눌림 없음 (상승 중) → 점수 없음
        if cur >= high * 0.995:
            return 0

        fib_range = high - low
        fib236    = high - fib_range * 0.236
        fib382    = high - fib_range * 0.382
        fib618    = high - fib_range * 0.618

        if fib236 <= cur < high:   return 5    # 얕은 눌림 (고점 근처)
        if fib382 <= cur < fib236: return 15   # 황금구간 Fib 0.236~0.382
        if fib618 <= cur < fib382: return 10   # 깊은 눌림 Fib 0.382~0.618
        return 0

    # ──────────────────────────────────────────────────────────
    # 6. 텔레그램 메시지 포맷
    # ──────────────────────────────────────────────────────────

    def format_scan_message(self, candidates: list[dict]) -> str:
        now_str = datetime.now(KST).strftime("%H:%M:%S")
        if not candidates:
            return (
                f"📡 <b>[ 단타 스캔 결과 ]</b> {now_str}\n"
                f"조건 충족 종목 없음"
            )
        lines = [f"📡 <b>[ 단타 스캔 결과 ]</b> {now_str}\n"]
        for i, c in enumerate(candidates[:10], 1):
            tv_str    = f"{c['trading_value']//100_000_000}억"
            vr_str    = f"{c['volume_ratio']:.1f}배" if c["volume_ratio"] > 0 else "N/A"
            new_str   = " 🆕" if c.get("is_new_listing") else ""
            high_str  = f" 고:{c['day_high']:,}" if c.get("day_high") else ""
            lines.append(
                f"<b>{i}. {c['name']}({c['code']})</b>{new_str}\n"
                f"   현재가: <b>{c['cur_price']:,}원</b> "
                f"(<b>{c['rise_pct']:+.1f}%</b>){high_str}\n"
                f"   TV:{tv_str} | 거래량:{vr_str}\n"
                f"   점수:{c['score']}점 | {c['source']}"
            )
        return "\n".join(lines)
