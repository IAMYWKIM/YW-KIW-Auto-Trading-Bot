# 키움증권 국내주식 자동매매 봇

> 종가베팅 전략 기반 자동매매 시스템  
> GCP Ubuntu 서버 + 키움 REST API + 텔레그램 봇

**현재 버전: v1.3.0** | [변경 이력 →](CHANGELOG.md)

---

## 파일 구조

```
kiw_trader/
├── main.py              # 스케줄러 + 봇 진입점          v1.1
├── broker.py            # 키움 REST API 통신            v1.0
├── config.py            # 계좌/장부/잠금 관리            v1.0
├── strategy.py          # 스캔 / 매매 신호 생성          v1.3
├── strategy_config.py   # 전략 조건 설정 관리            v1.3
├── telegram_bot.py      # 텔레그램 봇 명령어             v1.2
├── scan_logger.py       # 스캔 로그 저장 / 승률 분석     v1.0
├── VERSION              # 단일 버전 소스
├── CHANGELOG.md         # 전체 변경 이력
├── README.md            # 이 파일
├── .env                 # 환경 변수 (git 제외)
├── data/
│   ├── strategy_config.json   # 전략 설정 (텔레그램으로 변경 가능)
│   ├── positions.json         # 보유 포지션
│   ├── ledger.json            # 매매 장부
│   └── scan_log/              # 스캔 로그 (YYYYMMDD_HHMMSS_scan.json)
└── logs/
    ├── kiwoom_trader.log      # 운영 로그 (7일 보관)
    └── kiwoom_error.log       # 에러 로그
```

---

## 전략 개요

### 종가베팅 전략

장 마감 직전(15:10~15:20) 다음날 상승 가능성이 높은 종목을 매수하고,  
다음날 익절/손절하는 단기 전략입니다.

**후보 선정 3단계**

```
1단계: 후보 풀 수집 (3가지 소스)
   소스1: ka10016 — 신고가 종목
   소스2: ka10023 — 당일 거래량 급증
   소스3: ka10024 — 거래량 갱신

2단계: 핵심 필터
   ✅ 최근 5일 내 +5% 이상 급등일 존재
   ✅ 거래대금 100억 이상 (전일 기준)
   ✅ MA5 > MA20 정배열
   ✅ RSI 30~80 범위
   ✅ 20일 고점 대비 -20% 이내

3단계: 점수화 후 정렬
   거래대금(35점) + 급등강도(30점) + 수급(20점) + 신고가근접(15점)
```

**매수 조건 (15:10~15:20)**

```
전일 종가 대비 -0.5% ~ -10% 눌림 중인 종목
```

**매도 전략 (D+1)**

```
08:00~08:50  NXT 프리마켓 +2% 이상 → 즉시 매도
09:00~10:00  +3% 이상 → 50% 부분 매도
장중         +5% 이상 → 전량 매도
장중         -3% 이하 → 손절
장중         고점 대비 -2% → 트레일링 스탑
15:00        미청산 → 강제 청산
```

---

## 설치 및 환경 설정

### 1. 환경 변수 (.env)

```env
# 키움 REST API
KIWOOM_APP_KEY=발급받은_앱키
KIWOOM_SECRET_KEY=발급받은_시크릿키
KIWOOM_ACCOUNT=계좌번호
KIWOOM_MODE=MOCK         # MOCK(모의) 또는 REAL(실전)

# 텔레그램
TELEGRAM_BOT_TOKEN=봇_토큰
TELEGRAM_CHAT_ID=채팅방_ID
```

### 2. 패키지 설치

```bash
cd ~/kiw_trader
source kiw_venv/bin/activate
pip install python-telegram-bot apscheduler pytz python-dotenv requests
```

### 3. systemd 서비스 등록

```bash
sudo tee /etc/systemd/system/kiwoombot.service > /dev/null << 'EOF'
[Unit]
Description=키움증권 국내주식 자동매매 봇
After=network.target

[Service]
Type=simple
User=iamywkim
WorkingDirectory=/home/iamywkim/kiw_trader
Environment="TZ=Asia/Seoul"
ExecStart=/home/iamywkim/kiw_trader/kiw_venv/bin/python3 main.py
Restart=always
RestartSec=10
StandardOutput=append:/home/iamywkim/kiw_trader/logs/kiwoombot.log
StandardError=append:/home/iamywkim/kiw_trader/logs/kiwoombot_error.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kiwoombot
sudo systemctl start kiwoombot
```

### 4. 배포 스크립트

```bash
# /usr/local/bin/deploy_kiw 등록 후
deploy_kiw    # .py 파일 이동 + 캐시 삭제 + 서비스 재시작
```

---

## 자동 스케줄 (KST 평일)

| 시각 | 작업 |
|------|------|
| 09:00 | API 토큰 갱신 |
| 14:30 | 후보 종목 사전 스캔 + 텔레그램 알림 |
| 15:10 | 눌림 확인 + 자동 매수 |
| 15:30 | 전체 잠금 초기화 |
| 08:00~08:50 (매분) | D+1 NXT 프리마켓 감시 |
| 09:00~10:00 (매분) | D+1 오전 익절/손절 감시 |
| 15:00 | D+1 미청산 강제 청산 |
| 06:00 | 로그 7일 초과분 삭제 |

---

## 텔레그램 명령어

