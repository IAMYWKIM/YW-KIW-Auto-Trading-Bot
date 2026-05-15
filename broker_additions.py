"""
broker_additions.py — broker.py에 추가할 단타 전용 메서드

[적용 방법]
  broker.py의 KiwoomBroker 클래스 맨 아래에 이 메서드들을 붙여넣으세요.
  get_stock_info()의 반환값에 trading_value 필드도 추가합니다.

[추가 메서드 목록]
  1. get_stock_info()    — 기존 메서드에 trading_value 필드 추가
  2. get_minute_chart()  — 분봉 데이터 조회 (VWAP 계산용) [신규]
  3. get_today_info()    — 단타용 핵심 정보 한 번에 조회 [신규]
"""

# ──────────────────────────────────────────────────────────────
# [수정] broker.py의 get_stock_info()에 trading_value 추가
# 기존 return 딕셔너리에 아래 필드를 추가하세요:
#
#   "trading_value": to_int(data.get("acml_trde_prica", "0")),
#
# 완성된 get_stock_info()는 아래와 같습니다:
# ──────────────────────────────────────────────────────────────

MODIFIED_GET_STOCK_INFO = '''
    def get_stock_info(self, code: str) -> dict:
        """ka10001 — 주식기본정보 (현재가, 전일종가, 고/저가, 거래량, 거래대금)"""
        data = self._post("ka10001", "/api/dostk/stkinfo", {"stk_cd": code})

        def to_int(s):
            return abs(int((s or "0").lstrip("0+-") or "0"))

        return {
            "code":          data.get("stk_cd", code),
            "name":          data.get("stk_nm", ""),
            "cur_price":     to_int(data.get("cur_prc",         "0")),
            "prev_close":    to_int(data.get("pred_close_pric", "0")),
            "high":          to_int(data.get("high_pric",       "0")),
            "low":           to_int(data.get("low_pric",        "0")),
            "volume":        to_int(data.get("acml_vol",        "0")),
            # [단타 추가] 당일 누적 거래대금 (원) — ka10001 응답 필드
            # 필드명이 다를 경우: acml_trde_prica → acml_trde_pric 로 변경 시도
            "trading_value": to_int(data.get("acml_trde_prica", "0")),
        }
'''

# ──────────────────────────────────────────────────────────────
# [신규] broker.py KiwoomBroker 클래스에 추가할 메서드들
# ──────────────────────────────────────────────────────────────

NEW_METHODS = '''
    # ──────────────────────────────────────────────────────────
    # 단타 전용 추가 메서드 (by scalp_main.py)
    # ──────────────────────────────────────────────────────────

    def get_minute_chart(self, code: str,
                         interval: int = 1,
                         count: int = 60) -> list[dict]:
        """
        ka10080 — 주식분봉차트
        단타 VWAP 계산에 사용

        Args:
            code:     종목코드
            interval: 분봉 단위 (1=1분, 3=3분, 5=5분, 10, 15, 30, 60)
            count:    조회 봉 수 (최대 900, 기본 60봉 = 1시간)

        Returns:
            [{"date", "time", "open", "high", "low", "close", "volume"}, ...]
            index 0 = 가장 최근 봉

        [주의] ka10080 응답 키는 실전 서버에서 확인 필요
               기본 추정: stk_min_pole_chart_qry
               확인 코드: data.keys() 로 응답 최상위 키 출력
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

            # ⚠️ 응답 키 확인 필요 — 아래 줄의 주석을 일시적으로 해제하여 확인
            # logger.info(f"[Broker] ka10080 응답 키: {list(data.keys())}")

            # 추정 응답 키 (실제 다를 경우 수정)
            candles_raw = (
                data.get("stk_min_pole_chart_qry") or   # 추정 키1
                data.get("output") or                     # 추정 키2
                data.get("chart") or                      # 추정 키3
                []
            )

            result = []
            for c in candles_raw[:count]:
                try:
                    result.append({
                        "date":   c.get("trd_date", ""),
                        "time":   c.get("trd_time",  ""),
                        "open":   abs(int((c.get("opn_pric",  "0") or "0").lstrip("+-") or "0")),
                        "high":   abs(int((c.get("high_pric", "0") or "0").lstrip("+-") or "0")),
                        "low":    abs(int((c.get("low_pric",  "0") or "0").lstrip("+-") or "0")),
                        "close":  abs(int((c.get("cur_prc",   "0") or "0").lstrip("+-") or "0")),
                        "volume": abs(int((c.get("trde_qty",  "0") or "0").lstrip("+-") or "0")),
                    })
                except (ValueError, TypeError):
                    continue
            return result

        except Exception as e:
            logger.warning(f"[Broker] {code} 분봉 조회 실패: {e}")
            return []

    def get_today_info(self, code: str) -> dict:
        """
        단타 전용 — 현재가 + 당일 거래대금 + 전일 종가를 한 번에 반환
        scanner.py의 _get_stock_detail()에서 사용

        Returns:
            {
                "cur_price", "prev_close", "volume",
                "trading_value", "high", "low", "name"
            }
        """
        info = self.get_stock_info(code)
        if not info:
            return {}

        # trading_value가 0이면 현재가×거래량으로 근사
        tv = info.get("trading_value", 0)
        if tv == 0 and info["cur_price"] > 0:
            tv = info["cur_price"] * info["volume"]

        return {
            "code":          info["code"],
            "name":          info["name"],
            "cur_price":     info["cur_price"],
            "prev_close":    info["prev_close"],
            "high":          info["high"],
            "low":           info["low"],
            "volume":        info["volume"],
            "trading_value": tv,
        }
'''

# ──────────────────────────────────────────────────────────────
# 적용 가이드 출력
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("broker.py 수정 가이드")
    print("=" * 60)
    print()
    print("[1] get_stock_info() 에 trading_value 필드 추가:")
    print('    return 딕셔너리에 아래 한 줄 추가')
    print('    "trading_value": to_int(data.get("acml_trde_prica", "0")),')
    print()
    print("[2] KiwoomBroker 클래스 맨 아래에 추가할 메서드:")
    print("    - get_minute_chart(code, interval=1, count=60)")
    print("    - get_today_info(code)")
    print()
    print("[3] ka10080 응답 키 확인 방법:")
    print("    broker.get_minute_chart('005930') 호출 후")
    print("    로그에서 'ka10080 응답 키' 확인")
    print()
    print("[4] 거래대금 필드명 확인 방법:")
    print("    broker._post('ka10001', '/api/dostk/stkinfo', {'stk_cd': '005930'})")
    print("    로 원시 응답 출력 후 거래대금 관련 키 찾기")
    print("=" * 60)
