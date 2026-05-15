"""
diagnose_source.py — 후보 풀 누락 종목 진단

키움 조건검색에 나오는 종목이 우리 후보 풀에 있는지 확인하고
없다면 어느 API로 가져올 수 있는지 분석합니다.
"""

import sys, time, logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

from broker import KiwoomBroker
from strategy import Strategy
from strategy_config import StrategyConfig

broker   = KiwoomBroker()
scfg     = StrategyConfig()
strategy = Strategy(broker, scfg)

# ── 키움 조건검색에 실제로 나온 종목 (이미지 기준) ──────────
KIWOOM_RESULTS = {
    "008350": "남선알미늄",
    "010160": "대한광통신",   # 코드 확인 필요
    "088800": "에이스테크",
    "490470": "세미파이브",
    "115440": "우리넷",
    "340440": "세림B&G",
    "452300": "캡스톤파트너스",
    "010170": "대한광통신",
    "025860": "남해화학",
    "018470": "조일알미늄",
    "001780": "알루코",
    "032820": "우리기술",      # 우리로 가능성
    "082640": "머큐리",
    "064260": "다날",
    "059120": "iNtRON",
    "064480": "SNT에너지",
}

print("=" * 65)
print("후보 풀 소스 누락 종목 진단")
print("=" * 65)

# ── 현재 후보 풀 수집 ───────────────────────────────────────
print("\n[1] 현재 소스(VOL_SURGE + VOL_RENEW) 후보 풀 수집 중...")
pool = {}
for item in (
    strategy._source_volume_surge() +
    strategy._source_volume_renew()
):
    code = item["code"]
    if code and code not in pool:
        pool[code] = item

print(f"    수집된 후보 풀: {len(pool)}개")
print(f"    (ka10023 거래량급증 + ka10024 거래량갱신)")

# ── 키움 조건검색 종목 vs 후보 풀 비교 ─────────────────────
print("\n[2] 키움 조건검색 종목 — 후보 풀 포함 여부")
print("-" * 65)
missing = []
for code, name in KIWOOM_RESULTS.items():
    in_pool = code in pool
    status  = "✅ 풀 포함" if in_pool else "❌ 풀 누락"
    print(f"  {status}  {name}({code})")
    if not in_pool:
        missing.append((code, name))

print(f"\n  → 총 {len(KIWOOM_RESULTS)}개 중 {len(missing)}개 누락")

# ── 누락 종목 일봉 데이터로 조건 직접 확인 ─────────────────
if missing:
    print("\n[3] 누락 종목 조건 직접 확인 (일봉 조회)")
    print("-" * 65)
    cfg_scan  = scfg.get_scan()
    cfg_entry = scfg.get_entry()

    for code, name in missing[:5]:   # 상위 5개만
        time.sleep(0.5)
        daily = strategy.get_daily_data(code)
        if not daily:
            print(f"  {name}({code}): 일봉 데이터 없음")
            continue

        closes         = daily.get("closes", [])
        trading_values = daily.get("trading_values", [])
        env_period     = cfg_scan.get("envelope_period", 20)
        env_band_pct   = cfg_scan.get("envelope_band_pct", 20.0)
        lookback       = cfg_scan.get("envelope_lookback", 30)

        cond_B  = strategy.check_cond_B(trading_values, lookback)
        cond_EF = strategy.check_cond_EF(closes, lookback,
                                          cfg_scan.get("surge_threshold_e", 9.0),
                                          cfg_scan.get("surge_threshold_f", 29.5))
        cond_D  = strategy.check_cond_D(daily)
        cond_C  = strategy.check_cond_C(closes, env_period, env_band_pct)
        cond_A  = strategy.check_cond_A(closes, lookback, env_period, env_band_pct)

        b = "✅" if cond_B["pass"]  else f"❌({cond_B['prev_tv']//100_000_000}억)"
        e = "✅" if cond_EF["pass_E"] else f"❌({cond_EF['max_gain']:.1f}%)"
        d = "✅" if cond_D["pass"]  else f"❌(MA3>{closes[0]:,.0f})"
        ca= "✅" if (cond_C["pass"] or cond_A["pass"]) else f"❌(상한:{cond_C['upper']:,.0f})"

        print(f"  {name}({code}): B={b} E={e} D={d} C/A={ca}")
        if cond_B["pass"] and cond_EF["pass_E"] and cond_D["pass"] and (cond_C["pass"] or cond_A["pass"]):
            print(f"    → 조건 모두 통과! 후보 풀에만 없는 것")

# ── 결론 ────────────────────────────────────────────────────
print("\n" + "=" * 65)
print("[결론]")
print(f"  현재 소스(ka10023/ka10024)는 '오늘 거래량 급증/갱신' 종목만")
print(f"  수집합니다. 키움 조건검색 대상은 '전체 종목'이므로")
print(f"  거래량이 급증하지 않은 종목은 후보 풀에 아예 안 들어옵니다.")
print()
print(f"  해결책: ka10023 대신 전체 종목 리스트 API를 소스로 사용")
print(f"  → ka10001(주식기본정보) 순회 또는")
print(f"  → ka10024 파라미터 완화(trde_qty_tp: 0 = 수량 무관)")
print("=" * 65)
