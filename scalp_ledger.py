"""
scalp_ledger.py — 단타 매매 장부 분석기
v1.1: scalp_daily_log.json 직접 읽기 (오늘 포함 과거 데이터 모두 조회)

[데이터 소스 우선순위]
  1순위: data/scalp_daily_log.json  — strategy_scalping.py가 매매마다 기록
         형식: {"2026-05-14": [{"time","code","side","qty","price","profit","reason"}, ...]}
  2순위: data/scalp_trades.json     — notify_scalp_sell 시 쌍으로 기록 (신규 배포 후부터)

  → 오늘 매매가 안 보이는 이유: scalp_trades.json은 신규 파일이라 비어있음
  → scalp_daily_log.json에 오늘 것이 모두 있으므로 이걸 읽으면 됨

[BUY/SELL 매칭 방식]
  scalp_daily_log.json은 BUY/SELL이 개별 행으로 저장됨
  → SELL 레코드 기준으로, 직전 BUY를 찾아 짝 매칭
  → profit 필드가 strategy_scalping.py에서 이미 계산됨

[텔레그램 명령어]
  /scalp_summary          — 기간 선택 버튼 메뉴
  /scalp_summary daily    — 오늘 요약
  /scalp_summary weekly   — 이번 주 요약
  /scalp_summary monthly  — 이번 달 요약
  /scalp_summary 20260514 — 특정 날짜 상세
"""

import json
import logging
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

logger   = logging.getLogger(__name__)
KST      = pytz.timezone("Asia/Seoul")
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

DAILY_LOG_FILE = DATA_DIR / "scalp_daily_log.json"   # strategy_scalping 기록
TRADES_FILE    = DATA_DIR / "scalp_trades.json"       # 쌍 매칭 후 저장 (신규)


# ──────────────────────────────────────────────────────────────
# JSON 유틸
# ──────────────────────────────────────────────────────────────

def _read_json(path: Path, default=None):
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[Ledger] {path.name} 읽기 실패: {e}")
        return default


