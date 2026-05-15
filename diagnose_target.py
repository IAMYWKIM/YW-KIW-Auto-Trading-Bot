"""
diagnose_target.py — 특정 종목이 왜 탈락하는지 상세 진단

서버에서 실행: python3 diagnose_target.py
"""
import sys, time, logging
from pathlib import Path
logging.basicConfig(level=logging.WARNING, format="%(message)s")
sys.path.insert(0, str(Path(__file__).parent))

from broker import KiwoomBroker
from strategy import Strategy
from strategy_config import StrategyConfig

broker   = KiwoomBroker()
scfg     = StrategyConfig()
strategy = Strategy(broker, scfg)

# ── 확인할 종목 ──────────────────────────────────────────────
TARGETS = {
    "307950": "현대오토에버",
    "094360": "엑스게이트",
    "009470": "한솔테크닉스",
    "319400": "현대무벡스",
    "000500": "가온전선",
}

cfg_scan  = scfg.get_scan()
cfg_entry = scfg.get_entry()

print("=" * 65)
print("타겟 종목 조건별 탈락 원인 진단")
print("=" * 65)
print(f"\n[현재 설정]")
print(f"  surge_lookback_days  : {cfg_scan.get('surge_lookback_days', 5)}일")
print(f"  surge_min_pct        : {cfg_scan.get('surge_min_pct', 20.0)}%")
print(f"  surge_min_tv         : {cfg_scan.get('surge_min_tv', 100_000_000_000)//100_000_000}억")
print(f"  pullback_min_pct_scan: {cfg_scan.get('pullback_min_pct_scan', -5.0)}%")
print(f"  pullback_max_pct_scan: {cfg_scan.get('pullback_max_pct_scan', 0.0)}%")
print(f"  envelope_band_pct    : {cfg_scan.get('envelope_band_pct', 20.0)}%")
print(f"  min_trading_value    : {cfg_scan.get('min_trading_value', 0)//100_000_000}억")
print()

# v1.5.0 메서드 존재 여부 확인
has_v150 = hasattr(strategy, 'check_recent_surge_strong')
print(f"[전략 버전] v1.5.0 메서드 존재: {'✅ YES' if has_v150 else '❌ NO — 서버에 구버전 배포됨!'}")
print()

