# ==========================================================
# [version_history.py]
# ⚠️ 이 주석 및 파일명 표기는 절대 지우지 마세요.
# 🚨 [경고] 텔레그램 메시지 길이 제한(4096자) 주의!
# 봇이 5개씩 잘라서 전송(Paging)하므로 전송 에러는 완벽히 차단됩니다.
# ==========================================================

VERSION_HISTORY = [
    # ==========================================================
    # V1.0 시리즈 — 기초 아키텍처 구축 (2026.04.03)
    # ==========================================================
    "V1.0.0 [2026.04.03] 🚀 최초 릴리즈 — 키움 REST API 기반 국내주식 종가베팅 자동매매 봇 초기 아키텍처 구축 완료. broker.py(API 통신), config.py(계좌/장부/잠금 관리), strategy_config.py(전략 조건 설정), strategy.py(스캔/신호 생성), telegram_bot.py(봇 명령어), main.py(APScheduler 스케줄러) 6대 코어 완성",
    "V1.0.1 [2026.04.05] 🚨 [API 경로/필드명 문서 기준 전면 교정]: ka10023 URL /api/dostk/stkinfo → /api/dostk/rkinfo, 응답 키 stk_vlm_incrs → trde_qty_sdnin, 필드 acml_vol → now_trde_qty / pred_vol → prev_trde_qty 교정. ka10009 URL /api/dostk/stkinfo → /api/dostk/frgnistt, 필드 orgn_ntby_qty → orgn_daly_nettrde / frgn_ntby_qty → frgnr_daly_nettrde 교정. ka10081 종가 필드 cls_prc → cur_prc 교정 및 is_near_high() division by zero 방어 추가",

    # ==========================================================
    # V1.1 시리즈 — 스케줄러 안정화 (2026.04.06)
    # ==========================================================
    "V1.1.0 [2026.04.06] 🚀 main.py 완성 및 GCP Ubuntu 서버 systemd 서비스(kiwoombot.service) 등록. APScheduler 버전 호환성 패치: job.next_run_time 속성 제거. 텔레그램 봇 polling 정상 통신 확인",

    # ==========================================================
    # V1.2 시리즈 — 전략 개선 (2026.04.07)
    # ==========================================================
    "V1.2.0 [2026.04.07] 🎯 [전략 전환 — 당일 급증 스캔 → 전일 급등 + 오늘 눌림 2단계 스캔]: scan_prev_surge_today_pullback() 신규 탑재. 정배열 완화 MA5>MA20>MA60 → MA5>MA20. 신고가 허용 범위 -2% → -10%. get_daily_data()로 일봉 데이터 통합(거래량·거래대금 포함). near_high_threshold_pct 파라미터 신설. RSI 상한 70 → 80",
    "V1.2.1 [2026.04.09] 🚨 [ka10016/ka10024 URI 오류 수정 및 429 방어]: ka10016 URL /api/dostk/rkinfo → /api/dostk/stkinfo, 응답 키 new_high_low → ntl_pric, 파라미터 정정(ntl_tp, high_low_close_tp 등). ka10024 URL /api/dostk/rkinfo → /api/dostk/stkinfo, 응답 키 trde_qty_renew → trde_qty_updt, 파라미터 정정(cycle_tp 등). 429 Too Many Requests 방어: 일봉 API 5개 호출마다 1초 딜레이 추가",

    # ==========================================================
    # V1.3 시리즈 — 후보 소스 다양화 + 로그 분석 시스템 (2026.04.09)
    # ==========================================================
    "V1.3.0 [2026.04.09] 🎯 [3소스 병행 스캔 아키텍처 구축 및 조건 전면 완화]: 후보 소스 3개로 다양화 — 소스1: ka10016 신고가(ntl_pric), 소스2: ka10023 당일 거래량 급증(trde_qty_sdnin), 소스3: ka10024 거래량 갱신(trde_qty_updt). 핵심 조건 완화 — 거래대금 500억→100억(다날/보원케미칼/한패스 포함), 눌림 범위 -5%→-10%(시장 급락일 대응), RSI 상한 70→80, 신고가 범위 -10%→-20%, 수급 조건 OFF(눌림 구간엔 기관도 매도), 정배열 MA5>MA20만 유지. 최근 N일 내 급등 확인 로직(check_recent_surge) 신설. 점수화 시 PULLBACK 타입 +5 보너스 부여",
    "V1.3.0 [2026.04.09] 📊 [scan_logger.py 신규 탑재 — 스캔 결과 자동 저장 및 승률 분석 시스템]: /scan 실행 시 data/scan_log/YYYYMMDD_HHMMSS_scan.json 자동 저장. 선정 이유 전체 기록(surge_max_gain, surge_days_ago, pullback_pct, trading_value, volume_ratio, ma5/20/60, rsi, pct_from_high, institution_net, foreign_net). update_results() 다음날 실제 결과 자동 업데이트. generate_report() 조건별 승률 분석(거래대금별/RSI별/눌림깊이별)",
    "V1.3.0 [2026.04.09] 📱 [telegram_bot.py v1.2 — /scan 선정 이유 상세 표시 및 신규 명령어 탑재]: /scan 결과에 급등률·눌림·소스 타입 등 선정 이유 상세 표시 및 로그 자동 저장 연동. /report [일수] 신설 — 최근 N일 승률 분석 리포트. /update_results [날짜] 신설 — 전날 스캔 결과 업데이트. /help 명령어 목록 갱신",
    "V1.3.0 [2026.04.09] 🗂️ [버전 관리 아키텍처 구축 — VERSION / CHANGELOG.md / README.md 신설]: 단일 버전 소스(VERSION 파일) 관리 체계 확립. CHANGELOG.md에 전체 변경 이력 기록(X.Y.Z 규칙: X=전략 전면 개편, Y=기능 추가/조건 변경, Z=버그 수정). README.md에 프로젝트 개요/파일 구조/설치 방법/전략 설명/API 목록 통합 문서화. deploy_kiw.sh / deploy_kis.sh — .py 및 .md / VERSION 파일 자동 이동 지원으로 배포 스크립트 개선",

    # ==========================================================
    # V1.3.2 — 4대 버그 수정 (2026.04.10)
    # ==========================================================
    "V1.3.2 [2026.04.10] 🚨 [4대 버그 수정 + 설계 개선]: "
    "BUG2 — institution/foreign 수급 수집을 use_*_buy 필터 플래그와 분리, 항상 수집하여 로그/점수화 정상화. "
    "BUG3 — analyze_candidate()에서 closes[0]/closes[1] 기반 pullback_pct 실제 계산(기존 항상 0 기록 수정). "
    "BUG4 — analyze_candidate()에 volume_ratio_min 필터 추가. "
    "DESIGN1 — scan_candidates() max_positions 잘라내기 제거, 전체 반환 후 main.py 자동매수에서만 제한 적용. "
    "scan_logger v1.1 — update_results() candle 날짜 검증 추가(장 시작 전 오기록 방지), WIN 판정을 시가갭/고가/종가 기준으로 실제 전략에 맞게 개선, generate_report()에 수급별·시장상황별 분석 추가. "
    "telegram_bot v1.3 — get_kospi_pct() 추가, /scan 시 코스피 지수(0001) 실시간 등락률 저장",

    # ==========================================================
    # V1.4 시리즈 — 키움 조건검색식 코드 재현 (2026.04.10~11)
    # ==========================================================
    "V1.4.0 [2026.04.10] 🎯 [키움 조건검색식 A~G 코드 재현 — 스캔 로직 전면 교체]: "
    "기존 3소스(ka10016/ka10023/ka10024) 방식 → 사용자 조건검색식 직접 구현으로 대체. "
    "필수 조건 — B: 전일 거래대금 100억+, E: 30봉 내 전일대비 +9% 이상 급등일, "
    "D: MA3 &lt; 현재가(극단기 정배열), C/A: Envelope(20,20) 상한선 이상. "
    "가산점 — G: 당일 300억+(+15점), F: +29.5% 급등(+10점), A: Envelope 터치(+5점). "
    "calc_envelope()/check_cond_A~G()/apply_condition_search() 메서드 신설. "
    "strategy_config v1.4 — envelope_period/band_pct/lookback/surge_threshold_e/f/cond_g_min_tv 파라미터 추가",

    "V1.4.1 [2026.04.10] 🚨 [429 방어 강화 + strategy_config 마이그레이션]: "
    "_get_daily_data_with_retry() 신설 — 매 종목마다 api_delay_sec(기본 0.5초) 딜레이, "
    "429 발생 시 지수 백오프 재시도(2초→4초→6초, 최대 3회). "
    "strategy_config _migrate_v14() 추가 — 구버전 JSON에 신규 파라미터 자동 보완(기존 값 보존). "
    "telegram_bot cmd_scan() 메시지를 조건검색식 A~G 기준으로 업데이트",

    "V1.4.2 [2026.04.11] 🎯 [후보 풀 소스 전면 개편 — 키움 조건검색 전체 종목 커버]: "
    "근본 문제 발견 — ka10023(거래량급증)은 오늘 거래량 급증 종목만 수집, "
    "키움 조건검색 34개 종목 중 대부분 후보 풀 누락으로 조건 통과 0개 발생. "
    "소스1 신설 — ka10004 등락률 상위(stk_pric_updn): 오늘 상승 종목 전체 포괄(핵심). "
    "소스2 신설 — ka10005 거래대금 상위(stk_trde_prica): 조건B 충족 가능성 높은 종목. "
    "소스3 유지 — ka10023 거래량급증(trde_qty_tp 0 완화). "
    "_source_volume_renew(ka10024) 제거. "
    "diagnose_source.py 진단 도구 추가",
]