def _write_json(path: Path, data) -> bool:
    try:
        fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
        return True
    except Exception as e:
        logger.error(f"[Ledger] {path.name} 쓰기 실패: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# ScalpLedger
# ──────────────────────────────────────────────────────────────

class ScalpLedger:
    """
    단타 매매 장부 분석기

    [핵심 설계]
    - scalp_daily_log.json (BUY/SELL 개별 행) → 쌍 매칭 → 거래 레코드 생성
    - 과거 데이터도 모두 조회 가능 (날짜 범위 지정)
    - 장부 기록은 notify_scalp_sell 호출 시 scalp_trades.json에 저장
    """

    # ── 핵심: daily_log → 거래 쌍 변환 ────────────────────────

    def _parse_daily_log(self, date_str: str) -> list[dict]:
        """
        scalp_daily_log.json 에서 특정 날짜의 완성된 거래 쌍 추출

        BUY/SELL을 종목별로 스택 매칭:
          BUY  → 스택에 push
          SELL → 스택에서 pop하여 쌍 생성

        Args:
            date_str: "YYYY-MM-DD"

        Returns:
            완성된 거래 레코드 리스트 (SELL 기준 시각 정렬)
        """
        log  = _read_json(DAILY_LOG_FILE, {})
        rows = log.get(date_str, [])

        if not rows:
            return []

        # 종목별 매수 스택 {code: [buy_records...]}
        buy_stack: dict[str, list] = defaultdict(list)
        trades: list[dict] = []
        seq = 0

        for row in rows:
            side = row.get("side", "").upper()
            code = row.get("code", "")
            if not code:
                continue

            if side == "BUY":
                buy_stack[code].append(row)

            elif side == "SELL":
                # 매수 스택에서 꺼내 쌍 생성
                buy_row = buy_stack[code].pop(0) if buy_stack[code] else None

                buy_price  = buy_row["price"] if buy_row else row.get("price", 0)
                buy_time   = buy_row["time"]  if buy_row else "00:00:00"
                sell_price = row["price"]
                qty        = row["qty"]
                profit     = row.get("profit", (sell_price - buy_price) * qty)
                reason     = row.get("reason", "")
                name       = row.get("name", code)   # 이름 없으면 코드 사용

                seq += 1
                trades.append({
                    "id":         f"{date_str.replace('-','')}_{seq:03d}",
                    "date":       date_str,
                    "code":       code,
                    "name":       name if name else code,
                    "buy_price":  buy_price,
                    "sell_price": sell_price,
                    "qty":        qty,
                    "buy_time":   buy_time,
                    "sell_time":  row["time"],
                    "profit_amt": profit,
                    "profit_pct": round(
                        (sell_price - buy_price) / buy_price * 100
                        if buy_price > 0 else 0, 2
                    ),
                    "invest_amt": buy_price * qty,
                    "recv_amt":   sell_price * qty,
                    "reason":     reason,
                    "source":     row.get("source", ""),
                    "score":      row.get("score", 0),
                })

        return trades

    def _get_trades_for_date(self, date_str: str) -> list[dict]:
        """
        특정 날짜 거래 조회 — daily_log 우선, scalp_trades 보완

        scalp_trades.json 에는 deploy 이후 체결분만 있음
        → daily_log 에서 파싱한 게 더 완전함
        → 단, daily_log에 name이 없을 수 있어 scalp_trades의 name으로 보완
        """
        # 1순위: daily_log 파싱
        log_trades = self._parse_daily_log(date_str)

        if log_trades:
            # scalp_trades에서 name 보완 (daily_log에 name 없는 경우)
            stored    = _read_json(TRADES_FILE, {"trades": []})
            name_map  = {
                t["code"]: t["name"]
                for t in stored.get("trades", [])
                if t.get("date") == date_str and t.get("name")
            }
            for t in log_trades:
                if t["name"] == t["code"] and t["code"] in name_map:
                    t["name"] = name_map[t["code"]]
            return log_trades

        # fallback: scalp_trades.json
        stored = _read_json(TRADES_FILE, {"trades": []})
        return [t for t in stored.get("trades", []) if t.get("date") == date_str]

    # ── 날짜 범위 조회 ─────────────────────────────────────────

    def get_trades(self, date_from: str, date_to: str) -> list[dict]:
        """날짜 범위로 전체 거래 조회 (YYYY-MM-DD)"""
        result   = []
        cur_date = datetime.strptime(date_from, "%Y-%m-%d")
        end_date = datetime.strptime(date_to,   "%Y-%m-%d")

        while cur_date <= end_date:
            ds     = cur_date.strftime("%Y-%m-%d")
            trades = self._get_trades_for_date(ds)
            result.extend(trades)
            cur_date += timedelta(days=1)

        return sorted(result, key=lambda x: (x["date"], x["sell_time"]))

    def get_today(self) -> list[dict]:
        today = datetime.now(KST).strftime("%Y-%m-%d")
        return self._get_trades_for_date(today)

    def get_this_week(self) -> list[dict]:
        now = datetime.now(KST)
        mon = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        return self.get_trades(mon, today)

    def get_this_month(self) -> list[dict]:
        now   = datetime.now(KST)
        first = now.replace(day=1).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        return self.get_trades(first, today)

    def get_by_date(self, date_str: str) -> list[dict]:
        """특정 날짜 (YYYYMMDD 또는 YYYY-MM-DD)"""
        if len(date_str) == 8 and date_str.isdigit():
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        return self._get_trades_for_date(date_str)

    # ── 장부 기록 (매도 체결 시 호출) ─────────────────────────

    def record_trade(
        self,
        code: str, name: str,
        buy_price: int, sell_price: int, qty: int,
        buy_time: str, sell_time: str,
        reason: str = "", source: str = "", score: int = 0,
    ) -> dict:
        """
        완성된 거래를 scalp_trades.json에 추가 기록
        (daily_log 와 이중 기록 — 안전망 역할)
        """
        now    = datetime.now(KST)
        date   = now.strftime("%Y-%m-%d")
        data   = _read_json(TRADES_FILE, {"trades": []})
        today_n = len([t for t in data["trades"] if t.get("date") == date])

        profit_amt = (sell_price - buy_price) * qty
        profit_pct = round(
            (sell_price - buy_price) / buy_price * 100
            if buy_price > 0 else 0, 2
        )
        record = {
            "id":         f"{now.strftime('%Y%m%d')}_{today_n+1:03d}",
            "date":       date,
            "code":       code,
            "name":       name,
            "buy_price":  buy_price,
            "sell_price": sell_price,
            "qty":        qty,
            "buy_time":   buy_time,
            "sell_time":  sell_time,
            "profit_amt": profit_amt,
            "profit_pct": profit_pct,
            "invest_amt": buy_price  * qty,
            "recv_amt":   sell_price * qty,
            "reason":     reason,
            "source":     source,
            "score":      score,
        }
        data["trades"].append(record)
        _write_json(TRADES_FILE, data)
        logger.info(
            f"[Ledger] 기록: {name}({code}) "
            f"{profit_amt:+,}원 ({profit_pct:+.2f}%)"
        )
        return record

    # ── 통계 계산 ─────────────────────────────────────────────

    @staticmethod
    def calc_stats(trades: list[dict]) -> dict:
        """거래 목록 → 통계 dict"""
        if not trades:
            return {
                "total": 0, "wins": 0, "losses": 0, "break_even": 0,
                "win_rate": 0.0, "total_profit": 0, "total_invest": 0,
                "total_recv": 0, "avg_profit": 0, "avg_pct": 0.0,
                "profit_sum": 0, "loss_sum": 0, "profit_factor": 0,
                "best": None, "worst": None,
            }

        wins       = [t for t in trades if t["profit_amt"] > 0]
        losses     = [t for t in trades if t["profit_amt"] < 0]
        break_even = [t for t in trades if t["profit_amt"] == 0]

        total_profit  = sum(t["profit_amt"] for t in trades)
        total_invest  = sum(t["invest_amt"] for t in trades)
        total_recv    = sum(t["recv_amt"]   for t in trades)
        profit_sum    = sum(t["profit_amt"] for t in wins)
        loss_sum      = sum(t["profit_amt"] for t in losses)
        avg_pct       = round(sum(t["profit_pct"] for t in trades) / len(trades), 2)
        profit_factor = (
            round(profit_sum / abs(loss_sum), 2) if loss_sum != 0 else 0
        )

        return {
            "total":         len(trades),
            "wins":          len(wins),
            "losses":        len(losses),
            "break_even":    len(break_even),
            "win_rate":      round(len(wins) / len(trades) * 100, 1),
            "total_profit":  total_profit,
            "total_invest":  total_invest,
            "total_recv":    total_recv,
            "avg_profit":    round(total_profit / len(trades)),
            "avg_pct":       avg_pct,
            "profit_sum":    profit_sum,
            "loss_sum":      loss_sum,
            "profit_factor": profit_factor,
            "best":          max(trades, key=lambda x: x["profit_amt"]),
            "worst":         min(trades, key=lambda x: x["profit_amt"]),
        }

    # ── 텔레그램 메시지 포맷 ──────────────────────────────────

    def format_summary(self, period: str = "daily") -> str:
        """기간별 요약"""
        now = datetime.now(KST)

        if period == "daily":
            trades = self.get_today()
            title  = f"오늘  {now.strftime('%Y-%m-%d')}"
        elif period == "weekly":
            trades = self.get_this_week()
            mon    = now - timedelta(days=now.weekday())
            title  = f"이번 주  {mon.strftime('%m/%d')}~{now.strftime('%m/%d')}"
        elif period == "monthly":
            trades = self.get_this_month()
            title  = f"이번 달  {now.strftime('%Y년 %m월')}"
        else:
            return "❓ /scalp_summary [daily|weekly|monthly|날짜]"

        stats = self.calc_stats(trades)

        if stats["total"] == 0:
            return (
                f"📊 <b>[ 단타 요약 — {title} ]</b>\n\n"
                f"⚠️ 기간 내 완료된 매매 없음\n\n"
                f"<i>현재 보유 중인 포지션은 /scalp_status 확인</i>"
            )

        bar     = self._win_bar(stats["win_rate"])
        pl_icon = "🔺" if stats["total_profit"] >= 0 else "🔻"
        pf_str  = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] else "N/A"

        lines = [
            f"📊 <b>[ 단타 요약 — {title} ]</b>\n",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📈 총 거래:  <b>{stats['total']}회</b>  "
            f"(✅{stats['wins']} ❌{stats['losses']} ➡️{stats['break_even']})",
            f"🎯 승률:     <b>{stats['win_rate']}%</b>  {bar}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"{pl_icon} 총 손익:  <b>{stats['total_profit']:+,}원</b>",
            f"💰 투자금:  {stats['total_invest']:,}원",
            f"📥 회수금:  {stats['total_recv']:,}원",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📉 평균 수익률: <b>{stats['avg_pct']:+.2f}%</b>",
            f"💵 평균 손익:   {stats['avg_profit']:+,}원/건",
            f"🏆 수익 합계:  +{stats['profit_sum']:,}원",
            f"💔 손실 합계:  {stats['loss_sum']:,}원",
            f"⚖️ 손익비:     {pf_str}",
        ]

        if stats["best"] and stats["best"]["profit_amt"] > 0:
            b = stats["best"]
            lines.append(
                f"\n🥇 최고:  {b['name']}({b['code']}) "
                f"+{b['profit_amt']:,}원 ({b['profit_pct']:+.2f}%)"
            )
        if stats["worst"] and stats["worst"]["profit_amt"] < 0:
            w = stats["worst"]
            lines.append(
                f"💸 최저:  {w['name']}({w['code']}) "
                f"{w['profit_amt']:,}원 ({w['profit_pct']:+.2f}%)"
            )

        # 주/월간 — 일별 소계 추가
        if period in ("weekly", "monthly"):
            lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"<b>[ 일별 손익 ]</b>")
            by_date: dict[str, list] = {}
            for t in trades:
                by_date.setdefault(t["date"], []).append(t)
            for date in sorted(by_date.keys()):
                ds  = self.calc_stats(by_date[date])
                icon = "🔺" if ds["total_profit"] >= 0 else "🔻"
                dt   = datetime.strptime(date, "%Y-%m-%d")
                wd   = ["월","화","수","목","금","토","일"][dt.weekday()]
                lines.append(
                    f"  <b>{date[5:]}({wd})</b>  "
                    f"{icon}<b>{ds['total_profit']:+,}원</b>  "
                    f"{ds['total']}건 {ds['win_rate']}%"
                )

        lines.append(
            f"\n<i>자세히: /scalp_summary {now.strftime('%Y%m%d')}</i>"
        )
        return "\n".join(lines)

    def format_daily_detail(self, date_str: str) -> list[str]:
        """특정 날짜 종목별 상세 (5건씩 페이지 분할)"""
        trades = self.get_by_date(date_str)
        disp   = (
            f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            if len(date_str) == 8 else date_str
        )

        if not trades:
            return [
                f"📋 <b>{disp}</b>\n\n"
                f"완료된 매매 없음\n\n"
                f"<i>• 매매가 없었거나\n"
                f"• 아직 포지션 보유 중이거나\n"
                f"• 데이터가 아직 기록되지 않은 날입니다</i>"
            ]

        stats   = self.calc_stats(trades)
        bar     = self._win_bar(stats["win_rate"])
        pl_icon = "🔺" if stats["total_profit"] >= 0 else "🔻"

        pages = []
        header = "\n".join([
            f"📋 <b>[ 단타 상세 — {disp} ]</b>\n",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"총 {stats['total']}건  ✅{stats['wins']} ❌{stats['losses']}  "
            f"승률 <b>{stats['win_rate']}%</b>  {bar}",
            f"{pl_icon} 총손익: <b>{stats['total_profit']:+,}원</b>  "
            f"평균: {stats['avg_pct']:+.2f}%",
            f"━━━━━━━━━━━━━━━━━━━━",
        ])
        pages.append(header)

        chunk = []
        for i, t in enumerate(trades, 1):
            icon     = "✅" if t["profit_amt"] > 0 else "❌" if t["profit_amt"] < 0 else "➡️"
            hold_min = self._hold_minutes(t["buy_time"], t["sell_time"])
            hold_str = f"{hold_min}분" if hold_min >= 0 else "-"

            chunk.append(
                f"\n{i}. {icon} <b>{t['name']}({t['code']})</b>\n"
                f"   매수: <b>{t['buy_price']:,}원</b>  {t['buy_time']}\n"
                f"   매도: <b>{t['sell_price']:,}원</b>  {t['sell_time']}\n"
                f"   수량: {t['qty']:,}주  보유: {hold_str}\n"
                f"   수익률: <b>{t['profit_pct']:+.2f}%</b>  "
                f"손익: <b>{t['profit_amt']:+,}원</b>\n"
                f"   투자금: {t['invest_amt']:,}원\n"
                f"   사유: {t['reason'] or '-'}"
            )

            if len(chunk) >= 5 or i == len(trades):
                pages.append("\n".join(chunk))
                chunk = []

        return pages

    def format_monthly_calendar(self) -> str:
        """이번 달 일별 달력"""
        now    = datetime.now(KST)
        first  = now.replace(day=1).strftime("%Y-%m-%d")
        today  = now.strftime("%Y-%m-%d")
        trades = self.get_trades(first, today)

        by_date: dict[str, list] = {}
        for t in trades:
            by_date.setdefault(t["date"], []).append(t)

        total_pnl = 0
        lines = [
            f"🗓 <b>[ {now.strftime('%Y년 %m월')} 매매 달력 ]</b>\n",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        if not by_date:
            lines.append("이번 달 완료된 매매 없음")
            return "\n".join(lines)

        for d in sorted(by_date.keys()):
            st      = self.calc_stats(by_date[d])
            day_pnl = st["total_profit"]
            total_pnl += day_pnl
            icon    = "🔺" if day_pnl >= 0 else "🔻"
            dt_obj  = datetime.strptime(d, "%Y-%m-%d")
            wd      = ["월","화","수","목","금","토","일"][dt_obj.weekday()]
            bar     = self._mini_bar(st["win_rate"])
            lines.append(
                f"  <b>{d[8:]}일({wd})</b>  "
                f"{icon}<b>{day_pnl:+,}원</b>  "
                f"{st['total']}건 {bar} {st['win_rate']}%"
            )

        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(
            f"📊 월 누계: "
            f"{'🔺' if total_pnl >= 0 else '🔻'}"
            f"<b>{total_pnl:+,}원</b>"
        )
        return "\n".join(lines)

    # ── 유틸 ──────────────────────────────────────────────────

    @staticmethod
    def _win_bar(rate: float, w: int = 10) -> str:
        n = round(rate / 100 * w)
        return "█" * n + "░" * (w - n)

    @staticmethod
    def _mini_bar(rate: float, w: int = 5) -> str:
        n = round(rate / 100 * w)
        return "█" * n + "░" * (w - n)

    @staticmethod
    def _hold_minutes(buy_time: str, sell_time: str) -> int:
        try:
            fmt = "%H:%M:%S" if len(buy_time) > 5 else "%H:%M"
            bt  = datetime.strptime(buy_time[:8],  fmt)
            st  = datetime.strptime(sell_time[:8], fmt)
            return max(0, int((st - bt).total_seconds() / 60))
        except Exception:
            return -1