### 조회
| 명령어 | 설명 |
|--------|------|
| `/start` | 봇 시작 및 스케줄 안내 |
| `/status` | 보유 포지션 + 손익 |
| `/balance` | 계좌 잔고 |
| `/scan` | 후보 종목 스캔 (선정 이유 포함, 로그 저장) |
| `/history` | 매매 이력 |

### 분석
| 명령어 | 설명 |
|--------|------|
| `/report` | 최근 7일 승률 분석 |
| `/report 14` | 최근 14일 분석 |
| `/update_results` | 전날 스캔 결과 업데이트 |

### 전략 설정
| 명령어 | 설명 |
|--------|------|
| `/config show` | 전체 설정 조회 |
| `/config show scan` | 스캔 조건 |
| `/config show entry` | 진입 조건 |
| `/config show risk` | 리스크 관리 |
| `/config show sell` | 매도 전략 |
| `/config set [키] [값]` | 설정 변경 (즉시 반영) |
| `/config reset` | 기본값 초기화 |

### 주요 설정 변경 예시
```
/config set entry.max_positions 20       ← 분석 모드 (20개 스캔)
/config set entry.position_size_pct 0   ← 자동 매수 차단
/config set entry.pullback_min_pct -15.0 ← 급락장 대응
/config set scan.min_trading_value 10000000000  ← 거래대금 100억
```

### 수동 매매
```
/buy 005930 10 75000    ← 삼성전자 10주 75,000원 지정가
/buy 005930 10 0        ← 시장가
/sell 005930 10 76000
/lock 005930            ← 당일 매수 잠금
/unlock 005930
```

---

## 전략 설정 파라미터

### 스캔 조건 (scan)
| 키 | 기본값 | 설명 |
|----|--------|------|
| `min_trading_value` | 10000000000 | 거래대금 최소 (100억원) |
| `volume_ratio_min` | 1.5 | 거래량 비율 최소 |
| `near_high_threshold_pct` | -20.0 | 신고가 허용 범위 (%) |
| `recent_surge_days` | 5 | 급등 탐지 기간 (일) |
| `recent_surge_min_pct` | 5.0 | 급등 최소 상승률 (%) |
| `ma_alignment` | true | 정배열 필터 (MA5>MA20) |

### 진입 조건 (entry)
| 키 | 기본값 | 설명 |
|----|--------|------|
| `pullback_min_pct` | -10.0 | 눌림 최소 (%) |
| `pullback_max_pct` | -0.5 | 눌림 최대 (%) |
| `entry_start_time` | 15:10 | 매수 시작 |
| `entry_end_time` | 15:20 | 매수 마감 |
| `rsi_min` | 30 | RSI 최솟값 |
| `rsi_max` | 80 | RSI 최댓값 |
| `max_positions` | 3 | 최대 보유 종목 수 |
| `position_size_pct` | 15 | 종목당 자산 비율 (%) |

### 리스크 (risk)
| 키 | 기본값 | 설명 |
|----|--------|------|
| `stop_loss_pct` | -3.0 | 손절선 (%) |
| `take_profit_pct` | 5.0 | 익절선 (%) |
| `trailing_stop` | true | 트레일링 스탑 |
| `trailing_gap_pct` | 2.0 | 트레일링 간격 (%) |

---

## 데이터 분석 워크플로

```
매일 14:30  /scan 자동 실행 → data/scan_log/ 에 JSON 저장
             (급등률, 눌림, 거래대금, RSI, MA, 수급 등 기록)

다음날 장 후  /update_results → 전날 후보들의 실제 등락 업데이트

주 1회       /report 7 → 조건별 승률 분석
             (거래대금별, RSI별, 눌림 깊이별 승률 확인)

조건 최적화  분석 결과 기반으로 /config set 으로 조건 조정
```

---

## 버전 관리 규칙

```
X.Y.Z
│ │ └── Z: 버그 수정 / 소수 개선 (패치)
│ └──── Y: 기능 추가 / 전략 조건 변경 (마이너)
└────── X: 전략 전면 개편 (메이저)
```

각 변경 시:
1. `VERSION` 파일 업데이트
2. `CHANGELOG.md` 에 내용 추가
3. 변경된 `.py` 파일 헤더의 버전 업데이트
4. `deploy_kiw` 로 배포

---

## 사용 중인 키움 REST API

| API ID | 명칭 | URL | 용도 |
|--------|------|-----|------|
| ka10016 | 신고저가 | /api/dostk/stkinfo | 소스1: 신고가 종목 |
| ka10023 | 거래량급증 | /api/dostk/rkinfo | 소스2: 당일 급증 |
| ka10024 | 거래량갱신 | /api/dostk/stkinfo | 소스3: 갱신 종목 |
| ka10081 | 주식일봉 | /api/dostk/chart | MA / 거래대금 / 급등 확인 |
| ka10009 | 기관요청 | /api/dostk/frgnistt | 기관/외국인 수급 |
| ka10001 | 주식기본정보 | /api/dostk/stkinfo | 현재가 |
| kt10000 | 매수주문 | /api/dostk/ordr | 매수 |
| kt10001 | 매도주문 | /api/dostk/ordr | 매도 |
| kt00018 | 잔고조회 | /api/dostk/acnt | 보유 종목 |

---

*마지막 업데이트: 2026-04-09 | v1.3.0*
