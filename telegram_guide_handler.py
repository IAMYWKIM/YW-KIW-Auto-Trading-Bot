"""
telegram_guide_handler.py — /guide 명령어 핸들러  v3.0.0
──────────────────────────────────────────────────────────
사용법: telegram_bot.py 의 TelegramBot 클래스에 아래 메서드와
        CommandHandler 등록 코드를 추가한다.

등록 방법 (build() 메서드 내부):
    app.add_handler(CommandHandler("guide", self.cmd_guide))

텔레그램 BotFather 에서 명령어 등록:
    /setcommands 실행 후 아래 목록 추가:
    guide - 명령어 가이드 출력 (섹션별)
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# 섹션별 가이드 텍스트
# ─────────────────────────────────────────────────────────────────────────────

_GUIDE_ALL = """\
<b>[ 키움 자동매매봇 명령어 가이드 v3.1.0 ]</b>
GCP Ubuntu · 키움 REST API · 3전략 통합

<b>섹션별 상세 안내:</b>
/guide a      → 전략A 종가베팅
/guide b      → 전략B 장중단타
/guide c      → 전략C 상한가선진입
/guide perf   → 성과 리포트 / 분석 ★NEW
/guide trade  → 수동 매매
/guide cfg    → 설정 명령어
/guide report → 분석·리포트 (구 버전)
/guide sched  → 자동 스케줄표

━━━━━━━━━━━━━━━━━━━━━━━━
<b>[ 공통 ]</b>
/start          봇 시작 + 스케줄 안내
/status         3전략 통합 포지션 현황
/balance        계좌 잔고 + 전략별 자금
/registry       포지션 레지스트리 현황
/version        버전 정보

━━━━━━━━━━━━━━━━━━━━━━━━
<b>[ 전략A — 종가베팅 ]</b>
/scan             후보 스캔 (자동 14:30)
/history          매매 이력
/lock [코드]      당일 매수 차단
/unlock [코드]    차단 해제

<b>[ 전략B — 단타 ]</b>
/scalp_scan       즉시 스캔
/scalp_status     포지션 조회
/scalp_pause      신규 진입 정지
/scalp_resume     재개

<b>[ 전략C — 상한가선진입 ]</b>
/limit_scan       25%+ 종목 스캔
/limit_status     포지션 + 익일 매도 현황
/limit_resume     연속손절 후 재개

━━━━━━━━━━━━━━━━━━━━━━━━
<b>[ 성과 리포트 ★NEW ]</b>
/report_daily     일별 성과 (자동: 15:50)
/report_weekly    주별 성과 (자동: 금 16:00)
/report_monthly   월별 성과 (자동: 월말 16:10)
/report_all       일+주+월 한번에
/analysis [일수] [전략]  개선 분석

━━━━━━━━━━━━━━━━━━━━━━━━
<b>[ 수동 매매 ]</b>
/buy [코드] [수량] [가격]
/sell [코드] [수량] [가격]
"""

_GUIDE_A = """\
<b>[ 전략A — 종가베팅 명령어 ]</b>
장 마감 직전(15:10~15:20) 눌림 매수 → 익일 익절/손절

━━━━━━━━━━━━━━━━━━━━━━━━
<b>조회</b>
/scan             후보 종목 즉시 스캔 (자동: 14:30)
/status           보유 포지션 + 손익
/balance          계좌 잔고
/history          최근 매매 이력

<b>제어</b>
/lock [코드]      해당 종목 당일 자동 매수 차단
/unlock [코드]    차단 해제

<b>분석</b>
/report [일수]    최근 N일 승률 분석 (기본 7일)
/update_results   전날 스캔 후보 실제 등락 업데이트

━━━━━━━━━━━━━━━━━━━━━━━━
<b>설정 (/config)</b>
/config show                전체 설정 조회
/config show scan           스캔 조건
/config show entry          진입 조건
/config show risk           리스크
/config show sell           매도 전략
/config set [키] [값]       즉시 변경
/config reset               기본값 초기화

<b>주요 설정 예시:</b>
<code>/config set entry.max_positions 5</code>
<code>/config set entry.position_size_pct 0</code>  (자동매수 차단)
<code>/config set risk.stop_loss_pct -4.0</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>자동 스케줄 (KST 평일)</b>
14:30  사전 스캔 + 알림
15:10  눌림 확인 + 자동 매수
15:30  전체 잠금 초기화
08:00~08:50  D+1 NXT 감시 (매분)
09:00~10:00  D+1 오전 감시 (매분)
15:00  D+1 강제 청산
"""

_GUIDE_B = """\
<b>[ 전략B — 장중단타 명령어 ]</b>
09:00~13:00 장중 급등 종목 당일 매수·매도

