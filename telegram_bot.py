"""
telegram_bot.py — 키움 국내주식 자동매매 텔레그램 봇
종가베팅 + 단타 전략 통합 버전

[종가베팅 명령어]
  /start           — 봇 시작 및 명령어 안내
  /status          — 보유 포지션 및 손익 현황
  /balance         — 계좌 잔고 조회
  /scan            — 종가베팅 후보 종목 수동 스캔
  /config          — 종가베팅 전략 설정 조회/변경
  /buy [코드] [수량] [가격] — 수동 매수
  /sell [코드] [수량] [가격] — 수동 매도
  /history         — 매매 이력 조회
  /lock [코드]     — 특정 종목 당일 잠금
  /unlock [코드]   — 잠금 해제
  /report          — 스캔 결과 승률 분석 리포트
  /update_results  — 전날 스캔 결과 업데이트
  /version         — 버전 정보

[단타 명령어]
  /scalp_status    — 단타 포지션 현황
  /scalp_scan      — 단타 후보 즉시 스캔 (장외 시간에도 동작)
  /scalp_config    — 단타 전략 설정 조회/변경
  /scalp_stop      — 단타 신규 진입 ON/OFF 토글
  /scalp_exit_all  — 단타 보유 종목 전량 즉시 청산

[v2.0 변경]
  단타 봇(scalp_main.py) → 기존 main.py + telegram_bot.py에 통합
  동일 텔레그램 봇 토큰으로 두 전략을 동시 운영
"""

import asyncio
import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)
from dotenv import load_dotenv

from broker import KiwoomBroker
from config import Config
from strategy import Strategy
from strategy_config import StrategyConfig
from scan_logger import ScanLogger
from scalp_ledger import ScalpLedger
from version_history import VERSION_HISTORY, CURRENT_VERSION, CURRENT_FEATURES

load_dotenv()
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))


def is_authorized(update: Update) -> bool:
    """허용된 사용자만 명령 실행"""
    return update.effective_chat.id == TELEGRAM_CHAT_ID


