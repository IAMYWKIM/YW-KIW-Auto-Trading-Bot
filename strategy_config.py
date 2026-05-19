"""
strategy_config.py — 종가베팅 전략 조건 설정 관리자
v1.4: 급등일 탐색 범위 확대, 거래대금 기준 완화, 눌림 범위 확대

[v1.3 → v1.4 변경]
  surge_lookback_days: 5 → 10   (저스템·삼아알미늄 등 D-6 이상 급등 종목 포함)
  surge_min_tv: 1000억 → 300억  (소형 급등주 포함 — 저스템 164억, 삼아알미늄 165억)
  surge_min_pct: 20% → 15%      (중간 급등 종목도 포함)
  pullback_max_pct_scan: 0% → 2% (오늘 소폭 상승 중인 종목도 허용)
  min_trading_value: 100억 → 30억 (소형주 포함 — 저스템, 삼아알미늄)
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CONFIG_FILE = DATA_DIR / "strategy_config.json"

DEFAULTS: dict[str, Any] = {

    # ── SCAN: 종목 스캔 조건 ───────────────────────────────────
    "scan": {
        # ▼ v1.3: 500억 → 100억 (다날·보원케미칼·한패스 포함)
        # ▼ v1.4: 100억 → 30억 (저스템·삼아알미늄 등 소형주 포함)
        "min_trading_value":       3_000_000_000,   # 거래대금 최소 30억원 (조건B 기준)
        "volume_ratio_min":        1.5,             # 전일 대비 거래량 최소 150%
        "volume_ratio_prefer":     3.0,             # 선호 거래량 비율
        "use_52w_high":            True,
        "use_recent_high_days":    20,
        # ▼ v1.3: 신고가 허용 범위 확대 (-10% → -20%)
        "near_high_threshold_pct": -20.0,
        # ▼ v1.3: 정배열 MA5>MA20만 (MA60 완화)
        "ma_alignment":            True,
        "ma_short":                5,
        "ma_mid":                  20,
        "ma_long":                 60,
        "min_price":               500,             # 최소 주가 (500원)
        "max_price":               1_000_000,       # 최대 주가
        "exclude_etf":             True,
        "markets":                 ["KRX"],
        # ▼ v1.3: 최근 N일 내 급등 탐지 조건
        "recent_surge_days":       10,              # ▼ v1.4: 5→10일 (D-6 종목 포함)
        "recent_surge_min_pct":    5.0,             # 최근 5일 내 최소 5% 이상 급등일 존재
        # ▼ v1.4: 키움 조건검색식 A~G 파라미터
        "envelope_period":         20,              # Envelope 이동평균 기간 (조건A/C)
        "envelope_band_pct":       20.0,            # Envelope 밴드 폭 % (조건A/C)
        "envelope_lookback":       30,              # 조건A/E/F 탐색 봉 수
        "surge_threshold_e":       9.0,             # 조건E: 최소 급등률 %
        "surge_threshold_f":       29.5,            # 조건F: 가산점 급등률 %
        "cond_g_min_tv":           30_000_000_000, # 조건G: 당일 거래대금 기준 (300억)
        "api_delay_sec":           0.5,            # 일봉 API 호출 간격 (초) — 429 방지
        # ▼ v1.5.0: 급등 후 눌림 전략 파라미터
        # ▼ v1.4: surge_lookback_days 5→10, surge_min_tv 1000억→300억
        "surge_lookback_days":     10,             # ▼ v1.4: 5→10일 (D-6 이상 급등 포함)
        "surge_min_pct":           15.0,           # ▼ v1.4: 20%→15% (중간 급등도 포함)
        "surge_min_tv":            30_000_000_000, # ▼ v1.4: 1000억→300억 (저스템 164억 등 포함)
        "pullback_min_pct_scan":   -5.0,           # 오늘 눌림 최소 (%) — -5% 이상 하락만 허용
        "pullback_max_pct_scan":   2.0,            # ▼ v1.4: 0%→2% (소폭 상승 중도 허용)
    },

    # ── ENTRY: 진입 조건 ───────────────────────────────────────
    "entry": {
        # ▼ v1.3: 눌림 범위 확대 (-5% → -10%, 시장 급락일 대응)
        "pullback_min_pct":        -10.0,
        "pullback_max_pct":        -0.5,
        "entry_start_time":        "15:10",
        "entry_end_time":          "15:20",
        # ▼ v1.3: RSI 상한 80으로 완화 (모멘텀 종목 허용)
        "rsi_min":                 30,
        "rsi_max":                 80,
        "rsi_period":              14,
        "use_institution_buy":     False,           # ▼ v1.3: 수급 조건 OFF (눌림 중엔 기관 매도)
        "use_foreign_buy":         False,
        "max_positions":           3,
        "position_size_pct":       15,
    },

    # ── RISK: 리스크 관리 ──────────────────────────────────────
    "risk": {
        "stop_loss_pct":           -3.0,
        "take_profit_pct":         5.0,
        "partial_sell_pct":        50,
        "trailing_stop":           True,
        "trailing_gap_pct":        2.0,
        "max_hold_days":           1,
        "force_sell_time":         "15:00",
    },

    # ── SELL: 매도 전략 ────────────────────────────────────────
    "sell": {
        "use_nxt_premarket":       True,
        "nxt_gap_target_pct":      2.0,
        "morning_target_pct":      3.0,
        "morning_sell_end":        "10:00",
        "afternoon_cut_time":      "14:00",
        "afternoon_cut_ratio":     50,
        "eod_force_sell":          True,
    },

    "meta": {
        "version":        "1.4",
        "description":    "종가베팅 전략 설정 — 급등일 범위 확대 + 소형주 포함",
        "last_modified":  "",
    },
}

PARAM_DESCRIPTIONS = {
    "scan.min_trading_value":       ("거래대금 최소 (원)", "정수", "10000000000"),
    "scan.volume_ratio_min":        ("거래량 비율 최소 (배)", "실수", "1.5"),
    "scan.near_high_threshold_pct": ("신고가 허용 범위 (%)", "실수 음수", "-20.0"),
    "scan.recent_surge_days":       ("최근 급등 탐지 기간 (일)", "정수", "5"),
    "scan.recent_surge_min_pct":    ("최근 급등 최소 상승률 (%)", "실수", "5.0"),
    "scan.envelope_period":         ("Envelope 이동평균 기간 (조건A/C)", "정수", "20"),
    "scan.envelope_band_pct":       ("Envelope 밴드 폭 (조건A/C, %)", "실수", "20.0"),
    "scan.envelope_lookback":       ("조건A/E/F 탐색 봉 수", "정수", "30"),
    "scan.surge_threshold_e":       ("조건E 최소 급등률 (%)", "실수", "9.0"),
    "scan.surge_threshold_f":       ("조건F 가산점 급등률 (%)", "실수", "29.5"),
    "scan.cond_g_min_tv":           ("조건G 당일 거래대금 기준 (원)", "정수", "30000000000"),
    "scan.api_delay_sec":           ("일봉 API 호출 간격 초 — 429 방지", "실수", "0.5"),
    "scan.surge_lookback_days":     ("급등일 탐색 기간 (일)", "정수", "5"),
    "scan.surge_min_pct":           ("급등 최소 등락률 % (20=급등, 29.5=상한가)", "실수", "20.0"),
    "scan.surge_min_tv":            ("급등일 최소 거래대금 (원, 1000억=100000000000)", "정수", "100000000000"),
    "scan.pullback_min_pct_scan":   ("오늘 눌림 최소 % (-5=5%하락까지 허용)", "실수", "-5.0"),
    "scan.pullback_max_pct_scan":   ("오늘 눌림 최대 % (0=상승중 제외, 1=1%상승도 허용)", "실수", "0.0"),
    "entry.pullback_min_pct":       ("눌림 최소 (%)", "실수 음수", "-10.0"),
    "entry.pullback_max_pct":       ("눌림 최대 (%)", "실수 음수", "-0.5"),
    "entry.entry_start_time":       ("매수 시작 시각", "HH:MM", "15:10"),
    "entry.entry_end_time":         ("매수 마감 시각", "HH:MM", "15:20"),
    "entry.rsi_min":                ("RSI 최솟값", "정수", "30"),
    "entry.rsi_max":                ("RSI 최댓값", "정수", "80"),
    "entry.max_positions":          ("최대 동시 보유 종목", "정수", "3"),
    "entry.position_size_pct":      ("종목당 자산 비율 (%)", "정수", "15"),
    "entry.use_institution_buy":    ("기관 순매수 필터", "true/false", "false"),
    "entry.use_foreign_buy":        ("외국인 순매수 필터", "true/false", "false"),
    "risk.stop_loss_pct":           ("손절선 (%)", "실수 음수", "-3.0"),
    "risk.take_profit_pct":         ("1차 익절선 (%)", "실수", "5.0"),
    "risk.trailing_stop":           ("트레일링 스탑", "true/false", "true"),
    "risk.trailing_gap_pct":        ("트레일링 간격 (%)", "실수", "2.0"),
    "risk.force_sell_time":         ("D+1 강제청산 시각", "HH:MM", "15:00"),
    "sell.use_nxt_premarket":       ("NXT 프리마켓 활용", "true/false", "true"),
    "sell.nxt_gap_target_pct":      ("NXT 갭 즉시매도 기준 (%)", "실수", "2.0"),
    "sell.morning_target_pct":      ("오전 목표 수익률 (%)", "실수", "3.0"),
    "sell.morning_sell_end":        ("오전 매도 마감 시각", "HH:MM", "10:00"),
    "sell.eod_force_sell":          ("당일 강제 청산 여부", "true/false", "true"),
}


class StrategyConfig:

    def __init__(self):
        self._ensure_file()

    def _ensure_file(self):
        if not CONFIG_FILE.exists():
            self._write(DEFAULTS)
            logger.info("[StrategyConfig] 기본 설정 파일 생성")

    def _read(self) -> dict:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return self._deep_merge(DEFAULTS, data)
        except Exception as e:
            logger.warning(f"[StrategyConfig] 설정 읽기 실패, 기본값 사용: {e}")
            return dict(DEFAULTS)

    def _write(self, data: dict) -> bool:
        try:
            fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(CONFIG_FILE))
            return True
        except Exception as e:
            logger.error(f"[StrategyConfig] 쓰기 실패: {e}")
            return False

    def _deep_merge(self, base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = self._deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    def get_scan(self) -> dict:   return self._read()["scan"]
    def get_entry(self) -> dict:  return self._read()["entry"]
    def get_risk(self) -> dict:   return self._read()["risk"]
    def get_sell(self) -> dict:   return self._read()["sell"]
    def get_all(self) -> dict:    return self._read()

    def get(self, key: str) -> Any:
        parts = key.split(".")
        data  = self._read()
        for p in parts:
            if not isinstance(data, dict) or p not in data:
                raise KeyError(f"설정 키 없음: {key}")
            data = data[p]
        return data

    def set(self, key: str, value: Any) -> bool:
        parts = key.split(".")
        data  = self._read()
        node  = data
        for p in parts[:-1]:
            if p not in node or not isinstance(node[p], dict):
                raise KeyError(f"설정 경로 없음: {key}")
            node = node[p]
        if parts[-1] not in node:
            raise KeyError(f"설정 키 없음: {key}")
        orig = node[parts[-1]]
        if isinstance(orig, bool):
            value = str(value).lower() in ("true", "1", "yes")
        elif isinstance(orig, int):
            value = int(value)
        elif isinstance(orig, float):
            value = float(value)
        node[parts[-1]] = value
        from datetime import datetime
        data["meta"]["last_modified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ok = self._write(data)
        if ok:
            logger.info(f"[StrategyConfig] {key} = {value}")
        return ok

    def reset_to_defaults(self) -> bool:
        logger.info("[StrategyConfig] 기본값으로 초기화")
        return self._write(DEFAULTS)

    def format_for_telegram(self, group: str = "all") -> str:
        data   = self._read()
        lines  = ["<b>[ 전략 설정 현황 ]</b>\n"]
        groups = [group] if group != "all" else ["scan", "entry", "risk", "sell"]
        names  = {"scan": "스캔 조건", "entry": "진입 조건",
                  "risk": "리스크 관리", "sell": "매도 전략"}
        for g in groups:
            if g not in data:
                continue
            lines.append(f"<b>[ {names.get(g, g)} ]</b>")
            for k, v in data[g].items():
                full_key = f"{g}.{k}"
                desc     = PARAM_DESCRIPTIONS.get(full_key, (k, "", ""))[0]
                if k == "min_trading_value":
                    display = f"{v // 100_000_000}억원"
                elif isinstance(v, bool):
                    display = "ON" if v else "OFF"
                elif isinstance(v, float) and "pct" in k:
                    display = f"{v:+.1f}%"
                elif isinstance(v, int) and "pct" in k:
                    display = f"{v}%"
                else:
                    display = str(v)
                lines.append(f"  <code>{full_key}</code>  {desc}: <b>{display}</b>")
            lines.append("")
        last = data.get("meta", {}).get("last_modified", "")
        if last:
            lines.append(f"<i>최종 수정: {last}</i>")
        return "\n".join(lines)

    def format_help(self) -> str:
        lines = [
            "<b>[ 전략 설정 변경 ]</b>\n",
            "/config show          — 전체 설정",
            "/config show scan     — 스캔 조건",
            "/config show entry    — 진입 조건",
            "/config show risk     — 리스크",
            "/config show sell     — 매도 조건",
            "/config set [키] [값] — 변경",
            "/config reset         — 기본값 초기화\n",
            "<b>주요 파라미터:</b>",
        ]
        for key, (desc, typ, default) in PARAM_DESCRIPTIONS.items():
            lines.append(f"  <code>{key}</code> {desc} [{typ}] 기본:{default}")
        return "\n".join(lines)