━━━━━━━━━━━━━━━━━━━━━━━━
<b>조회·제어</b>
/scalp_scan       즉시 스캔 (자동: 30초 주기)
/scalp_status     보유 포지션 + 실시간 손익
/scalp_pause      신규 진입 일시 정지
/scalp_resume     재개

<b>설정 (/scalp_config)</b>
/scalp_config show             전체 조회
/scalp_config show exit        매도 설정
/scalp_config show scan        스캔 설정
/scalp_config set [키] [값]    즉시 변경

<b>주요 설정 예시:</b>
<code>/scalp_config set exit.take_profit_pct 1.5</code>
<code>/scalp_config set exit.stop_loss_pct -1.0</code>
<code>/scalp_config set entry.max_positions 2</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>자동 스케줄 (KST 평일)</b>
08:50  장전 준비 (거래량 캐시 + 잔고)
09:00~15:25  스캔/매매 루프 (30초)
13:00  신규 진입 중단
15:10  강제 청산 경고
15:20  전량 강제 청산
15:35  당일 결산 리포트
"""

_GUIDE_C = """\
<b>[ 전략C — 상한가선진입 명령어 ]</b> ★ v3.0 NEW
25%+ 급등 종목에 7대 신호 복합 분석 → 상한가 전 선진입
익일 아침 갭 구간별 자동 매도

━━━━━━━━━━━━━━━━━━━━━━━━
<b>조회·제어</b>
/limit_scan       25%+ 종목 스캔 (자동: 30초)
/limit_status     보유 포지션 + 익일 매도 대기 현황
/limit_resume     연속손절 후 전략C 수동 재개

━━━━━━━━━━━━━━━━━━━━━━━━
<b>7대 신호 가중치</b>
테마 모멘텀  25%  선행 종목 상한가 비율
뉴스 감성    20%  공시·뉴스 키워드 강도
기관·외인    20%  순매수 전환 감지
거래량 패턴  15%  전일比 폭발 + 매수 비중
기술적 지표  10%  이평선 정배열 / RSI / 잔량
소셜 버즈     5%  커뮤니티 관심도 (예정)
시장 흐름     5%  KOSPI/KOSDAQ 방향성

<b>매수 기준:</b> 종합점수 70점↑ + 체결강도 150%+ + 잔량비 10%↓

━━━━━━━━━━━━━━━━━━━━━━━━
<b>익일 갭별 자동 매도</b>
갭 +5%↑  → 동시호가 전량 매도
갭 +2~5% → 동시호가 50% + 장초반 50%
갭 보합  → 09:30 강제청산
갭 -3%↓  → 즉시 손절

━━━━━━━━━━━━━━━━━━━━━━━━
<b>설정 (/limit_config)</b>
/limit_config show [섹션]      조회 (scan·signal·entry·exit·risk)
/limit_config set [키] [값]    즉시 변경
/limit_config reset            기본값 초기화

<b>주요 설정 예시:</b>
<code>/limit_config set signal.composite_score_min 75</code>
<code>/limit_config set entry.max_positions 1</code>
<code>/limit_config set exit.force_sell_time 09:20</code>
<code>/limit_config set scan.scan_stop_time 13:30</code>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>자동 스케줄 (KST 평일)</b>
08:50  장전 초기화
08:00~09:30  익일 갭 매도 감시 (매분)
09:00~14:30  스캔·진입·청산 루프 (30초)
09:25  강제청산 5분 전 경고
09:30  익일 미청산 전량 강제청산
15:40  당일 결산 리포트
"""

_GUIDE_TRADE = """\
<b>[ 수동 매매 명령어 ]</b>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>매수</b>
<code>/buy [종목코드] [수량] [가격]</code>
가격 0 = 시장가

예시:
<code>/buy 005930 10 75000</code>   삼성전자 10주 @75,000
<code>/buy 005930 10 0</code>       삼성전자 10주 시장가

━━━━━━━━━━━━━━━━━━━━━━━━
<b>매도</b>
<code>/sell [종목코드] [수량] [가격]</code>

예시:
<code>/sell 005930 10 76000</code>  지정가 매도
<code>/sell 005930 10 0</code>      시장가 매도

