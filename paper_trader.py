"""仅本地A股模拟账本。开盘预留资金，完整bar可得后确认OHLC模拟成交。

不包含券商连接；OHLC与整根成交量是事后执行模型，不是可见开仓信号。
动态止损在bar开盘捕获先前可见状态，无法消除T+1、跳空、涨跌停风险。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
from typing import Any

from config import QuantConfig
from models import Advice, Bar, Instrument, cn_time, jsonable, stable_hash


@dataclass
class Position:
    position_id: str
    advice: Advice
    instrument: Instrument
    entry_time: datetime
    observed_at: datetime
    entry_price: float
    original_shares: int
    shares: int
    entry_fees: float
    entry_cost: float
    stop_price: float
    peak_price: float
    peak_roi_pct: float = 0.0
    max_adverse_roi_pct: float = 0.0
    sold_shares: int = 0
    sale_proceeds: float = 0.0
    exit_fees: float = 0.0
    pending_exit_reason: str | None = None
    last_price: float = 0.0
    last_time: datetime | None = None
    stale: bool = False
    entry_adj_factor: float | None = None
    last_adj_factor: float | None = None

    @property
    def symbol(self) -> str:
        return self.advice.symbol


@dataclass
class EntryIntent:
    position_id: str
    advice: Advice
    instrument: Instrument
    bar_key: tuple[str, str, datetime]
    entry_time: datetime
    entry_price: float
    requested_shares: int
    reserve: float
    stop_price: float


class ASharePaperTrader:
    def __init__(self, config: QuantConfig, instruments: dict[str, Instrument], calendar: tuple[date, ...]):
        self.config, self.instruments = config, instruments
        self.calendar = tuple(sorted(set(calendar)))
        self.initial_cash = self.cash = config.paper_initial_cash_cny
        self.positions: dict[str, Position] = {}
        self.entry_intents: dict[str, EntryIntent] = {}
        self.advice_queue: dict[str, Advice] = {}
        self.seen_signals: set[str] = set()
        self.trades: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.equity_curve: list[dict[str, Any]] = []
        self.contexts: dict[tuple[str, str, datetime], dict[str, Any]] = {}
        self.new_entries_enabled = True
        self.corporate_action_halt = False
        self.incomplete_reasons: set[str] = set()
        self.total_buy_fees = self.total_sell_fees = 0.0
        self.total_filled_shares = 0
        self.max_concurrent_positions = 0
        self._equity_peak = self.initial_cash
        self.known_adj_factors: dict[str, float] = {}
        self.exposed_days: set[tuple[str, date]] = set()

    @staticmethod
    def key(bar: Bar) -> tuple[str, str, datetime]:
        return bar.symbol, bar.timeframe, bar.timestamp

    def event(self, now: datetime, kind: str, symbol: str = "", **details: Any) -> None:
        self.events.append(jsonable({"timestamp": now, "time": now, "event": kind, "symbol": symbol, "reason": details.get("reason"), "details": details, **details}))

    def fees(self, instrument: Instrument, price: float, shares: int, side: str) -> float:
        if shares <= 0:
            return 0.0
        notional = price * shares
        commission = max(self.config.minimum_commission_cny, notional * self.config.commission_rate)
        transfer = notional * self.config.stock_transfer_rate if instrument.asset_type == "STOCK" else 0.0
        stamp = notional * (self.config.stock_sell_stamp_rate if instrument.asset_type == "STOCK" else self.config.etf_sell_stamp_rate) if side == "sell" else 0.0
        return round(commission + transfer + stamp, 2)

    @staticmethod
    def sector(instrument: Instrument) -> str:
        value = str(instrument.sector or "").strip().upper()
        return value if value not in {"", "NONE", "NULL", "N/A", "UNKNOWN", "未知"} else "UNKNOWN"

    @staticmethod
    def tick(instrument: Instrument, price: float, side: str) -> float:
        quantum = Decimal("0.001") if instrument.asset_type == "ETF" else Decimal("0.01")
        return float(Decimal(str(price)).quantize(quantum, rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR))

    def equity(self) -> float:
        # 预留款尚未确认成交仍计为资产，不把它当作已发生买入费用。
        reserve = sum(item.reserve for item in self.entry_intents.values())
        marks = sum(item.shares * item.last_price - self.fees(item.instrument, item.last_price, item.shares, "sell") for item in self.positions.values())
        return self.cash + reserve + marks

    def exposure(self, *, sector: str | None = None) -> float:
        active = sum(item.shares * item.last_price for item in self.positions.values() if sector is None or self.sector(item.instrument) == sector)
        pending = sum(item.requested_shares * item.entry_price for item in self.entry_intents.values() if sector is None or self.sector(item.instrument) == sector)
        return active + pending

    def queue_advice(self, advice: Advice) -> None:
        if advice.signal_id in self.seen_signals:
            return
        self.seen_signals.add(advice.signal_id)
        if advice.action != "BUY_CANDIDATE" or not advice.allow_paper_entry:
            previous = self.advice_queue.get(advice.symbol)
            if previous and previous.timeframe == advice.timeframe and advice.as_of >= previous.as_of:
                self.advice_queue.pop(advice.symbol, None)
                self.event(advice.as_of, "PENDING_SIGNAL_REVOKED_BY_LATEST_ADVICE", advice.symbol, signal_id=previous.signal_id)
            return
        if advice.timeframe not in {"daily", "5m"} or any(not math.isfinite(value) for value in (advice.score, advice.entry_low, advice.entry_high, advice.stop_price, advice.suggested_weight)) or not 0 < advice.entry_low <= advice.entry_high or advice.valid_until <= advice.as_of:
            self.event(advice.as_of, "INVALID_ADVICE_REJECTED", advice.symbol)
            return
        if advice.symbol in self.positions or advice.symbol in self.entry_intents:
            self.event(advice.as_of, "SIGNAL_IGNORED_EXISTING_POSITION", advice.symbol, signal_id=advice.signal_id)
            return
        previous = self.advice_queue.get(advice.symbol)
        if previous is None or advice.as_of > previous.as_of or advice.score > previous.score or previous.valid_until < advice.as_of:
            self.advice_queue[advice.symbol] = advice

    def _limit_known(self, bar: Bar) -> bool:
        return bar.has_price_limit is False or (bar.limit_up is not None and bar.limit_down is not None)

    def _fillable(self, bar: Bar, side: str, price: float) -> str | None:
        if bar.suspended or bar.volume <= 0:
            return "SUSPENDED_OR_ZERO_VOLUME"
        if not self._limit_known(bar):
            return "UNVERIFIED_PRICE_LIMITS"
        if side == "buy" and bar.limit_up is not None and price >= bar.limit_up - 1e-8:
            return "LIMIT_UP_NO_BUY_FILL"
        if side == "sell" and bar.limit_down is not None and (bar.open <= bar.limit_down + 1e-8 or price <= bar.limit_down + 1e-8):
            return "LIMIT_DOWN_NO_SELL_FILL"
        return None

    def _size(self, advice: Advice, instrument: Instrument, price: float, stop: float) -> int:
        equity = self.equity()
        weight = min(advice.suggested_weight, self.config.suggested_weight_cap)
        if equity <= 0 or weight <= 0:
            return 0
        sector = self.sector(instrument)
        max_notional = min(equity * weight, equity * self.config.max_total_weight - self.exposure(), equity * self.config.max_sector_weight - self.exposure(sector=sector), self.cash)
        risk_budget = equity * self.config.risk_per_trade_fraction
        max_notional = max(0.0, max_notional)
        lot = instrument.lot_size
        shares = int(max_notional / price) // lot * lot
        while shares > 0:
            buy_fee = self.fees(instrument, price, shares, "buy")
            stop_execution = self.tick(instrument, stop * (1 - self.config.slippage_fraction), "sell")
            stop_fee = self.fees(instrument, stop_execution, shares, "sell")
            planned_loss = (price - stop_execution) * shares + buy_fee + stop_fee
            if price * shares + buy_fee <= self.cash + 1e-8 and price * shares + buy_fee <= equity * weight + 1e-8 and planned_loss <= risk_budget + 1e-8:
                return shares
            shares -= lot
        return 0

    def on_open(self, bar: Bar, *, allow_entry: bool = True) -> None:
        key = self.key(bar)
        position = self.positions.get(bar.symbol)
        intent = self.entry_intents.get(bar.symbol)
        if position or intent:
            self.exposed_days.add((bar.symbol, bar.day))
        self.contexts[key] = {
            "position_id": position.position_id if position else intent.position_id if intent else None,
            "stop_price": position.stop_price if position else intent.stop_price if intent else None,
            "pending_exit_reason": position.pending_exit_reason if position else None,
            "corporate_action_halt": self.corporate_action_halt,
        }
        if not allow_entry or not self.new_entries_enabled or position or intent:
            return
        advice = self.advice_queue.get(bar.symbol)
        if advice is None:
            return
        now = bar.open_time
        if now <= advice.as_of or now <= advice.data_time:
            return
        if advice.timeframe == "daily" and bar.day <= advice.data_time.date():
            return
        if advice.timeframe == "daily":
            target_day = next((day for day in self.calendar if day > advice.data_time.date() and cn_time(day, 9, 30) > advice.as_of), None)
            if target_day is None or bar.day != target_day or now != cn_time(bar.day, 9, 30):
                if target_day is not None and bar.day >= target_day:
                    self.advice_queue.pop(bar.symbol, None)
                    self.event(now, "DAILY_OPEN_NOT_AVAILABLE_NO_CHASE", bar.symbol, intended_session=target_day)
                return
            # 日线只尝试这个开盘，不把被拒绝的日线订单改成盘中追单。
            self.advice_queue.pop(bar.symbol, None)
        if now > advice.valid_until:
            self.advice_queue.pop(bar.symbol, None)
            self.event(now, "SIGNAL_EXPIRED", bar.symbol, signal_id=advice.signal_id)
            return
        instrument = self.instruments.get(bar.symbol)
        if instrument is None or instrument.asset_type not in {"STOCK", "ETF"} or instrument.board not in self.config.allowed_boards:
            self.event(now, "ENTRY_REJECTED_INSTRUMENT", bar.symbol)
            return
        if len(self.positions) + len(self.entry_intents) >= self.config.max_positions:
            self.event(now, "ENTRY_REJECTED_POSITION_LIMIT", bar.symbol)
            return
        price = self.tick(instrument, bar.open * (1 + self.config.slippage_fraction), "buy")
        if not advice.entry_low <= price <= advice.entry_high:
            self.event(now, "ENTRY_OUTSIDE_PRICE_RANGE", bar.symbol, simulated_price=price, entry_low=advice.entry_low, entry_high=advice.entry_high)
            return
        # 不用未来bar总成交量决定下单数，流动性在bar可得时才确认。
        if bar.limit_up is not None and price >= bar.limit_up - 1e-8:
            self.event(now, "LIMIT_UP_NO_BUY_FILL", bar.symbol)
            return
        stop = advice.stop_price if 0 < advice.stop_price < price else price * (1 - self.config.initial_stop_pct)
        if not 0 < (price - stop) / price <= self.config.max_stop_pct:
            self.event(now, "ENTRY_REJECTED_STOP_DISTANCE", bar.symbol)
            return
        shares = self._size(advice, instrument, price, stop)
        if shares < instrument.lot_size:
            self.event(now, "ENTRY_REJECTED_BUDGET_OR_RISK_OR_SECTOR", bar.symbol)
            return
        reserve = price * shares + self.fees(instrument, price, shares, "buy")
        identifier = stable_hash((advice.signal_id, key))[:24]
        intent = EntryIntent(identifier, advice, instrument, key, now, price, shares, reserve, stop)
        self.entry_intents[bar.symbol] = intent
        self.exposed_days.add((bar.symbol, bar.day))
        self.cash -= reserve
        self.advice_queue.pop(bar.symbol, None)
        self.contexts[key].update(position_id=identifier, stop_price=stop)
        self.max_concurrent_positions = max(self.max_concurrent_positions, len(self.positions) + len(self.entry_intents))
        self.event(now, "ENTRY_INTENT_RESERVED", bar.symbol, signal_id=advice.signal_id, requested_shares=shares, reserved_cash_cny=reserve, price=price)

    def _settle_entry(self, bar: Bar) -> Position | None:
        intent = self.entry_intents.get(bar.symbol)
        if intent is None or intent.bar_key != self.key(bar):
            return None
        self.entry_intents.pop(bar.symbol)
        self.cash += intent.reserve
        refusal = self._fillable(bar, "buy", intent.entry_price)
        if intent.instrument.asset_type == "STOCK" and bar.is_st is not False:
            refusal = "STOCK_ST_NO_BUY_FILL" if bar.is_st is True else "UNVERIFIED_CURRENT_ST_STATUS"
        if self.corporate_action_halt:
            refusal = "UNSUPPORTED_CORPORATE_ACTION_GLOBAL_HALT"
        lot = intent.instrument.lot_size
        cap = int(bar.volume * self.config.max_volume_participation) // lot * lot
        shares = min(intent.requested_shares, cap)
        if refusal or shares < lot:
            self.event(bar.available_at, refusal or "NO_BUY_FILL_VOLUME_CAP", bar.symbol, execution_time=intent.entry_time, requested_shares=intent.requested_shares)
            if refusal in {"UNVERIFIED_PRICE_LIMITS", "UNVERIFIED_CURRENT_ST_STATUS"}:
                self.incomplete_reasons.add("价格限制/ST状态元数据不足，无法确认成交")
            return None
        fee = self.fees(intent.instrument, intent.entry_price, shares, "buy")
        cost = intent.entry_price * shares + fee
        self.cash -= cost
        self.total_buy_fees += fee
        self.total_filled_shares += shares
        factor = bar.adj_factor if bar.adj_factor is not None else self.known_adj_factors.get(bar.symbol)
        position = Position(intent.position_id, intent.advice, intent.instrument, intent.entry_time, bar.available_at, intent.entry_price, shares, shares, fee, cost, intent.stop_price, intent.entry_price, last_price=intent.entry_price, last_time=intent.entry_time, entry_adj_factor=factor, last_adj_factor=factor)
        self.positions[bar.symbol] = position
        self.event(bar.available_at, "SIMULATED_BUY_FILL", bar.symbol, position_id=position.position_id, execution_time=intent.entry_time, shares=shares, requested_shares=intent.requested_shares, price=intent.entry_price, fees_cny=fee, fill_model="open_price_with_slippage_and_ex_post_whole_bar_volume_cap")
        return position

    def _observe_factor(self, bar: Bar) -> bool:
        previous = self.known_adj_factors.get(bar.symbol)
        if previous is not None and bar.adj_factor is not None and not math.isclose(previous, bar.adj_factor, rel_tol=1e-9, abs_tol=1e-10):
            if (bar.symbol, bar.day) in self.exposed_days or bar.symbol in self.positions or bar.symbol in self.entry_intents:
                self.corporate_action_halt = True
                self.new_entries_enabled = False
                self.incomplete_reasons.add("unsupported corporate action：持仓期间复权因子变化，缺股数/现金公司行动记录")
                for position in self.positions.values():
                    position.stale = True
                for trade in self.trades:
                    if trade["symbol"] == bar.symbol and trade["entry_time"][:10] <= bar.day.isoformat() <= trade["exit_time"][:10]:
                        trade["valid_performance"] = False
                        trade["unsupported_corporate_action"] = True
                self.event(bar.available_at, "UNSUPPORTED_CORPORATE_ACTION", bar.symbol, old_adj_factor=previous, new_adj_factor=bar.adj_factor, action="暂停所有新的模拟成交，保留未结账本；迟到因子也使相关已结记录不可验证")
        if bar.adj_factor is not None:
            self.known_adj_factors[bar.symbol] = bar.adj_factor
        return not self.corporate_action_halt

    def _check_adjustment(self, position: Position, bar: Bar) -> bool:
        if not self._observe_factor(bar):
            return False
        if bar.adj_factor is not None:
            position.last_adj_factor = bar.adj_factor
        return True

    def roi_at(self, position: Position, price: float) -> float:
        proceeds = price * position.original_shares - self.fees(position.instrument, price, position.original_shares, "sell")
        return proceeds / position.entry_cost - 1

    def price_for_roi(self, position: Position, roi: float) -> float:
        low, high = 0.0, max(position.entry_price * (2 + roi), position.entry_price * 2)
        while self.roi_at(position, high) < roi:
            high *= 2
        for _ in range(60):
            middle = (low + high) / 2
            if self.roi_at(position, middle) < roi:
                low = middle
            else:
                high = middle
        return self.tick(position.instrument, high, "buy")

    def trigger_price_for_roi(self, position: Position, roi: float) -> float:
        # 把估计卖出滑点也计入保本/硬止盈触发价格；跳空依然可能亏损。
        desired_fill = self.price_for_roi(position, roi)
        return self.tick(position.instrument, desired_fill / (1 - self.config.slippage_fraction), "buy")

    def holding_sessions(self, position: Position, day: date) -> int:
        return sum(position.entry_time.date() <= item <= day for item in self.calendar)

    def _sell(self, position: Position, bar: Bar, raw_price: float, reason: str) -> bool:
        # 本版对股票和ETF均采用保守T+1；ETF的特殊T+0资格不由名称猜测。
        if bar.day <= position.entry_time.date():
            position.pending_exit_reason = position.pending_exit_reason or reason
            self.event(bar.available_at, "T_PLUS_ONE_EXIT_DEFERRED", bar.symbol, reason=reason, shares=position.shares)
            return False
        price = self.tick(position.instrument, raw_price * (1 - self.config.slippage_fraction), "sell")
        refusal = self._fillable(bar, "sell", price)
        if refusal or self.corporate_action_halt:
            position.pending_exit_reason = position.pending_exit_reason or reason
            self.event(bar.available_at, refusal or "UNSUPPORTED_CORPORATE_ACTION_GLOBAL_HALT", bar.symbol, reason=reason, shares=position.shares)
            if refusal == "UNVERIFIED_PRICE_LIMITS":
                self.incomplete_reasons.add("价格限制元数据不足，无法确认成交")
            return False
        lot = position.instrument.lot_size
        shares = min(position.shares, int(bar.volume * self.config.max_volume_participation) // lot * lot)
        if shares < lot:
            position.pending_exit_reason = position.pending_exit_reason or reason
            self.event(bar.available_at, "NO_SELL_FILL_VOLUME_CAP", bar.symbol, reason=reason, shares=position.shares)
            return False
        fee = self.fees(position.instrument, price, shares, "sell")
        proceeds = price * shares - fee
        self.cash += proceeds
        position.shares -= shares
        position.sold_shares += shares
        position.sale_proceeds += proceeds
        position.exit_fees += fee
        self.total_sell_fees += fee
        position.pending_exit_reason = reason if position.shares else None
        # OHLC没有内部分笔时刻；不编造精确成交秒，明确是bar内模拟。
        execution_time = bar.open_time if raw_price == bar.open else bar.timestamp
        self.event(bar.available_at, "SIMULATED_SELL_FILL", bar.symbol, position_id=position.position_id, execution_time=execution_time, shares=shares, remaining_shares=position.shares, price=price, fees_cny=fee, reason=reason, execution_time_precision="bar_open" if raw_price == bar.open else "bar_interval_unknown")
        if position.shares == 0:
            pnl = position.sale_proceeds - position.entry_cost
            roi = pnl / position.entry_cost * 100
            position.peak_roi_pct = max(position.peak_roi_pct, roi)
            position.max_adverse_roi_pct = min(position.max_adverse_roi_pct, roi)
            self.trades.append(jsonable({
                "position_id": position.position_id, "signal_id": position.advice.signal_id,
                "symbol": position.symbol, "name": position.instrument.name,
                "strategy": position.advice.strategy, "asset_type": position.instrument.asset_type,
                "sector": self.sector(position.instrument), "entry_time": position.entry_time,
                "entry_observed_at": position.observed_at, "entry_price": position.entry_price,
                "shares": position.original_shares, "entry_cost_cny": position.entry_cost,
                "quantity": position.original_shares,
                "exit_time": execution_time, "exit_observed_at": bar.available_at,
                "exit_bar_end": bar.timestamp,
                "exit_price": (position.sale_proceeds + position.exit_fees) / position.sold_shares,
                "entry_fees_cny": position.entry_fees, "exit_fees_cny": position.exit_fees,
                "entry_fees": position.entry_fees, "exit_fees": position.exit_fees,
                "net_pnl_cny": pnl, "realized_roi_pct": roi,
                "realized_pnl_cny": pnl,
                "peak_mfe_pct": position.peak_roi_pct, "max_adverse_roi_pct": position.max_adverse_roi_pct,
                "peak_roi_pct": position.peak_roi_pct,
                "direction_correct": position.peak_roi_pct >= 5.0, "exit_reason": reason,
                "holding_sessions": self.holding_sessions(position, bar.day),
                "execution_source": "local_ohlc_simulation", "price_is_estimate": True,
            }))
            self.positions.pop(position.symbol)
            return True
        return False

    def on_bar(self, bar: Bar) -> None:
        if not bar.is_complete:
            return
        self._pending_observation_gap(bar)
        self._observe_factor(bar)
        self._settle_entry(bar)
        position = self.positions.get(bar.symbol)
        context = self.contexts.pop(self.key(bar), None)
        if position is None:
            return
        if not self._check_adjustment(position, bar):
            return
        if position.last_time is not None and bar.timestamp < position.last_time:
            position.stale = True
            self.incomplete_reasons.add("行情可得顺序导致旧bar晚到，未回写历史成交")
            self.event(bar.available_at, "OUT_OF_ORDER_BAR_NO_EXECUTION", bar.symbol, data_time=bar.timestamp)
            return
        if bar.suspended or bar.volume <= 0:
            position.stale = True
            self.incomplete_reasons.add("持仓期间停牌/零成交量，估值使用此前价格")
            self.event(bar.available_at, "STALE_MARK_SUSPENDED", bar.symbol)
            return
        stop = context.get("stop_price") if context and context.get("position_id") == position.position_id else None
        pending = context.get("pending_exit_reason") if context else None
        reason = pending
        raw_price = bar.open
        if reason is None and self.holding_sessions(position, bar.day) >= self.config.max_holding_sessions:
            reason = "MAX_HOLDING_SESSIONS"
        target = self.trigger_price_for_roi(position, self.config.hard_take_profit_roi)
        if reason is None and stop is not None and bar.low <= stop:
            breakeven_price = self.trigger_price_for_roi(position, 0)
            reason = "TRAILING_STOP" if stop > breakeven_price + 1e-8 else "BREAKEVEN_STOP" if stop >= breakeven_price - 1e-8 else "INITIAL_STOP"
            raw_price = min(bar.open, stop)  # gap按更差开盘价，不保证止损价成交
        elif reason is None and bar.high >= target:
            reason = "HARD_TAKE_PROFIT"
            raw_price = max(bar.open, target)
        if reason:
            position.peak_roi_pct = max(position.peak_roi_pct, self.roi_at(position, raw_price) * 100)
            position.max_adverse_roi_pct = min(position.max_adverse_roi_pct, self.roi_at(position, raw_price) * 100)
            if self._sell(position, bar, raw_price, reason):
                return
        # 只有仍持有股数才看完整bar高低；新stop供后续bar使用。
        position.last_price, position.last_time, position.stale = bar.close, bar.timestamp, False
        position.peak_price = max(position.peak_price, bar.high)
        position.peak_roi_pct = max(position.peak_roi_pct, self.roi_at(position, bar.high) * 100)
        position.max_adverse_roi_pct = min(position.max_adverse_roi_pct, self.roi_at(position, bar.low) * 100)
        if position.peak_roi_pct >= self.config.breakeven_trigger_roi * 100:
            position.stop_price = max(position.stop_price, self.trigger_price_for_roi(position, 0))
        if position.peak_roi_pct >= self.config.trailing_trigger_roi * 100:
            position.stop_price = max(position.stop_price, position.peak_price * (1 - self.config.trailing_drawdown_fraction))

    def mark_only(self, bar: Bar) -> None:
        self._pending_observation_gap(bar)
        self._observe_factor(bar)
        position = self.positions.get(bar.symbol)
        if position is None or not self._check_adjustment(position, bar):
            return
        if bar.suspended or bar.volume <= 0:
            position.stale = True
            self.incomplete_reasons.add("持仓收盘估值停牌/零成交量")
        elif position.last_time is None or bar.timestamp >= position.last_time:
            position.last_price, position.last_time, position.stale = bar.close, bar.timestamp, False

    def _pending_observation_gap(self, bar: Bar) -> None:
        intent = self.entry_intents.get(bar.symbol)
        if intent and bar.timestamp > intent.bar_key[2]:
            self.incomplete_reasons.add("入场确认晚于后续bar可得时刻，预留期间路径无法验证，未事后补成交")
            self.event(bar.available_at, "UNCONFIRMED_ENTRY_OBSERVATION_GAP", bar.symbol, intended_entry_time=intent.entry_time, pending_entry_bar_end=intent.bar_key[2], skipped_data_time=bar.timestamp)

    def mark_missing(self, symbol: str, day: date, now: datetime) -> None:
        position = self.positions.get(symbol)
        if position and (position.last_time is None or position.last_time.date() < day):
            position.stale = True
            self.incomplete_reasons.add("持仓交易日缺bar，估值滞后")
            self.event(now, "STALE_MARK_MISSING_BAR", symbol, session=day, last_data_time=position.last_time)

    def record_equity(self, now: datetime, reason: str = "BAR_AVAILABLE") -> None:
        equity = self.equity()
        market_value = sum(item.shares * item.last_price for item in self.positions.values())
        stale = any(item.stale for item in self.positions.values())
        self._equity_peak = max(self._equity_peak, equity)
        peak = self._equity_peak
        row = jsonable({"timestamp": now, "time": now, "cash_cny": self.cash, "reserved_cash_cny": sum(item.reserve for item in self.entry_intents.values()), "market_value_cny": market_value, "gross_market_value_cny": market_value, "estimated_exit_fees_cny": sum(self.fees(item.instrument, item.last_price, item.shares, "sell") for item in self.positions.values()), "equity_cny": equity, "drawdown_pct": (peak - equity) / peak * 100 if peak > 0 else 0.0, "open_positions": len(self.positions), "pending_entries": len(self.entry_intents), "stale": stale, "valuation_stale": stale, "reason": reason})
        if self.equity_curve and self.equity_curve[-1]["time"] == row["time"]:
            self.equity_curve[-1] = row
        else:
            self.equity_curve.append(row)

    @staticmethod
    def closed_statistics(trades: list[dict[str, Any]]) -> dict[str, Any]:
        pnls = [item["net_pnl_cny"] for item in trades]
        wins, losses = [value for value in pnls if value > 1e-8], [value for value in pnls if value < -1e-8]
        loss_sum = -sum(losses)
        return {"closed_trades": len(trades), "winning_trades": len(wins), "losing_trades": len(losses), "breakeven_trades": len(trades) - len(wins) - len(losses), "realized_win_rate_pct": len(wins) / len(trades) * 100 if trades else None, "closed_net_pnl_cny": sum(pnls), "profit_factor": sum(wins) / loss_sum if loss_sum > 0 else None, "profit_factor_status": "defined" if loss_sum > 0 else "no_losing_closed_trades" if trades else "no_closed_trades", "avg_realized_roi_pct": sum(item["realized_roi_pct"] for item in trades) / len(trades) if trades else None, "directional_accuracy_pct": sum(item["direction_correct"] for item in trades) / len(trades) * 100 if trades else None, "direction_threshold_mfe_pct": 5.0}

    def summary(self) -> dict[str, Any]:
        equity = self.equity()
        peak = self.initial_cash
        max_dd_cny = max_dd_pct = 0.0
        for row in self.equity_curve:
            peak = max(peak, row["equity_cny"])
            drawdown = peak - row["equity_cny"]
            max_dd_cny = max(max_dd_cny, drawdown)
            max_dd_pct = max(max_dd_pct, drawdown / peak * 100 if peak > 0 else 0.0)
        result = self.closed_statistics(self.trades)
        result.update({
            "initial_cash_cny": self.initial_cash, "cash_cny": self.cash, "ending_equity_cny": equity,
            "net_pnl_cny": equity - self.initial_cash, "total_roi_pct": (equity / self.initial_cash - 1) * 100,
            "max_drawdown_cny": max_dd_cny, "max_drawdown_pct": max_dd_pct,
            "equity_basis": "cash_plus_pending_reserve_plus_unadjusted_close_mark_minus_estimated_exit_fees",
            "drawdown_basis": "observed_portfolio_equity_including_open_positions_and_estimated_exit_fees",
            "open_positions": len(self.positions), "pending_entries": len(self.entry_intents),
            "max_concurrent_positions": self.max_concurrent_positions,
            "filled_shares": self.total_filled_shares, "total_buy_fees_cny": self.total_buy_fees,
            "total_sell_fees_cny": self.total_sell_fees,
            "valid_performance": not self.incomplete_reasons and not self.corporate_action_halt and not self.entry_intents,
            "performance_complete": not self.incomplete_reasons and not any(item.stale for item in self.positions.values()) and not self.entry_intents,
            "incomplete_reasons": sorted(self.incomplete_reasons), "corporate_action_halt": self.corporate_action_halt,
            "outstanding_positions": jsonable([{"symbol": item.symbol, "strategy": item.advice.strategy, "shares": item.shares, "entry_price": item.entry_price, "entry_cost_cny": item.entry_cost, "last_price": item.last_price, "last_data_time": item.last_time, "stale": item.stale, "pending_exit_reason": item.pending_exit_reason, "stop_price": item.stop_price, "peak_mfe_pct": item.peak_roi_pct} for item in self.positions.values()]),
            "by_strategy": {strategy: {**self.closed_statistics([item for item in self.trades if item["strategy"] == strategy]), "open_positions": sum(item.advice.strategy == strategy for item in self.positions.values()), "pending_entries": sum(item.advice.strategy == strategy for item in self.entry_intents.values())} for strategy in sorted({item["strategy"] for item in self.trades} | {item.advice.strategy for item in self.positions.values()} | {item.advice.strategy for item in self.entry_intents.values()})},
            "execution_source": "local_ohlc_simulation_no_broker", "price_is_estimate": True,
            "sample_unit": "closed_signal_trade", "sample_independence_assumed": False,
            "survivorship_bias_assessed": False,
            "assumptions": ["股票与ETF均按T+1，买入当日触发退出只锁存，后续可卖bar再尝试", "开盘预留资金；完整bar可得后按实际bar总量1%及整手约束确认模拟成交", "止损与止盈同bar触发优先止损；bar开盘捕获prior stop，本bar高点只更新未来stop", "0.5%风险及单票/总/行业上限在入场时检查，浮动价格及跳空可使实际风险超出计划", "止损为条件而非保证成交价；跳空、跌停、停牌、零量可能使损失超过计划", "佣金、过户、印花税与滑点只是可配置估计；未结不强造清仓", "MDD只采样数据可得时的收盘估值，不是逐笔或盘中最坏回撤", "缺公司行动数据时复权因子变化会停止新模拟成交，保留无法验证的未结账本"],
        })
        return result
