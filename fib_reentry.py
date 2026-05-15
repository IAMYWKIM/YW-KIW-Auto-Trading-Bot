"""
fib_reentry.py — 피보나치 조정대 기반 재진입 감시 시스템
v1.0

[전략 배경]
  단타에서 손절 후 같은 종목 완전 차단은 주도주의 경우 기회를 놓침.
  주도주는 손절 이후에도 피보나치 조정대에서 지지를 받고 다시 상승하는
  패턴이 자주 나타남. 이 지지를 기다렸다가 재진입하는 전략.

[피보나치 조정 계산 방식]
  1. 갭상승 주도주 (오늘 시가 > 전일 종가):
     - 기준점 High: 당일 고점
     - 기준점 Base: 전일 종가 (갭 레벨 = 강한 지지선)
     - Fib 0.236, 0.382 선이 핵심 지지

  2. 일반 급등 종목:
     - 기준점 High: 당일 고점
     - 기준점 Base: 당일 저점
     - Fib 0.382, 0.500 선이 핵심 지지

[재진입 조건 (모두 충족 시)]
  ① 손절 후 최소 N분 경과 (기본 10분) — 노이즈 구간 회피
  ② 현재가가 Fib 조정대 진입 (±0.5% 이내)
  ③ 해당 Fib 레벨에서 로컬 저점 형성 (추가 하락 멈춤)
  ④ 저점 대비 반등 0.3% 이상 확인 (모멘텀 복귀)
  ⑤ 현재가가 손절가보다 낮음 (더 저렴하게 재진입)

[사용 흐름]
  # 손절 시
  watcher = FibWatcher.from_position(pos, prev_close, today_open)
  fib_mgr.add(watcher)

  # 30초 루프에서
  signals = fib_mgr.check_all(broker)  → 재진입 신호 목록
  for sig in signals:
      entry_sig = strategy.check_entry(sig.to_candidate(), cash)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
KST    = pytz.timezone("Asia/Seoul")

# 피보나치 비율
FIB_LEVELS = [
    (0.236, "0.236"),
    (0.382, "0.382"),
    (0.500, "0.500"),
    (0.618, "0.618"),
]

# Fib 레벨 근접 판단 허용 오차 (±0.5%)
FIB_TOLERANCE_PCT = 0.5

# 반등 확인 최소 상승률 (%)
BOUNCE_MIN_PCT = 0.3


@dataclass
class FibWatcher:
    """
    손절 후 피보나치 조정대 재진입 감시 단위

    [핵심 속성]
      code          : 종목코드
      name          : 종목명
      stop_price    : 손절 체결가
      stop_time     : 손절 시각 (unix timestamp)
      today_high    : 손절 당시 당일 고점 (day_high)
      base_price    : 피보나치 기준 저점
                      - 갭상승: prev_close
                      - 일반  : today_low (손절 시점의 저점)
      is_gap_up     : 갭상승 여부 (시가 > 전일종가)

    [상태 추적]
      fib_levels    : {비율: 가격} — 계산된 Fib 레벨
      at_fib        : 현재 진입 중인 Fib 레벨 비율 (None이면 미진입)
      fib_zone_low  : Fib 구간 내 최저가 (반등 측정 기준점)
      bounce_pct    : 최저가 대비 현재 반등률
      ready         : 재진입 신호 발생 여부
      triggered     : 신호 이미 소비됨 (중복 방지)
    """
    code:          str
    name:          str
    stop_price:    int
    stop_time:     float   # time.time()
    today_high:    int
    base_price:    int     # Fib 기준 저점
    is_gap_up:     bool
    prev_close:    int

    # 계산된 Fib 레벨 {비율: 가격}
    fib_levels:    dict = field(default_factory=dict)

    # 상태 추적
    at_fib:        Optional[float] = None   # 현재 Fib 레벨 (0.236 등)
    fib_zone_low:  Optional[int]   = None   # Fib 구간 내 최저가
    bounce_pct:    float           = 0.0
    ready:         bool            = False
    triggered:     bool            = False
    last_price:    int             = 0
    check_count:   int             = 0

    def __post_init__(self):
        self._calc_fib_levels()

    def _calc_fib_levels(self):
        """피보나치 레벨 계산"""
        swing = self.today_high - self.base_price
        if swing <= 0:
            # 고점=저점인 경우 (비정상) → 감시 불필요
            self.triggered = True
            return

        self.fib_levels = {}
        for ratio, label in FIB_LEVELS:
            price = int(self.today_high - swing * ratio)
            self.fib_levels[ratio] = price
            logger.debug(
                f"[FibWatcher] {self.name}({self.code}) "
                f"Fib {label}: {price:,}원 "
                f"({'갭기준' if self.is_gap_up else '저점기준'})"
            )

    @classmethod
    def from_position(
        cls,
        code:       str,
        name:       str,
        stop_price: int,
        day_high:   int,
        today_low:  int,
        prev_close: int,
        today_open: int,
    ) -> "FibWatcher":
        """
        손절된 포지션으로부터 FibWatcher 생성

        Args:
            code       : 종목코드
            name       : 종목명
            stop_price : 손절 체결가
            day_high   : 손절 시점까지의 당일 고점
            today_low  : 손절 시점까지의 당일 저점
            prev_close : 전일 종가
            today_open : 당일 시가
        """
        is_gap_up  = today_open > prev_close
        base_price = prev_close if is_gap_up else today_low

        return cls(
            code       = code,
            name       = name,
            stop_price = stop_price,
            stop_time  = time.time(),
            today_high = day_high,
            base_price = base_price,
            is_gap_up  = is_gap_up,
            prev_close = prev_close,
        )

    def check(
        self,
        cur_price:     int,
        min_wait_min:  int = 10,
        active_fib:    list[float] = None,   # 감시할 Fib 레벨 (기본: [0.236, 0.382])
    ) -> bool:
        """
        현재가 기반으로 Fib 재진입 신호 확인

        Args:
            cur_price    : 현재 시세
            min_wait_min : 손절 후 최소 대기 시간 (분)
            active_fib   : 감시할 Fib 레벨 목록

        Returns:
            True = 재진입 신호 발생
        """
        if self.triggered or not self.fib_levels:
            return False

        if active_fib is None:
            # 갭상승 주도주: 0.236, 0.382 / 일반: 0.382, 0.500
            active_fib = [0.236, 0.382] if self.is_gap_up else [0.382, 0.500]

        self.check_count += 1
        self.last_price   = cur_price

        # ① 최소 대기 시간 체크
        elapsed_min = (time.time() - self.stop_time) / 60
        if elapsed_min < min_wait_min:
            logger.debug(
                f"[FibWatcher] {self.code} 대기중 "
                f"({elapsed_min:.1f}/{min_wait_min}분)"
            )
            return False

        # ② 손절가보다 현재가가 낮은지 확인
        #    (더 비싸게 재진입하면 리스크 증가)
        if cur_price > self.stop_price * 1.005:   # 0.5% 여유
            logger.debug(
                f"[FibWatcher] {self.code} 손절가({self.stop_price:,}) 위 — 스킵"
            )
            return False

        # ③ 어느 Fib 레벨 근처인지 확인
        near_fib = None
        for ratio in active_fib:
            fib_price = self.fib_levels.get(ratio, 0)
            if fib_price <= 0:
                continue
            pct_diff = abs(cur_price - fib_price) / fib_price * 100
            if pct_diff <= FIB_TOLERANCE_PCT:
                near_fib = ratio
                break   # 가장 가까운 상위 Fib 레벨 우선

        # Fib 구간 진입
        if near_fib is not None:
            if self.at_fib != near_fib:
                self.at_fib       = near_fib
                self.fib_zone_low = cur_price   # 새 Fib 구간 진입 시 저점 초기화
                self.bounce_pct   = 0.0
                logger.info(
                    f"[FibWatcher] {self.name}({self.code}) "
                    f"Fib {near_fib} 구간 진입! "
                    f"가격:{cur_price:,}원 "
                    f"레벨:{self.fib_levels[near_fib]:,}원"
                )

            # ④ 저점 갱신
            if self.fib_zone_low is None or cur_price < self.fib_zone_low:
                self.fib_zone_low = cur_price

            # ⑤ 반등률 계산
            if self.fib_zone_low and self.fib_zone_low > 0:
                self.bounce_pct = (
                    (cur_price - self.fib_zone_low) / self.fib_zone_low * 100
                )

            # ⑥ 반등 확인 → 재진입 신호
            if self.bounce_pct >= BOUNCE_MIN_PCT:
                self.ready = True
                logger.info(
                    f"[FibWatcher] ✅ {self.name}({self.code}) "
                    f"Fib {near_fib} 반등 확인! "
                    f"저점:{self.fib_zone_low:,}원 → 현재:{cur_price:,}원 "
                    f"(+{self.bounce_pct:.2f}%) "
                    f"{'갭기준' if self.is_gap_up else '저점기준'}"
                )
                return True

        else:
            # Fib 구간 벗어남 (너무 많이 하락하거나 회복)
            if self.at_fib is not None:
                fib_price = self.fib_levels.get(self.at_fib, 0)
                if cur_price < fib_price * (1 - 0.01):
                    # 다음 Fib 레벨로 낙하 중
                    self.at_fib       = None
                    self.fib_zone_low = None
                    self.bounce_pct   = 0.0
                    logger.debug(
                        f"[FibWatcher] {self.code} Fib 구간 이탈 (추가 하락)"
                    )

        return False

    def consume(self) -> dict:
        """재진입 신호 소비 — candidate dict 형식으로 반환"""
        self.triggered = True
        fib_price = self.fib_levels.get(self.at_fib, self.last_price)
        return {
            "code":          self.code,
            "name":          self.name,
            "cur_price":     self.last_price,
            "prev_close":    self.prev_close,
            "rise_pct":      round(
                (self.last_price - self.prev_close) / self.prev_close * 100, 2
            ) if self.prev_close > 0 else 0,
            "volume":        0,
            "volume_ratio":  0,
            "trading_value": 0,
            "vwap":          0,
            "source":        f"FIB_{self.at_fib}",
            "score":         60,   # Fib 재진입은 기본 60점 부여
            "scan_time":     datetime.now(KST).strftime("%H:%M"),
            # 추가 메타
            "_fib_ratio":    self.at_fib,
            "_fib_price":    fib_price,
            "_fib_zone_low": self.fib_zone_low,
            "_bounce_pct":   self.bounce_pct,
            "_is_gap_up":    self.is_gap_up,
            "_reentry":      True,   # 재진입 플래그
        }

    def summary(self) -> str:
        """현재 상태 요약"""
        elapsed = int((time.time() - self.stop_time) / 60)
        levels  = ", ".join(
            f"Fib{r}: {p:,}"
            for r, p in sorted(self.fib_levels.items())
            if r <= 0.618
        )
        at_str  = f"Fib{self.at_fib} 구간" if self.at_fib else "대기중"
        return (
            f"{self.name}({self.code})\n"
            f"  손절가:{self.stop_price:,} | 고점:{self.today_high:,} "
            f"| {'갭' if self.is_gap_up else '일반'}기준:{self.base_price:,}\n"
            f"  {levels}\n"
            f"  상태:{at_str} | 반등:{self.bounce_pct:.2f}% | "
            f"경과:{elapsed}분"
        )


class FibReentryManager:
    """
    FibWatcher 목록 관리 및 일괄 감시

    [사용법]
        fib_mgr = FibReentryManager()

        # 손절 시 등록
        watcher = FibWatcher.from_position(...)
        fib_mgr.add(watcher)

        # 30초 주기에서 체크
        signals = fib_mgr.check_all(broker, cfg)
        for candidate in signals:
            # candidate는 scanner.scan() 결과와 동일한 형식
            entry_sig = strategy.check_entry(candidate, cash)
    """

    def __init__(self):
        self._watchers: dict[str, FibWatcher] = {}

    def add(self, watcher: FibWatcher):
        """감시 대상 등록"""
        if watcher.triggered:
            logger.warning(
                f"[FibMgr] {watcher.code} Fib 레벨 계산 불가 — 감시 건너뜀"
            )
            return
        self._watchers[watcher.code] = watcher
        logger.info(
            f"[FibMgr] {watcher.name}({watcher.code}) Fib 감시 등록\n"
            f"  {watcher.summary()}"
        )

    def remove(self, code: str):
        """감시 해제"""
        self._watchers.pop(code, None)

    def is_watching(self, code: str) -> bool:
        """해당 종목이 감시 중인지"""
        return code in self._watchers and not self._watchers[code].triggered

    def check_all(
        self,
        broker,
        min_wait_min:  int         = 10,
        active_fib:    list[float] = None,
    ) -> list[dict]:
        """
        모든 감시 대상 일괄 체크 — 재진입 신호 목록 반환

        Args:
            broker       : KiwoomBroker 인스턴스
            min_wait_min : 최소 대기 시간 (분)
            active_fib   : 감시할 Fib 레벨

        Returns:
            재진입 신호가 발생한 candidate dict 목록
        """
        if not self._watchers:
            return []

        signals    = []
        to_remove  = []

        for code, watcher in self._watchers.items():
            if watcher.triggered:
                to_remove.append(code)
                continue

            try:
                info      = broker.get_stock_info(code)
                cur_price = info["cur_price"]
                today_low = info["low"]

                # 당일 저점 갱신 반영 (비갭 종목용)
                if not watcher.is_gap_up and today_low > 0:
                    if today_low < watcher.base_price:
                        old_base = watcher.base_price
                        watcher.base_price = today_low
                        watcher._calc_fib_levels()
                        logger.debug(
                            f"[FibMgr] {code} 저점 갱신 "
                            f"{old_base:,} → {today_low:,} (Fib 재계산)"
                        )

                if watcher.check(cur_price, min_wait_min, active_fib):
                    signals.append(watcher.consume())
                    to_remove.append(code)
                    logger.info(
                        f"[FibMgr] {code} 재진입 신호 발생 → "
                        f"후보 목록에 추가"
                    )

            except Exception as e:
                logger.debug(f"[FibMgr] {code} 체크 오류: {e}")

        for code in to_remove:
            self._watchers.pop(code, None)

        return signals

    def init_daily(self):
        """장 시작 시 전일 감시 목록 초기화"""
        self._watchers.clear()
        logger.info("[FibMgr] 일일 초기화 — 감시 목록 초기화")

    def get_watching_codes(self) -> list[str]:
        """현재 감시 중인 종목 코드 목록"""
        return [
            code for code, w in self._watchers.items()
            if not w.triggered
        ]

    def format_for_telegram(self) -> str:
        """텔레그램 메시지 포맷"""
        active = [
            w for w in self._watchers.values()
            if not w.triggered
        ]
        if not active:
            return "📡 <b>Fib 감시 대상 없음</b>"

        lines = [f"📡 <b>[ Fib 재진입 감시 — {len(active)}개 ]</b>\n"]
        for w in active:
            elapsed = int((time.time() - w.stop_time) / 60)
            at_str  = f"⚡ Fib{w.at_fib} 구간" if w.at_fib else "🔍 대기"
            bounce  = f"+{w.bounce_pct:.2f}%" if w.bounce_pct > 0 else "-"
            lines.append(
                f"{at_str}  <b>{w.name}({w.code})</b>\n"
                f"  손절가:{w.stop_price:,}  고점:{w.today_high:,}\n"
                f"  Fib0.236:{w.fib_levels.get(0.236, 0):,}  "
                f"Fib0.382:{w.fib_levels.get(0.382, 0):,}\n"
                f"  반등:{bounce}  경과:{elapsed}분  "
                f"{'갭상승' if w.is_gap_up else '일반'}"
            )
        return "\n".join(lines)
