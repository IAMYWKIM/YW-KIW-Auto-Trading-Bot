# Changelog

키움증권 국내주식 자동매매 봇 변경 이력  
버전 규칙: **X.Y.Z** — X: 전략 전면 개편, Y: 기능/모듈 추가, Z: 버그 수정/소수 개선

---

## [2.1.0] - 2026-05-15  ← 현재

### 전략 개선 — 피보나치 재진입 + 시장 필터 고도화

#### fib_reentry.py v1.0 (신규)
- **[추가]** 손절 후 완전 차단 대신 피보나치 조정대 기반 재진입 감시
- **[추가]** `FibWatcher` — 손절 포지션의 Fib 레벨 자동 계산 및 상태 추적
  - 갭상승 주도주: `전일종가`를 기준점으로 Fib 0.236 / 0.382 감시
  - 일반 급등주: `당일저점`을 기준점으로 Fib 0.382 / 0.500 감시
- **[추가]** `FibReentryManager` — 다종목 Fib 감시 일괄 관리
  - 30초 루프에서 자동 체크, 반등 확인 시 재진입 candidate 반환
  - 당일 저점 갱신 시 Fib 레벨 자동 재계산
- **[추가]** 재진입 조건: 손절 후 10분 대기 + Fib 구간 진입 + 저점 대비 +0.3% 반등
- **[추가]** `/scalp_fib` 텔레그램 명령어 — Fib 감시 현황 조회

#### strategy_scalping.py v1.2
- **[변경]** 손절 후 당일 블랙리스트 → `FibWatcher` 등록으로 교체
- **[추가]** `fib_mgr` 인스턴스 주입 구조 (main.py에서 주입)
- **[수정]** `init_daily()` — `fib_mgr.init_daily()` 호출 추가 (전일 감시 목록 초기화)
- **[유지]** `calc_real_cost()` — 세금+수수료 계산 (v1.1에서 유지)

#### main.py v2.1
- **[추가]** `FibReentryManager` import 및 초기화
- **[추가]** `job_scalp_loop` — Fib 재진입 신호 체크 (30초 루프 내)
- **[추가]** Fib 재진입 체결 시 전용 텔레그램 알림 (레벨, 저점, 반등률 표시)

#### telegram_bot.py v2.1
- **[추가]** `/scalp_fib` 명령어 — Fib 감시 현황 (감시 종목, 레벨, 반등률, 경과시간)

---

## [2.0.0] - 2026-05-15

### 단타 전략 통합 + 시장 필터 + 손익 정확화

#### 아키텍처 전환 — 단타봇 통합
- **[변경]** `scalp_main.py` 폐기 → `main.py` + `telegram_bot.py`에 단타 전략 통합
- **[변경]** 단일 프로세스, 단일 텔레그램 봇 토큰으로 종가베팅 + 단타 동시 운영
- **[추가]** `market_filter.py` 신규 — 시장 상황 모니터링
- **[추가]** `scalp_ledger.py` 신규 — 단타 매매 장부 및 손익 분석
- **[추가]** `fib_reentry.py` 신규 — 피보나치 재진입 감시

#### market_filter.py v1.0 (신규)
- **[추가]** KODEX 200 ETF(069500) → KOSPI 대리 지표
- **[추가]** KODEX KOSDAQ150(229200) → KOSDAQ 대리 지표
- **[추가]** 외인 매수 강도 측정 (등락률 상위 10개 종목 집계)
- **[추가]** 시장 점수 0~100 → STOP / CAUTION / NORMAL / BULLISH 4단계
- **[추가]** STOP 시 신규 진입 차단, CAUTION 시 최대 포지션 1개 제한
- **[추가]** `/scalp_market` 텔레그램 명령어

#### scalp_ledger.py v1.1 (신규)
- **[추가]** `scalp_daily_log.json` 직접 파싱 (오늘 포함 과거 모든 데이터 조회)
- **[추가]** BUY/SELL 쌍 매칭 로직 (종목별 스택 방식)
- **[추가]** 일별 / 주별 / 월별 손익 요약
- **[추가]** 날짜별 상세 내역 (매수가, 매도가, 수익률, 보유시간, 사유)
- **[추가]** 달력 뷰 (`format_monthly_calendar`)
- **[추가]** `/scalp_summary` 텔레그램 명령어 (인라인 버튼 포함)

#### broker.py v1.1
- **[수정]** `get_stock_info()` 응답 키 교정 (실전 서버 실측 기준)
  - 거래량: `acml_vol` → `trde_qty`
  - 전일종가: `pred_close_pric` → `base_pric`
  - 시가: `open_pric` 필드 신규 추가
  - 등락률: `flu_rt` 필드 신규 추가
- **[추가]** `trading_value` 필드 (당일 거래대금 근사값 = cur_prc × trde_qty)
- **[추가]** `get_minute_chart()` — ka10080 분봉차트 (VWAP 계산용)
  - 응답 키 실측 교정: `cntr_tm` (YYYYMMDDHHmmss) 파싱
- **[추가]** `calc_vwap()` — 분봉 기반 당일 VWAP 계산
- **[추가]** `get_today_info()` — 단타 전용 통합 조회
- **[추가]** `debug_api_keys()` — API 응답 키 진단 도구

#### scanner.py v1.2
- **[수정]** `_get_stock_detail_with_retry()` — `get_today_info()` 대신 `get_stock_info()` 직접 호출 (429 재시도 정상화)
- **[수정]** API 호출 93회 → 38회 최적화
  - 루프 내 `_get_prev_day_volumes()` 제거 (캐시 없으면 0으로 처리)
  - VWAP 계산을 최종 통과 상위 5개만 실행
