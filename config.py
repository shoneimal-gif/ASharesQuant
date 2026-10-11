"""研究参数，不代表经过收益优化的实盘策略；只用 Python 3.10+ 标准库。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import math


@dataclass(frozen=True)
class QuantConfig:
    reference_capital_cny: float | None = None
    paper_initial_cash_cny: float = 100_000.0
    allowed_boards: tuple[str, ...] = ("MAIN", "ETF")
    benchmark_symbol: str = "000300.SH"
    require_market_filter: bool = True
    min_listing_calendar_days: int = 180
    min_history_bars: int = 65
    min_price_cny: float = 3.0
    min_daily_amount_cny: float = 50_000_000.0
    top_candidates: int = 20
    daily_volume_ratio: float = 1.5
    daily_breakout_days: int = 20
    atr_period: int = 14
    initial_stop_pct: float = 0.06
    max_stop_pct: float = 0.12
    atr_stop_multiple: float = 2.0
    target_r_multiple: float = 2.0
    suggested_weight_cap: float = 0.05
    risk_per_trade_fraction: float = 0.005
    max_total_weight: float = 0.30
    max_sector_weight: float = 0.15
    max_positions: int = 6
    intraday_volume_ratio: float = 1.8
    intraday_volume_lookback: int = 12
    max_intraday_age_seconds: int = 600
    intraday_cooldown_minutes: int = 30
    intraday_signal_valid_minutes: int = 20
    watch_poll_seconds: int = 30
    watch_history_limit: int = 500
    commission_rate: float = 0.00025
    minimum_commission_cny: float = 5.0
    stock_sell_stamp_rate: float = 0.0005
    stock_transfer_rate: float = 0.00001
    etf_sell_stamp_rate: float = 0.0
    slippage_fraction: float = 0.001
    max_volume_participation: float = 0.01
    breakeven_trigger_roi: float = 0.08
    trailing_trigger_roi: float = 0.12
    trailing_drawdown_fraction: float = 0.06
    hard_take_profit_roi: float = 0.20
    max_holding_sessions: int = 20
    api_max_rpm: int = 40
    api_timeout_seconds: float = 15.0
    api_retries: int = 2
    minute_max_symbols: int = 20

    def __post_init__(self) -> None:
        if not isinstance(self.require_market_filter, bool):
            raise ValueError("require_market_filter 必须是 true 或 false")
        if not isinstance(self.allowed_boards, tuple) or any(not isinstance(value, str) for value in self.allowed_boards):
            raise ValueError("allowed_boards 必须为板块名称列表")
        from models import validate_symbol
        validate_symbol(self.benchmark_symbol)
        for name, value in asdict(self).items():
            if name not in {"reference_capital_cny", "allowed_boards", "benchmark_symbol", "require_market_filter"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"配置 {name} 必须为数值")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"配置 {name} 必须为有限非负值")
        if self.reference_capital_cny is not None and (isinstance(self.reference_capital_cny, bool) or not isinstance(self.reference_capital_cny, (int, float))):
            raise ValueError("实际参考资金必须为数值或 null")
        if self.reference_capital_cny is not None and self.reference_capital_cny <= 0:
            raise ValueError("资金未定请用 null；实际参考资金须大于零")
        if self.paper_initial_cash_cny <= 0 or self.min_history_bars < 61 or self.atr_period < 2:
            raise ValueError("模拟资金或历史窗口不足")
        if not self.allowed_boards or set(self.allowed_boards) - {"MAIN", "ETF", "CHINEXT", "STAR", "BSE"}:
            raise ValueError("未知板块")
        if {"STAR", "BSE"} & set(self.allowed_boards):
            raise ValueError("第一版暂不模拟科创板/北交所的特殊申报规则，不能仅修改板块名启用")
        if not 0 < self.risk_per_trade_fraction <= 0.02 or not 0 < self.suggested_weight_cap <= self.max_total_weight <= 1:
            raise ValueError("风险比例/仓位上限不合理")
        if not 0 < self.max_sector_weight <= self.max_total_weight or self.max_positions < 1:
            raise ValueError("行业/持仓限制不合理")
        if not 0 < self.initial_stop_pct <= self.max_stop_pct < 1 or self.target_r_multiple <= 0:
            raise ValueError("止损/目标参数不合理")
        if not 0 < self.max_volume_participation <= 0.10 or not 0 <= self.slippage_fraction < 0.05:
            raise ValueError("模拟成交假设不合理")
        if not 0 < self.breakeven_trigger_roi < self.trailing_trigger_roi < self.hard_take_profit_roi:
            raise ValueError("动态退出参数次序不合理")
        if not 0 < self.trailing_drawdown_fraction < 1 or self.max_holding_sessions < 1:
            raise ValueError("追踪回撤或持有周期不合理")
        if self.watch_poll_seconds < 5 or self.watch_history_limit < 1 or self.api_max_rpm < 1 or self.api_timeout_seconds <= 0:
            raise ValueError("监控/接口配置不合理")
        if self.intraday_volume_lookback < 3 or self.daily_breakout_days < 2 or self.top_candidates < 1:
            raise ValueError("策略窗口/输出数量不足")
        if self.max_intraday_age_seconds < 1 or self.intraday_signal_valid_minutes < 1 or self.minute_max_symbols < 1:
            raise ValueError("分钟新鲜度、有效期和自选数量必须大于零")
        integer_names = ("min_listing_calendar_days", "min_history_bars", "daily_breakout_days", "atr_period", "max_positions", "watch_history_limit", "watch_poll_seconds", "intraday_volume_lookback", "max_intraday_age_seconds", "intraday_cooldown_minutes", "intraday_signal_valid_minutes", "api_max_rpm", "api_retries", "minute_max_symbols", "top_candidates", "max_holding_sessions")
        for name in integer_names:
            if isinstance(getattr(self, name), bool) or not isinstance(getattr(self, name), int):
                raise ValueError(f"配置 {name} 必须为整数")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "QuantConfig":
        if path is None:
            return cls()
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("配置须为 JSON 对象")
        unknown = set(payload) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"未知配置项：{', '.join(sorted(unknown))}")
        if "allowed_boards" in payload:
            if not isinstance(payload["allowed_boards"], list):
                raise ValueError("allowed_boards 须为 JSON 数组")
            payload["allowed_boards"] = tuple(payload["allowed_boards"])
        return cls(**payload)