━━━━━━━━━━━━━━━━━━━━━━━━
⚠ 수동 매수도 포지션 레지스트리 자금 한도를 체크합니다.
⚠ MOCK 모드에서는 실제 체결이 발생하지 않습니다.
"""

_GUIDE_CFG = """\
<b>[ 설정 명령어 ]</b>

━━━━━━━━━━━━━━━━━━━━━━━━
<b>전략A (/config)</b>
/config show [scan|entry|risk|sell]
/config set [키] [값]
/config reset

<b>전략B (/scalp_config)</b>
/scalp_config show [scan|entry|exit]
/scalp_config set [키] [값]

<b>전략C (/limit_config) ★NEW</b>
/limit_config show [scan|signal|entry|exit|risk]
/limit_config set [키] [값]
/limit_config reset

━━━━━━━━━━━━━━━━━━━━━━━━
<b>전략C 주요 파라미터</b>
scan.entry_pct_min         25.0  (스캔 시작 등락률 %)
scan.scan_stop_time        14:30 (스캔 중단 시각)
signal.composite_score_min 70    (점수 최소값)
signal.strength_min        150   (체결강도 최소 %)
entry.max_positions        2     (최대 보유 종목)
entry.position_size_pct    12    (종목당 투자 비율 %)
exit.stop_loss_pct         -5.0  (당일 손절선 %)
exit.next_day_stop_loss_pct -3.0 (익일 손절선 %)
exit.force_sell_time       09:30 (강제청산 시각)
risk.max_consecutive_loss  3     (연속 손절 허용)
"""

_GUIDE_REPORT = """\
<b>[ 분석 / 리포트 명령어 ]</b>

━━━━━━━━━━━━━━━━━━━━━━━━
/report [일수]      전략A 승률 분석 (기본 7일)
/report 14          최근 14일 분석
/report 30          최근 30일 분석

/update_results     전날 스캔 후보 실제 등락 업데이트
                    (장 마감 후 실행 권장)

━━━━━━━━━━━━━━━━━━━━━━━━
<b>분석 워크플로</b>
매일 14:30   /scan 자동 → scan_log 저장
다음날 장 후  /update_results 실행
주 1회       /report 7 → 조건별 승률 확인
조건 최적화  /config set 으로 파라미터 조정
"""

_GUIDE_SCHED = """\
<b>[ 자동 스케줄표 ]</b> KST 평일 기준

━━━━━━━━━━━━━━━━━━━━━━━━
<b>전략A (종가베팅)</b>
09:00        API 토큰 갱신
14:30        후보 스캔 + 텔레그램 알림
15:10        눌림 확인 + 자동 매수
15:30        전체 잠금 초기화
08:00~08:50  D+1 NXT 프리마켓 감시 (매분)
09:00~10:00  D+1 오전 익절/손절 감시 (매분)
15:00        D+1 미청산 강제 청산

━━━━━━━━━━━━━━━━━━━━━━━━
<b>전략B (장중단타)</b>
08:50        장전 준비
09:00~15:25  스캔/매매 루프 (30초)
13:00        신규 진입 중단
15:10        강제청산 경고
15:20        전량 강제청산
15:35        당일 결산

━━━━━━━━━━━━━━━━━━━━━━━━
<b>전략C (상한가선진입)</b>
08:50        장전 초기화
08:00~09:30  익일 갭 매도 감시 (매분)
09:00~14:30  스캔·진입·청산 루프 (30초)
09:25        강제청산 5분 전 경고
09:30        익일 미청산 강제청산
15:40        당일 결산

━━━━━━━━━━━━━━━━━━━━━━━━
<b>성과 리포트 ★NEW</b>
15:50        3전략 통합 일별 성과 자동 발송
16:00 금요일  주별 성과 + 주간 분석 자동 발송
16:10 월말   월별 성과 + 30일 심층분석 자동 발송

━━━━━━━━━━━━━━━━━━━━━━━━
<b>공통</b>
06:00        로그 7일 초과분 삭제
"""

_GUIDE_PERF = """\
<b>[ 성과 리포트 / 알고리즘 분석 ]</b> ★ v3.1 NEW
장 종료 후 자동 발송 + 수동 조회 모두 지원

━━━━━━━━━━━━━━━━━━━━━━━━
<b>자동 발송 스케줄</b>
15:50 매일      일별 3전략 통합 성과 + 개선 힌트
16:00 금요일    주별 성과 + 요일·점수구간 분석
16:10 월말      월별 성과 + 30일 심층 분석