- **[추가]** `scan()` — `force_time` 파라미터 (장외 시간 테스트용)
- **[수정]** 소스간 딜레이 1초 추가 (429 방지)
- **[수정]** 스캔 예상 소요시간: 63초+ → 약 14초

#### strategy_scalping.py v1.1
- **[추가]** `calc_real_cost()` — 수수료(0.015%) + 거래세(0.18%) 계산
- **[추가]** `remove_position()` — 총손익 / 거래비용 / 실손익 분리 반환
- **[추가]** `result["gross_profit"]`, `result["total_cost"]` 필드 신규
- **[추가]** 손절 시 Fib 감시 등록 (fib_mgr 주입 시)

#### scalp_config.py v1.1
- **[변경]** `entry_end_time`: `13:00` → `14:30` (스캘핑 시간 연장)
- **[변경]** `api_delay_sec`: `0.3` → `0.5` (모의서버 429 방지)
- **[추가]** `risk.commission_rate`: 수수료 0.00015 (0.015%)
- **[추가]** `risk.tax_rate_kosdaq`: 거래세 0.0018 (0.18%)
- **[추가]** `risk.blacklist_on_stoploss`: 손절 후 Fib 감시 여부

#### telegram_bot.py v2.0
- **[추가]** 단타 컴포넌트 주입 구조 (`scanner`, `scalp_strategy`, `scalp_cfg`)
- **[추가]** 단타 명령어 동적 등록 (컴포넌트 주입 시에만 등록)
- **[추가]** `/scalp_status` — 단타 포지션 현황
- **[추가]** `/scalp_scan` — 즉시 스캔 (장외 force_time=True)
- **[추가]** `/scalp_config` — 단타 설정 조회/변경
- **[추가]** `/scalp_stop` — 신규 진입 ON/OFF 토글
- **[추가]** `/scalp_exit_all` — 전량 즉시 청산
- **[추가]** `/scalp_debug` — 봇 상태 진단 (잔고/시간/캐시/설정)
- **[추가]** `/scalp_summary` — 일별/주별/월별 손익 요약 + 상세 내역
- **[추가]** `/scalp_market` — 시장 상황 조회
- **[추가]** `/scalp_fib` — Fib 재진입 감시 현황
- **[강화]** `notify_scalp_buy()` — 목표가/손절가/거래대금/거래량/VWAP 포함 상세 알림
- **[강화]** `notify_scalp_sell()` — 총손익/거래비용/실손익 분리 표시
- **[추가]** `scalp_ledger.record_trade()` — 매도 체결 시 장부 자동 기록

#### main.py v2.0
- **[추가]** 단타 전략 컴포넌트 전역 초기화
  - `ScalpConfig`, `DayTradingScanner`, `ScalpStrategy`, `MarketFilter`, `FibReentryManager`
- **[추가]** 단타 스케줄 (기존 종가베팅 스케줄과 병행)
  - `job_scalp_pre_market` (08:50) — 장전 준비, 거래량 캐시
  - `job_scalp_loop` (30초) — 스캔/매매/청산 루프
  - `job_scalp_force_exit_warn` (15:10) — 강제청산 경고
  - `job_scalp_force_exit` (15:20) — 전량 강제청산
  - `job_scalp_daily_report` (15:35) — 당일 결산

---

## [1.4.2] - 2026-04-11  (이하 종가베팅 v1.x 시리즈)

### strategy.py v1.4.2
- **[추가]** 후보 풀 소스 전면 개편
  - 소스1: ka10004 등락률 상위 (핵심)
  - 소스2: ka10005 거래대금 상위
  - 소스3: ka10023 거래량급증 (보조)
- **[제거]** `_source_volume_renew()` (ka10024)
- **[추가]** `diagnose_source.py` 진단 도구

---

## [1.4.1] - 2026-04-10

### strategy.py v1.4.1
- **[추가]** `_get_daily_data_with_retry()` — 429 지수 백오프 (2초→4초→6초)
- **[추가]** `strategy_config._migrate_v14()` — 구버전 JSON 자동 보완

---

## [1.4.0] - 2026-04-10

### strategy.py v1.4.0
- **[변경]** 키움 조건검색식 A~G 코드 재현으로 스캔 로직 전면 교체
- **[추가]** 조건B/D/E/C/A/G/F 체크 메서드
- **[추가]** Envelope(20,20) 상한선 조건

---

## [1.3.2] - 2026-04-10

- **[수정]** 수급 수집 / pullback_pct 계산 / volume_ratio 필터 버그 4건 수정
- **[변경]** `scan_candidates()` 전체 반환 후 `main.py`에서 포지션 제한 적용

---

## [1.3.0] - 2026-04-09

- **[추가]** 3소스 병행 스캔 아키텍처 (ka10016 / ka10023 / ka10024)
- **[추가]** `scan_logger.py` — 스캔 결과 JSON 저장 + 승률 분석
- **[변경]** 거래대금 500억 → 100억, 눌림 -5% → -10%, RSI 70 → 80

---

## [1.0.0] - 2026-04-03

### 최초 릴리즈 (종가베팅)
- `broker.py` / `config.py` / `strategy.py` / `strategy_config.py`
- `telegram_bot.py` / `main.py` 6대 코어 완성
- GCP Ubuntu 서버 + 키움 REST API + 텔레그램 봇 아키텍처