# ==========================================================
# 현재 버전 정보
# ==========================================================
CURRENT_VERSION = "V1.4.2"

CURRENT_FEATURES = """🤖 <b>키움증권 국내주식 종가베팅 자동매매 봇</b>
<b>현재 버전: V1.4.2</b>

<b>[ 핵심 전략 ]</b>
🎯 <b>종가베팅 전략</b> — 장 마감 직전(15:10~15:20) 다음날 상승 가능성이 높은 종목을 매수하고 익일 청산

<b>[ 후보 선정 — 키움 조건검색식 A~G 코드 재현 ]</b>
📡 소스1: ka10004 등락률 상위 → 오늘 상승 종목 전체 포괄 (핵심)
📡 소스2: ka10005 거래대금 상위 → 조건B 충족 가능성 높은 종목
📡 소스3: ka10023 거래량급증 → 보조 소스
🔍 조건B (필수): 전일 거래대금 100억+
🔍 조건E (필수): 30봉 이내 전일대비 +9% 이상 급등일 존재
🔍 조건D (필수): MA3 &lt; 현재가 — 극단기 정배열
🔍 조건C/A (필수): Envelope(20,20) 상한선 이상 — 현재 or 30봉 이내

<b>[ 가산점 조건 ]</b>
⭐ 조건G: 당일 거래대금 300억+ → +15점
⭐ 조건F: 30봉 이내 +29.5% 급등 → +10점
⭐ 조건A: 30봉 이내 Envelope 터치 경험 → +5점

<b>[ 자동 매수 조건 ]</b>
📉 전일 종가 대비 -0.5% ~ -10% 눌림 중인 종목 (15:10~15:20)

<b>[ 매도 전략 (D+1) ]</b>
🌅 08:00~08:50 NXT 갭 +2%↑ → 즉시 매도
📈 09:00~10:00 +3%↑ → 50% 부분 매도
💰 +5%↑ → 전량 익절
🔻 -3%↓ → 손절
🎯 고점 대비 -2% → 트레일링 스탑
⏰ 15:00 미청산 → 강제 청산

<b>[ 분석 시스템 ]</b>
📊 /scan — 조건별 통과 여부 + 선정 이유 상세 + 로그 자동 저장
📈 /report — 조건별/수급별/시장상황별/눌림깊이별 승률 분석
🔄 /update_results — 전날 스캔 결과 업데이트 (날짜 검증 포함)

<b>[ 실시간 파라미터 조정 ]</b>
⚙️ /config set scan.surge_threshold_e 7.0   ← 조건E 급등 기준 완화
⚙️ /config set scan.envelope_band_pct 15.0  ← Envelope 밴드 조정
⚙️ /config set scan.api_delay_sec 1.0       ← 429 방지 딜레이 조정
"""
