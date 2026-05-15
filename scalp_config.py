"""
scalp_config.py — 단타 자동매매 전략 설정 관리자
v1.0: strategy_config.py와 동일한 JSON 파일 기반 패턴으로 작성

[설정 파일 위치]
  data/scalp_config.json — 단타 전략 파라미터 (텔레그램으로 실시간 변경 가능)

[핵심 설계 원칙]
  - 종가베팅(strategy_config.py)과 완전히 분리된 독립 설정
  - _deep_merge()로 JSON 누락 필드 자동 보완 (버전 업 시 기존 값 보존)
  - 원자적 쓰기(tmp → replace)로 설정 파일 손상 방지
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR    = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CONFIG_FILE = DATA_DIR / "scalp_config.json"

# ──────────────────────────────────────────────────────────────
# 기본값 — 단타 특화 파라미터
# ──────────────────────────────────────────────────────────────

DEFAULTS: dict[str, Any] = {

    # ── SCAN: 장중 급등 종목 스캐너 조건 ─────────────────────
    "scan": {
        # 상승률 범위 — 갭상승 과열 제외, 상한가 근접 제외
        "min_rise_pct":          3.0,            # 최소 상승률 (%)
        "max_rise_pct":          20.0,           # 최대 상승률 (%) — 이 이상은 추격 금지

        # 유동성 조건 — 장중 급등이라도 거래대금 빈약하면 제외
        "min_trading_value":     5_000_000_000,  # 당일 거래대금 최소 50억원

        # 거래량 폭발 — 단타의 핵심 신호
        "volume_ratio_min":      3.0,            # 전일 대비 거래량 최소 3배
        "volume_ratio_prefer":   5.0,            # 선호 거래량 비율 (점수화 기준)

        # 주가 범위
        "min_price":             1_000,          # 최소 주가 1,000원 (동전주 제외)
        "max_price":             300_000,        # 최대 주가 30만원 (고가주 슬리피지)

        # 스캔 타이밍
        "scan_interval_sec":     30,             # 종목 스캔 주기 (초)
        "entry_end_time":        "14:30",        # 신규 진입 마감 — 14:30 이후는 청산만

        # API 호출 제한
        "api_delay_sec":         0.5,            # 종목당 API 호출 간격 (초) — 모의서버 0.5 권장
        "max_candidates":        30,             # 스캔 대상 최대 종목 수

        # 제외 조건
        "exclude_already_held":  True,           # 이미 보유 중인 종목 제외
        "exclude_upper_limit":   True,           # 상한가(+29.9%) 이상 제외
    },

    # ── ENTRY: 진입 조건 ───────────────────────────────────────
    "entry": {
        "max_positions":         3,              # 최대 동시 보유 종목 수
        "position_size_pct":     20,             # 종목당 투자 비율 (가용 현금의 %)

        # VWAP 필터 — 가격이 VWAP 이상일 때만 진입 (상승 모멘텀 확인)
        "use_vwap_filter":       True,           # True: 현재가 > VWAP 조건 추가
        "vwap_margin_pct":       0.0,            # VWAP 대비 최소 % (0 = 같거나 이상)

        # 동일 종목 재진입 쿨다운
        "cooldown_sec":          300,            # 동일 종목 매도 후 5분간 재진입 금지

        # 장 초반 가중치 — 09:00~09:30 매수 신호는 신뢰도 높음
        "early_bird_end_time":   "09:30",        # 장 초반 기준 시각
        "early_bird_bonus":      10,             # 초반 진입 시 점수 보너스
    },

    # ── EXIT: 청산 조건 ────────────────────────────────────────
    "exit": {
        # 익절 설정
        "take_profit_pct":       2.5,            # 1차 전량 익절선 (%)
        "partial_profit_pct":    1.5,            # 부분 익절선 (%) — partial_ratio만큼
        "partial_ratio":         50,             # 부분 익절 비율 (%)

        # 손절 설정 — 단타는 손절이 빨라야 함
        "stop_loss_pct":         -1.5,           # 손절선 (%)
        "stop_loss_time":        "15:20",        # 이 시간 이후는 수익/손실 무관 손절 가능

        # 트레일링 스탑 — 수익을 지키는 핵심 장치
        "trailing_stop":         True,           # 트레일링 스탑 활성화
        "trailing_gap_pct":      1.0,            # 장중 고점 대비 하락 허용 폭 (%)
        "trailing_activate_pct": 1.0,            # 이 수익률 이상 달성 시 트레일링 활성화

        # 강제 청산 — 당일 종가 전 무조건 청산 (단타의 철칙)
        "force_exit_time":       "15:20",        # 전량 강제 청산 시각
        "force_exit_warn_time":  "15:10",        # 강제청산 경고 알림 시각

        # 시간 손절 — 일정 시간 이상 보유 후 수익 없으면 청산
        "time_stop_minutes":     60,             # 매수 후 N분 경과 시 손익 무관 청산
        "time_stop_min_profit":  0.0,            # 시간 손절 시 최소 수익률 조건 (0 = 손실도 청산)
    },

    # ── RISK: 계좌 전체 리스크 관리 ──────────────────────────
    "risk": {
        # 일일 손실 한도
        "daily_loss_limit_pct":  -3.0,
        "daily_loss_hard_stop":  True,

        # 연속 손절 방지
        "max_consecutive_loss":  3,
        "consecutive_loss_cooldown_min": 30,

        # 포지션 크기 안전장치
        "max_single_position_pct": 30,
        "reserve_cash_pct":      10,

        # [v1.1] 거래 비용 설정 (세금 + 수수료)
        "commission_rate":       0.00015,   # 수수료 0.015% (키움 기준)
        "tax_rate_kosdaq":       0.0018,    # 코스닥 거래세 0.18%
        "tax_rate_kospi":        0.0018,    # 코스피 거래세 0.18%
        # 비용 포함 실질 손절/익절 (참고용)
        # 손절 -1.5% → 실질 -1.71%
        # 익절 +2.5% → 실질 +2.29%

        # [v1.1] 손절 후 재진입 차단
        "blacklist_on_stoploss": True,      # 손절 종목 당일 블랙리스트 등록
    },

    "meta": {
        "version":       "1.0",
        "description":   "단타 자동매매 전략 설정",
        "last_modified": "",
    },
}

# 파라미터 설명 — 텔레그램 /scalp_config show 에서 사용
PARAM_DESCRIPTIONS = {
    "scan.min_rise_pct":          ("최소 상승률 (%)",            "실수",       "3.0"),
    "scan.max_rise_pct":          ("최대 상승률 (%)",            "실수",       "20.0"),
    "scan.min_trading_value":     ("당일 거래대금 최소 (원)",     "정수",       "5000000000"),
    "scan.volume_ratio_min":      ("거래량 비율 최소 (배)",       "실수",       "3.0"),
    "scan.min_price":             ("최소 주가 (원)",              "정수",       "1000"),
    "scan.max_price":             ("최대 주가 (원)",              "정수",       "300000"),
    "scan.scan_interval_sec":     ("스캔 주기 (초)",              "정수",       "30"),
    "scan.entry_end_time":        ("신규 진입 마감",              "HH:MM",      "13:00"),
    "scan.api_delay_sec":         ("API 호출 간격 (초) — 모의:0.5, 실전:0.3",  "실수",       "0.5"),
    "scan.max_candidates":        ("스캔 대상 종목 수",           "정수",       "30"),
    "entry.max_positions":        ("최대 동시 보유 종목",         "정수",       "3"),
    "entry.position_size_pct":    ("종목당 투자 비율 (%)",        "정수",       "20"),
    "entry.use_vwap_filter":      ("VWAP 필터 사용",             "true/false", "true"),
    "entry.cooldown_sec":         ("재진입 쿨다운 (초)",          "정수",       "300"),
    "exit.take_profit_pct":       ("전량 익절선 (%)",            "실수",       "2.5"),
    "exit.stop_loss_pct":         ("손절선 (%)",                 "실수 음수",  "-1.5"),
    "exit.trailing_stop":         ("트레일링 스탑",              "true/false", "true"),
    "exit.trailing_gap_pct":      ("트레일링 간격 (%)",          "실수",       "1.0"),
    "exit.force_exit_time":       ("강제 청산 시각",             "HH:MM",      "15:20"),
    "exit.time_stop_minutes":     ("시간 손절 (분)",             "정수",       "60"),
    "risk.daily_loss_limit_pct":  ("일일 손실 한도 (%)",         "실수 음수",  "-3.0"),
    "risk.max_consecutive_loss":  ("연속 손절 허용 횟수",         "정수",       "3"),
    "risk.reserve_cash_pct":      ("현금 보유 비율 (%)",         "정수",       "10"),
}


# ──────────────────────────────────────────────────────────────
# ScalpConfig 클래스 — StrategyConfig와 동일한 인터페이스
# ──────────────────────────────────────────────────────────────

class ScalpConfig:

    def __init__(self):
        self._ensure_file()

    def _ensure_file(self):
        if not CONFIG_FILE.exists():
            self._write(DEFAULTS)
            logger.info("[ScalpConfig] 기본 설정 파일 생성")

    def _read(self) -> dict:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return self._deep_merge(DEFAULTS, data)
        except Exception as e:
            logger.warning(f"[ScalpConfig] 설정 읽기 실패, 기본값 사용: {e}")
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
            logger.error(f"[ScalpConfig] 쓰기 실패: {e}")
            return False

    def _deep_merge(self, base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = self._deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    # ── 그룹별 조회 ──────────────────────────────────────────
    def get_scan(self)  -> dict: return self._read()["scan"]
    def get_entry(self) -> dict: return self._read()["entry"]
    def get_exit(self)  -> dict: return self._read()["exit"]
    def get_risk(self)  -> dict: return self._read()["risk"]
    def get_all(self)   -> dict: return self._read()

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
            logger.info(f"[ScalpConfig] {key} = {value}")
        return ok

    def reset_to_defaults(self) -> bool:
        logger.info("[ScalpConfig] 기본값으로 초기화")
        return self._write(DEFAULTS)

    def format_for_telegram(self, group: str = "all") -> str:
        data   = self._read()
        lines  = ["<b>[ 단타 전략 설정 ]</b>\n"]
        groups = [group] if group != "all" else ["scan", "entry", "exit", "risk"]
        names  = {"scan": "스캔 조건", "entry": "진입 조건",
                  "exit": "청산 조건", "risk": "리스크 관리"}
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
                lines.append(
                    f"  <code>{full_key}</code>  {desc}: <b>{display}</b>"
                )
            lines.append("")
        last = data.get("meta", {}).get("last_modified", "")
        if last:
            lines.append(f"<i>최종 수정: {last}</i>")
        return "\n".join(lines)

    def format_help(self) -> str:
        lines = [
            "<b>[ 단타 설정 변경 명령어 ]</b>\n",
            "/scalp_config show          — 전체 설정",
            "/scalp_config show scan     — 스캔 조건",
            "/scalp_config show entry    — 진입 조건",
            "/scalp_config show exit     — 청산 조건",
            "/scalp_config show risk     — 리스크",
            "/scalp_config set [키] [값] — 변경 (즉시 반영)",
            "/scalp_config reset         — 기본값 초기화\n",
            "<b>주요 파라미터:</b>",
        ]
        for key, (desc, typ, default) in PARAM_DESCRIPTIONS.items():
            lines.append(f"  <code>{key}</code> {desc} [{typ}] 기본:{default}")
        return "\n".join(lines)
