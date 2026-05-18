"""
performance_tracker.py — 3전략 통합 성과 추적 · 분석 엔진  v1.0
════════════════════════════════════════════════════════════════
역할:
  - 3전략(A 종가베팅 / B 장중단타 / C 상한가선진입) 매매 기록을 저장
  - 일별·주별·월별 성과 집계 및 텔레그램 리포트 생성
  - 알고리즘 개선을 위한 데이터 분석 (조건별 승률, 최적 파라미터 힌트)

저장 구조:
  data/performance/
    ├── trades.json        # 전체 체결 기록 (append-only)
    ├── daily/
    │   └── 20260518.json  # 일별 집계 스냅샷
    ├── weekly/
    │   └── 2026W20.json   # 주별 집계 스냅샷
    └── monthly/
        └── 202605.json    # 월별 집계 스냅샷

사용법:
  tracker = PerformanceTracker()
  tracker.record_trade(trade_dict)          # 매매 기록
  msg = tracker.daily_report()              # 일별 리포트 문자열
  msg = tracker.weekly_report()             # 주별 리포트
  msg = tracker.monthly_report()            # 월별 리포트
  msg = tracker.improvement_hints()         # 알고리즘 개선 힌트
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

# ── 저장 경로 ─────────────────────────────────────────────────
_BASE  = Path("data/performance")
_TRADES_FILE = _BASE / "trades.json"

for _d in [_BASE, _BASE/"daily", _BASE/"weekly", _BASE/"monthly"]:
    _d.mkdir(parents=True, exist_ok=True)

# ── 전략 레이블 ───────────────────────────────────────────────
STRAT_LABEL = {"A": "종가베팅", "B": "장중단타", "C": "상한가선진입"}
STRAT_ICON  = {"A": "📊", "B": "⚡", "C": "🎯"}


# ─────────────────────────────────────────────────────────────
# 핵심 데이터 구조
# ─────────────────────────────────────────────────────────────

class PerformanceTracker:
    """
    3전략 통합 성과 추적기

    trade_dict 필수 키
    ──────────────────
    strategy   : "A" | "B" | "C"
    code       : 종목코드
    name       : 종목명
    side       : "BUY" | "SELL"
    price      : 체결가
    qty        : 체결 수량
    timestamp  : ISO 문자열 (KST)
    pnl        : 손익 (원)  — SELL 에만
    pnl_pct    : 손익률 (%) — SELL 에만
    reason     : 매도 사유 (익절/손절/강제청산/갭매도 등)
    buy_price  : 매수가 — SELL 에만
    score      : 복합점수 (전략C) / 스캔점수 (전략A)
    hold_hours : 보유 시간 (실수)
    """

    def __init__(self) -> None:
        self._trades: list[dict] = self._load_trades()

    # ──────────────────────────────────────────────────────────
    # 데이터 기록
    # ──────────────────────────────────────────────────────────

    def record_trade(self, trade: dict) -> None:
        """매매 체결 1건을 기록한다."""
        if "timestamp" not in trade:
            trade["timestamp"] = datetime.now(KST).isoformat()
        self._trades.append(trade)
        self._save_trades()

    def record_sell(
        self,
        strategy  : str,
        code      : str,
        name      : str,
        buy_price : int,
        sell_price: int,
        qty       : int,
        reason    : str = "",
        score     : float = 0.0,
        hold_hours: float = 0.0,
    ) -> None:
        """SELL 레코드를 편리하게 기록하는 헬퍼."""
        pnl     = (sell_price - buy_price) * qty
        pnl_pct = (sell_price - buy_price) / buy_price * 100 if buy_price > 0 else 0
        self.record_trade({
            "strategy"  : strategy,
            "code"      : code,
            "name"      : name,
            "side"      : "SELL",
            "price"     : sell_price,
            "buy_price" : buy_price,
            "qty"       : qty,
            "pnl"       : pnl,
            "pnl_pct"   : round(pnl_pct, 2),
            "reason"    : reason,
            "score"     : score,
            "hold_hours": round(hold_hours, 2),
            "timestamp" : datetime.now(KST).isoformat(),
        })

    # ──────────────────────────────────────────────────────────
    # 집계 헬퍼
    # ──────────────────────────────────────────────────────────

    def _sell_trades(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        strategy: Optional[str]   = None,
    ) -> list[dict]:
        """조건에 맞는 SELL 체결만 반환."""
        result = []
        for t in self._trades:
            if t.get("side") != "SELL":
                continue
            if strategy and t.get("strategy") != strategy:
                continue
            ts = datetime.fromisoformat(t["timestamp"])
            if ts.tzinfo is None:
                ts = KST.localize(ts)
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            result.append(t)
        return result

    @staticmethod
    def _calc_metrics(trades: list[dict]) -> dict:
        """체결 리스트에서 핵심 지표를 계산한다."""
        if not trades:
            return {
                "count": 0, "win": 0, "lose": 0, "draw": 0,
                "win_rate": 0.0, "total_pnl": 0, "avg_pnl": 0.0,
                "avg_pnl_pct": 0.0, "max_win": 0, "max_lose": 0,
                "profit_factor": 0.0, "avg_hold_hours": 0.0,
            }
        wins  = [t for t in trades if t.get("pnl", 0) > 0]
        loses = [t for t in trades if t.get("pnl", 0) < 0]
        draws = [t for t in trades if t.get("pnl", 0) == 0]

        total_pnl  = sum(t.get("pnl", 0)     for t in trades)
        gross_win  = sum(t.get("pnl", 0)     for t in wins)
        gross_lose = abs(sum(t.get("pnl", 0) for t in loses))

        return {
            "count"         : len(trades),
            "win"           : len(wins),
            "lose"          : len(loses),
            "draw"          : len(draws),
            "win_rate"      : round(len(wins) / len(trades) * 100, 1),
            "total_pnl"     : total_pnl,
            "avg_pnl"       : round(total_pnl / len(trades)),
            "avg_pnl_pct"   : round(sum(t.get("pnl_pct", 0) for t in trades) / len(trades), 2),
            "max_win"       : max((t.get("pnl", 0) for t in trades), default=0),
            "max_lose"      : min((t.get("pnl", 0) for t in trades), default=0),
            "profit_factor" : round(gross_win / gross_lose, 2) if gross_lose > 0 else 0.0,
            "avg_hold_hours": round(
                sum(t.get("hold_hours", 0) for t in trades) / len(trades), 1
            ),
        }

    # ──────────────────────────────────────────────────────────
    # 리포트 생성
    # ──────────────────────────────────────────────────────────

    def daily_report(self, date: Optional[datetime] = None) -> str:
        """일별 성과 리포트."""
        d     = date or datetime.now(KST)
        since = d.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        until = d.replace(hour=23, minute=59, second=59, microsecond=0)
        if since.tzinfo is None:
            since = KST.localize(since)
            until = KST.localize(until)
        return self._build_report(
            since, until,
            title=f"일별 성과 — {d.strftime('%Y-%m-%d (%a)')}",
            period_tag="daily",
            save_key=d.strftime("%Y%m%d"),
        )

    def weekly_report(self, date: Optional[datetime] = None) -> str:
        """주별 성과 리포트 (해당 주 월~금)."""
        d      = date or datetime.now(KST)
        monday = d - timedelta(days=d.weekday())
        since  = monday.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        until  = (monday + timedelta(days=4)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        if since.tzinfo is None:
            since = KST.localize(since)
            until = KST.localize(until)
        week_num = d.strftime("%Y W%V")
        return self._build_report(
            since, until,
            title=f"주별 성과 — {week_num} ({since.strftime('%m/%d')}~{until.strftime('%m/%d')})",
            period_tag="weekly",
            save_key=d.strftime("%YW%V"),
        )

    def monthly_report(self, date: Optional[datetime] = None) -> str:
        """월별 성과 리포트."""
        d     = date or datetime.now(KST)
        since = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # 다음달 1일 - 1초
        if d.month == 12:
            until = d.replace(year=d.year+1, month=1, day=1,
                              hour=0, minute=0, second=0) - timedelta(seconds=1)
        else:
            until = d.replace(month=d.month+1, day=1,
                              hour=0, minute=0, second=0) - timedelta(seconds=1)
        if since.tzinfo is None:
            since = KST.localize(since)
            until = KST.localize(until)
        return self._build_report(
            since, until,
            title=f"월별 성과 — {d.strftime('%Y년 %m월')}",
            period_tag="monthly",
            save_key=d.strftime("%Y%m"),
        )

    def _build_report(
        self,
        since     : datetime,
        until     : datetime,
        title     : str,
        period_tag: str,
        save_key  : str,
    ) -> str:
        """공통 리포트 빌더."""
        all_trades = self._sell_trades(since, until)
        all_m      = self._calc_metrics(all_trades)

        lines = [
            f"<b>[ {title} ]</b>",
            f"<i>{since.strftime('%m/%d %H:%M')} ~ {until.strftime('%m/%d %H:%M')}</i>",
            "",
        ]

        # ── 전략별 섹션 ────────────────────────────────────────
        for strat in ("A", "B", "C"):
            st = self._sell_trades(since, until, strategy=strat)
            m  = self._calc_metrics(st)
            if m["count"] == 0:
                lines.append(
                    f"{STRAT_ICON[strat]} <b>전략{strat} {STRAT_LABEL[strat]}</b>  거래 없음"
                )
                continue

            pnl_sign = "+" if m["total_pnl"] >= 0 else ""
            lines.append(
                f"{STRAT_ICON[strat]} <b>전략{strat} {STRAT_LABEL[strat]}</b>\n"
                f"  거래 {m['count']}회 | 승률 <b>{m['win_rate']}%</b>"
                f" ({m['win']}승 {m['lose']}패)\n"
                f"  총손익 <b>{pnl_sign}{m['total_pnl']:,}원</b>"
                f" | 평균 {pnl_sign}{m['avg_pnl']:,}원\n"
                f"  PF {m['profit_factor']} | 평균보유 {m['avg_hold_hours']}h"
            )

            # 최대 익절/손절 종목
            if st:
                best  = max(st, key=lambda x: x.get("pnl", 0))
                worst = min(st, key=lambda x: x.get("pnl", 0))
                lines.append(
                    f"  🏆 최대익절 {best['name']} {best.get('pnl_pct', 0):+.1f}%"
                    f" ({best.get('pnl', 0):+,}원)\n"
                    f"  💔 최대손절 {worst['name']} {worst.get('pnl_pct', 0):+.1f}%"
                    f" ({worst.get('pnl', 0):+,}원)"
                )
            lines.append("")

        # ── 전략 합계 ──────────────────────────────────────────
        if all_m["count"] > 0:
            pnl_sign = "+" if all_m["total_pnl"] >= 0 else ""
            result_icon = "📈" if all_m["total_pnl"] >= 0 else "📉"
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"{result_icon} <b>3전략 합계</b>\n"
                f"  총 {all_m['count']}회 | 승률 <b>{all_m['win_rate']}%</b>\n"
                f"  총손익 <b>{pnl_sign}{all_m['total_pnl']:,}원</b>\n"
                f"  Profit Factor: <b>{all_m['profit_factor']}</b>",
            ]

        # ── 개선 힌트 요약 (일별에만) ──────────────────────────
        if period_tag == "daily" and all_m["count"] > 0:
            hints = self._quick_hints(since, until)
            if hints:
                lines += ["", "💡 <b>오늘의 개선 힌트</b>"] + hints

        report = "\n".join(lines)

        # ── 스냅샷 저장 ───────────────────────────────────────
        snap = {
            "period"   : period_tag,
            "key"      : save_key,
            "since"    : since.isoformat(),
            "until"    : until.isoformat(),
            "all"      : all_m,
            "by_strat" : {
                s: self._calc_metrics(self._sell_trades(since, until, s))
                for s in ("A", "B", "C")
            },
        }
        snap_path = _BASE / period_tag / f"{save_key}.json"
        snap_path.write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return report

    # ──────────────────────────────────────────────────────────
    # 알고리즘 개선 힌트
    # ──────────────────────────────────────────────────────────

    def _quick_hints(self, since: datetime, until: datetime) -> list[str]:
        """당일 데이터 기반 간단 힌트 (일별 리포트 하단 추가용)."""
        hints = []
        for strat in ("A", "B", "C"):
            st = self._sell_trades(since, until, strategy=strat)
            if len(st) < 2:
                continue
            m = self._calc_metrics(st)

            if m["win_rate"] < 40:
                hints.append(
                    f"  전략{strat}: 승률 {m['win_rate']}% 저조 → "
                    f"진입 기준 강화 검토"
                )
            if m["profit_factor"] < 1.0 and m["count"] >= 3:
                hints.append(
                    f"  전략{strat}: PF {m['profit_factor']} < 1.0 → "
                    f"손절폭 축소 또는 익절폭 확대 검토"
                )
            # 손절 비율 체크
            stop_trades = [t for t in st if "손절" in t.get("reason", "")]
            if stop_trades and len(stop_trades) / len(st) > 0.5:
                hints.append(
                    f"  전략{strat}: 손절 {len(stop_trades)}/{len(st)}회 → "
                    f"진입 타이밍 재검토"
                )
        return hints

    def improvement_hints(
        self,
        days: int = 30,
        strategy: Optional[str] = None,
    ) -> str:
        """
        최근 N일 데이터 기반 알고리즘 개선 제안 리포트.
        전략별 세부 분석: 시간대별·요일별·점수별 승률
        """
        until = datetime.now(KST)
        since = until - timedelta(days=days)

        lines = [
            f"<b>[ 알고리즘 개선 분석 — 최근 {days}일 ]</b>",
            f"<i>{since.strftime('%Y/%m/%d')} ~ {until.strftime('%Y/%m/%d')}</i>",
            "",
        ]

        strats = [strategy] if strategy else ["A", "B", "C"]
        for strat in strats:
            st = self._sell_trades(since, until, strategy=strat)
            if len(st) < 5:
                lines.append(
                    f"{STRAT_ICON[strat]} 전략{strat}: 분석 데이터 부족 ({len(st)}건)"
                )
                continue

            m = self._calc_metrics(st)
            lines.append(
                f"{STRAT_ICON[strat]} <b>전략{strat} {STRAT_LABEL[strat]}</b>  "
                f"{len(st)}건 / 승률 {m['win_rate']}% / PF {m['profit_factor']}"
            )

            # ① 요일별 승률
            by_dow = self._group_by(st, lambda t: datetime.fromisoformat(
                t["timestamp"]).strftime("%a"))
            dow_line = "  요일별: "
            for dow, trades in sorted(by_dow.items()):
                dm = self._calc_metrics(trades)
                dow_line += f"{dow} {dm['win_rate']}%({len(trades)}) "
            lines.append(dow_line.rstrip())

            # ② 시간대별 승률 (전략B)
            if strat == "B":
                by_hour = self._group_by(
                    st,
                    lambda t: f"{datetime.fromisoformat(t['timestamp']).hour:02d}시"
                )
                hour_line = "  시간대: "
                for h, trades in sorted(by_hour.items()):
                    hm = self._calc_metrics(trades)
                    hour_line += f"{h} {hm['win_rate']}%({len(trades)}) "
                lines.append(hour_line.rstrip())

            # ③ 점수 구간별 승률 (전략A·C)
            if strat in ("A", "C"):
                scored = [t for t in st if t.get("score", 0) > 0]
                if scored:
                    by_score = self._group_by(
                        scored,
                        lambda t: f"{int(t.get('score', 0) // 10) * 10}~"
                                  f"{int(t.get('score', 0) // 10) * 10 + 9}점"
                    )
                    sc_line = "  점수구간: "
                    for s_range, trades in sorted(by_score.items()):
                        sm = self._calc_metrics(trades)
                        sc_line += f"{s_range} {sm['win_rate']}%({len(trades)}) "
                    lines.append(sc_line.rstrip())

            # ④ 보유시간별 수익률 (전략A·C)
            if strat in ("A", "C"):
                timed = [t for t in st if t.get("hold_hours", 0) > 0]
                if timed:
                    def hold_bucket(t):
                        h = t.get("hold_hours", 0)
                        if h < 1: return "<1h"
                        if h < 4: return "1~4h"
                        if h < 12: return "4~12h"
                        return "12h~"
                    by_hold = self._group_by(timed, hold_bucket)
                    hold_line = "  보유시간: "
                    for bkt, trades in sorted(by_hold.items()):
                        hm = self._calc_metrics(trades)
                        hold_line += f"{bkt} 승률{hm['win_rate']}%/평균{hm['avg_pnl_pct']:+.1f}% "
                    lines.append(hold_line.rstrip())

            # ⑤ 매도 사유별 집계
            by_reason = self._group_by(st, lambda t: t.get("reason", "기타")[:5])
            reason_line = "  매도사유: "
            for r, trades in sorted(by_reason.items(), key=lambda x: -len(x[1])):
                rm = self._calc_metrics(trades)
                reason_line += f"{r} {len(trades)}건({rm['win_rate']}%) "
            lines.append(reason_line.rstrip())

            # ⑥ 개선 제안
            suggestions = self._suggest(strat, st, m)
            if suggestions:
                lines.append("  ▶ 개선 제안:")
                for s in suggestions:
                    lines.append(f"    {s}")
            lines.append("")

        return "\n".join(lines)

    def _suggest(self, strat: str, trades: list[dict], m: dict) -> list[str]:
        """전략별 개선 제안 생성."""
        suggestions = []

        # 승률 기반
        if m["win_rate"] < 45:
            suggestions.append(
                f"승률 {m['win_rate']}% → 진입점수 기준 +5~10점 상향 고려"
            )
        elif m["win_rate"] > 70:
            suggestions.append(
                f"승률 {m['win_rate']}% 우수 → 포지션 사이즈 +2~3% 확대 고려"
            )

        # Profit Factor 기반
        if 0 < m["profit_factor"] < 1.2:
            suggestions.append(
                f"PF {m['profit_factor']} 낮음 → "
                f"익절폭 +1~2% 확대 또는 손절폭 -1% 축소 고려"
            )

        # 손절 과다
        stops = [t for t in trades if "손절" in t.get("reason", "")]
        if stops and len(stops) / len(trades) > 0.4:
            avg_stop = sum(t.get("pnl_pct", 0) for t in stops) / len(stops)
            suggestions.append(
                f"손절 {len(stops)}/{len(trades)}건 (평균 {avg_stop:.1f}%) → "
                f"진입 타이밍 재검토 또는 손절폭 조정"
            )

        # 전략별 특화 제안
        if strat == "C":
            scored = [t for t in trades if t.get("score", 0) > 0]
            if scored:
                low = [t for t in scored if t.get("score", 0) < 75]
                low_m = self._calc_metrics(low)
                high = [t for t in scored if t.get("score", 0) >= 75]
                high_m = self._calc_metrics(high)
                if low_m["count"] > 0 and high_m["count"] > 0:
                    if high_m["win_rate"] - low_m["win_rate"] > 15:
                        suggestions.append(
                            f"점수 75+ 승률 {high_m['win_rate']}% vs "
                            f"75- 승률 {low_m['win_rate']}% → "
                            f"composite_score_min 75 이상 권장"
                        )

        return suggestions

    # ──────────────────────────────────────────────────────────
    # 유틸
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _group_by(items: list, key_fn) -> dict:
        result = defaultdict(list)
        for item in items:
            try:
                result[key_fn(item)].append(item)
            except Exception:
                result["기타"].append(item)
        return dict(result)

    def _load_trades(self) -> list[dict]:
        if not _TRADES_FILE.exists():
            return []
        try:
            return json.loads(_TRADES_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Tracker] trades.json 로드 실패: {e}")
            return []

    def _save_trades(self) -> None:
        _TRADES_FILE.write_text(
            json.dumps(self._trades[-10000:],  # 최대 1만건 유지
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def total_count(self) -> int:
        return len([t for t in self._trades if t.get("side") == "SELL"])

    def snapshot_path(self, period: str, key: str) -> Path:
        return _BASE / period / f"{key}.json"
