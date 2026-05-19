"""
scanner.py — 키움 국내주식 단타 자동매매 종목 스캐너
v1.3: 신규상장 종목 감지 + 스캔 풀 확대 + VWAP 완화

[v1.2 → v1.3 핵심 변경]
  문제1: 신규상장 종목(상장 60일 미만)이 스캔 풀에 안 잡히는 문제
         → _source_new_listing() 소스 추가 (ka10016 신고가 기반)
  문제2: 상위 5개 VWAP 계산 제한 → 탐색 기회 부족
         → scalp_config scan.vwap_top_n 설정 추가 (기본 10개)
  문제3: 신규상장 종목 VWAP 하회로 무조건 거부
         → 상장 N일 미만 종목은 VWAP 필터 완화 (tolerance 적용)
  문제4: 상승률 15~20% 구간 점수 낮음 (8점)
         → 신규상장 종목 보너스 점수 +10점 추가

[v1.1 → v1.2 핵심 변경]
  문제: 캐시 미스 시 종목마다 ka10081(일봉) + ka10080(VWAP) 개별 호출
        → 30종목 × 2 API = 60회 호출, 사실상 무한 대기
  해결:
    ① _get_prev_day_volumes() — scan() 루프 내 개별 호출 완전 제거
       캐시 없으면 volume_ratio=0 처리 (중간 점수, 필터 통과)
       init_daily() 호출 시에만 캐시 구축
    ② VWAP — 필터 통과한 최종 후보 상위 N개만 계산
    ③ 소스간 딜레이 1초 유지 (429 방지)
    ④ 상세 조회 429 재시도 유지 (2초→4초→6초)

[API 호출 횟수 비교]
  v1.2: 소스3회 + 상세30회 + VWAP5회              = 38회
  v1.3: 소스4회 + 상세30회 + VWAP10회 + 신규5회   = 49회 (신규상장 포함)
"""

