"""
scan_logger.py v1.1 — 스캔 결과 로그 저장 및 다음날 결과 업데이트

[저장 파일]
  data/scan_log/YYYYMMDD_HHMMSS_scan.json  — 당일 스캔 결과
  data/scan_log/YYYYMMDD_result.json       — 다음날 결과 업데이트

[v1.1 버그 수정]
  DESIGN2 수정: update_results()에서 candle 날짜 검증 추가
               → 장 시작 전 실행 시 어제 봉을 오늘 결과로 덮어쓰는 문제 방지
  DESIGN3 수정: WIN 판정 로직을 실제 전략에 맞게 개선
               → 종가 기준 외에 오전 고가/시가 갭 기준 WIN 판정 추가
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_DIR = Path("data/scan_log")
LOG_DIR.mkdir(parents=True, exist_ok=True)


class ScanLogger:

    # ──────────────────────────────────────────────────────────
    # 스캔 결과 저장
    # ──────────────────────────────────────────────────────────

    def save_scan(self, candidates: list[dict],
                  config_snapshot: dict = None,
                  kospi_pct: float = 0.0) -> str:
        """
        스캔 결과를 JSON 파일로 저장
        반환: 저장된 파일 경로
        """
        now       = datetime.now()
        date_str  = now.strftime("%Y%m%d")
        time_str  = now.strftime("%H%M%S")
        filename  = LOG_DIR / f"{date_str}_{time_str}_scan.json"

        log_data = {
            "scan_date":        date_str,
            "scan_time":        now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_condition": {
                "kospi_pct": kospi_pct,
                "market_type": self._classify_market(kospi_pct),
            },
            "config_snapshot": config_snapshot or {},
            "total_candidates": len(candidates),
            "candidates": [],
        }

        for c in candidates:
            entry = {
                # 기본 정보
                "code":            c.get("code", ""),
                "name":            c.get("name", ""),
                "cur_price":       c.get("cur_price", 0),
                "scan_type":       c.get("source", c.get("scan_type", "UNKNOWN")),
                "score":           c.get("score", 0),

                # ── 선정 이유 (핵심) ──────────────────────────
                "selection_reason": {
                    # 최근 급등
                    "surge_max_gain":   c.get("surge_max_gain", 0),
                    "surge_days_ago":   c.get("surge_days_ago", -1),

                    # [v1.3.2] 오늘 눌림 — analyze_candidate에서 실제 계산된 값
                    "pullback_pct":     c.get("pullback_pct", 0),
                    "prev_close":       c.get("prev_close", 0),

                    # 거래 강도
                    "trading_value_억": round(c.get("trading_value", 0) / 100_000_000, 1),
                    "volume_ratio":     c.get("volume_ratio", 0),

                    # 기술적 지표
                    "ma5":              c.get("ma5", 0),
                    "ma20":             c.get("ma20", 0),
                    "ma60":             c.get("ma60", 0),
                    "rsi":              c.get("rsi", 0),
                    "pct_from_high":    c.get("pct_from_high", 0),

                    # [v1.3.2] 수급 — 항상 실제 데이터가 수집됨
                    "institution_net":  c.get("institution_net", 0),
                    "foreign_net":      c.get("foreign_net", 0),
                },

                # 다음날 결과 (초기값, 나중에 업데이트)
                "result": {
                    "updated":            False,
                    "next_open_pct":      None,
                    "next_high_pct":      None,
                    "next_close_pct":     None,
                    "nxt_premarket_pct":  None,
                    "outcome":            None,   # WIN / LOSS / NEUTRAL
                    "max_profit_pct":     None,   # 장중 최대 수익률
                    "note":               "",
                },
            }
            log_data["candidates"].append(entry)

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)

        logger.info(f"[ScanLogger] 스캔 로그 저장: {filename} ({len(candidates)}개)")
        return str(filename)

    # ──────────────────────────────────────────────────────────
    # 다음날 결과 업데이트
    # [v1.1] candle 날짜 검증 추가 / WIN 판정 로직 개선
    # ──────────────────────────────────────────────────────────

    def update_results(self, broker, target_date: str = None) -> int:
        """
        전날 스캔 파일들에 다음날 실제 가격 결과 업데이트
        target_date: YYYYMMDD (None이면 어제 날짜)
        반환: 업데이트된 종목 수

        [v1.1 수정]
        - candle[0] 날짜가 실제로 target_date 다음날인지 검증
          → 장 시작 전 09:00 이전 실행 시 어제 봉을 오늘 결과로 오기록하는 버그 방지
        - WIN 판정: 종가 기준 외에 오전 고가/갭 기준 판정 추가
        """
        if target_date is None:
            target_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

        # 다음날 날짜 계산 (결과를 조회할 날짜)
        target_dt   = datetime.strptime(target_date, "%Y%m%d")
        next_dt     = target_dt + timedelta(days=1)
        # 주말 건너뜀
        while next_dt.weekday() >= 5:
            next_dt += timedelta(days=1)
        next_date_str = next_dt.strftime("%Y%m%d")

        # 오늘 날짜가 next_date 이후여야 업데이트 가능
        today_str = datetime.now().strftime("%Y%m%d")
        if today_str < next_date_str:
            logger.warning(
                f"[ScanLogger] 아직 결과 업데이트 불가 "
                f"(target: {target_date}, next: {next_date_str}, today: {today_str})"
            )
            return 0

        scan_files = list(LOG_DIR.glob(f"{target_date}_*_scan.json"))
        if not scan_files:
            logger.info(f"[ScanLogger] {target_date} 스캔 파일 없음")
            return 0

        total_updated = 0

        for scan_file in scan_files:
            with open(scan_file, encoding="utf-8") as f:
                data = json.load(f)

            updated = 0
            for c in data["candidates"]:
                if c["result"]["updated"]:
                    continue
                try:
                    code      = c["code"]
                    buy_price = c["cur_price"]

                    # 다음날 일봉 데이터 조회
                    daily = broker._post(
                        "ka10081", "/api/dostk/chart",
                        {
                            "stk_cd":       code,
                            "base_dt":      next_date_str,   # [v1.1] 다음날 기준으로 조회
                            "upd_stkpc_tp": "1",
                        }
                    )
                    candles = daily.get("stk_dt_pole_chart_qry", [])
                    if not candles:
                        continue

                    # [v1.1] candle[0] 날짜 검증 — 다음날 봉인지 확인
                    latest      = candles[0]
                    candle_date = latest.get("dt", "")  # YYYYMMDD
                    if candle_date and candle_date != next_date_str:
                        logger.debug(
                            f"[ScanLogger] {code} candle 날짜 불일치 "
                            f"(expected:{next_date_str}, got:{candle_date}) — 스킵"
                        )
                        continue

                    open_p  = abs(float(latest.get("open_pric", "0").lstrip("+-") or "0"))
                    high_p  = abs(float(latest.get("high_pric", "0").lstrip("+-") or "0"))
                    close_p = abs(float(latest.get("cur_prc",   "0").lstrip("+-") or "0"))

                    if buy_price <= 0 or open_p <= 0:
                        continue

                    open_pct  = round((open_p  - buy_price) / buy_price * 100, 2)
                    high_pct  = round((high_p  - buy_price) / buy_price * 100, 2)
                    close_pct = round((close_p - buy_price) / buy_price * 100, 2)

                    # [v1.1 DESIGN3 수정] WIN 판정 — 실제 전략에 맞게 개선
                    # 전략: NXT +2% 갭 / 오전 고가 +3% / 종가 +2%
                    outcome = self._judge_outcome(open_pct, high_pct, close_pct)

                    c["result"] = {
                        "updated":           True,
                        "next_open_pct":     open_pct,
                        "next_high_pct":     high_pct,
                        "next_close_pct":    close_pct,
                        "nxt_premarket_pct": None,
                        "outcome":           outcome,
                        "max_profit_pct":    high_pct,
                        "note":              "",
                    }
                    updated += 1

                except Exception as e:
                    logger.warning(f"[ScanLogger] {c['code']} 결과 업데이트 실패: {e}")

            # 파일 저장
            with open(scan_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            total_updated += updated
            logger.info(f"[ScanLogger] {scan_file.name}: {updated}개 업데이트")

        return total_updated

    def _judge_outcome(self, open_pct: float, high_pct: float, close_pct: float) -> str:
        """
        [v1.1] WIN/LOSS/NEUTRAL 판정 — 실제 매도 전략 반영
        전략 우선순위:
          1. 시가 갭 +2% 이상 → NXT 갭 익절 가능 → WIN
          2. 고가 +3% 이상 & 시가 -1% 이내 → 오전 익절 가능 → WIN
          3. 종가 +2% 이상 → 종가 기준 WIN
          4. 고가 +1% 미만 & 종가 -3% 이하 → 확실한 LOSS
          5. 나머지 → NEUTRAL
        """
        # NXT 갭 익절 조건 (시가가 매수가 대비 +2% 이상)
        if open_pct >= 2.0:
            return "WIN"

        # 오전 익절 조건 (고가 +3% 이상 & 시가가 크게 빠지지 않은 경우)
        if high_pct >= 3.0 and open_pct >= -1.0:
            return "WIN"

        # 종가 기준 WIN
        if close_pct >= 2.0:
            return "WIN"

        # 확실한 LOSS (고가도 못 오르고 종가도 -3% 이하)
        if high_pct < 1.0 and close_pct <= -3.0:
            return "LOSS"

        # 종가 기준 LOSS
        if close_pct <= -3.0:
            return "LOSS"

        return "NEUTRAL"

    # ──────────────────────────────────────────────────────────
    # 분석 리포트 생성
    # ──────────────────────────────────────────────────────────

    def generate_report(self, days: int = 7) -> str:
        """
        최근 N일 스캔 결과 분석 리포트 생성
        """
        files = sorted(LOG_DIR.glob("*_scan.json"), reverse=True)
        if not files:
            return "분석할 스캔 데이터 없음"

        all_candidates = []
        for f in files[:days * 3]:  # 하루 여러 번 스캔 가능
            try:
                data = json.load(open(f, encoding="utf-8"))
                for c in data["candidates"]:
                    if c["result"]["updated"]:
                        c["scan_date"]   = data["scan_date"]
                        c["market_type"] = data["market_condition"]["market_type"]
                        c["kospi_pct"]   = data["market_condition"]["kospi_pct"]
                        all_candidates.append(c)
            except Exception:
                continue

        if not all_candidates:
            return "아직 결과가 업데이트된 데이터 없음\n(다음날 /update_results 실행 필요)"

        total    = len(all_candidates)
        wins     = [c for c in all_candidates if c["result"]["outcome"] == "WIN"]
        losses   = [c for c in all_candidates if c["result"]["outcome"] == "LOSS"]
        neutrals = [c for c in all_candidates if c["result"]["outcome"] == "NEUTRAL"]

        win_rate  = len(wins) / total * 100 if total > 0 else 0
        avg_close = sum(c["result"]["next_close_pct"] for c in all_candidates) / total
        avg_high  = sum(c["result"]["next_high_pct"]  for c in all_candidates) / total
        avg_open  = sum(c["result"]["next_open_pct"]  for c in all_candidates) / total

        lines = [
            f"<b>[ 스캔 결과 분석 ({days}일) ]</b>\n",
            f"총 후보: {total}개",
            f"승률: {win_rate:.1f}% ({len(wins)}승/{len(losses)}패/{len(neutrals)}중립)",
            f"평균 시가 수익률: {avg_open:+.2f}%",
            f"평균 고가 수익률: {avg_high:+.2f}%",
            f"평균 종가 수익률: {avg_close:+.2f}%\n",
        ]

        # 조건별 승률 분석
        def win_rate_by_key(key, threshold, op="gte"):
            if op == "gte":
                group = [c for c in all_candidates
                         if c["selection_reason"].get(key, 0) >= threshold]
            else:
                group = [c for c in all_candidates
                         if c["selection_reason"].get(key, 0) <= threshold]
            if not group:
                return None
            w = sum(1 for c in group if c["result"]["outcome"] == "WIN")
            return w / len(group) * 100, len(group)

        lines.append("<b>[ 조건별 승률 ]</b>")

        # 급등 강도별
        for min_gain in [5, 10, 20]:
            r = win_rate_by_key("surge_max_gain", min_gain)
            if r:
                lines.append(f"  급등 {min_gain}%↑: 승률 {r[0]:.1f}% ({r[1]}개)")

        # 거래대금별
        for min_tv in [100, 500, 1000]:
            group = [c for c in all_candidates
                     if c["selection_reason"].get("trading_value_억", 0) >= min_tv]
            if group:
                w = sum(1 for c in group if c["result"]["outcome"] == "WIN")
                lines.append(f"  거래대금 {min_tv}억↑: 승률 {w/len(group)*100:.1f}% ({len(group)}개)")

        # RSI별
        for rsi_max in [50, 60, 70]:
            group = [c for c in all_candidates
                     if c["selection_reason"].get("rsi", 100) <= rsi_max]
            if group:
                w = sum(1 for c in group if c["result"]["outcome"] == "WIN")
                lines.append(f"  RSI {rsi_max}↓: 승률 {w/len(group)*100:.1f}% ({len(group)}개)")

        # 수급별 승률 — [v1.1] 실제 데이터가 수집되므로 의미있는 분석
        lines.append("")
        lines.append("<b>[ 수급별 승률 ]</b>")
        inst_buy = [c for c in all_candidates if c["selection_reason"].get("institution_net", 0) > 0]
        frgn_buy = [c for c in all_candidates if c["selection_reason"].get("foreign_net", 0) > 0]
        if inst_buy:
            w = sum(1 for c in inst_buy if c["result"]["outcome"] == "WIN")
            lines.append(f"  기관 순매수: 승률 {w/len(inst_buy)*100:.1f}% ({len(inst_buy)}개)")
        if frgn_buy:
            w = sum(1 for c in frgn_buy if c["result"]["outcome"] == "WIN")
            lines.append(f"  외인 순매수: 승률 {w/len(frgn_buy)*100:.1f}% ({len(frgn_buy)}개)")

        # 눌림 깊이별 승률 — [v1.1] pullback_pct 실제 데이터로 의미있는 분석
        lines.append("")
        lines.append("<b>[ 눌림 깊이별 승률 ]</b>")
        for pb_max, pb_min in [(-0.5, -2), (-2, -5), (-5, -10), (-10, -15)]:
            group = [c for c in all_candidates
                     if pb_min <= c["selection_reason"].get("pullback_pct", 0) <= pb_max]
            if group:
                w = sum(1 for c in group if c["result"]["outcome"] == "WIN")
                lines.append(
                    f"  눌림 {pb_min}~{pb_max}%: 승률 {w/len(group)*100:.1f}% ({len(group)}개)"
                )

        # 시장 상황별 승률
        lines.append("")
        lines.append("<b>[ 시장 상황별 승률 ]</b>")
        for mtype in ["STRONG_UP", "UP", "FLAT", "DOWN", "CRASH"]:
            group = [c for c in all_candidates if c.get("market_type") == mtype]
            if group:
                w = sum(1 for c in group if c["result"]["outcome"] == "WIN")
                lines.append(f"  {mtype}: 승률 {w/len(group)*100:.1f}% ({len(group)}개)")

        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────
    # 텔레그램용 당일 스캔 요약 포맷
    # ──────────────────────────────────────────────────────────

    def format_candidate_for_telegram(self, c: dict) -> str:
        """후보 종목 1개를 텔레그램 메시지 형식으로 변환"""
        r    = c.get("selection_reason", {})
        name = c.get("name", "")
        code = c.get("code", "")
        score = c.get("score", 0)

        surge_gain = r.get("surge_max_gain", 0)
        surge_days = r.get("surge_days_ago", -1)
        pullback   = r.get("pullback_pct", 0)
        tv         = r.get("trading_value_억", 0)
        rsi        = r.get("rsi", 0)
        pct_high   = r.get("pct_from_high", 0)
        inst       = r.get("institution_net", 0)
        frgn       = r.get("foreign_net", 0)

        days_str = f"D-{surge_days}" if surge_days > 0 else "오늘"
        supply   = ""
        if inst > 0: supply += "기관✅"
        if frgn > 0: supply += " 외인✅"
        if not supply: supply = "수급중립"

        return (
            f"⭐ <b>{name}({code})</b>  점수:{score}\n"
            f"   급등: <b>+{surge_gain:.1f}%</b> ({days_str})  "
            f"눌림: <b>{pullback:+.1f}%</b>\n"
            f"   거래대금:{tv:.0f}억  RSI:{rsi}  "
            f"신고가대비:{pct_high:+.1f}%\n"
            f"   {supply}"
        )

    # ──────────────────────────────────────────────────────────
    # 유틸
    # ──────────────────────────────────────────────────────────

    def _classify_market(self, kospi_pct: float) -> str:
        if kospi_pct >= 1.0:   return "STRONG_UP"
        if kospi_pct >= 0.0:   return "UP"
        if kospi_pct >= -1.0:  return "FLAT"
        if kospi_pct >= -3.0:  return "DOWN"
        return "CRASH"

    def list_scan_files(self, days: int = 7) -> list[str]:
        files = sorted(LOG_DIR.glob("*_scan.json"), reverse=True)
        return [str(f) for f in files[:days * 3]]
