"""
diagnose_scan.py — 스캔 조건별 탈락 원인 진단 스크립트
서버에서 직접 실행: python3 diagnose_scan.py

상위 10개 종목에 대해 조건 B→E→D→C/A를 단계별로 확인하고
어느 조건에서 탈락하는지 출력합니다.
"""

import sys
import time
import logging
from pathlib import Path

# 로그 간단 설정
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(message)s")

sys.path.insert(0, str(Path(__file__).parent))

from broker import KiwoomBroker
from strategy import Strategy
from strategy_config import StrategyConfig

broker   = KiwoomBroker()
scfg     = StrategyConfig()
strategy = Strategy(broker, scfg)

print("=" * 60)
print("조건검색식 탈락 원인 진단")
print("=" * 60)

# ── 현재 설정 출력 ──────────────────────────────────────────
cfg_scan  = scfg.get_scan()
cfg_entry = scfg.get_entry()
print(f"\n[현재 설정]")
print(f"  min_trading_value : {cfg_scan['min_trading_value']//100_000_000}억")
print(f"  volume_ratio_min  : {cfg_scan['volume_ratio_min']}배")
print(f"  envelope_period   : {cfg_scan.get('envelope_period', 20)}")
print(f"  envelope_band_pct : {cfg_scan.get('envelope_band_pct', 20.0)}%")
print(f"  envelope_lookback : {cfg_scan.get('envelope_lookback', 30)}봉")
print(f"  surge_threshold_e : {cfg_scan.get('surge_threshold_e', 9.0)}%")
print(f"  rsi_min/max       : {cfg_entry['rsi_min']}/{cfg_entry['rsi_max']}")
print(f"  api_delay_sec     : {cfg_scan.get('api_delay_sec', 0.5)}초")

# ── 소스 수집 ───────────────────────────────────────────────
print(f"\n[1] 후보 풀 수집 중... (v1.4.2 신규 소스)")
pool = {}

src1 = strategy._source_price_rise()
src2 = strategy._source_trading_value()
src3 = strategy._source_volume_surge()

print(f"    소스1 ka10004 등락률상위 : {len(src1)}개")
print(f"    소스2 ka10005 거래대금상위: {len(src2)}개")
print(f"    소스3 ka10023 거래량급증  : {len(src3)}개")

for item in src1 + src2 + src3:
    code = item["code"]
    if code and code not in pool:
        pool[code] = item

print(f"    중복 제거 후 후보 풀: {len(pool)}개")

# ── 상위 20개 상세 진단 ─────────────────────────────────────
print(f"\n[2] 상위 20개 종목 조건별 진단")
print("-" * 60)

items = list(pool.items())[:20]
counts = {"B": 0, "E": 0, "D": 0, "CA": 0, "RSI": 0, "VOL": 0, "PASS": 0}

for i, (code, item) in enumerate(items):
    time.sleep(cfg_scan.get("api_delay_sec", 0.5))
    name = item.get("name", code)

    daily = strategy.get_daily_data(code)
    if not daily:
        print(f"  [{i+1:02d}] {name}({code}) ❌ 일봉 데이터 없음")
        continue

    closes         = daily.get("closes", [])
    trading_values = daily.get("trading_values", [])
    volumes        = daily.get("volumes", [])

    env_period   = cfg_scan.get("envelope_period", 20)
    env_band_pct = cfg_scan.get("envelope_band_pct", 20.0)
    lookback     = cfg_scan.get("envelope_lookback", 30)

    # 조건 B
    cond_B = strategy.check_cond_B(trading_values, lookback)
    if not cond_B["pass"]:
        counts["B"] += 1
        print(f"  [{i+1:02d}] {name}({code}) ❌ 조건B — "
              f"전일TV {cond_B['prev_tv']//100_000_000}억 "
              f"< {cfg_scan['min_trading_value']//100_000_000}억")
        continue

    # 조건 E
    cond_EF = strategy.check_cond_EF(
        closes, lookback,
        cfg_scan.get("surge_threshold_e", 9.0),
        cfg_scan.get("surge_threshold_f", 29.5),
    )
    if not cond_EF["pass_E"]:
        counts["E"] += 1
        print(f"  [{i+1:02d}] {name}({code}) ❌ 조건E — "
              f"30봉 내 최대급등 {cond_EF['max_gain']:.1f}% "
              f"< {cfg_scan.get('surge_threshold_e', 9.0)}%  "
              f"(TV:{cond_B['prev_tv']//100_000_000}억)")
        continue

    # 조건 D
    cond_D = strategy.check_cond_D(daily)
    if not cond_D["pass"]:
        counts["D"] += 1
        print(f"  [{i+1:02d}] {name}({code}) ❌ 조건D — "
              f"MA3({cond_D['ma3']:,.0f}) >= 현재가({closes[0]:,.0f})  "
              f"급등:{cond_EF['max_gain']:.1f}%")
        continue

    # 조건 C / A
    cond_C = strategy.check_cond_C(closes, env_period, env_band_pct)
    cond_A = strategy.check_cond_A(closes, lookback, env_period, env_band_pct)
    if not cond_C["pass"] and not cond_A["pass"]:
        counts["CA"] += 1
        env_upper = cond_C["upper"]
        print(f"  [{i+1:02d}] {name}({code}) ❌ 조건C/A — "
              f"현재가({closes[0]:,.0f}) < Envelope상한({env_upper:,.0f})  "
              f"(괴리 {cond_C['pct_from_upper']:+.1f}%)  "
              f"30봉터치없음")
        continue

    # RSI
    rsi = strategy.calculate_rsi(closes, cfg_entry["rsi_period"])
    if not (cfg_entry["rsi_min"] <= rsi <= cfg_entry["rsi_max"]):
        counts["RSI"] += 1
        print(f"  [{i+1:02d}] {name}({code}) ❌ RSI — "
              f"{rsi} 범위({cfg_entry['rsi_min']}~{cfg_entry['rsi_max']}) 초과")
        continue

    # 거래량 비율
    today_vol = volumes[0] if volumes else 0
    prev_vol  = volumes[1] if len(volumes) > 1 else 1
    vol_ratio = round(today_vol / max(prev_vol, 1), 1)
    if vol_ratio < cfg_scan.get("volume_ratio_min", 1.5):
        counts["VOL"] += 1
        print(f"  [{i+1:02d}] {name}({code}) ❌ 거래량비율 — "
              f"{vol_ratio}배 < {cfg_scan['volume_ratio_min']}배")
        continue

    counts["PASS"] += 1
    e_str = f"Envelope상한:{cond_C['upper']:,.0f}" if cond_C["pass"] else f"A터치:{cond_A['touch_days_ago']}봉전"
    print(f"  [{i+1:02d}] {name}({code}) ✅ 통과 — "
          f"급등:{cond_EF['max_gain']:.1f}%(D-{cond_EF['best_days_ago']})  "
          f"TV:{cond_B['prev_tv']//100_000_000}억  "
          f"RSI:{rsi}  {e_str}")

