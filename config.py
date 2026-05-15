"""
config.py — 키움 국내주식 자동매매 설정 및 데이터 관리
모든 설정/장부를 JSON 파일로 읽고 쓰는 데이터 관리자

[관리 파일 목록] data/ 폴더
  active_tickers.json  — 운용 중인 종목 코드 목록
  seed_config.json     — 종목별 시드머니 (원)
  split_config.json    — 종목별 분할 횟수
  profit_config.json   — 종목별 목표 수익률 (%)
  compound_config.json — 종목별 복리 적용 비율 (%)
  trade_locks.json     — 당일 매매 잠금 상태
  manual_ledger.json   — 현재 진행 중인 가상 장부
  manual_history.json  — 졸업(완료)된 사이클 기록
"""

import json
import os
import tempfile
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ──────────────────────────────────────────────────────────────
# 원자적 JSON 읽기 / 쓰기 (파일 손상 방지)
# ──────────────────────────────────────────────────────────────

def _read_json(path: Path, default):
    """JSON 파일 읽기 — 없거나 손상 시 default 반환"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[Config] JSON 읽기 실패 {path.name}: {e}")
        return default


def _write_json(path: Path, data) -> bool:
    """JSON 원자적 쓰기 — tmp 파일 → rename (데이터 무결성 보장)"""
    try:
        dir_name = str(path.parent)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
        return True
    except Exception as e:
        logger.error(f"[Config] JSON 쓰기 실패 {path.name}: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# Config 클래스
# ──────────────────────────────────────────────────────────────

class Config:

    # ── 파일 경로 ──────────────────────────────────────────────
    F_TICKERS  = DATA_DIR / "active_tickers.json"
    F_SEED     = DATA_DIR / "seed_config.json"
    F_SPLIT    = DATA_DIR / "split_config.json"
    F_PROFIT   = DATA_DIR / "profit_config.json"
    F_COMPOUND = DATA_DIR / "compound_config.json"
    F_LOCKS    = DATA_DIR / "trade_locks.json"
    F_LEDGER   = DATA_DIR / "manual_ledger.json"
    F_HISTORY  = DATA_DIR / "manual_history.json"

    # ── 기본값 ────────────────────────────────────────────────
    DEFAULT_SEED     = 1_000_000   # 100만원
    DEFAULT_SPLIT    = 20          # 20분할
    DEFAULT_PROFIT   = 5.0         # 목표 수익률 5%
    DEFAULT_COMPOUND = 70          # 복리 70%

    def __init__(self):
        self._ensure_defaults()

    def _ensure_defaults(self):
        """초기 실행 시 기본값 파일 생성"""
        if not self.F_TICKERS.exists():
            _write_json(self.F_TICKERS, ["005930"])     # 삼성전자로 시작
            logger.info("[Config] 기본 종목(삼성전자) 설정 완료")

    # ── 종목 관리 ─────────────────────────────────────────────

    def get_active_tickers(self) -> list[str]:
        return _read_json(self.F_TICKERS, ["005930"])

    def set_active_tickers(self, tickers: list[str]) -> bool:
        return _write_json(self.F_TICKERS, tickers)

    # ── 시드머니 ──────────────────────────────────────────────

    def get_seed(self, ticker: str) -> int:
        data = _read_json(self.F_SEED, {})
        return int(data.get(ticker, self.DEFAULT_SEED))

    def set_seed(self, ticker: str, amount: int) -> bool:
        data = _read_json(self.F_SEED, {})
        data[ticker] = amount
        logger.info(f"[Config] {ticker} 시드머니 설정: {amount:,}원")
        return _write_json(self.F_SEED, data)

    # ── 분할 횟수 ─────────────────────────────────────────────

    def get_split_count(self, ticker: str) -> int:
        data = _read_json(self.F_SPLIT, {})
        return int(data.get(ticker, self.DEFAULT_SPLIT))

    def set_split_count(self, ticker: str, count: int) -> bool:
        data = _read_json(self.F_SPLIT, {})
        data[ticker] = count
        logger.info(f"[Config] {ticker} 분할 설정: {count}회")
        return _write_json(self.F_SPLIT, data)

    # ── 목표 수익률 ───────────────────────────────────────────

    def get_target_profit(self, ticker: str) -> float:
        data = _read_json(self.F_PROFIT, {})
        return float(data.get(ticker, self.DEFAULT_PROFIT))

    def set_target_profit(self, ticker: str, pct: float) -> bool:
        data = _read_json(self.F_PROFIT, {})
        data[ticker] = pct
        logger.info(f"[Config] {ticker} 목표 수익률 설정: {pct}%")
        return _write_json(self.F_PROFIT, data)

    # ── 복리 비율 ─────────────────────────────────────────────

    def get_compound_rate(self, ticker: str) -> int:
        data = _read_json(self.F_COMPOUND, {})
        return int(data.get(ticker, self.DEFAULT_COMPOUND))

    def set_compound_rate(self, ticker: str, rate: int) -> bool:
        data = _read_json(self.F_COMPOUND, {})
        data[ticker] = rate
        logger.info(f"[Config] {ticker} 복리 비율 설정: {rate}%")
        return _write_json(self.F_COMPOUND, data)

    # ── 매매 잠금 ─────────────────────────────────────────────

    def check_lock(self, ticker: str) -> bool:
        """오늘 이미 주문했으면 True"""
        data  = _read_json(self.F_LOCKS, {})
        today = datetime.now().strftime("%Y-%m-%d")
        lock  = data.get(ticker, {})
        return lock.get("date") == today and lock.get("locked", False)

    def set_lock(self, ticker: str) -> bool:
        """당일 주문 완료 잠금"""
        data  = _read_json(self.F_LOCKS, {})
        today = datetime.now().strftime("%Y-%m-%d")
        data[ticker] = {"date": today, "locked": True}
        logger.info(f"[Config] {ticker} 당일 주문 잠금")
        return _write_json(self.F_LOCKS, data)

    def release_lock(self, ticker: str) -> bool:
        """잠금 해제 (수동 또는 자동 초기화)"""
        data = _read_json(self.F_LOCKS, {})
        if ticker in data:
            data[ticker]["locked"] = False
        logger.info(f"[Config] {ticker} 잠금 해제")
        return _write_json(self.F_LOCKS, data)

    def release_all_locks(self) -> bool:
        """전체 잠금 해제 (장 마감 후 일괄 초기화)"""
        data = _read_json(self.F_LOCKS, {})
        for t in data:
            data[t]["locked"] = False
        logger.info("[Config] 전체 잠금 해제 완료")
        return _write_json(self.F_LOCKS, data)

    # ── 가상 장부 (매매 기록) ─────────────────────────────────

    def get_ledger(self, ticker: str) -> list[dict]:
        """ticker의 현재 매매 기록 반환"""
        data = _read_json(self.F_LEDGER, {})
        return data.get(ticker, [])

    def add_ledger_record(self, ticker: str, side: str,
                          price: int, qty: int) -> bool:
        """
        매매 기록 추가
        side: "BUY" or "SELL"
        """
        data    = _read_json(self.F_LEDGER, {})
        records = data.get(ticker, [])
        records.append({
            "date":  datetime.now().strftime("%Y-%m-%d"),
            "side":  side,
            "price": price,
            "qty":   qty,
        })
        data[ticker] = records
        logger.info(f"[Config] 장부 기록: {ticker} {side} {qty}주 @{price:,}원")
        return _write_json(self.F_LEDGER, data)

    def clear_ledger(self, ticker: str) -> bool:
        """졸업 후 장부 초기화"""
        data = _read_json(self.F_LEDGER, {})
        data[ticker] = []
        logger.info(f"[Config] {ticker} 장부 초기화")
        return _write_json(self.F_LEDGER, data)

    def get_position(self, ticker: str) -> dict:
        """
        장부 기반 포지션 계산
        반환: {"qty": 보유수량, "avg_price": 평균단가, "invested": 총투자금}
        """
        records = self.get_ledger(ticker)
        qty      = 0
        invested = 0

        for r in records:
            if r["side"] == "BUY":
                qty      += r["qty"]
                invested += r["price"] * r["qty"]
            elif r["side"] == "SELL":
                qty      -= r["qty"]
                sell_cost = (invested / max(qty + r["qty"], 1)) * r["qty"]
                invested  -= sell_cost

        avg_price = int(invested / qty) if qty > 0 else 0
        return {"qty": qty, "avg_price": avg_price, "invested": int(invested)}

    # ── 졸업 히스토리 ─────────────────────────────────────────

    def add_history(self, ticker: str, profit: int,
                    profit_pct: float, invested: int, revenue: int) -> bool:
        """졸업(목표 수익 달성) 기록 저장"""
        history = _read_json(self.F_HISTORY, [])
        history.append({
            "ticker":     ticker,
            "date":       datetime.now().strftime("%Y-%m-%d"),
            "profit":     profit,
            "profit_pct": profit_pct,
            "invested":   invested,
            "revenue":    revenue,
        })
        logger.info(f"[Config] {ticker} 졸업! 수익: {profit:,}원 ({profit_pct:.2f}%)")
        return _write_json(self.F_HISTORY, history)

    def get_history(self) -> list[dict]:
        return _read_json(self.F_HISTORY, [])

    # ── 요약 정보 ─────────────────────────────────────────────

    def summary(self) -> str:
        """현재 설정 요약 출력 (디버그용)"""
        tickers = self.get_active_tickers()
        lines   = ["[Config 요약]"]
        for t in tickers:
            lines.append(
                f"  {t} | 시드:{self.get_seed(t):,}원 | "
                f"분할:{self.get_split_count(t)}회 | "
                f"목표:{self.get_target_profit(t)}% | "
                f"복리:{self.get_compound_rate(t)}% | "
                f"잠금:{'ON' if self.check_lock(t) else 'OFF'}"
            )
        return "\n".join(lines)
