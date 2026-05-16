# Changelog

키움증권 국내주식 자동매매 봇 변경 이력
버전 규칙: **X.Y.Z** — X: 전략 전면 개편, Y: 기능/모듈 추가, Z: 버그 수정/소수 개선

---

## [2.3.0] - 2026-05-17  ← 현재

### 하이브리드 모드 — 유저 수동 주도주 지정 단타

#### strategy_scalping.py v1.3
- **[추가]** `WATCHLIST_FILE` — `data/scalp_watchlist.json` 수동 감시 목록 영속화
- **[추가]** `self._watchlist` — 봇 시작 시 JSON에서 자동 로드 (재시작 후에도 유지)
- **[추가]** `watchlist_add(code, name)` — 수동 감시 종목 추가
  - 이름 미입력 시 `broker.get_stock_info()`로 자동 조회
  - 이미 있으면 비활성 → 재활성화
- **[추가]** `watchlist_remove(code)` — 감시 중단 (이력 보존)
  - 보유 중이면 포지션 유지, 신규 진입만 차단
- **[추가]** `watchlist_get_active()` — 현재 활성 감시 목록
- **[추가]** `is_in_watchlist(code)` — 활성 감시 여부 확인
- **[추가]** `build_watchlist_candidate(code)` — 수동 종목을 candidate 형식으로 변환
  - `source: "MANUAL"`, `_manual: True` 플래그로 스캐너 필터 우회 마킹
- **[추가]** `format_watchlist_message()` — 감시 목록 텔레그램 메시지

#### main.py v2.3
- **[추가]** `job_scalp_loop` 내 하이브리드 수동 감시 종목 처리 블록
  - 스캐너 결과와 별도로, 매 30초마다 수동 감시 목록도 진입 시도
  - **스캐너 필터 우회**: 상승률/거래대금/거래량 조건 없이 진입
  - **진입 조건 유지**: 포지션 수 / 현금 / 시간 마감 / 쿨다운 동일 적용
  - **청산 로직 동일**: 손절/익절/트레일링/강제청산 동일하게 적용
  - Fib 감시 중인 종목은 Fib 신호 대기 (중복 진입 방지)

#### telegram_bot.py v2.3
- **[추가]** `/scalp_add [코드] [이름선택]` — 수동 주도주 단타 감시 추가
  - 추가 즉시 현재가 조회 및 확인 메시지 발송
  - 30초 후 루프에서 자동 매수 시도 시작
- **[추가]** `/scalp_remove [코드]` — 수동 감시 중단
  - 보유 중이면 "신규 진입 중단, 포지션 유지" 안내
- **[추가]** `/scalp_watchlist` — 감시 목록 전체 조회
  - 활성 종목의 실시간 현재가 + 등락률 함께 표시
  - 보유 중인 종목 📌 표시
- **[추가]** `/help` 에 하이브리드 모드 섹션 추가

---

## [2.2.0] - 2026-05-16

### 거래일 판별 + /guide 버그 수정

#### main.py v2.2
- **[추가]** `is_trading_day(dt)` — 주말/한국 공휴일 판별
  - 토요일(weekday=5), 일요일(weekday=6) → False
  - `_KRX_HOLIDAYS` set 에 2025~2026년 KRX 공식 휴장일 등록
- **[추가]** `assert_trading_day(job_name)` — 스케줄 작업용 가드
- **[적용]** 9개 스케줄 작업에 거래일 체크 적용
  - 단타: `job_scalp_loop` / `job_scalp_pre_market` / `job_scalp_force_exit_warn`
    `job_scalp_force_exit` / `job_scalp_daily_report`
  - 종가베팅: `job_token_refresh` / `job_pre_scan` / `job_auto_buy` / `job_monitor_exit`
- **[효과]** 주말/공휴일에 API 에러 없음, 월요일 08:50 자동 재개

#### telegram_bot.py v2.2
- **[수정]** `/guide` 버튼 탭 시 설정 초기화 오작동 버그 수정
  - 원인: `GUIDE:` elif 블록에 `return` 누락 → reset 코드가 항상 실행됨
  - 수정: 블록 끝에 `return` 추가, `CONFIG:RESET:CONFIRM` elif 복원
- **[수정]** `_guide_reply()` — `callback_query.edit_message_text()` 직접 호출
- **[추가]** `/guide` — 아이폰 최적화 12카테고리 2열 인라인 버튼 가이드
- **[추가]** `/scalp_market` — 주말 호출 시 친화적 안내 메시지

---

## [2.1.0] - 2026-05-15

### 피보나치 재진입 + 시장 필터

#### fib_reentry.py v1.0 (신규)
- **[추가]** `FibWatcher` — 손절 후 Fib 조정대 기반 재진입 감시
  - 갭상승 주도주: 전일종가 기준 Fib 0.236 / 0.382
  - 일반 급등주: 당일저점 기준 Fib 0.382 / 0.500
- **[추가]** `FibReentryManager` — 다종목 일괄 감시, 30초 루프 자동 체크
- **[추가]** 재진입 조건: 손절 후 10분 + Fib 구간 + 저점 대비 +0.3% 반등

#### strategy_scalping.py v1.2
- **[변경]** 손절 후 블랙리스트 → FibWatcher 등록으로 교체
- **[추가]** `calc_real_cost()` — 수수료(0.015%) + 거래세(0.18%) 계산
- **[추가]** `remove_position()` — 총손익/거래비용/실손익 분리 반환

#### scalp_config.py v1.1
- **[변경]** `entry_end_time` 13:00 → 14:30
- **[변경]** `api_delay_sec` 0.3 → 0.5
- **[추가]** `risk.commission_rate`, `risk.tax_rate_kosdaq`

#### market_filter.py v1.0 (신규)
- **[추가]** KOSPI/KOSDAQ ETF 등락률 기반 시장 점수 (0~100)
- **[추가]** STOP/CAUTION/NORMAL/BULLISH 4단계 허용 포지션 제한

#### telegram_bot.py v2.1
- **[추가]** `/scalp_fib`, `/scalp_market` 명령어
- **[강화]** 매수/매도 알림 — 목표가/손절가/거래비용/실손익 상세 표시

---

## [2.0.0] - 2026-05-15

### 단타 전략 통합

- **[변경]** `scalp_main.py` 폐기 → `main.py`/`telegram_bot.py`에 단타 통합
- **[추가]** `scalp_ledger.py` — 일별/주별/월별 손익 분석, `/scalp_summary`
- **[추가]** `broker.py` v1.1 — API 응답 키 교정, VWAP 계산 추가

---

## [1.4.2] - 2026-04-11

### strategy.py v1.4.2
- **[추가]** 후보 풀 소스: ka10004(등락률) + ka10005(거래대금) + ka10023(거래량)

---

## [1.0.0] - 2026-04-03

### 최초 릴리즈 (종가베팅)
- GCP Ubuntu + 키움 REST API + 텔레그램 봇 아키텍처
- broker / config / strategy / strategy_config / telegram_bot / main 6대 코어