━━━━━━━━━━━━━━━━━━━━━━━━
<b>수동 조회 명령어</b>
/report_daily              오늘 일별 성과
/report_daily 20260501     특정 날짜 성과
/report_weekly             이번 주 성과
/report_monthly            이번 달 성과
/report_all                일+주+월 한번에

━━━━━━━━━━━━━━━━━━━━━━━━
<b>알고리즘 개선 분석</b>
/analysis                  최근 30일 전체 분석
/analysis 7                최근 7일
/analysis 30 C             전략C 30일 심층분석
/analysis 90 A             전략A 90일 심층분석

<b>분석 항목:</b>
• 요일별 승률 (어떤 요일이 유리한지)
• 시간대별 승률 (전략B)
• 점수 구간별 승률 (전략A·C)
• 보유시간별 수익률
• 매도 사유별 집계 (익절/손절/강제청산 비율)
• Profit Factor 기반 파라미터 조정 제안

<b>성과 데이터 저장:</b>
data/performance/trades.json   전체 기록
data/performance/daily/        일별 스냅샷
data/performance/weekly/       주별 스냅샷
data/performance/monthly/      월별 스냅샷
"""

_SECTION_MAP = {
    "all"      : _GUIDE_ALL,
    "a"        : _GUIDE_A,
    "b"        : _GUIDE_B,
    "c"        : _GUIDE_C,
    "perf"     : _GUIDE_PERF,
    "performance": _GUIDE_PERF,
    "trade"    : _GUIDE_TRADE,
    "cfg"      : _GUIDE_CFG,
    "config"   : _GUIDE_CFG,
    "report"   : _GUIDE_REPORT,
    "sched"    : _GUIDE_SCHED,
    "schedule" : _GUIDE_SCHED,
}

# ─────────────────────────────────────────────────────────────────────────────
# 텔레그램 봇 핸들러 메서드
# telegram_bot.py 의 TelegramBot 클래스에 붙여 넣는다.
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_guide(self, update, context):
    """
    /guide [섹션]
    섹션: all(기본) · a · b · c · trade · cfg · report · sched
    """
    args    = context.args
    section = (args[0].lower() if args else "all").strip("/")
    text    = _SECTION_MAP.get(section, _GUIDE_ALL)

    # 텔레그램 메시지 4096자 제한 — 초과 시 분할 전송
    MAX = 4000
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# build() 메서드에 추가할 CommandHandler 등록 코드
# ─────────────────────────────────────────────────────────────────────────────
#
# app.add_handler(CommandHandler("guide", self.cmd_guide))
#
# ─────────────────────────────────────────────────────────────────────────────
# BotFather /setcommands 전체 목록 (기존 명령어 + 신규 추가)
# ─────────────────────────────────────────────────────────────────────────────

BOTFATHER_COMMANDS = """\
start - 봇 시작 및 명령어 안내
status - 3전략 통합 포지션 현황
balance - 계좌 잔고
scan - 전략A 후보 종목 스캔
history - 전략A 매매 이력
report - 전략A 승률 분석
update_results - 전날 스캔 결과 업데이트
config - 전략A 설정 조회/변경
lock - 종목 당일 매수 차단
unlock - 종목 차단 해제
buy - 수동 매수
sell - 수동 매도
scalp_scan - 전략B 즉시 스캔
scalp_status - 전략B 포지션 조회
scalp_pause - 전략B 신규 진입 정지
scalp_resume - 전략B 재개
scalp_config - 전략B 설정 조회/변경
limit_scan - 전략C 상한가선진입 스캔
limit_status - 전략C 포지션 조회
limit_resume - 전략C 연속손절 후 재개
limit_config - 전략C 설정 조회/변경
registry - 3전략 포지션 레지스트리 현황
report_daily - 일별 성과 리포트
report_weekly - 주별 성과 리포트
report_monthly - 월별 성과 리포트
report_all - 일+주+월 성과 한번에
analysis - 알고리즘 개선 분석
version - 버전 정보
guide - 명령어 가이드 출력
"""

if __name__ == "__main__":
    # 로컬 테스트용 — 각 섹션 출력 확인
    import re
    for sec, text in _SECTION_MAP.items():
        cleaned = re.sub(r"<[^>]+>", "", text)
        print(f"\n{'='*40}\n섹션: {sec}\n{'='*40}")
        print(cleaned[:200], "...")
