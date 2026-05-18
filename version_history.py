"""
version_history.py — 버전 히스토리  v3.0.0
────────────────────────────────────────────
텔레그램 /version 명령어에서 사용하는 버전 정보 모듈.
"""

CURRENT_VERSION = "3.1.0"

# telegram_bot.py 에서 import 하는 현재 버전 주요 기능 목록
CURRENT_FEATURES = [
    "전략A 종가베팅 — 15:10 눌림 매수 / 익일 익절·손절",
    "전략B 장중단타 — 09:00~13:00 급등 종목 당일 매매",
    "전략C 상한가선진입 — 25%+ 7대 신호 복합분석",
    "포지션 레지스트리 — 3전략 충돌·자금 경합 방지",
    "성과 추적기 — 일/주/월별 리포트 + 알고리즘 개선 분석 ★NEW",
    "피보나치 재진입 (FibReentryManager)",
    "시장 등급 필터 (MarketFilter)",
    "수동 감시 종목 (Watchlist)",
    "텔레그램 런타임 설정 변경 (/config set)",
]

VERSION_HISTORY = [
    {
        "version": "3.1.0",
        "date"   : "2026-05-18",
        "type"   : "minor",
        "summary": "성과 추적 시스템 추가 — 일/주/월별 리포트 + 알고리즘 개선 분석",
        "changes": [
            "[NEW] performance_tracker.py — 3전략 통합 성과 추적 · 분석 엔진",
            "[NEW] main.py — job_daily_performance (15:50 자동 일별 리포트)",
            "[NEW] main.py — job_weekly_performance (금요일 16:00 주별 리포트)",
            "[NEW] main.py — job_monthly_performance (월말 16:10 월별 리포트)",
            "[NEW] telegram_bot.py — /report_daily / _weekly / _monthly / _all",
            "[NEW] telegram_bot.py — /analysis [일수] [전략] 개선 분석",
            "[FIX] main.py — job_auto_buy cur_price 할당 전 참조 버그 수정",
        ],
    },
    {
        "version": "3.0.0",
        "date"   : "2026-05-18",
        "type"   : "major",
        "summary": "전략C 상한가선진입 추가 + 3전략 통합 아키텍처",
        "changes": [
            "[NEW] position_registry.py — 3전략 공유 포지션 레지스트리",
            "[NEW] limit_config.py     — 전략C 설정 관리",
            "[NEW] limit_scanner.py    — 25%+ 종목 스캐너",
            "[NEW] strategy_limit.py   — 7대 신호 복합 스코어링 + 진입/청산",
            "[NEW] INTEGRATION_GUIDE.md — main.py 패치 가이드",
            "[CHG] main.py             — 전략C 스케줄 6개 추가",
            "[CHG] main.py             — PositionRegistry 적용 (전략A·B 포함)",
            "[CHG] telegram_bot.py     — /limit_* / /registry 명령어 추가",
        ],
    },
    {
        "version": "2.0.0",
        "date"   : "2026-04-09",
        "type"   : "major",
        "summary": "단타 전략 main.py 통합 (scalp_main.py 폐기)",
        "changes": [
            "[CHG] main.py         — 단타 스케줄 통합, 단일 프로세스 운영",
            "[DEL] scalp_main.py   — main.py 에 통합으로 폐기",
            "[NEW] broker_additions.py — broker 확장 메서드 분리",
            "[NEW] check_pool.py   — 후보 풀 검증 도구",
            "[NEW] diagnose_*.py   — 진단 도구 3종 추가",
        ],
    },
    {
        "version": "1.3.0",
        "date"   : "2026-03-28",
        "type"   : "minor",
        "summary": "전략 스코어링 개선 + 수급 신호 추가",
        "changes": [
            "[CHG] strategy.py        — 4요소 점수화 (거래대금·급등강도·수급·신고가)",
            "[CHG] strategy_config.py — near_high_threshold_pct 등 파라미터 추가",
        ],
    },
    {
        "version": "1.2.0",
        "date"   : "2026-03-12",
        "type"   : "minor",
        "summary": "텔레그램 명령어 확장 + Fib 재진입 추가",
        "changes": [
            "[CHG] telegram_bot.py — /report / /update_results 추가",
            "[NEW] fib_reentry.py  — 피보나치 재진입 관리",
            "[NEW] market_filter.py — 시장 등급 필터",
        ],
    },
    {
        "version": "1.1.0",
        "date"   : "2026-02-25",
        "type"   : "minor",
        "summary": "스케줄러 안정화 + 단타 전략 초기 분리",
        "changes": [
            "[CHG] main.py            — is_trading_day() KRX 공휴일 체크",
            "[NEW] scalp_main.py      — 단타 별도 프로세스 (임시)",
            "[NEW] scanner.py         — DayTradingScanner",
            "[NEW] strategy_scalping.py — ScalpStrategy",
            "[NEW] scalp_config.py    — 단타 설정",
            "[NEW] scalp_ledger.py    — 단타 거래 기록",
        ],
    },
    {
        "version": "1.0.0",
        "date"   : "2026-01-15",
        "type"   : "major",
        "summary": "초기 버전 — 종가베팅 전략 기반 자동매매",
        "changes": [
            "[NEW] main.py, broker.py, config.py",
            "[NEW] strategy.py, strategy_config.py",
            "[NEW] telegram_bot.py, scan_logger.py",
        ],
    },
]


def get_version_text(limit: int = 3) -> str:
    """텔레그램 /version 명령어용 버전 정보 문자열"""
    lines = [
        f"<b>[ 자동매매 봇 버전 정보 ]</b>",
        f"현재 버전: <b>v{CURRENT_VERSION}</b>",
        "",
    ]
    for v in VERSION_HISTORY[:limit]:
        icon = {"major": "🔴", "minor": "🟡", "patch": "🟢"}.get(v["type"], "⚪")
        lines.append(
            f"{icon} <b>v{v['version']}</b> ({v['date']}) — {v['summary']}"
        )
    if len(VERSION_HISTORY) > limit:
        lines.append(f"\n<i>전체 이력: CHANGELOG.md 참고</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    print(get_version_text())