class TelegramBot:

    def __init__(self, broker: KiwoomBroker, cfg: Config,
                 scfg: StrategyConfig, strategy: Strategy,
                 # ── 단타 전략 컴포넌트 (선택적) ──────────────
                 scanner=None,         # DayTradingScanner
                 scalp_strategy=None,  # ScalpStrategy
                 scalp_cfg=None):      # ScalpConfig
        self.broker         = broker
        self.cfg            = cfg
        self.scfg           = scfg
        self.strategy       = strategy
        self.scan_logger    = ScanLogger()
        self.scalp_ledger   = ScalpLedger()   # 단타 매매 장부
        self.app            = None

        # 단타 컴포넌트 (main.py에서 주입)
        self.scanner        = scanner
        self.scalp_strategy = scalp_strategy
        self.scalp_cfg      = scalp_cfg

        # 단타 신규 진입 ON/OFF 플래그 (/scalp_stop 으로 토글)
        self.scalp_paused   = False

    # ──────────────────────────────────────────────────────────
    # 봇 초기화
    # ──────────────────────────────────────────────────────────

    def build(self) -> Application:
        self.app = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .connect_timeout(30)
            .read_timeout(120)
            .write_timeout(120)
            .pool_timeout(30)
            .build()
        )

        # ── 종가베팅 명령어 ───────────────────────────────────
        handlers = [
            ("start",          self.cmd_start),
            ("help",           self.cmd_help),
            ("guide",          self.cmd_guide),   # 📱 모바일 가이드 (신규)
            ("status",         self.cmd_status),
            ("balance",        self.cmd_balance),
            ("scan",           self.cmd_scan),
            ("config",         self.cmd_config),
            ("buy",            self.cmd_buy),
            ("sell",           self.cmd_sell),
            ("history",        self.cmd_history),
            ("lock",           self.cmd_lock),
            ("unlock",         self.cmd_unlock),
            ("report",         self.cmd_report),
            ("update_results", self.cmd_update_results),
            ("version",        self.cmd_version),
        ]
        # ── 단타 명령어 (컴포넌트 주입된 경우에만 등록) ──────
        if self.scanner and self.scalp_strategy:
            handlers += [
                ("scalp_status",    self.cmd_scalp_status),
                ("scalp_scan",      self.cmd_scalp_scan),
                ("scalp_config",    self.cmd_scalp_config),
                ("scalp_stop",      self.cmd_scalp_stop),
                ("scalp_exit_all",  self.cmd_scalp_exit_all),
                ("scalp_debug",     self.cmd_scalp_debug),
                ("scalp_summary",   self.cmd_scalp_summary),
                ("scalp_market",    self.cmd_scalp_market),
                ("scalp_fib",       self.cmd_scalp_fib),
                # ── [v1.3] 하이브리드 모드 수동 감시 ──────────────
                ("scalp_add",       self.cmd_scalp_add),
                ("scalp_remove",    self.cmd_scalp_remove),
                ("scalp_watchlist", self.cmd_scalp_watchlist),
            ]
            logger.info("[TelegramBot] 단타 명령어 등록 완료")

        for cmd, handler in handlers:
            self.app.add_handler(CommandHandler(cmd, handler))

        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        logger.info("[TelegramBot] 봇 초기화 완료")
        return self.app

    async def send(self, text: str, parse_mode: str = "HTML",
                   keyboard=None) -> None:
        """채팅방에 메시지 전송 (4096자 초과 시 자동 분할)"""
        for i in range(0, max(1, len(text)), 4000):
            await self.app.bot.send_message(
                chat_id      = TELEGRAM_CHAT_ID,
                text         = text[i:i+4000],
                parse_mode   = parse_mode,
                reply_markup = keyboard if i == 0 else None,
            )

    # ──────────────────────────────────────────────────────────
    # /start
    # ──────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        now       = datetime.now().strftime("%Y-%m-%d %H:%M")
        scalp_on  = self.scanner is not None
        msg = (
            f"<b>[ 키움 국내주식 자동매매 봇 ]</b>\n"
            f"<i>{now}</i>\n\n"
            f"<b>[ 종가베팅 스케줄 ]</b>\n"
            f"🔹 14:30 — 후보 종목 사전 스캔\n"
            f"🔹 15:10 — 눌림 확인 및 자동 매수\n"
            f"🔹 D+1 08:00 — NXT 프리마켓 감시\n"
            f"🔹 D+1 09:00 — 정규장 오전 익절/손절\n"
            f"🔹 D+1 15:00 — 미청산 강제 청산\n\n"
        )
        if scalp_on:
            msg += (
                f"<b>[ 단타 스케줄 ]</b>\n"
                f"⚡ 08:50 — 장전 준비 (거래량 캐시)\n"
                f"⚡ 09:00~13:00 — 30초 주기 자동 매매\n"
                f"⚡ 15:10 — 강제 청산 경고\n"
                f"⚡ 15:20 — 전량 강제 청산\n\n"
            )
        msg += (
            f"<b>[ 주요 명령어 ]</b>\n"
            f"▶️ /status    — 종가베팅 포지션\n"
            f"▶️ /scan      — 종가베팅 스캔\n"
            f"▶️ /balance   — 계좌 잔고\n"
            f"▶️ /config    — 종가베팅 설정\n"
        )
        if scalp_on:
            msg += (
                f"⚡ /scalp_status  — 단타 포지션\n"
                f"⚡ /scalp_scan    — 단타 스캔\n"
                f"⚡ /scalp_stop    — 단타 ON/OFF\n"
                f"⚡ /scalp_config  — 단타 설정\n"
            )
        msg += "▶️ /help — 전체 명령어"
        await update.message.reply_text(msg, parse_mode="HTML")

    # ──────────────────────────────────────────────────────────
    # /help
    # ──────────────────────────────────────────────────────────

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        scalp_on = self.scanner is not None
        msg = (
            "<b>[ 전체 명령어 ]</b>\n\n"
            "<b>── 종가베팅 ──</b>\n"
            "  /status              — 보유 포지션 + 손익\n"
            "  /balance             — 예수금 + 잔고\n"
            "  /scan                — 후보 종목 수동 스캔\n"
            "  /history             — 매매 이력\n"
            "  /report [일수]       — 승률 분석 리포트\n"
            "  /update_results      — 전날 결과 업데이트\n"
            "  /version             — 버전 정보\n"
            "  /config show [그룹]  — 설정 조회\n"
            "  /config set [키] [값]— 설정 변경\n"
            "  /buy [코드] [수량] [가격]\n"
            "  /sell [코드] [수량] [가격]\n"
            "  /lock [코드] / /unlock [코드]\n"
        )
        if scalp_on:
            status_icon = "🟢" if not self.scalp_paused else "🔴"
            msg += (
                f"\n<b>── 단타 ({status_icon} {'실행중' if not self.scalp_paused else '중단중'}) ──</b>\n"
                "  /scalp_status        — 단타 포지션 현황\n"
                "  /scalp_scan          — 급등 종목 즉시 스캔\n"
                "  /scalp_stop          — 신규 진입 ON/OFF\n"
                "  /scalp_exit_all      — 전량 즉시 청산\n"
                "  /scalp_market        — 시장 상황 조회\n"
                "  /scalp_fib           — Fib 재진입 감시 현황\n"
                "  /scalp_config show   — 단타 설정 조회\n"
                "  /scalp_config set [키] [값]\n"
                "  /scalp_summary [daily|weekly|monthly|날짜]\n"
                "  /scalp_debug         — 봇 상태 진단\n"
                "\n<b>── 하이브리드 (수동 주도주 지정) ──</b>\n"
                "  /scalp_add [코드]    — 주도주 수동 추가\n"
                "  /scalp_remove [코드] — 감시 중단\n"
                "  /scalp_watchlist     — 수동 감시 목록\n"
            )
        msg += "\n📱 <b>/guide</b> — 아이폰 최적화 인라인 가이드"
        await update.message.reply_text(msg, parse_mode="HTML")

    # ──────────────────────────────────────────────────────────
    # /guide — 📱 아이폰 최적화 인라인 명령어 가이드
    # ──────────────────────────────────────────────────────────

    async def cmd_guide(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /guide — 📱 아이폰 최적화 인라인 명령어 가이드
        카테고리별 버튼 메뉴 → 탭 한 번으로 해당 섹션 조회
        앱 전환 없이 텔레그램 안에서 바로 참조 가능
        """
        if not is_authorized(update): return
        args    = ctx.args or []
        section = args[0].lower() if args else ""
        if not section:
            await self._guide_menu(update)
        else:
            guide_map = {
                "info": self._guide_swing_info, "scan": self._guide_swing_scan,
                "order": self._guide_swing_order, "config": self._guide_swing_config,
                "scalp_info": self._guide_scalp_info, "scalp_ctrl": self._guide_scalp_ctrl,
                "scalp_sum": self._guide_scalp_sum, "scalp_cfg": self._guide_scalp_cfg,
                "params": self._guide_params, "scalp_params": self._guide_scalp_params,
                "costs": self._guide_costs, "fib": self._guide_fib,
            }
            fn = guide_map.get(section)
            if fn: await fn(update)
            else:  await self._guide_menu(update)

    async def _guide_menu(self, update):
        text = (
            "📱 <b>명령어 가이드</b>\n"
            "버튼을 탭하면 해당 섹션으로 이동합니다\n\n"
            "버전: <code>v2.1.0</code>  |  종가베팅 + 단타 통합"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 종가베팅 조회",    callback_data="GUIDE:info"),
             InlineKeyboardButton("🔍 스캔·분석",        callback_data="GUIDE:scan")],
            [InlineKeyboardButton("💰 수동 주문",        callback_data="GUIDE:order"),
             InlineKeyboardButton("⚙️ 종가베팅 설정",    callback_data="GUIDE:config")],
            [InlineKeyboardButton("⚡ 단타 조회",        callback_data="GUIDE:scalp_info"),
             InlineKeyboardButton("🎛️ 단타 제어",        callback_data="GUIDE:scalp_ctrl")],
            [InlineKeyboardButton("📈 매매 요약",        callback_data="GUIDE:scalp_sum"),
             InlineKeyboardButton("🔧 단타 설정",        callback_data="GUIDE:scalp_cfg")],
            [InlineKeyboardButton("📋 종가베팅 파라미터",callback_data="GUIDE:params"),
             InlineKeyboardButton("📋 단타 파라미터",    callback_data="GUIDE:scalp_params")],
            [InlineKeyboardButton("💸 거래 비용 안내",   callback_data="GUIDE:costs"),
             InlineKeyboardButton("📡 Fib 재진입 전략",  callback_data="GUIDE:fib")],
        ])
        # 명령어로 호출 시 → 새 메시지 전송
        # 콜백 버튼으로 호출 시 → 기존 메시지 수정
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )
        else:
            await update.message.reply_html(text, reply_markup=keyboard)

    async def _guide_swing_info(self, update):
        msg = (
            "📊 <b>[ 종가베팅 — 정보 조회 ]</b>\n\n"
            "<b>/start</b>  봇 시작 및 운영 스케줄 안내\n\n"
            "<b>/status</b>  보유 포지션 + 평가손익\n"
            "  → 잠금🔒/해제🔓 상태 + 인라인 매도 버튼\n\n"
            "<b>/balance</b>  예수금 + 총평가금액 + 총손익\n\n"
            "<b>/history</b> <code>[종목코드]</code>  매매 이력 (최근 10건)\n"
            "  예) <code>/history</code> — 전체\n"
            "  예) <code>/history 005930</code> — 삼성전자만\n\n"
            "<b>/version</b> <code>[history]</code>  버전 + 변경 이력\n"
            "  예) <code>/version history</code> — 전체 이력"
        )
        await self._guide_reply(update, msg, back="menu")

    async def _guide_swing_scan(self, update):
        msg = (
            "🔍 <b>[ 종가베팅 — 스캔·분석 ]</b>\n\n"
            "<b>/scan</b>  후보 종목 즉시 스캔 (10~30초)\n"
            "  → 조건 A~G 필터 / 결과 자동 로그 저장\n"
            "  → 14:30에 자동 실행\n\n"
            "<b>/report</b> <code>[일수]</code>  과거 스캔 승률 분석\n"
            "  예) <code>/report</code> — 최근 7일\n"
            "  예) <code>/report 14</code> — 14일\n"
            "  예) <code>/report 30</code> — 한 달\n\n"
            "<b>/update_results</b> <code>[날짜]</code>  실제 등락 기록\n"
            "  예) <code>/update_results</code> — 어제\n"
            "  예) <code>/update_results 20260514</code>"
        )
        await self._guide_reply(update, msg, back="menu")

    async def _guide_swing_order(self, update):
        msg = (
            "💰 <b>[ 종가베팅 — 수동 주문 ]</b>\n\n"
            "<b>/buy</b> <code>종목코드 수량 가격</code>\n"
            "  가격 0 = 시장가  (확인 버튼 있음)\n"
            "  예) <code>/buy 005930 10 75000</code>\n"
            "  예) <code>/buy 005930 10 0</code> — 시장가\n\n"
            "<b>/sell</b> <code>종목코드 수량 가격</code>\n"
            "  예) <code>/sell 005930 10 76000</code>\n"
            "  예) <code>/sell 005930 10 0</code> — 시장가\n\n"
            "<b>/lock</b> <code>종목코드</code>  당일 자동 매수 차단\n"
            "  예) <code>/lock 005930</code>\n\n"
            "<b>/unlock</b> <code>종목코드</code>  잠금 해제\n"
            "  → 15:30에 전체 자동 초기화"
        )
        await self._guide_reply(update, msg, back="menu")

    async def _guide_swing_config(self, update):
        msg = (
            "⚙️ <b>[ 종가베팅 — /config 설정 ]</b>\n\n"
            "<b>조회</b>\n"
            "<code>/config show</code>       — 전체\n"
            "<code>/config show scan</code>  — 스캔 조건\n"
            "<code>/config show entry</code> — 진입 조건\n"
            "<code>/config show risk</code>  — 리스크\n\n"
            "<b>자주 쓰는 변경</b>\n"
            "<code>/config set risk.stop_loss_pct -4.0</code>  손절 -4%\n"
            "<code>/config set entry.max_positions 5</code>    최대 5종목\n"
            "<code>/config set entry.position_size_pct 0</code> 자동매수 차단\n"
            "<code>/config reset</code>  기본값 초기화"
        )
        await self._guide_reply(update, msg, back="menu",
                                extra_btn=("📋 전체 파라미터", "GUIDE:params"))

    async def _guide_scalp_info(self, update):
        msg = (
            "⚡ <b>[ 단타 — 정보 조회 ]</b>\n\n"
            "<b>/scalp_status</b>  포지션 현황\n"
            "  → 평가손익 / 장중 고점 / 보유 시간\n\n"
            "<b>/scalp_scan</b>  급등 종목 즉시 스캔 (~15초)\n"
            "  → 장외 시간에도 동작\n\n"
            "<b>/scalp_market</b>  시장 상황\n"
            "  → KOSPI/KOSDAQ ETF 등락률\n"
            "  → 외인 매수 강도 점수\n"
            "  → STOP/CAUTION/NORMAL/BULLISH\n\n"
            "<b>/scalp_fib</b>  피보나치 감시 현황\n"
            "  → 손절 후 Fib 레벨 대기 종목\n\n"
            "<b>/scalp_debug</b>  봇 상태 진단\n"
            "  → 매수 안 될 때 원인 파악"
        )
        await self._guide_reply(update, msg, back="menu",
                                extra_btn=("📡 Fib 전략", "GUIDE:fib"))

    async def _guide_scalp_ctrl(self, update):
        msg = (
            "🎛️ <b>[ 단타 — 제어 명령 ]</b>\n\n"
            "<b>/scalp_stop</b>  신규 진입 ON/OFF 토글\n"
            "  → 중단 중에도 청산 감시는 계속\n\n"
            "<b>/scalp_exit_all</b>  전체 즉시 시장가 청산\n"
            "  ⚠️ 긴급 리스크 관리 시\n\n"
            "─────────────────\n"
            "<b>자동 강제청산 스케줄</b>\n"
            "15:10 — 경고 알림\n"
            "15:20 — 전량 자동 강제청산\n\n"
            "<b>시장 필터 등급별 제한</b>\n"
            "🛑 STOP → 진입 0개\n"
            "⚠️ CAUTION → 최대 1개\n"
            "🟢 NORMAL → 최대 2개\n"
            "🚀 BULLISH → 최대 3개"
        )
        await self._guide_reply(update, msg, back="menu")

    async def _guide_scalp_sum(self, update):
        msg = (
            "📈 <b>[ 단타 — /scalp_summary ]</b>\n\n"
            "<code>/scalp_summary</code>           버튼 선택 메뉴\n"
            "<code>/scalp_summary daily</code>     오늘 요약\n"
            "<code>/scalp_summary weekly</code>    이번 주 요약\n"
            "<code>/scalp_summary monthly</code>   이번 달 요약\n"
            "<code>/scalp_summary YYYYMMDD</code>  날짜별 상세\n\n"
            "상세 내역 포함 항목:\n"
            "  매수가 / 매도가 / 수익률 / 손익\n"
            "  투자금 / 보유시간 / 청산사유\n\n"
            "예) <code>/scalp_summary 20260515</code>"
        )
        await self._guide_reply(update, msg, back="menu")

    async def _guide_scalp_cfg(self, update):
        msg = (
            "🔧 <b>[ 단타 — /scalp_config ]</b>\n\n"
            "<b>조회</b>\n"
            "<code>/scalp_config show</code>        전체\n"
            "<code>/scalp_config show exit</code>   청산 조건\n"
            "<code>/scalp_config show risk</code>   리스크\n\n"
            "<b>자주 쓰는 변경</b>\n"
            "<code>/scalp_config set scan.entry_end_time 14:30</code>\n"
            "<code>/scalp_config set exit.stop_loss_pct -2.0</code>\n"
            "<code>/scalp_config set exit.take_profit_pct 3.0</code>\n"
            "<code>/scalp_config set entry.max_positions 2</code>\n"
            "<code>/scalp_config set entry.use_vwap_filter false</code>\n"
            "<code>/scalp_config reset</code>  기본값 초기화"
        )
        await self._guide_reply(update, msg, back="menu",
                                extra_btn=("📋 단타 파라미터", "GUIDE:scalp_params"))

    async def _guide_params(self, update):
        msg = (
            "📋 <b>[ 종가베팅 파라미터 ]</b>\n"
            "<i>/config set [키] [값]</i>\n\n"
            "<b>SCAN</b>\n"
            "<code>scan.min_trading_value</code>   <i>10000000000</i>\n"
            "<code>scan.surge_threshold_e</code>   <i>9.0</i>\n"
            "<code>scan.envelope_period</code>     <i>20</i>\n"
            "<code>scan.envelope_band_pct</code>   <i>20.0</i>\n"
            "<code>scan.volume_ratio_min</code>    <i>1.5</i>\n\n"
            "<b>ENTRY</b>\n"
            "<code>entry.pullback_min_pct</code>   <i>-10.0</i>\n"
            "<code>entry.pullback_max_pct</code>   <i>-0.5</i>\n"
            "<code>entry.entry_start_time</code>   <i>15:10</i>\n"
            "<code>entry.max_positions</code>      <i>3</i>\n"
            "<code>entry.position_size_pct</code>  <i>15</i>\n"
            "<code>entry.rsi_min</code>            <i>30</i>\n"
            "<code>entry.rsi_max</code>            <i>80</i>\n\n"
            "<b>RISK</b>\n"
            "<code>risk.stop_loss_pct</code>       <i>-3.0</i>\n"
            "<code>risk.take_profit_pct</code>     <i>5.0</i>\n"
            "<code>risk.trailing_gap_pct</code>    <i>2.0</i>\n"
            "<code>risk.force_sell_time</code>     <i>15:00</i>\n\n"
            "<b>SELL</b>\n"
            "<code>sell.nxt_gap_target_pct</code>  <i>2.0</i>\n"
            "<code>sell.morning_target_pct</code>  <i>3.0</i>"
        )
        await self._guide_reply(update, msg, back="menu")

    async def _guide_scalp_params(self, update):
        msg = (
            "📋 <b>[ 단타 파라미터 ]</b>\n"
            "<i>/scalp_config set [키] [값]</i>\n\n"
            "<b>SCAN</b>\n"
            "<code>scan.min_rise_pct</code>         <i>3.0</i>\n"
            "<code>scan.max_rise_pct</code>          <i>20.0</i>\n"
            "<code>scan.min_trading_value</code>     <i>5000000000</i>\n"
            "<code>scan.volume_ratio_min</code>      <i>3.0</i>\n"
            "<code>scan.entry_end_time</code>        <i>14:30</i>\n"
            "<code>scan.api_delay_sec</code>         <i>0.5</i>\n\n"
            "<b>ENTRY</b>\n"
            "<code>entry.max_positions</code>        <i>3</i>\n"
            "<code>entry.position_size_pct</code>    <i>20</i>\n"
            "<code>entry.use_vwap_filter</code>      <i>true</i>\n"
            "<code>entry.cooldown_sec</code>         <i>300</i>\n\n"
            "<b>EXIT</b>\n"
            "<code>exit.take_profit_pct</code>       <i>2.5</i>  → 실질+2.29%\n"
            "<code>exit.stop_loss_pct</code>         <i>-1.5</i> → 실질-1.71%\n"
            "<code>exit.partial_profit_pct</code>    <i>1.5</i>\n"
            "<code>exit.trailing_stop</code>         <i>true</i>\n"
            "<code>exit.trailing_gap_pct</code>      <i>1.0</i>\n"
            "<code>exit.trailing_activate_pct</code> <i>1.0</i>\n"
            "<code>exit.force_exit_time</code>       <i>15:20</i>\n"
            "<code>exit.time_stop_minutes</code>     <i>60</i>\n\n"
            "<b>RISK</b>\n"
            "<code>risk.daily_loss_limit_pct</code>  <i>-3.0</i>\n"
            "<code>risk.max_consecutive_loss</code>  <i>3</i>\n"
            "<code>risk.reserve_cash_pct</code>      <i>10</i>"
        )
        await self._guide_reply(update, msg, back="menu",
                                extra_btn=("💸 거래 비용", "GUIDE:costs"))

    async def _guide_costs(self, update):
        msg = (
            "💸 <b>[ 거래 비용 안내 ]</b>\n\n"
            "수수료 (매수):  <code>+0.015%</code>\n"
            "수수료 (매도):  <code>+0.015%</code>\n"
            "거래세 (매도):  <code>+0.180%</code>\n"
            "━━━━━━━━━━━━━━━━\n"
            "Round-trip 합계: <b>~0.21%</b>\n\n"
            "<b>실질 손절/익절</b>\n"
            "손절 <code>-1.5%</code> → 실질 <b>-1.71%</b>\n"
            "익절 <code>+2.5%</code> → 실질 <b>+2.29%</b>\n"
            "손익비: 2.29 ÷ 1.71 ≈ <b>1.34</b>\n\n"
            "매도 알림:\n"
            "총손익 / 수수료+세금 / <b>실손익</b>\n"
            "3가지 분리 표시\n\n"
            "⚠️ 승률이 낮을 때는\n"
            "거래 횟수를 줄이는 것이 최선"
        )
        await self._guide_reply(update, msg, back="menu")

    async def _guide_fib(self, update):
        msg = (
            "📡 <b>[ 피보나치 재진입 전략 ]</b>\n\n"
            "손절/트레일링 체결 시 자동 시작\n\n"
            "<b>① FibWatcher 자동 생성</b>\n"
            "  갭상승 주도주 → 전일종가 기준\n"
            "  일반 급등주 → 당일저점 기준\n\n"
            "<b>② 10분 대기</b> (노이즈 회피)\n\n"
            "<b>③ 30초마다 Fib 레벨 감시</b>\n"
            "  갭상승: Fib <b>0.236 / 0.382</b>\n"
            "  일반:   Fib <b>0.382 / 0.500</b>\n\n"
            "<b>④ 재진입 조건 (모두 충족)</b>\n"
            "  Fib 레벨 ±0.5% 이내 진입\n"
            "  해당 구간 내 저점 형성\n"
            "  저점 대비 <b>+0.3% 반등</b> 확인\n"
            "  현재가 &lt; 손절가\n\n"
            "<b>/scalp_fib</b> — 감시 현황 조회"
        )
        await self._guide_reply(update, msg, back="menu",
                                extra_btn=("⚡ 단타 조회", "GUIDE:scalp_info"))

    async def _guide_reply(self, update, msg: str,
                            back: str = "menu",
                            extra_btn: tuple = None):
        """가이드 섹션 응답 공통 헬퍼
        - 명령어(/guide info) 호출 시: 새 메시지 전송
        - 버튼 콜백 호출 시: 기존 메시지 수정 (edit)
        """
        buttons = [InlineKeyboardButton("◀ 메뉴로", callback_data=f"GUIDE:{back}")]
        if extra_btn:
            buttons.append(InlineKeyboardButton(extra_btn[0], callback_data=extra_btn[1]))
        keyboard = InlineKeyboardMarkup([buttons])

        if update.callback_query:
            # 콜백 버튼 → 기존 메시지를 해당 섹션 내용으로 교체
            try:
                await update.callback_query.edit_message_text(
                    msg, parse_mode="HTML", reply_markup=keyboard
                )
            except Exception as e:
                logger.debug(f"[Guide] edit_message_text 실패: {e}")
        else:
            # /guide 명령어 → 새 메시지 전송
            await update.message.reply_html(msg, reply_markup=keyboard)

    # ──────────────────────────────────────────────────────────
    # /status — 종가베팅 포지션
    # ──────────────────────────────────────────────────────────

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        await update.message.reply_text("🔄 포지션 조회 중...")
        try:
            balance  = self.broker.get_balance()
            holdings = balance["holdings"]
            now      = datetime.now().strftime("%Y-%m-%d %H:%M")

            if not holdings:
                await update.message.reply_text(
                    f"<b>[ 포지션 현황 ]</b>  <i>{now}</i>\n\n"
                    "보유 종목 없음\n\n"
                    f"💵 주문가능금액: {balance['cash']:,}원",
                    parse_mode="HTML"
                )
                return

            lines    = [f"<b>[ 포지션 현황 ]</b>  <i>{now}</i>\n"]
            keyboard = []

            for h in holdings:
                pl_icon    = "🔺" if h["profit_pct"] >= 0 else "🔻"
                lock_ico   = "🔒" if self.cfg.check_lock(h["code"]) else "🔓"
                profit_amt = h["eval_amt"] - (h["avg_price"] * h["qty"])
                sign       = "+" if profit_amt >= 0 else ""
                lines.append(
                    f"{pl_icon} <b>{h['name']}({h['code']})</b> {lock_ico}\n"
                    f"   현재 <b>{h['cur_price']:,}원</b> / "
                    f"평단 {h['avg_price']:,}원 ({h['qty']}주)\n"
                    f"   손익: <b>{sign}{profit_amt:,.0f}원 ({h['profit_pct']:+.2f}%)</b>"
                )
                keyboard.append([
                    InlineKeyboardButton(
                        f"매도 {h['name']}",
                        callback_data=f"SELL:{h['code']}:{h['qty']}:0"
                    )
                ])

            lines.append(
                f"\n💵 주문가능금액: {balance['cash']:,}원\n"
                f"📊 총평가금액:   {balance['total_eval']:,}원\n"
                f"{'🔺' if balance['total_pl'] >= 0 else '🔻'} "
                f"총손익: <b>{balance['total_pl']:+,}원 "
                f"({balance['profit_pct']:+.2f}%)</b>"
            )

            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"[Bot] status 오류: {e}")
            await update.message.reply_text(f"❌ 조회 실패: {e}")

    # ──────────────────────────────────────────────────────────
    # /balance
    # ──────────────────────────────────────────────────────────

    async def cmd_balance(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        try:
            bal = self.broker.get_balance()
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            holdings_lines = ""
            for h in bal['holdings']:
                pl_icon = "🔺" if h["profit_pct"] >= 0 else "🔻"
                holdings_lines += (
                    f"  {pl_icon} <b>{h['name']}({h['code']})</b>\n"
                    f"      단가 {h['avg_price']:,}원 · {h['qty']}주 · "
                    f"<b>{h['profit_pct']:+.2f}%</b>\n"
                )
            msg = (
                f"<b>[ 계좌 잔고 ]</b>  <i>{now}</i>\n\n"
                f"💵 주문가능금액: <b>{bal['cash']:,}원</b>\n"
                f"📈 총평가금액:   {bal['total_eval']:,}원\n"
                f"{'🔺' if bal['total_pl'] >= 0 else '🔻'} "
                f"총평가손익: <b>{bal['total_pl']:+,}원 "
                f"({bal['profit_pct']:+.2f}%)</b>\n\n"
                f"📋 보유종목: {len(bal['holdings'])}개"
                + (f"\n{holdings_lines}" if holdings_lines else "")
            )
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"❌ 잔고 조회 실패: {e}")

    # ──────────────────────────────────────────────────────────
    # /scan — 종가베팅 후보 스캔
    # ──────────────────────────────────────────────────────────

    async def cmd_scan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        await update.message.reply_text("🔍 종가베팅 후보 종목 스캔 중... (10~30초 소요)")
        try:
            candidates = self.strategy.scan_candidates()
            now        = datetime.now().strftime("%Y-%m-%d %H:%M")
            cfg_scan   = self.scfg.get_scan()

            if not candidates:
                await update.message.reply_text(
                    f"<b>[ 종가베팅 스캔 결과 ]</b>  <i>{now}</i>\n\n"
                    f"조건 충족 종목 없음\n\n"
                    f"<i>기준: 거래대금 {cfg_scan['min_trading_value']//100_000_000}억+ "
                    f"/ 급등 {cfg_scan.get('surge_threshold_e', 9.0)}%+ "
                    f"/ MA3&lt;현재가 / Envelope({cfg_scan.get('envelope_period',20)},{cfg_scan.get('envelope_band_pct',20.0):.0f})</i>",
                    parse_mode="HTML"
                )
                return

            log_path = self.scan_logger.save_scan(
                candidates, config_snapshot=self.scfg.get_all()
            )
            logger.info(f"[Bot] 스캔 로그 저장: {log_path}")

            lines    = [f"<b>[ 종가베팅 스캔 {len(candidates)}개 ]</b>  <i>{now}</i>\n"]
            keyboard = []

            for c in candidates:
                surge_gain = c.get("surge_max_gain", 0)
                surge_days = c.get("surge_days_ago", -1)
                pullback   = c.get("pullback_pct", 0)
                pct_high   = c.get("pct_from_high", 0)
                inst       = c.get("institution_net", 0)
                frgn       = c.get("foreign_net", 0)
                tv         = c.get("trading_value", 0) // 100_000_000
                source     = c.get("source", c.get("scan_type", ""))
                days_str   = f"D-{surge_days}" if surge_days > 0 else "오늘"
                supply     = ""
                if inst > 0: supply += "기관✅ "
                if frgn > 0: supply += "외인✅"
                if not supply: supply = "수급중립"

                lines.append(
                    f"⭐ <b>{c['name']}({c['code']})</b>  "
                    f"점수:{c['score']}  [{source}]\n"
                    f"   🔥급등: <b>+{surge_gain:.1f}%</b>({days_str})  "
                    f"📉눌림: <b>{pullback:+.1f}%</b>\n"
                    f"   💰거래대금:{tv}억  RSI:{c.get('rsi', 0)}  "
                    f"신고가대비:{pct_high:+.1f}%\n"
                    f"   {supply}"
                )
                keyboard.append([
                    InlineKeyboardButton(
                        f"매수 {c['name']}",
                        callback_data=f"BUY:{c['code']}:0:0"
                    )
                ])

            lines.append(f"\n<i>📁 로그 저장 완료</i>")
            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"[Bot] scan 오류: {e}")
            await update.message.reply_text(f"❌ 스캔 실패: {e}")

    # ──────────────────────────────────────────────────────────
    # /config — 종가베팅 전략 설정
    # ──────────────────────────────────────────────────────────

    async def cmd_config(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        args = ctx.args
        if not args:
            await update.message.reply_text(
                self.scfg.format_help(), parse_mode="HTML"
            )
            return
        sub = args[0].lower()
        if sub == "show":
            group = args[1].lower() if len(args) > 1 else "all"
            await update.message.reply_text(
                self.scfg.format_for_telegram(group), parse_mode="HTML"
            )
        elif sub == "set":
            if len(args) < 3:
                await update.message.reply_text(
                    "사용법: /config set [키] [값]\n예) /config set risk.stop_loss_pct -4.0"
                )
                return
            key, val = args[1], args[2]
            try:
                self.scfg.set(key, val)
                await update.message.reply_text(
                    f"✅ 변경 완료\n<code>{key}</code> = <b>{self.scfg.get(key)}</b>",
                    parse_mode="HTML"
                )
            except KeyError:
                await update.message.reply_text(f"❌ 키 없음: {key}")
            except Exception as e:
                await update.message.reply_text(f"❌ 변경 실패: {e}")
        elif sub == "reset":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 초기화 확인", callback_data="CONFIG:RESET:CONFIRM"),
                InlineKeyboardButton("❌ 취소",        callback_data="CONFIG:RESET:CANCEL"),
            ]])
            await update.message.reply_text(
                "⚠️ 종가베팅 설정을 기본값으로 초기화합니다. 계속하시겠습니까?",
                reply_markup=keyboard
            )
        elif sub == "help":
            await update.message.reply_text(self.scfg.format_help(), parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ 알 수 없는 명령: {sub}")

    # ──────────────────────────────────────────────────────────
    # /buy / /sell
    # ──────────────────────────────────────────────────────────

    async def cmd_buy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        args = ctx.args
        if len(args) < 2:
            await update.message.reply_text(
                "사용법: /buy [종목코드] [수량] [가격]\n"
                "예) /buy 005930 10 75000\n    /buy 005930 10 0 ← 시장가"
            )
            return
        code  = args[0]
        qty   = int(args[1])
        price = int(args[2]) if len(args) > 2 else 0
        try:
            cur = self.broker.get_current_price(code)
        except Exception:
            cur = price
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 매수 확인", callback_data=f"BUY:{code}:{qty}:{price}"),
            InlineKeyboardButton("❌ 취소",       callback_data="CANCEL"),
        ]])
        await update.message.reply_text(
            f"<b>[ 매수 주문 확인 ]</b>\n\n"
            f"종목: <b>{code}</b>  현재가: {cur:,}원\n"
            f"주문: {qty}주 @ {'시장가' if price == 0 else f'{price:,}원'}\n"
            f"예상금액: {(price or cur) * qty:,}원",
            parse_mode="HTML", reply_markup=keyboard
        )

    async def cmd_sell(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        args = ctx.args
        if len(args) < 2:
            await update.message.reply_text(
                "사용법: /sell [종목코드] [수량] [가격]\n"
                "예) /sell 005930 10 76000\n    /sell 005930 10 0 ← 시장가"
            )
            return
        code  = args[0]
        qty   = int(args[1])
        price = int(args[2]) if len(args) > 2 else 0
        try:
            cur = self.broker.get_current_price(code)
        except Exception:
            cur = price
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 매도 확인", callback_data=f"SELL:{code}:{qty}:{price}"),
            InlineKeyboardButton("❌ 취소",       callback_data="CANCEL"),
        ]])
        await update.message.reply_text(
            f"<b>[ 매도 주문 확인 ]</b>\n\n"
            f"종목: <b>{code}</b>  현재가: {cur:,}원\n"
            f"주문: {qty}주 @ {'시장가' if price == 0 else f'{price:,}원'}",
            parse_mode="HTML", reply_markup=keyboard
        )

    # ──────────────────────────────────────────────────────────
    # /history / /lock / /unlock
    # ──────────────────────────────────────────────────────────

    async def cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        args    = ctx.args
        tickers = [args[0]] if args else self.cfg.get_active_tickers()
        lines   = ["<b>[ 매매 이력 ]</b>\n"]
        for t in tickers:
            records = self.cfg.get_ledger(t)
            if not records:
                continue
            pos = self.cfg.get_position(t)
            lines.append(
                f"<b>{t}</b> — 보유:{pos['qty']}주 평단:{pos['avg_price']:,}원"
            )
            for r in records[-10:]:
                icon = "🔴" if r["side"] == "BUY" else "🔵"
                lines.append(f"  {icon} {r['date']} {r['qty']}주 @{r['price']:,}원")
            lines.append("")
        if len(lines) == 1:
            lines.append("매매 이력 없음")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_lock(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        if not ctx.args:
            await update.message.reply_text("사용법: /lock [종목코드]")
            return
        code = ctx.args[0]
        self.cfg.set_lock(code)
        await update.message.reply_text(f"🔒 {code} 당일 주문 잠금")

    async def cmd_unlock(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        if not ctx.args:
            await update.message.reply_text("사용법: /unlock [종목코드]")
            return
        code = ctx.args[0]
        self.cfg.release_lock(code)
        await update.message.reply_text(f"🔓 {code} 잠금 해제")

    # ──────────────────────────────────────────────────────────
    # /report / /update_results / /version
    # ──────────────────────────────────────────────────────────

    async def cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        days = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 7
        await update.message.reply_text(f"📊 최근 {days}일 분석 중...")
        try:
            report = self.scan_logger.generate_report(days=days)
            await update.message.reply_text(report, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"❌ 리포트 생성 실패: {e}")

    async def cmd_update_results(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        target_date = ctx.args[0] if ctx.args else None
        await update.message.reply_text(f"🔄 {target_date or '어제'} 결과 업데이트 중...")
        try:
            count = self.scan_logger.update_results(self.broker, target_date)
            await update.message.reply_text(f"✅ 완료: {count}개 종목\n/report 로 확인")
        except Exception as e:
            await update.message.reply_text(f"❌ 업데이트 실패: {e}")

    async def cmd_version(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update): return
        args         = ctx.args
        show_history = args and args[0].lower() in ("history", "all", "log")
        if not show_history:
            await update.message.reply_text(CURRENT_FEATURES, parse_mode="HTML")
            recent = VERSION_HISTORY[-5:]
            lines  = ["<b>[ 최근 변경 이력 ]</b>\n"]
            for item in reversed(recent):
                lines.append(f"• {item}\n")
            lines.append(f"\n<i>전체 이력: /version history</i>")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return
        await update.message.reply_text(
            f"📋 <b>[ 전체 이력 — {CURRENT_VERSION} ]</b>  총 {len(VERSION_HISTORY)}개",
            parse_mode="HTML"
        )
        history_rev = list(reversed(VERSION_HISTORY))
        for i in range(0, len(history_rev), 5):
            chunk = history_rev[i:i+5]
            await update.message.reply_text(
                "\n".join(f"• {item}\n" for item in chunk), parse_mode="HTML"
            )

    # ──────────────────────────────────────────────────────────
    # 단타 명령어
    # ──────────────────────────────────────────────────────────

    async def cmd_scalp_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/scalp_status — 단타 포지션 현황"""
        if not is_authorized(update): return
        if not self.scalp_strategy:
            await update.message.reply_text("⚠️ 단타 전략이 초기화되지 않았습니다.")
            return
        msg = self.scalp_strategy.format_positions_message()
        # 단타 중단 상태 표시
        if self.scalp_paused:
            msg += "\n\n⏸ <b>신규 진입 중단 중</b> — /scalp_stop 으로 재개"
        await update.message.reply_html(msg)

    async def cmd_scalp_scan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/scalp_scan — 장중 급등 종목 즉시 스캔 (장외에도 동작)"""
        if not is_authorized(update): return
        if not self.scanner:
            await update.message.reply_text("⚠️ 스캐너가 초기화되지 않았습니다.")
            return
        await update.message.reply_text("⚡ 단타 종목 스캔 중... (약 15초 소요)")
        try:
            held = self.scalp_strategy.held_codes() if self.scalp_strategy else []
            # force_time=True: 장외 시간에도 수동 스캔 가능
            candidates = await asyncio.to_thread(
                self.scanner.scan, held, set(), True
            )
            msg = self.scanner.format_scan_message(candidates)
            await update.message.reply_html(msg)
        except Exception as e:
            logger.error(f"[Bot] /scalp_scan 오류: {e}")
            await update.message.reply_text(f"❌ 스캔 오류: {e}")

    async def cmd_scalp_config(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/scalp_config [show|set|reset|help] — 단타 설정 관리"""
        if not is_authorized(update): return
        if not self.scalp_cfg:
            await update.message.reply_text("⚠️ 단타 설정이 초기화되지 않았습니다.")
            return
        args = ctx.args or []
        if not args or args[0] == "show":
            group = args[1] if len(args) > 1 else "all"
            await update.message.reply_html(self.scalp_cfg.format_for_telegram(group))
        elif args[0] == "set" and len(args) == 3:
            try:
                self.scalp_cfg.set(args[1], args[2])
                await update.message.reply_text(
                    f"✅ 단타 설정 변경\n<code>{args[1]}</code> = <b>{args[2]}</b>",
                    parse_mode="HTML"
                )
            except KeyError as e:
                await update.message.reply_text(f"❌ {e}")
        elif args[0] == "reset":
            self.scalp_cfg.reset_to_defaults()
            await update.message.reply_text("✅ 단타 설정 기본값으로 초기화")
        elif args[0] == "help":
            await update.message.reply_html(self.scalp_cfg.format_help())
        else:
            await update.message.reply_text(
                "/scalp_config show\n"
                "/scalp_config set [키] [값]\n"
                "/scalp_config reset\n"
                "/scalp_config help"
            )

    async def cmd_scalp_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/scalp_stop — 단타 신규 진입 ON/OFF 토글"""
        if not is_authorized(update): return
        self.scalp_paused = not self.scalp_paused
        if self.scalp_paused:
            await update.message.reply_text(
                "⏸ <b>단타 신규 진입 중단</b>\n"
                "보유 포지션 청산 감시는 계속됩니다.\n"
                "/scalp_stop 으로 재개",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "▶️ <b>단타 신규 진입 재개</b>",
                parse_mode="HTML"
            )

    async def cmd_scalp_exit_all(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/scalp_exit_all — 단타 보유 포지션 전량 즉시 청산"""
        if not is_authorized(update): return
        if not self.scalp_strategy:
            await update.message.reply_text("⚠️ 단타 전략이 초기화되지 않았습니다.")
            return
        positions = self.scalp_strategy.get_positions()
        if not positions:
            await update.message.reply_text("📭 단타 보유 포지션 없음")
            return
        await update.message.reply_text(f"⛔ {len(positions)}개 포지션 즉시 청산 실행...")
        try:
            exited = await asyncio.to_thread(self.scalp_strategy.force_exit_all)
            await update.message.reply_text(
                f"✅ 청산 완료: {', '.join(exited) if exited else '없음'}"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ 청산 실패: {e}")

    async def cmd_scalp_summary(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /scalp_summary [daily|weekly|monthly|YYYYMMDD]
        단타 매매 요약 및 상세 내역 조회
        """
        if not is_authorized(update): return

        args = ctx.args
        sub  = args[0].lower() if args else "daily"

        # 특정 날짜 (8자리 숫자)
        if sub.isdigit() and len(sub) == 8:
            await update.message.reply_text("📋 상세 내역 조회 중...")
            pages = self.scalp_ledger.format_daily_detail(sub)
            for page in pages:
                await update.message.reply_html(page)
            return

        # 인라인 키보드로 기간 선택 (인수 없이 호출 시)
        if not args:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📅 오늘",     callback_data="SUMMARY:daily"),
                    InlineKeyboardButton("📆 이번 주",  callback_data="SUMMARY:weekly"),
                    InlineKeyboardButton("🗓 이번 달",  callback_data="SUMMARY:monthly"),
                ],
                [
                    InlineKeyboardButton("📊 달력 보기", callback_data="SUMMARY:calendar"),
                ],
            ])
            await update.message.reply_text(
                "📊 <b>단타 매매 요약</b>\n기간을 선택하세요:",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        # 기간별 요약
        if sub in ("daily", "weekly", "monthly"):
            await update.message.reply_text("📊 요약 생성 중...")
            msg = self.scalp_ledger.format_summary(sub)
            await update.message.reply_html(msg)

            # daily인 경우 자세히 보기 버튼 제공
            if sub == "daily":
                today = datetime.now().strftime("%Y%m%d")
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "📋 오늘 상세 내역 보기",
                        callback_data=f"SUMMARY:detail:{today}"
                    )
                ]])
                await update.message.reply_text(
                    "종목별 상세 내역을 보시겠습니까?",
                    reply_markup=keyboard
                )
        elif sub == "calendar":
            msg = self.scalp_ledger.format_monthly_calendar()
            await update.message.reply_html(msg)
        else:
            await update.message.reply_text(
                "사용법:\n"
                "/scalp_summary          — 기간 선택 메뉴\n"
                "/scalp_summary daily    — 오늘 요약\n"
                "/scalp_summary weekly   — 이번 주 요약\n"
                "/scalp_summary monthly  — 이번 달 요약\n"
                "/scalp_summary 20260514 — 특정 날짜 상세"
            )

    async def cmd_scalp_market(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/scalp_market — 현재 시장 상황 조회"""
        if not is_authorized(update): return
        import pytz
        from datetime import datetime as _dt
        KST = pytz.timezone("Asia/Seoul")
        now = _dt.now(KST)
        wd  = now.weekday()
        if wd >= 5:
            day_str = "토요일" if wd == 5 else "일요일"
            await update.message.reply_text(
                f"📴 오늘은 {day_str}입니다.\n"
                f"단타봇은 평일 장 중(09:00~15:25)에만 동작합니다.\n"
                f"월요일 08:50에 자동으로 재개됩니다."
            )
            return
        await update.message.reply_text("📡 시장 상황 조회 중...")
        try:
            from market_filter import MarketFilter
            mf  = MarketFilter(self.broker)
            msg = mf.format_for_telegram()
            await update.message.reply_html(msg)
        except Exception as e:
            await update.message.reply_text(f"❌ 조회 실패: {e}")

    async def cmd_scalp_fib(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/scalp_fib — Fib 재진입 감시 현황"""
        if not is_authorized(update): return
        if not self.scalp_strategy or not self.scalp_strategy.fib_mgr:
            await update.message.reply_text("⚠️ Fib 매니저가 초기화되지 않았습니다.")
            return
        msg = self.scalp_strategy.fib_mgr.format_for_telegram()
        await update.message.reply_html(msg)

    # ──────────────────────────────────────────────────────────
    # [v1.3] 하이브리드 모드 — 수동 감시 명령어
    # ──────────────────────────────────────────────────────────

    async def cmd_scalp_add(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /scalp_add [종목코드] [종목명(선택)]
        당일 주도주를 직접 지정하여 단타 감시 목록에 추가

        예) /scalp_add 005930
            /scalp_add 005930 삼성전자
        """
        if not is_authorized(update): return
        if not self.scalp_strategy:
            await update.message.reply_text("⚠️ 단타 전략이 초기화되지 않았습니다.")
            return

        args = ctx.args or []
        if not args:
            await update.message.reply_html(
                "📌 <b>사용법</b>\n"
                "<code>/scalp_add [종목코드]</code>\n"
                "<code>/scalp_add [종목코드] [종목명]</code>\n\n"
                "예) <code>/scalp_add 005930</code>\n"
                "예) <code>/scalp_add 247540 에코프로비엠</code>\n\n"
                "추가된 종목은 장중 자동 스캔 종목과 동일한 "
                "단타 로직으로 매매됩니다."
            )
            return

        code = args[0].strip().zfill(6)    # 6자리 패딩
        name = args[1] if len(args) > 1 else ""

        await update.message.reply_text(f"🔍 {code} 종목 정보 조회 중...")
        try:
            result = self.scalp_strategy.watchlist_add(code, name)
        except Exception as e:
            await update.message.reply_text(f"❌ 오류: {e}")
            return

        if result["ok"]:
            item = result["item"]
            cur  = 0
            try:
                info = self.broker.get_stock_info(code)
                cur  = info.get("cur_price", 0)
            except Exception:
                pass

            msg = (
                f"✅ <b>하이브리드 감시 추가</b>\n\n"
                f"📌 종목: <b>{item['name']}({code})</b>\n"
                f"💰 현재가: {cur:,}원\n\n"
                f"이 종목은 다음 30초 루프부터 자동으로 매수 시도합니다.\n"
                f"진입 조건: 포지션 여유 + 현금 + 진입 마감 시각 이내\n\n"
                f"<i>/scalp_remove {code} — 감시 중단\n"
                f"/scalp_watchlist — 전체 목록 확인</i>"
            )
            await update.message.reply_html(msg)
        else:
            await update.message.reply_text(result["msg"])

    async def cmd_scalp_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /scalp_remove [종목코드]
        수동 감시 종목 중단 (보유 포지션은 유지, 신규 진입만 차단)

        예) /scalp_remove 005930
        """
        if not is_authorized(update): return
        if not self.scalp_strategy:
            await update.message.reply_text("⚠️ 단타 전략이 초기화되지 않았습니다.")
            return

        args = ctx.args or []
        if not args:
            await update.message.reply_html(
                "📌 <b>사용법</b>\n"
                "<code>/scalp_remove [종목코드]</code>\n\n"
                "예) <code>/scalp_remove 005930</code>\n\n"
                "보유 중인 포지션은 유지되고\n"
                "신규 진입만 중단됩니다."
            )
            return

        code   = args[0].strip().zfill(6)
        result = self.scalp_strategy.watchlist_remove(code)
        if result["ok"]:
            await update.message.reply_html(
                f"<b>하이브리드 감시 중단</b>\n\n{result['msg']}\n\n"
                f"<i>/scalp_watchlist — 전체 목록 확인</i>"
            )
        else:
            await update.message.reply_text(result["msg"])

    async def cmd_scalp_watchlist(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /scalp_watchlist
        수동 감시 목록 전체 조회 (활성/중단 포함)
        """
        if not is_authorized(update): return
        if not self.scalp_strategy:
            await update.message.reply_text("⚠️ 단타 전략이 초기화되지 않았습니다.")
            return

        msg = self.scalp_strategy.format_watchlist_message()

        # 활성 종목에 대해 실시간 현재가 추가
        active = self.scalp_strategy.watchlist_get_active()
        if active:
            price_lines = ["\n<b>📊 실시간 현재가</b>"]
            for item in active:
                try:
                    info  = self.broker.get_stock_info(item["code"])
                    cur   = info.get("cur_price", 0)
                    flu   = info.get("flu_rt", "0")
                    sign  = "🔺" if float(flu) >= 0 else "🔻"
                    held  = "📌" if item["code"] in [
                        p.code for p in self.scalp_strategy.get_positions()
                    ] else "  "
                    price_lines.append(
                        f"{held} {item['name']}({item['code']}): "
                        f"<b>{cur:,}원</b> {sign}{flu}%"
                    )
                except Exception:
                    price_lines.append(f"  {item['name']}({item['code']}): 조회 실패")
            msg += "\n" + "\n".join(price_lines)

        await update.message.reply_html(msg)

    async def cmd_scalp_debug(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/scalp_debug — 단타봇 현재 상태 진단 (매수 안 될 때 원인 파악)"""
        if not is_authorized(update): return
        import pytz
        KST     = pytz.timezone("Asia/Seoul")
        now_str = datetime.now(KST).strftime("%H:%M")
        lines   = [f"🔍 <b>[ 단타봇 진단 ]</b>  {now_str}\n"]

        # ① 잔고
        try:
            bal  = self.broker.get_balance()
            cash = bal["cash"]
            dep  = self.broker.get_deposit()
            cash_ok = cash > 0 or dep > 0
            lines.append(
                f"<b>① 잔고</b>\n"
                f"   balance.cash : <b>{cash:,}원</b>\n"
                f"   get_deposit  : <b>{dep:,}원</b>\n"
                f"   {'✅ 정상' if cash_ok else '❌ 0원 — MOCK 잔고 없음 → 수동 매매만 가능'}"
            )
        except Exception as e:
            lines.append(f"<b>① 잔고</b>  ❌ 조회 실패: {e}")

        # ② 스캐너 상태
        if self.scanner and self.scalp_cfg:
            entry_end  = self.scalp_cfg.get_scan()["entry_end_time"]
            cache_cnt  = len(getattr(self.scanner, '_prev_vol_cache', {}))
            in_time    = now_str < entry_end
            lines.append(
                f"\n<b>② 스캐너</b>\n"
                f"   진입마감   : {entry_end}  현재: {now_str}\n"
                f"   시간상태   : {'✅ 진입 가능' if in_time else '⛔ 마감 — 장중에만 동작'}\n"
                f"   거래량캐시 : {cache_cnt}개  "
                f"{'✅' if cache_cnt > 0 else '⚠️ 0 — 내일 08:50 자동 초기화'}"
            )
        else:
            lines.append("\n<b>② 스캐너</b>  ❌ 미초기화")

        # ③ 전략 상태
        if self.scalp_strategy and self.scalp_cfg:
            pos_cnt   = len(self.scalp_strategy.get_positions())
            max_pos   = self.scalp_cfg.get_entry()["max_positions"]
            cons_loss = self.scalp_strategy._consecutive_loss
            max_loss  = self.scalp_cfg.get_risk()["max_consecutive_loss"]
            lines.append(
                f"\n<b>③ 전략 상태</b>\n"
                f"   신규진입   : {'⏸ 중단 (/scalp_stop 으로 재개)' if self.scalp_paused else '▶️ 실행중'}\n"
                f"   보유/최대  : {pos_cnt}/{max_pos}  "
                f"{'✅' if pos_cnt < max_pos else '⛔ 최대 도달'}\n"
                f"   연속손절   : {cons_loss}/{max_loss}  "
                f"{'✅' if cons_loss < max_loss else '⛔ 한도 초과 → /scalp_stop 후 재개'}"
            )
        else:
            lines.append("\n<b>③ 전략 상태</b>  ❌ 미초기화")

        # ④ 마지막 스캔 결과
        if self.scanner:
            last = getattr(self.scanner, 'last_scan_result', [])
            if last:
                lines.append(f"\n<b>④ 마지막 스캔 결과</b> ({len(last)}개)")
                for c in last[:3]:
                    tv = c.get('trading_value', 0) // 100_000_000
                    lines.append(
                        f"   • {c.get('name','?')}({c.get('code','?')}) "
                        f"{c.get('rise_pct', 0):+.1f}% "
                        f"TV:{tv}억 점수:{c.get('score', 0)}"
                    )
            else:
                lines.append("\n<b>④ 마지막 스캔 결과</b>  아직 없음 (장중 자동 실행 대기)")

        # ⑤ 설정 요약
        if self.scalp_cfg:
            ec = self.scalp_cfg.get_entry()
            ex = self.scalp_cfg.get_exit()
            sc = self.scalp_cfg.get_scan()
            lines.append(
                f"\n<b>⑤ 핵심 설정</b>\n"
                f"   종목당 투자 : {ec['position_size_pct']}% (예: 1억×20%=2천만)\n"
                f"   익절 / 손절 : +{ex['take_profit_pct']}% / {ex['stop_loss_pct']}%\n"
                f"   거래대금기준: {sc['min_trading_value']//100_000_000}억↑\n"
                f"   상승률 범위 : {sc['min_rise_pct']}~{sc['max_rise_pct']}%"
            )

        lines.append(
            f"\n💡 <b>매수가 안 된다면 체크리스트</b>\n"
            f"  □ 잔고 0원 → /balance 확인, MOCK 모드 한계\n"
            f"  □ 진입마감 → 09:00~13:00 장중에만 자동 매매\n"
            f"  □ 거래량캐시 0 → 내일 08:50 자동 초기화\n"
            f"  □ 신규진입 중단 → /scalp_stop 으로 재개"
        )
        await update.message.reply_html("\n".join(lines))

    # ──────────────────────────────────────────────────────────
    # 인라인 버튼 콜백
    # ──────────────────────────────────────────────────────────

    async def handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not is_authorized(update): return
        data = query.data

        if data.startswith("BUY:"):
            _, code, qty_s, price_s = data.split(":")
            qty        = int(qty_s)
            price      = int(price_s)
            order_type = "3" if price == 0 else "0"
            if qty == 0:
                try:
                    bal = self.broker.get_balance()
                    cur = self.broker.get_current_price(code)
                    qty = self.strategy.calculate_buy_qty(code, cur, bal["cash"])
                except Exception as e:
                    await query.edit_message_text(f"❌ 수량 계산 실패: {e}")
                    return
            result = self.broker.buy_order(code, qty, price, order_type)
            if result["success"]:
                exec_price = price or self.broker.get_current_price(code)
                self.cfg.add_ledger_record(code, "BUY", exec_price, qty)
                self.cfg.set_lock(code)
                try:
                    stock_name = self.broker.get_stock_info(code).get("name", code)
                except Exception:
                    stock_name = code
                await query.edit_message_text(
                    f"✅ <b>매수 완료</b>\n{stock_name}({code}) {qty}주  "
                    f"주문번호: {result['order_no']}",
                    parse_mode="HTML"
                )
            else:
                await query.edit_message_text(
                    f"❌ 매수 실패: {result['raw'].get('return_msg', '오류')}"
                )

        elif data.startswith("SELL:"):
            parts      = data.split(":")
            code       = parts[1]
            qty        = int(parts[2]) if len(parts) > 2 else 0
            price      = int(parts[3]) if len(parts) > 3 else 0
            order_type = "3" if price == 0 else "0"
            if qty == 0:
                pos = self.cfg.get_position(code)
                qty = pos["qty"]
            result = self.broker.sell_order(code, qty, price, order_type)
            if result["success"]:
                exec_price = price or self.broker.get_current_price(code)
                self.cfg.add_ledger_record(code, "SELL", exec_price, qty)
                await query.edit_message_text(
                    f"✅ <b>매도 완료</b>\n{code} {qty}주  주문번호: {result['order_no']}",
                    parse_mode="HTML"
                )
            else:
                await query.edit_message_text(
                    f"❌ 매도 실패: {result['raw'].get('return_msg', '오류')}"
                )

        elif data.startswith("GUIDE:"):
            # 가이드 메뉴 — 섹션별 라우팅
            parts   = data.split(":", 1)
            section = parts[1] if len(parts) > 1 else "menu"
            guide_map = {
                "menu":         self._guide_menu,
                "info":         self._guide_swing_info,
                "scan":         self._guide_swing_scan,
                "order":        self._guide_swing_order,
                "config":       self._guide_swing_config,
                "scalp_info":   self._guide_scalp_info,
                "scalp_ctrl":   self._guide_scalp_ctrl,
                "scalp_sum":    self._guide_scalp_sum,
                "scalp_cfg":    self._guide_scalp_cfg,
                "params":       self._guide_params,
                "scalp_params": self._guide_scalp_params,
                "costs":        self._guide_costs,
                "fib":          self._guide_fib,
            }
            fn = guide_map.get(section, self._guide_menu)
            await fn(update)   # ← 여기서 반드시 return (다음 블록 실행 방지)
            return

        elif data == "CONFIG:RESET:CONFIRM":
            self.scfg.reset_to_defaults()
            await query.edit_message_text("✅ 종가베팅 설정 기본값으로 초기화")
            await query.edit_message_text("❌ 초기화 취소")
        elif data == "CANCEL":
            await query.edit_message_text("❌ 취소")

        elif data.startswith("SUMMARY:"):
            parts = data.split(":")
            sub   = parts[1] if len(parts) > 1 else "daily"

            if sub == "detail" and len(parts) > 2:
                date_str = parts[2]
                await query.edit_message_text("📋 상세 내역 조회 중...")
                pages = self.scalp_ledger.format_daily_detail(date_str)
                for page in pages:
                    await query.message.reply_html(page)

            elif sub == "calendar":
                msg = self.scalp_ledger.format_monthly_calendar()
                await query.edit_message_text(msg, parse_mode="HTML")

            elif sub in ("daily", "weekly", "monthly"):
                msg = self.scalp_ledger.format_summary(sub)
                await query.edit_message_text(msg, parse_mode="HTML")

                if sub == "daily":
                    today = datetime.now().strftime("%Y%m%d")
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "📋 오늘 상세 내역",
                            callback_data=f"SUMMARY:detail:{today}"
                        )
                    ]])
                    await query.message.reply_text(
                        "종목별 상세를 보시겠습니까?",
                        reply_markup=keyboard
                    )

    # ──────────────────────────────────────────────────────────
    # 외부 알림 발송 (main.py에서 호출)
    # ──────────────────────────────────────────────────────────

    async def notify_buy(self, code: str, name: str, qty: int,
                         price: int, reason: str = "") -> None:
        """종가베팅 자동 매수 알림"""
        await self.send(
            f"🟢 <b>종가베팅 매수</b>\n\n"
            f"종목: <b>{name}({code})</b>\n"
            f"수량: {qty}주 @ {price:,}원\n"
            f"금액: {qty * price:,}원\n"
            f"<i>{reason}</i>"
        )

    async def notify_sell(self, code: str, name: str, qty: int,
                          price: int, buy_price: int,
                          reason: str = "") -> None:
        """종가베팅 자동 매도 알림"""
        profit_pct = (price - buy_price) / buy_price * 100 if buy_price > 0 else 0
        profit_amt = (price - buy_price) * qty
        icon       = "🔴" if profit_amt < 0 else "🔵"
        sign       = "+" if profit_amt >= 0 else ""
        await self.send(
            f"{icon} <b>종가베팅 매도</b>\n\n"
            f"종목: <b>{name}({code})</b>\n"
            f"수량: {qty}주 @ {price:,}원\n"
            f"손익: <b>{sign}{profit_amt:,}원 ({profit_pct:+.2f}%)</b>\n"
            f"<i>{reason}</i>"
        )

    async def notify_scalp_buy(self, code: str, name: str, qty: int,
                                price: int, candidate: dict) -> None:
        """
        단타 자동 매수 알림 — 상세 정보 포함
        매수 체결 즉시 텔레그램 발송
        """
        import pytz
        KST     = pytz.timezone("Asia/Seoul")
        now_str = datetime.now(KST).strftime("%H:%M:%S")

        tv       = candidate.get("trading_value", 0) // 100_000_000
        vr       = candidate.get("volume_ratio", 0)
        rise     = candidate.get("rise_pct", 0)
        score    = candidate.get("score", 0)
        vwap     = candidate.get("vwap", 0)
        source   = candidate.get("source", "")
        total_amt = price * qty

        exit_cfg = self.scalp_cfg.get_exit() if self.scalp_cfg else {}
        tp       = exit_cfg.get("take_profit_pct", 2.5)
        sl       = exit_cfg.get("stop_loss_pct", -1.5)

        # 목표가 / 손절가 계산
        target_price = int(price * (1 + tp / 100))
        stop_price   = int(price * (1 + sl / 100))

        vwap_str = f"VWAP: {vwap:,.0f}원 ({'↑위' if price >= vwap else '↓아래'})\n" if vwap > 0 else ""
        vr_str   = f"{vr:.1f}배" if vr > 0 else "N/A"

        await self.send(
            f"🟢 <b>[단타 매수 체결]</b>  {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{name} ({code})</b>\n"
            f"💰 체결가: <b>{price:,}원</b> "
            f"(<b>{rise:+.1f}%</b>)\n"
            f"📦 수량: <b>{qty:,}주</b>  "
            f"투자금: <b>{total_amt:,}원</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 목표가: <b>{target_price:,}원</b> "
            f"(+{tp}%)\n"
            f"🛑 손절가: <b>{stop_price:,}원</b> "
            f"({sl}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 거래대금: {tv}억  "
            f"거래량: {vr_str}\n"
            f"{vwap_str}"
            f"⭐ 점수: {score}점  [{source}]"
        )

    async def notify_scalp_sell(self, code: str, name: str, qty: int,
                                 price: int, buy_price: int,
                                 reason: str = "",
                                 buy_time: str = "",
                                 source: str = "", score: int = 0) -> None:
        """
        단타 자동 매도/청산 알림 — 손익 상세 포함 + 장부 자동 기록
        청산 즉시 텔레그램 발송
        """
        import pytz
        KST     = pytz.timezone("Asia/Seoul")
        now_str = datetime.now(KST).strftime("%H:%M:%S")

        profit_pct = (price - buy_price) / buy_price * 100 if buy_price > 0 else 0
        profit_amt = (price - buy_price) * qty
        total_recv = price * qty

        # 결과 아이콘
        if profit_amt > 0:
            result_icon = "✅"
            result_txt  = "익절"
        elif profit_amt < 0:
            result_icon = "❌"
            result_txt  = "손절"
        else:
            result_icon = "➡️"
            result_txt  = "본절"

        # 사유 이모지
        reason_map = {
            "익절":       "🎯",
            "손절":       "🛑",
            "트레일링":   "📉",
            "강제 청산":  "⛔",
            "시간 손절":  "⏰",
            "부분 익절":  "💰",
        }
        reason_icon = next(
            (v for k, v in reason_map.items() if k in reason), "📋"
        )

        # 거래 비용 계산
        from strategy_scalping import ScalpStrategy as _SS
        cost = _SS.calc_real_cost(buy_price, price, qty)

        await self.send(
            f"{result_icon} <b>[단타 {result_txt}]</b>  {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{name} ({code})</b>\n"
            f"💸 매도가: <b>{price:,}원</b>\n"
            f"📦 수량: <b>{qty:,}주</b>  "
            f"회수금: <b>{total_recv:,}원</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 매수가 → 매도가\n"
            f"   {buy_price:,}원 → {price:,}원\n"
            f"   수익률: <b>{profit_pct:+.2f}%</b>\n"
            f"💵 총손익: {profit_amt:+,}원\n"
            f"🏦 수수료+세금: -{cost['total_cost']:,}원\n"
            f"✅ <b>실손익: {profit_amt - cost['total_cost']:+,}원</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{reason_icon} 사유: {reason}"
        )

        # ── 장부 자동 기록 ────────────────────────────────────
        try:
            self.scalp_ledger.record_trade(
                code       = code,
                name       = name,
                buy_price  = buy_price,
                sell_price = price,
                qty        = qty,
                buy_time   = buy_time or "00:00",
                sell_time  = now_str,
                reason     = reason,
                source     = source,
                score      = score,
            )
        except Exception as e:
            logger.warning(f"[Bot] 장부 기록 실패: {e}")

    async def notify_scan_result(self, candidates: list) -> None:
        """종가베팅 자동 스캔 결과 알림"""
        if not candidates:
            return
        now = datetime.now().strftime("%H:%M")
        try:
            self.scan_logger.save_scan(candidates, config_snapshot=self.scfg.get_all())
        except Exception as e:
            logger.warning(f"[Bot] 스캔 로그 저장 실패: {e}")
        lines = [f"📡 <b>[ 14:30 종가베팅 스캔 — {len(candidates)}개 ]</b>\n"]
        for c in candidates:
            surge_gain = c.get("surge_max_gain", 0)
            surge_days = c.get("surge_days_ago", -1)
            pullback   = c.get("pullback_pct", 0)
            tv         = c.get("trading_value", 0) // 100_000_000
            days_str   = f"D-{surge_days}" if surge_days > 0 else "오늘"
            lines.append(
                f"⭐ <b>{c['name']}({c['code']})</b>  점수:{c['score']}\n"
                f"   🔥급등:+{surge_gain:.1f}%({days_str})  "
                f"📉눌림:{pullback:+.1f}%  "
                f"💰{tv}억  RSI:{c.get('rsi', 0)}"
            )
        lines.append("\n<i>📁 로그 자동 저장됨</i>")
        await self.send("\n".join(lines))

    async def notify_error(self, msg: str) -> None:
        """오류 알림"""
        await self.send(f"⚠️ <b>오류 발생</b>\n{msg}")