for code, name in TARGETS.items():
    print(f"{'─'*65}")
    print(f"[{name}({code})]")
    time.sleep(0.6)

    daily = strategy.get_daily_data(code)
    if not daily:
        print("  ❌ 일봉 데이터 없음")
        continue

    closes         = daily.get("closes", [])
    trading_values = daily.get("trading_values", [])
    ma5            = daily.get("ma5", 0)

    if len(closes) < 2:
        print("  ❌ 일봉 데이터 부족")
        continue

    # 오늘 등락률
    today_pct = round((closes[0] - closes[1]) / closes[1] * 100, 2) if closes[1] > 0 else 0
    print(f"  현재가:    {closes[0]:,.0f}원")
    print(f"  전일종가:  {closes[1]:,.0f}원")
    print(f"  오늘등락률:{today_pct:+.1f}%")
    print(f"  MA5:       {ma5:,.0f}원  (현재가{'>' if closes[0]>ma5 else '<'}MA5: {'✅' if closes[0]>ma5 else '❌'})")
    print()

    # 최근 5일 일봉 상세
    print(f"  [최근 일봉 — closes[0]=오늘, closes[1]=어제]")
    lookback = cfg_scan.get("surge_lookback_days", 5)
    for i in range(min(lookback+1, len(closes)-1)):
        prev = closes[i+1]
        cur  = closes[i]
        tv   = trading_values[i] if i < len(trading_values) else 0
        pct  = round((cur-prev)/prev*100, 2) if prev > 0 else 0
        tv_억 = tv // 100_000_000
        surge_ok = pct >= cfg_scan.get("surge_min_pct", 20.0) and tv >= cfg_scan.get("surge_min_tv", 100_000_000_000)
        flag = "🔥급등" if surge_ok else ("⚠️부족" if pct >= 10 else "")
        print(f"    D-{i}: {cur:>10,.0f}원  {pct:+6.1f}%  {tv_억:>5}억  {flag}")
    print()

    # v1.5.0 메서드로 조건 확인
    if has_v150:
        surge = strategy.check_recent_surge_strong(closes, trading_values)
        pb    = strategy.check_pullback_condition(closes, daily)

        print(f"  [조건1] 급등일 존재: {'✅' if surge['pass'] else '❌'}")
        if surge["pass"]:
            print(f"    → D-{surge['surge_days_ago']}: {surge['surge_pct']:+.1f}% / {surge['surge_tv']}억 {'(상한가)' if surge['is_upper_limit'] else ''}")
        else:
            print(f"    → 최근 {lookback}일 내 {cfg_scan.get('surge_min_pct',20)}%+ / {cfg_scan.get('surge_min_tv',100_000_000_000)//100_000_000}억+ 급등일 없음")

        print(f"  [조건2] 오늘 눌림: {'✅' if pb['pass'] else '❌'}")
        if not pb["pass"]:
            print(f"    → {pb.get('reason', '')}")
        else:
            print(f"    → 오늘{pb['today_pct']:+.1f}% / MA5위✅ / Envelope상한({pb['envelope_upper']:,.0f})위✅")

        # 거래대금
        min_tv   = cfg_scan.get("min_trading_value", 10_000_000_000)
        today_tv = trading_values[0] if trading_values else 0
        prev_tv  = trading_values[1] if len(trading_values) > 1 else 0
        tv_ok    = prev_tv >= min_tv or today_tv >= min_tv
        print(f"  [조건3] 거래대금: {'✅' if tv_ok else '❌'} (전일{prev_tv//100_000_000}억/당일{today_tv//100_000_000}억)")

        overall = surge["pass"] and pb["pass"] and tv_ok
        print(f"\n  최종: {'✅ 통과' if overall else '❌ 탈락'}")
    else:
        # 구버전 — 수동으로 조건 확인
        print(f"  ⚠️  v1.5.0 미배포 — 수동 확인:")
        lookback = cfg_scan.get("surge_lookback_days", 5)
        min_surge = cfg_scan.get("surge_min_pct", 20.0)
        min_tv_s  = cfg_scan.get("surge_min_tv", 100_000_000_000)
        surge_found = False
        for i in range(min(lookback+1, len(closes)-1)):
            prev = closes[i+1]; cur = closes[i]
            tv   = trading_values[i] if i < len(trading_values) else 0
            pct  = round((cur-prev)/prev*100,2) if prev > 0 else 0
            if pct >= min_surge and tv >= min_tv_s:
                print(f"    → 급등일 D-{i}: {pct:+.1f}% / {tv//100_000_000}억 ✅")
                surge_found = True
                break
        if not surge_found:
            print(f"    → 급등일 없음 ❌")

        pb_min = cfg_scan.get("pullback_min_pct_scan", -5.0)
        pb_max = cfg_scan.get("pullback_max_pct_scan", 0.0)
        pb_ok  = pb_min <= today_pct <= pb_max
        ma5_ok = closes[0] > ma5

        envelopes = strategy.calc_envelope(closes,
            cfg_scan.get("envelope_period", 20),
            cfg_scan.get("envelope_band_pct", 20.0))
        env_upper = envelopes[0]["upper"] if envelopes else 0
        env_ok    = closes[0] >= env_upper

        print(f"    → 오늘눌림({pb_min}~{pb_max}%): {today_pct:+.1f}% {'✅' if pb_ok else '❌'}")
        print(f"    → MA5 위: {closes[0]:,.0f} > {ma5:,.0f} {'✅' if ma5_ok else '❌'}")
        print(f"    → Envelope상한({env_upper:,.0f}) 위: {'✅' if env_ok else '❌'}")
        print(f"    ⚠️  strategy.py v1.5.0을 먼저 배포하세요!")

print(f"\n{'='*65}")
print("결론: strategy.py v1.5.0 배포 여부를 먼저 확인하세요.")
print("배포 후 재실행하면 정확한 원인을 알 수 있습니다.")