# ── 요약 ────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("[탈락 원인 요약 (상위 20개 기준)]")
print(f"  조건B (거래대금)   : {counts['B']}개")
print(f"  조건E (급등 9%+)   : {counts['E']}개")
print(f"  조건D (MA3<현재가) : {counts['D']}개")
print(f"  조건C/A (Envelope) : {counts['CA']}개")
print(f"  RSI 범위 초과      : {counts['RSI']}개")
print(f"  거래량 비율 부족   : {counts['VOL']}개")
print(f"  최종 통과          : {counts['PASS']}개")
print("=" * 60)

if counts["PASS"] == 0:
    print("\n⚠️  전체 탈락 — 가능한 원인:")

    # 소스 자체가 0개인 경우 API 응답 키 확인
    if len(src1) == 0 or len(src2) == 0:
        print("\n  [소스 수집 0개 — API 응답 키 직접 확인]")
        if len(src1) == 0:
            print("  소스1(ka10004) 0개 → 응답 키 확인:")
            try:
                raw = broker._post("ka10004", "/api/dostk/rkinfo",
                    {"mrkt_tp":"000","sort_tp":"1","stk_cnd":"20","pric_tp":"0","stex_tp":"3"})
                keys = list(raw.keys())
                print(f"    응답 최상위 키: {keys}")
                for k in keys:
                    if isinstance(raw[k], list) and len(raw[k]) > 0:
                        print(f"    리스트 키 '{k}': {len(raw[k])}개, 첫번째 항목 키: {list(raw[k][0].keys())[:6]}")
            except Exception as e:
                print(f"    ka10004 호출 실패: {e}")

        if len(src2) == 0:
            print("  소스2(ka10005) 0개 → 응답 키 확인:")
            try:
                raw = broker._post("ka10005", "/api/dostk/rkinfo",
                    {"mrkt_tp":"000","sort_tp":"1","stk_cnd":"20","pric_tp":"0","stex_tp":"3"})
                keys = list(raw.keys())
                print(f"    응답 최상위 키: {keys}")
                for k in keys:
                    if isinstance(raw[k], list) and len(raw[k]) > 0:
                        print(f"    리스트 키 '{k}': {len(raw[k])}개, 첫번째 항목 키: {list(raw[k][0].keys())[:6]}")
            except Exception as e:
                print(f"    ka10005 호출 실패: {e}")

    if counts["CA"] > counts["E"] and counts["CA"] > counts["D"]:
        print("  → 조건C/A 탈락이 많음: Envelope(20,20) 기준이 너무 높음")
        print("    완화 방법: /config set scan.envelope_band_pct 30.0")
        print("              /config set scan.envelope_lookback 60")
    if counts["E"] > 3:
        print("  → 조건E 탈락이 많음: 30봉 내 +9% 급등 기준이 너무 높음")
        print("    완화 방법: /config set scan.surge_threshold_e 5.0")
    if counts["B"] > 3:
        print("  → 조건B 탈락이 많음: 거래대금 기준이 너무 높음")
        print(f"    현재: {cfg_scan['min_trading_value']//100_000_000}억")
        print("    완화 방법: /config set scan.min_trading_value 5000000000")
    if counts["D"] > 3:
        print("  → 조건D 탈락이 많음: MA3 > 현재가 종목이 많음 (하락장 징후)")
    print("\n  ※ 모의투자 서버는 일부 API 응답이 실전과 다를 수 있음")
