import sys, logging
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '.')
from broker import KiwoomBroker
from strategy import Strategy
from strategy_config import StrategyConfig

broker   = KiwoomBroker()
strategy = Strategy(broker, StrategyConfig())

src1 = strategy._source_price_rise()
src2 = strategy._source_trading_value()
src3 = strategy._source_volume_surge()

all_codes = {i['code']: i['name'] for i in src1 + src2 + src3}

targets = {
    "307950": "현대오토에버",
    "319400": "현대무벡스",
    "094360": "엑스게이트",
    "009470": "한솔테크닉스",
    "000500": "가온전선",
}

print(f"소스1(등락률기준): {len(src1)}개")
print(f"소스2(거래대금기준): {len(src2)}개")
print(f"소스3(거래량급증): {len(src3)}개")
print(f"총 풀: {len(all_codes)}개")
print()

for code, name in targets.items():
    status = "✅ 풀 포함" if code in all_codes else "❌ 풀 누락"
    print(f"  {status}  {name}({code})")
