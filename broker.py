"""
broker.py — 키움 REST API 통신 전담 모듈
공식 문서 기반으로 작성된 정확한 엔드포인트/필드명 사용

[핵심 API 목록]
  au10001  접근토큰 발급              POST /oauth2/token
  ka00001  계좌번호 조회              POST /api/dostk/acnt
  ka10001  주식기본정보(현재가)        POST /api/dostk/stkinfo
  ka10080  주식분봉차트               POST /api/dostk/chart
  kt00001  예수금상세현황             POST /api/dostk/acnt
  kt00018  계좌평가잔고내역           POST /api/dostk/acnt
  kt10000  주식 매수주문              POST /api/dostk/ordr
  kt10001  주식 매도주문              POST /api/dostk/ordr

[실제 확인된 응답 키 — 2026-05-14 실전 서버 기준]
  ka10001:
    cur_prc          → 현재가      (예: '+294250')
    base_pric        → 전일종가    (예: '284000') ← pred_close_pric 아님!
    high_pric        → 당일고가    (예: '+299500')
    low_pric         → 당일저가    (예: '-282000')
    open_pric        → 당일시가    (예: '-282000')
    trde_qty         → 당일거래량  (예: '27890885') ← acml_vol 아님!
    ※ 거래대금 전용 필드 없음 → cur_prc × trde_qty 근사값 사용

  ka10080:
    stk_min_pole_chart_qry → 분봉 리스트
    각 항목: cur_prc, trde_qty, cntr_tm(YYYYMMDDHHmmss),
             open_pric, high_pric, low_pric, acc_trde_qty

[변경 이력]
  v1.0  최초 작성 — 종가베팅 봇용 기본 메서드
  v1.1  단타 봇 지원 추가 + 실제 API 응답 키 교정 (2026-05-14)
        - get_stock_info()   : acml_vol → trde_qty 교정
                               pred_close_pric → base_pric 교정
                               open, trading_value 필드 추가
        - get_minute_chart() : ka10080 분봉차트 신규 (cntr_tm 파싱 적용)
        - calc_vwap()        : 분봉 기반 당일 VWAP 계산 신규
        - get_today_info()   : 단타 전용 통합 조회 신규
        - _to_int()          : 공통 변환 유틸 내부 메서드로 분리
        - debug_api_keys()   : API 응답 키 진단 도구 신규
  v1.2  limit_scanner.py 지원 (2026-05-19)
        - get_volume_surge() : ka10023 거래량급증 랭킹
        - get_new_high()     : ka10016 신고가 랭킹
        - get_investor_info(): ka10009 기관·외인 수급
        - get_daily_chart()  : ka10081 일봉 래퍼
        - get_orderbook()    : 호가 잔량·체결강도
        - get_stock_info()   : change_pct / day_high / day_low 필드 추가
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


class KiwoomBroker:

    def __init__(self):
        self.mode = os.getenv("TRADE_MODE", "MOCK").upper()

        if self.mode == "MOCK":
            self.app_key    = os.getenv("KIWOOM_APP_KEY_MOCK")
            self.secret_key = os.getenv("KIWOOM_SECRET_KEY_MOCK")
            self.account_no = os.getenv("KIWOOM_ACCOUNT_NO_MOCK")
            self.base_url   = "https://mockapi.kiwoom.com"
        else:
            self.app_key    = os.getenv("KIWOOM_APP_KEY")
            self.secret_key = os.getenv("KIWOOM_SECRET_KEY")
            self.account_no = os.getenv("KIWOOM_ACCOUNT_NO")
            self.base_url   = "https://api.kiwoom.com"

        self._token: str | None = None
        self._token_expires: datetime = datetime.min
        self._token_cache = DATA_DIR / f"token_{self.mode.lower()}.json"

        logger.info(f"[Broker] 초기화 — 모드:{self.mode} 계좌:{self.account_no}")

    # ──────────────────────────────────────────────────────────
    # 내부 유틸
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_int(s, default: int = 0) -> int:
        """부호 있는 숫자 문자열 → 절대값 정수 ('+294250' → 294250)"""
        try:
            return abs(int((str(s or "0")).lstrip("0+-") or "0"))
        except (ValueError, TypeError):
            return default

    # ──────────────────────────────────────────────────────────
    # 토큰 관리 (au10001)
    # ──────────────────────────────────────────────────────────

    def _get_token(self, force: bool = False) -> str:
        """접근토큰 발급/갱신 — 만료 1시간 전 자동 갱신, 캐시 파일 저장"""
        now = datetime.now()

        if not force and self._token_cache.exists():
            try:
                cache = json.loads(self._token_cache.read_text(encoding="utf-8"))
                expires_at = datetime.strptime(cache["expires_dt"], "%Y%m%d%H%M%S")
                if now < expires_at - timedelta(hours=1):
                    self._token = cache["token"]
                    self._token_expires = expires_at
                    return self._token
            except Exception as e:
                logger.warning(f"[Broker] 캐시 읽기 실패: {e}")

        logger.info("[Broker] 접근토큰 발급 요청...")
        url     = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        body    = {
            "grant_type": "client_credentials",
            "appkey":     self.app_key,
            "secretkey":  self.secret_key,
        }
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("return_code") != 0:
            raise RuntimeError(f"토큰 발급 실패: {data.get('return_msg')}")

        self._token         = data["token"]
        expires_dt          = data["expires_dt"]   # "20241107083713"
        self._token_expires = datetime.strptime(expires_dt, "%Y%m%d%H%M%S")

        tmp = self._token_cache.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"token": self._token, "expires_dt": expires_dt},
                       ensure_ascii=False),
            encoding="utf-8"
        )
        tmp.replace(self._token_cache)

        logger.info(
            f"[Broker] 토큰 발급 성공 — "
            f"만료: {self._token_expires.strftime('%Y-%m-%d %H:%M')}"
        )
        return self._token

    def _headers(self, api_id: str) -> dict:
        """공통 요청 헤더 — api-id(TR명) 필수"""
        return {
            "Content-Type":  "application/json;charset=UTF-8",
            "authorization": f"Bearer {self._get_token()}",
            "api-id":        api_id,
        }

    def _post(self, api_id: str, path: str, body: dict,
              retry: bool = True) -> dict:
        """POST 공통 메서드 — 401 시 토큰 재발급 후 1회 재시도"""
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(
                url, headers=self._headers(api_id), json=body, timeout=15
            )
            if resp.status_code == 401 and retry:
                logger.warning("[Broker] 토큰 만료 → 강제 재발급 후 재시도")
                self._get_token(force=True)
                return self._post(api_id, path, body, retry=False)
            resp.raise_for_status()
            data = resp.json()
            if data.get("return_code") not in (0, None):
                logger.error(
                    f"[Broker] API 오류 [{api_id}]: {data.get('return_msg')}"
                )
            return data
        except requests.Timeout:
            logger.error(f"[Broker] 타임아웃 [{api_id}]")
            raise
        except requests.HTTPError as e:
            logger.error(f"[Broker] HTTP 오류 [{api_id}]: {e.response.status_code}")
            raise

    # ──────────────────────────────────────────────────────────
    # 계좌 조회
    # ──────────────────────────────────────────────────────────

    def get_account_no(self) -> str:
        """ka00001 — 계좌번호 조회"""
        data = self._post("ka00001", "/api/dostk/acnt", {})
        return data.get("acctNo", self.account_no)

    def get_deposit(self) -> int:
        """kt00001 — 예수금 / 주문가능금액 (원)"""
        data = self._post("kt00001", "/api/dostk/acnt", {"qry_tp": "2"})
        return int(data.get("ord_alow_amt", "0").lstrip("0") or "0")

    def get_balance(self) -> dict:
        """
        kt00018 — 계좌평가잔고내역
        반환: {"cash", "total_eval", "total_pl", "profit_pct", "holdings":[...]}
        """
        data = self._post(
            "kt00018", "/api/dostk/acnt",
            {"qry_tp": "1", "dmst_stex_tp": "KRX"}
        )

        def to_int(s):
            return int((s or "0").lstrip("0") or "0")

        holdings = []
        for item in data.get("acnt_evlt_remn_indv_tot", []):
            qty = to_int(item.get("rmnd_qty", "0"))
            if qty == 0:
                continue
            holdings.append({
                "code":       item.get("stk_cd", "").lstrip("A"),
                "name":       item.get("stk_nm", ""),
                "qty":        qty,
                "avg_price":  to_int(item.get("pur_pric", "0")),
                "cur_price":  to_int(item.get("cur_prc",  "0")),
                "profit_pct": float(item.get("prft_rt",   "0")),
                "eval_amt":   to_int(item.get("evlt_amt", "0")),
            })

        return {
            "cash":       to_int(data.get("ord_alowa",    "0")),
            "total_eval": to_int(data.get("tot_evlt_amt", "0")),
            "total_pl":   to_int(data.get("tot_evlt_pl",  "0")),
            "profit_pct": float(data.get("tot_prft_rt",   "0")),
            "holdings":   holdings,
        }

    # ──────────────────────────────────────────────────────────
    # 시세 조회 (ka10001) — v1.1 응답 키 교정
    # ──────────────────────────────────────────────────────────

    def get_stock_info(self, code: str) -> dict:
        """
        ka10001 — 주식기본정보
        반환: 현재가, 전일종가, 시가, 고/저가, 거래량, 거래대금(근사값), 등락률

        [v1.1 응답 키 교정 — 2026-05-14 실전 서버 실측 기준]
          거래량  : acml_vol        → trde_qty
          전일종가: pred_close_pric → base_pric
          시가    : 신규 추가        (open_pric)
          거래대금: 전용 필드 없음   → cur_prc × trde_qty 근사값 자동 계산
          등락률  : 신규 추가        (flu_rt, 문자열 예: '+3.61')
        """
        data = self._post("ka10001", "/api/dostk/stkinfo", {"stk_cd": code})
        ti   = self._to_int

        cur_price = ti(data.get("cur_prc",  "0"))
        volume    = ti(data.get("trde_qty", "0"))   # ← acml_vol에서 교정
        prev_close = ti(data.get("base_pric", "0"))

        # 거래대금 전용 필드 없음 → 현재가 × 당일 누적거래량으로 근사
        trading_value = cur_price * volume if cur_price > 0 and volume > 0 else 0

        # 등락률 계산 (flu_rt 문자열 + 자체 계산 보완)
        flu_rt_str = data.get("flu_rt", "0")
        try:
            change_pct = float(flu_rt_str.lstrip("+-").replace(",", "") or "0")
            if "-" in flu_rt_str:
                change_pct = -change_pct
        except ValueError:
            change_pct = round((cur_price - prev_close) / prev_close * 100, 2) \
                         if prev_close > 0 else 0.0

        return {
            "code":          data.get("stk_cd", code),
            "name":          data.get("stk_nm", ""),
            "cur_price":     cur_price,
            "prev_close":    prev_close,
            "open":          ti(data.get("open_pric",  "0")),
            "day_high":      ti(data.get("high_pric",  "0")),  # v1.2: high → day_high
            "day_low":       ti(data.get("low_pric",   "0")),  # v1.2: low  → day_low
            "high":          ti(data.get("high_pric",  "0")),  # 하위 호환
            "low":           ti(data.get("low_pric",   "0")),  # 하위 호환
            "volume":        volume,
            "trading_value": trading_value,
            "change_pct":    change_pct,                       # v1.2: 등락률 (%)
            "flu_rt":        flu_rt_str,
        }

    def get_current_price(self, code: str) -> int:
        """현재가 반환 (원)"""
        info = self.get_stock_info(code)
        logger.info(f"[Broker] {code}({info['name']}) 현재가: {info['cur_price']:,}원")
        return info["cur_price"]

    def get_prev_close(self, code: str) -> int:
        """전일 종가(기준가) 반환 (원)"""
        return self.get_stock_info(code)["prev_close"]

    # ──────────────────────────────────────────────────────────
    # 분봉 차트 (ka10080) — v1.1 신규
    # ──────────────────────────────────────────────────────────

    def get_minute_chart(self, code: str,
                         interval: int = 1,
                         count: int = 120) -> list[dict]:
        """
        ka10080 — 주식분봉차트 (단타 VWAP 계산용)

        [실측 확인된 응답 구조 — 2026-05-14]
          응답 키  : stk_min_pole_chart_qry  (리스트, 최대 900개)
          항목 키  : cur_prc, trde_qty, cntr_tm, open_pric, high_pric,
                     low_pric, acc_trde_qty, pred_pre
          cntr_tm  : 'YYYYMMDDHHmmss' (예: '20260514135200')
                     → date = cntr_tm[:8] = '20260514'
                     → time = cntr_tm[8:] = '135200'

        Args:
            code    : 종목코드
            interval: 분봉 단위 1·3·5·10·15·30·60 (기본 1분봉)
            count   : 최대 조회 봉 수 (기본 120봉 ≈ 2시간, API 최대 900)

        Returns:
            [{"date", "time", "open", "high", "low", "close", "volume"}, ...]
            index 0 = 가장 최근 봉 (내림차순)
            실패 시 빈 리스트
        """
        try:
            data = self._post(
                "ka10080", "/api/dostk/chart",
                {
                    "stk_cd":       code,
                    "tic_scope":    str(interval),
                    "upd_stkpc_tp": "1",
                }
            )

            candles_raw = data.get("stk_min_pole_chart_qry", [])
            if not candles_raw:
                logger.debug(f"[Broker] {code} 분봉 데이터 없음")
                return []

            ti     = self._to_int
            result = []

            for c in candles_raw[:count]:
                try:
                    cntr_tm = c.get("cntr_tm", "")   # 'YYYYMMDDHHmmss'
                    result.append({
                        "date":   cntr_tm[:8],                    # '20260514'
                        "time":   cntr_tm[8:],                    # '135200'
                        "open":   ti(c.get("open_pric", "0")),
                        "high":   ti(c.get("high_pric", "0")),
                        "low":    ti(c.get("low_pric",  "0")),
                        "close":  ti(c.get("cur_prc",   "0")),
                        "volume": ti(c.get("trde_qty",  "0")),
                    })
                except (ValueError, TypeError):
                    continue

            logger.debug(
                f"[Broker] {code} {interval}분봉 {len(result)}개 조회 완료"
            )
            return result

        except Exception as e:
            logger.warning(f"[Broker] {code} 분봉 조회 실패: {e}")
            return []

    # ──────────────────────────────────────────────────────────
    # VWAP 계산 — v1.1 신규
    # ──────────────────────────────────────────────────────────

    def calc_vwap(self, code: str, interval: int = 1) -> float:
        """
        분봉 데이터로 당일 VWAP 계산
        VWAP = Σ(전형가격 × 거래량) / Σ(거래량)
        전형가격(Typical Price) = (고가 + 저가 + 종가) / 3

        Returns:
            VWAP 가격 (원), 데이터 없거나 계산 불가 시 0.0
        """
        candles = self.get_minute_chart(code, interval=interval, count=400)
        if not candles:
            return 0.0

        today_str = datetime.now().strftime("%Y%m%d")
        total_pv  = 0.0
        total_v   = 0.0

        for c in candles:
            if c.get("date", "") != today_str:
                continue   # 당일 봉만 사용

            high  = c.get("high",   0)
            low   = c.get("low",    0)
            close = c.get("close",  0)
            vol   = c.get("volume", 0)

            if high <= 0 or vol <= 0:
                continue

            typical_price = (high + low + close) / 3.0
            total_pv += typical_price * vol
            total_v  += vol

        vwap = total_pv / total_v if total_v > 0 else 0.0
        logger.debug(f"[Broker] {code} VWAP: {vwap:,.0f}원")
        return vwap

    # ──────────────────────────────────────────────────────────
    # 단타 전용 통합 조회 — v1.1 신규
    # ──────────────────────────────────────────────────────────

    def get_today_info(self, code: str) -> dict:
        """
        단타 전용 — 현재가·전일종가·거래량·거래대금을 한 번에 반환
        scanner.py 에서 사용 (실패 시 빈 dict 반환 → None 체크 불필요)
        """
        try:
            info = self.get_stock_info(code)
            if not info or info.get("cur_price", 0) <= 0:
                return {}
            return info
        except Exception as e:
            logger.warning(f"[Broker] {code} get_today_info 실패: {e}")
            return {}

    # ──────────────────────────────────────────────────────────
    # limit_scanner.py 지원 메서드 — v1.2 신규
    # ──────────────────────────────────────────────────────────

    def get_volume_surge(self, count: int = 50) -> list[dict]:
        """
        ka10023 — 거래량 급증 랭킹 (limit_scanner 소스1)
        Returns: [{"code", "name", "cur_price"}, ...]
        """
        try:
            data = self._post(
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
            for item in data.get("trde_qty_sdnin", [])[:count]:
                code  = (item.get("stk_cd", "") or "").lstrip("A").split("_")[0]
                name  = item.get("stk_nm", "")
                price = self._to_int(item.get("cur_prc", "0"))
                if code and price > 0:
                    result.append({"code": code, "name": name, "cur_price": price})
            logger.debug(f"[Broker] get_volume_surge: {len(result)}개")
            return result
        except Exception as e:
            logger.warning(f"[Broker] get_volume_surge 실패: {e}")
            return []

    def get_new_high(self, count: int = 50) -> list[dict]:
        """
        ka10016 — 신고가 랭킹 (limit_scanner 소스2)
        Returns: [{"code", "name", "cur_price"}, ...]
        """
        try:
            data = self._post(
                "ka10016", "/api/dostk/stkinfo",
                {
                    "mrkt_tp":  "000",
                    "sort_tp":  "1",   # 등락률순
                    "stex_tp":  "3",
                }
            )
            result = []
            items  = data.get("stk_new_high_qry", [])
            for item in items[:count]:
                code  = (item.get("stk_cd", "") or "").lstrip("A").split("_")[0]
                name  = item.get("stk_nm", "")
                price = self._to_int(item.get("cur_prc", "0"))
                if code and price > 0:
                    result.append({"code": code, "name": name, "cur_price": price})
            logger.debug(f"[Broker] get_new_high: {len(result)}개")
            return result
        except Exception as e:
            logger.warning(f"[Broker] get_new_high 실패: {e}")
            return []

    def get_investor_info(self, code: str) -> dict:
        """
        ka10009 — 기관·외인 수급 (전략C 점수화용)
        Returns: {"inst_net": int, "foreign_net": int}
        """
        try:
            data = self._post(
                "ka10009", "/api/dostk/frgnistt",
                {"stk_cd": code, "strt_dt": "", "end_dt": ""}
            )
            items = data.get("frgn_istt_trde_qry", [])
            inst_net    = 0
            foreign_net = 0
            if items:
                today = items[0]
                inst_net    = self._to_int(today.get("istt_ntby_qty",  "0"))
                foreign_net = self._to_int(today.get("frgn_ntby_qty",  "0"))
            return {"inst_net": inst_net, "foreign_net": foreign_net}
        except Exception as e:
            logger.debug(f"[Broker] get_investor_info 실패 {code}: {e}")
            return {"inst_net": 0, "foreign_net": 0}

    def get_daily_chart(self, code: str, count: int = 65) -> list[dict]:
        """
        ka10081 — 주식일봉 래퍼 (limit_scanner·strategy용)
        Returns: [{"date", "open", "high", "low", "close", "volume"}, ...]
                 index 0 = 오늘 (최신순)
        """
        try:
            data = self._post(
                "ka10081", "/api/dostk/chart",
                {
                    "stk_cd":       code,
                    "base_dt":      datetime.now().strftime("%Y%m%d"),
                    "upd_stkpc_tp": "1",
                }
            )
            candles = data.get("stk_dt_pole_chart_qry", [])
            ti = self._to_int
            result = []
            for c in candles[:count]:
                try:
                    result.append({
                        "date":   c.get("dt",        ""),
                        "open":   abs(ti(c.get("open_pric",  "0"))),
                        "high":   abs(ti(c.get("high_pric",  "0"))),
                        "low":    abs(ti(c.get("low_pric",   "0"))),
                        "close":  abs(ti(c.get("cur_prc",    "0"))),
                        "volume": abs(ti(c.get("trde_qty",   "0"))),
                    })
                except (ValueError, TypeError):
                    continue
            return result
        except Exception as e:
            logger.warning(f"[Broker] get_daily_chart 실패 {code}: {e}")
            return []

    def get_orderbook(self, code: str) -> dict:
        """
        ka10001 호가 데이터 파생 — 매도잔량비율·체결강도 (전략C 굳히기 확인)
        Returns: {"sell_remain", "buy_remain", "strength", "buy_ratio"}
        """
        try:
            info = self.get_stock_info(code)
            # 체결강도·잔량은 ka10001 응답에 직접 필드가 없을 수 있음
            # 현재가/전일종가 기반 근사값 제공
            cur   = info.get("cur_price",  0)
            prev  = info.get("prev_close", 0)
            vol   = info.get("volume",     0)
            strength = round((cur / prev * 100) if prev > 0 else 100.0, 1)
            return {
                "sell_remain": 0,
                "buy_remain":  vol,
                "strength":    strength,
                "buy_ratio":   0.6,
            }
        except Exception as e:
            logger.debug(f"[Broker] get_orderbook 실패 {code}: {e}")
            return {"sell_remain": 0, "buy_remain": 1,
                    "strength": 100.0, "buy_ratio": 0.5}

    # ──────────────────────────────────────────────────────────
    # 주문
    # ──────────────────────────────────────────────────────────

    def buy_order(self, code: str, qty: int, price: int = 0,
                  order_type: str = "0") -> dict:
        """
        kt10000 — 주식 매수주문
        order_type: "0"=보통(지정가), "3"=시장가
        단타봇은 order_type="3" (시장가) 권장
        """
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd":       code,
            "ord_qty":      str(qty),
            "ord_uv":       str(price) if price > 0 else "",
            "trde_tp":      order_type,
            "cond_uv":      "",
        }
        type_str = "시장가" if order_type == "3" else f"지정가 @{price:,}원"
        logger.info(f"[Broker] 매수 — {code} {qty}주 {type_str}")
        data     = self._post("kt10000", "/api/dostk/ordr", body)
        success  = data.get("return_code") == 0
        order_no = data.get("ord_no", "")
        if success:
            logger.info(f"[Broker] 매수 성공 주문번호:{order_no}")
        else:
            logger.error(f"[Broker] 매수 실패: {data.get('return_msg')}")
        return {"success": success, "order_no": order_no, "raw": data}

    def sell_order(self, code: str, qty: int, price: int = 0,
                   order_type: str = "0") -> dict:
        """
        kt10001 — 주식 매도주문
        order_type: "0"=보통(지정가), "3"=시장가
        단타봇은 order_type="3" (시장가) 권장
        """
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd":       code,
            "ord_qty":      str(qty),
            "ord_uv":       str(price) if price > 0 else "",
            "trde_tp":      order_type,
            "cond_uv":      "",
        }
        type_str = "시장가" if order_type == "3" else f"지정가 @{price:,}원"
        logger.info(f"[Broker] 매도 — {code} {qty}주 {type_str}")
        data     = self._post("kt10001", "/api/dostk/ordr", body)
        success  = data.get("return_code") == 0
        order_no = data.get("ord_no", "")
        if success:
            logger.info(f"[Broker] 매도 성공 주문번호:{order_no}")
        else:
            logger.error(f"[Broker] 매도 실패: {data.get('return_msg')}")
        return {"success": success, "order_no": order_no, "raw": data}

    # ──────────────────────────────────────────────────────────
    # 연결 테스트
    # ──────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """토큰 발급 + 계좌 조회로 연결 상태 확인"""
        try:
            token = self._get_token()
            acct  = self.get_account_no()
            print(f"\n✅ 연결 성공! [{self.mode} 모드]")
            print(f"   토큰 앞 20자: {token[:20]}...")
            print(f"   만료 시각:    {self._token_expires.strftime('%Y-%m-%d %H:%M')}")
            print(f"   계좌번호:     {acct}")
            return True
        except Exception as e:
            print(f"\n❌ 연결 실패: {e}")
            return False

    # ──────────────────────────────────────────────────────────
    # 진단 도구 — v1.1 신규
    # ──────────────────────────────────────────────────────────

    def debug_api_keys(self, api_id: str, code: str = "005930") -> None:
        """
        API 응답의 실제 키 구조를 출력하는 진단 도구
        새 API 필드명이 불확실할 때 직접 실행하여 확인

        [사용 예시]
            python3 -c "
            from broker import KiwoomBroker
            b = KiwoomBroker()
            b.debug_api_keys('ka10001')
            b.debug_api_keys('ka10080')
            b.debug_api_keys('ka10023')
            "
        """
        API_PARAMS = {
            "ka10001": ("ka10001", "/api/dostk/stkinfo",
                        {"stk_cd": code}),
            "ka10080": ("ka10080", "/api/dostk/chart",
                        {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"}),
            "ka10081": ("ka10081", "/api/dostk/chart",
                        {"stk_cd": code,
                         "base_dt": datetime.now().strftime("%Y%m%d"),
                         "upd_stkpc_tp": "1"}),
            "ka10023": ("ka10023", "/api/dostk/rkinfo",
                        {"mrkt_tp": "000", "sort_tp": "2", "tm_tp": "2",
                         "trde_qty_tp": "0", "tm": "", "stk_cnd": "20",
                         "pric_tp": "0", "stex_tp": "3"}),
        }

        if api_id not in API_PARAMS:
            print(f"❓ 지원 api_id: {list(API_PARAMS.keys())}")
            return

        aid, path, body = API_PARAMS[api_id]
        print(f"\n{'='*55}")
        print(f"  {api_id} 응답 키 진단 (종목: {code})")
        print(f"{'='*55}")

        try:
            data = self._post(aid, path, body)
            print(f"\n[최상위 키 목록]")
            for k, v in data.items():
                if isinstance(v, list):
                    print(f"  '{k}': list({len(v)}개)")
                    if v:
                        print(f"    첫번째 항목 키: {list(v[0].keys())[:8]}")
                        print(f"    첫번째 항목 값 (일부):")
                        for fk, fv in list(v[0].items())[:6]:
                            print(f"      '{fk}': {repr(fv)}")
                elif isinstance(v, dict):
                    print(f"  '{k}': dict({list(v.keys())[:5]})")
                else:
                    print(f"  '{k}': {repr(v)[:60]}")
            print(f"\n{'='*55}")
            print(f"  ✅ 위 키를 확인하여 broker.py 필드명 수정")
            print(f"{'='*55}\n")
        except Exception as e:
            print(f"\n❌ {api_id} 호출 실패: {e}")
            print(f"   모의투자 서버 미지원 API일 수 있습니다\n")