import logging
import time
from datetime import datetime
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
    단타 종목 스캐너

    [권장 사용법]
        scanner = DayTradingScanner(broker, scalp_cfg)

        # 장 시작 전 1회만 (전일 거래량 캐시 구축)
        scanner.init_daily()

        # 30초 주기로 반복 호출
        candidates = scanner.scan(held_codes=[], blacklist=set())

        # 장마감 후 테스트 시
        candidates = scanner.scan(force_time=True)

    [init_daily() 미호출 시]
        volume_ratio=0 으로 처리 → 거래량 비율 필터 통과, 점수는 중간값
        실 거래에서는 반드시 init_daily() 먼저 호출 권장
    """

    def __init__(self, broker: KiwoomBroker, scalp_cfg: ScalpConfig):
        self.broker             = broker
        self.cfg                = scalp_cfg
        self._prev_vol_cache: dict[str, int]  = {}   # init_daily() 로 채워짐
        self._listing_date:   dict[str, str]  = {}   # 신규상장 종목 상장일 캐시
        self.last_scan_result:  list[dict]    = []

    # ──────────────────────────────────────────────────────────
    # 1. 장전 초기화 (08:50 1회 호출 — 선택적)
    # ──────────────────────────────────────────────────────────

    def init_daily(self):
        """
        전일 거래량 캐시 구축
        → 거래량 비율(오늘 거래량 / 전일 거래량) 계산에 사용
        → 미호출 시 volume_ratio=0 처리 (필터 통과, 중간 점수)
        """
        logger.info("[Scanner] init_daily 시작 — 전일 거래량 캐시 구축")
        self._prev_vol_cache.clear()
        cfg = self.cfg.get_scan()

        try:
            candidates = self._fetch_rank_list(sort_tp="1", count=100)
            for i, item in enumerate(candidates):
                if i % 10 == 0:
                    logger.info(f"[Scanner] 초기화 {i+1}/{len(candidates)}")
                time.sleep(cfg.get("api_delay_sec", 0.3))
                prev = self._get_prev_day_volumes(item["code"])
                if prev and prev["prev_volume"] > 0:
                    self._prev_vol_cache[item["code"]] = prev["prev_volume"]

            logger.info(
                f"[Scanner] init_daily 완료 — "
                f"캐시 {len(self._prev_vol_cache)}개 종목"
            )
        except Exception as e:
            logger.error(f"[Scanner] init_daily 실패: {e}")

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
        장중 급등 종목 실시간 스캔 (v1.2 — 빠른 버전)

        소요시간: 약 12~15초 (API 호출 38회)

        Args:
            held_codes : 보유 중인 종목 코드 (제외)
            blacklist  : 쿨다운 종목 집합 (제외)
            force_time : True 시 entry_end_time 시간 제한 무시
                         (장마감 후 테스트, 텔레그램 수동 실행)
        """
        cfg     = self.cfg.get_scan()
        now_str = datetime.now(KST).strftime("%H:%M")

        # 진입 마감 체크
        if not force_time and now_str >= cfg["entry_end_time"]:
            logger.info(
                f"[Scanner] 진입마감 ({now_str} >= {cfg['entry_end_time']}) "
                f"스킵. 테스트는 force_time=True"
            )
            return []

        held_codes = set(held_codes or [])
        blacklist  = blacklist or set()

        # ── Step 1: 후보 풀 수집 (소스간 1초 딜레이) ─────────
        src1 = self._source_rise()
        time.sleep(1.0)
        src2 = self._source_trading_val()
        time.sleep(1.0)
        src3 = self._source_volume()
        time.sleep(1.0)
        src4 = self._source_new_listing()   # v1.3 신규상장 소스 추가

        pool: dict[str, dict] = {}
        for item in src1 + src2 + src3 + src4:
            code = item["code"]
            if code and code not in pool:
                pool[code] = item

        logger.info(
            f"[Scanner] 풀 {len(pool)}개 "
            f"(소스1:{len(src1)} 소스2:{len(src2)} "
            f"소스3:{len(src3)} 소스4_신규:{len(src4)})"
        )

        # ── Step 2: 1차 빠른 필터 (API 호출 없음) ────────────
        pre: list[dict] = []
        for code, item in pool.items():
            if code in held_codes or code in blacklist:
                continue
            price = item.get("cur_price", 0)
            if not (cfg["min_price"] <= price <= cfg["max_price"]):
                continue
            pre.append(item)

        pre = pre[:cfg.get("max_candidates", 30)]
        logger.info(f"[Scanner] 1차 필터: {len(pre)}개")

        # ── Step 3: 2차 상세 필터 (ka10001, 429 재시도) ──────
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

            if prev_close <= 0 or cur_price <= 0:
                continue

            rise_pct = (cur_price - prev_close) / prev_close * 100

            # 상승률 필터
            if not (cfg["min_rise_pct"] <= rise_pct <= cfg["max_rise_pct"]):
                logger.debug(f"[Scanner] {code} ❌ 상승률 {rise_pct:.1f}%")
                continue

            # 상한가 근접 제외
            if cfg.get("exclude_upper_limit", True) and rise_pct >= 29.0:
                logger.debug(f"[Scanner] {code} ❌ 상한가 근접 {rise_pct:.1f}%")
                continue

            # 거래대금 필터
            if today_tv < cfg["min_trading_value"]:
                logger.debug(
                    f"[Scanner] {code} ❌ 거래대금 "
                    f"{today_tv//100_000_000}억 "
                    f"< {cfg['min_trading_value']//100_000_000}억"
                )
                continue

            # 거래량 비율 — 캐시에 있을 때만 필터, 없으면 0(중간 점수)으로 통과
            prev_vol     = self._prev_vol_cache.get(code, 0)
            volume_ratio = (
                round(volume / prev_vol, 1) if prev_vol > 0 else 0.0
            )
            # init_daily() 호출된 경우에만 거래량 비율 필터 적용
            if prev_vol > 0 and volume_ratio < cfg["volume_ratio_min"]:
                logger.debug(
                    f"[Scanner] {code} ❌ 거래량비율 "
                    f"{volume_ratio}배 < {cfg['volume_ratio_min']}배"
                )
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
                "vwap":           0.0,   # Step 4에서 상위만 채움
                "source":         item.get("source", ""),
                "scan_time":      now_str,
                "is_new_listing": item.get("is_new_listing", False),  # v1.3
            })
            logger.info(
                f"[Scanner] ✅ {detail['name']}({code}) "
                f"상승:{rise_pct:.1f}% "
                f"TV:{today_tv//100_000_000}억 "
                f"거래량:{volume_ratio}배"
            )

        # ── Step 4: 1차 점수 정렬 후 상위 N개만 VWAP 계산 ───
        # VWAP 없이 먼저 점수화 → 상위 N개만 ka10080 호출
        for c in passed:
            c["score"] = self._score(c, now_str)
        passed.sort(key=lambda x: x["score"], reverse=True)

        entry_cfg  = self.cfg.get_entry()
        vwap_top_n = entry_cfg.get("vwap_top_n", 10)   # v1.3: 기본 10개
        # 신규상장 VWAP 허용 범위 (예: -5 → VWAP 대비 -5%까지 허용)
        new_listing_vwap_tol = entry_cfg.get("new_listing_vwap_tolerance_pct", -5.0)

        if entry_cfg.get("use_vwap_filter", True) and passed:
            logger.info(
                f"[Scanner] 상위 {min(vwap_top_n, len(passed))}개 VWAP 계산 중..."
            )
            for c in passed[:vwap_top_n]:
                time.sleep(api_delay)
                vwap = self.broker.calc_vwap(c["code"])
                c["vwap"]  = round(vwap, 0)
                c["score"] = self._score(
                    c, now_str,
                    new_listing_vwap_tol=new_listing_vwap_tol
                )
            # VWAP 반영 후 재정렬
            passed.sort(key=lambda x: x["score"], reverse=True)

        self.last_scan_result = passed
        logger.info(
            f"[Scanner] 완료 — "
            f"풀:{len(pool)} → 2차:{len(passed)}개 최종"
        )
        return passed

    # ──────────────────────────────────────────────────────────
    # 3. 소스 수집
    # ──────────────────────────────────────────────────────────

    def _fetch_rank_list(self, sort_tp: str, count: int = 50) -> list[dict]:
        """ka10023 순위 조회 — sort_tp: 1=거래량, 2=등락률, 3=거래대금"""
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
            for item in data.get("trde_qty_sdnin", [])[:count]:
                code  = _clean_code(item.get("stk_cd", "").lstrip("A"))
                name  = item.get("stk_nm", "")
                price = _safe_int(item.get("cur_prc", "0"))
                if code and price > 0:
                    result.append({
                        "code":      code,
                        "name":      name,
                        "cur_price": price,
                    })
            return result
        except Exception as e:
            logger.warning(f"[Scanner] ka10023 sort_tp={sort_tp} 실패: {e}")
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
        """
        v1.3 신규 — 신규상장 종목 소스 (ka10016 신고가 + 상장일 필터)

        상장 60일 이내 종목 중 당일 5% 이상 상승 중인 종목을 수집.
        RISE_RANK에서 누락되는 신규상장 강세 종목을 보완한다.
        """
        cfg            = self.cfg.get_scan()
        new_days_limit = cfg.get("new_listing_days", 60)   # 상장 N일 이내
        result         = []
        today_str      = datetime.now(KST).strftime("%Y%m%d")

        try:
            data = self.broker._post(
                "ka10016", "/api/dostk/stkinfo",
                {
                    "mrkt_tp":  "000",   # 전체
                    "sort_tp":  "1",     # 등락률순
                    "new_tp":   "1",     # 신규상장 필터
                    "stex_tp":  "3",
                }
            )
            items = data.get("stk_new_high_qry", [])
            for item in items[:30]:
                code     = _clean_code(item.get("stk_cd", "").lstrip("A"))
                name     = item.get("stk_nm", "")
                price    = _safe_int(item.get("cur_prc", "0"))
                list_dt  = item.get("list_dt", "")   # 상장일 YYYYMMDD

                if not code or price <= 0:
                    continue

                # 상장일 캐시에 저장
                if list_dt:
                    self._listing_date[code] = list_dt

                # 상장 경과일 계산
                if list_dt and len(list_dt) == 8:
                    try:
                        from datetime import date
                        listed = date(int(list_dt[:4]),
                                      int(list_dt[4:6]),
                                      int(list_dt[6:8]))
                        elapsed_days = (date.today() - listed).days
                        if elapsed_days > new_days_limit:
                            continue   # 너무 오래된 종목 제외
                    except Exception:
                        pass

                result.append({
                    "code"           : code,
                    "name"           : name,
                    "cur_price"      : price,
                    "source"         : "NEW_LISTING",
                    "is_new_listing" : True,
                })
        except Exception as e:
            logger.warning(f"[Scanner] 신규상장 소스 실패: {e}")

        logger.info(f"[Scanner] 소스4(신규상장): {len(result)}개")
        return result

    # ──────────────────────────────────────────────────────────
    # 4. 상세 조회 (429 재시도)
    # ──────────────────────────────────────────────────────────

    def _get_stock_detail_with_retry(self, code: str,
                                      max_retry: int = 3) -> Optional[dict]:
        """
        ka10001 상세 조회 — 429 시 2초→4초→6초 재시도

        [v1.2 수정] get_today_info() 대신 get_stock_info() 직접 호출
        get_today_info()는 내부에서 예외를 잡아 {} 반환 → retry 불가
        get_stock_info()는 예외를 그대로 raise → retry 정상 동작
        """
        for attempt in range(max_retry):
            try:
                # get_today_info() 대신 get_stock_info() 직접 호출
                # → 429 예외가 그대로 raise되어 아래 except에서 포착됨
                info = self.broker.get_stock_info(code)
                if not info or info.get("cur_price", 0) <= 0:
                    return None
                return info
            except Exception as e:
                err_str = str(e)
                if "429" in err_str and attempt < max_retry - 1:
                    wait = 2.0 * (attempt + 1)   # 2초 → 4초 → 6초
                    logger.warning(
                        f"[Scanner] {code} 429 — "
                        f"{attempt+1}/{max_retry}회 재시도 ({wait:.0f}초 대기)"
                    )
                    time.sleep(wait)
                else:
                    logger.debug(f"[Scanner] {code} 조회 최종 실패: {e}")
                    return None
        return None

    def _get_prev_day_volumes(self, code: str) -> Optional[dict]:
        """ka10081 일봉 2개로 전일 거래량 조회 — init_daily()에서만 사용"""
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
    # 5. 점수화
    # ──────────────────────────────────────────────────────────

    def _score(
        self,
        item        : dict,
        now_str     : str,
        new_listing_vwap_tol: float = -5.0,
    ) -> int:
        """
        단타 후보 점수화 (최대 115점)
          거래대금   40점: 200억+ =40 / 100억+ =30 / 50억+ =20
          거래량비율 30점: 10배+ =30 / 5배+ =20 / 3배+ =10 / 캐시없음 =15
          모멘텀    20점: 10~15% =20 / 7~10% =15 / 3~7% =10 / 15~20% =8
          시간대    10점: 09:00~09:30 =10 / ~10:30 =7 / ~13:00 =3
          VWAP보너스 +5점: 현재가 >= VWAP
          신규상장  +10점: 상장 60일 이내 종목 (v1.3)
        """
        score = 0

        tv = item.get("trading_value", 0)
        if   tv >= 20_000_000_000: score += 40
        elif tv >= 10_000_000_000: score += 30
        elif tv >=  5_000_000_000: score += 20
        else:                      score += 5

        vr = item.get("volume_ratio", 0)
        if   vr >= 10: score += 30
        elif vr >= 5:  score += 20
        elif vr >= 3:  score += 10
        elif vr == 0:  score += 15   # 캐시 없음 → 중간값

        rise = item.get("rise_pct", 0)
        if   10.0 <= rise <= 15.0: score += 20
        elif  7.0 <= rise < 10.0:  score += 15
        elif  3.0 <= rise <  7.0:  score += 10
        elif 15.0 <  rise < 20.0:  score += 8
        elif 20.0 <= rise < 29.0:  score += 5   # v1.3: 20% 이상도 부분 점수

        if   "09:00" <= now_str < "09:30": score += 10
        elif now_str < "10:30":            score += 7
        elif now_str < "13:00":            score += 3

        # VWAP 보너스 — 신규상장은 tolerance 적용
        vwap = item.get("vwap", 0)
        if vwap > 0:
            is_new = item.get("is_new_listing", False)
            margin = self.cfg.get_entry().get("vwap_margin_pct", 0.0)
            # 신규상장: VWAP보다 tolerance% 아래까지 허용
            effective_vwap = vwap * (1 + (new_listing_vwap_tol / 100)) if is_new else vwap
            if item.get("cur_price", 0) >= effective_vwap * (1 + margin / 100):
                score += 5

        # v1.3: 신규상장 보너스
        if item.get("is_new_listing", False):
            score += 10

        return score

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
        for i, c in enumerate(candidates[:10], 1):   # v1.3: 10개까지 표시
            tv_str   = f"{c['trading_value']//100_000_000}억"
            vr_str   = f"{c['volume_ratio']:.1f}배" if c["volume_ratio"] > 0 else "N/A"
            vwap_str = f" | VWAP:{c['vwap']:,.0f}" if c["vwap"] > 0 else ""
            new_str  = " 🆕" if c.get("is_new_listing") else ""   # v1.3
            lines.append(
                f"<b>{i}. {c['name']}({c['code']})</b>{new_str}\n"
                f"   현재가: <b>{c['cur_price']:,}원</b> "
                f"(<b>{c['rise_pct']:+.1f}%</b>)\n"
                f"   TV:{tv_str} | 거래량:{vr_str}{vwap_str}\n"
                f"   점수:{c['score']}점 | {c['source']}"
            )
        return "\n".join(lines)
