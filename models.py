"""A 股建议版共享数据契约。价格均为未复权人民币，成交量均为股。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
import hashlib
import json
import math
import re

CN_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def parse_time(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=CN_TZ)
    return result.astimezone(CN_TZ)


def cn_time(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), CN_TZ)


def validate_symbol(symbol: str) -> str:
    result = symbol.strip().upper()
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", result):
        raise ValueError(f"证券代码须为六位数字加交易所后缀，例如 600000.SH：{result!r}")
    return result


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    asset_type: str  # STOCK / ETF / INDEX
    board: str  # MAIN / CHINEXT / STAR / BSE / ETF / INDEX
    sector: str = "UNKNOWN"
    list_date: date = date(1900, 1, 1)
    lot_size: int = 100
    t_plus_one: bool = True
    delist_date: date | None = None

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        if self.asset_type not in {"STOCK", "ETF", "INDEX"} or self.board not in {"MAIN", "CHINEXT", "STAR", "BSE", "ETF", "INDEX"}:
            raise ValueError("证券类型或板块不明确，拒绝推断交易资格")
        if self.lot_size <= 0 or self.lot_size != int(self.lot_size):
            raise ValueError("交易单位必须为正整数")
        if self.asset_type == "STOCK" and not self.t_plus_one:
            raise ValueError("本版本 A 股股票按 T+1 建模")


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime  # K 线结束时刻，5m 示例为 09:35
    available_at: datetime  # 该条数据可被策略观察的时刻
    timeframe: str  # daily / 5m
    open: float
    high: float
    low: float
    close: float
    volume: float  # 股，ETF 亦使用份额；不是手
    amount: float  # 元
    adj_factor: float | None = None
    prev_close: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    suspended: bool = False
    is_st: bool | None = None
    has_price_limit: bool | None = None
    is_complete: bool = True

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        if self.timeframe not in {"daily", "5m"}:
            raise ValueError("仅支持 daily / 5m；不要把未完成 K 线冒充完整行情")
        if self.timestamp.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("数据时间必须带时区")
        if self.available_at < self.timestamp:
            raise ValueError("数据可得时刻不能早于 K 线结束时刻")
        values = (self.open, self.high, self.low, self.close, self.volume, self.amount)
        if not all(math.isfinite(value) for value in values) or min(values[:4]) <= 0 or min(values[4:]) < 0:
            raise ValueError("价格/成交量包含无效值")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("OHLC 高低价格矛盾")
        for item in (self.adj_factor, self.prev_close, self.limit_up, self.limit_down):
            if item is not None and (not math.isfinite(item) or item <= 0):
                raise ValueError("复权因子/前收盘/涨跌停价必须为正数")
        if self.limit_up is not None and self.limit_down is not None and self.limit_down > self.limit_up:
            raise ValueError("涨跌停价颠倒")

    @property
    def day(self) -> date:
        return self.timestamp.astimezone(CN_TZ).date()

    @property
    def open_time(self) -> datetime:
        return cn_time(self.day, 9, 30) if self.timeframe == "daily" else self.timestamp - timedelta(minutes=5)


@dataclass
class DataSet:
    instruments: dict[str, Instrument]
    daily: dict[str, list[Bar]]
    intraday: dict[str, list[Bar]]
    calendar: tuple[date, ...]
    source_label: str = "用户 CSV"
    metadata: dict[str, Any] = field(default_factory=dict)

    def next_session(self, day: date) -> date | None:
        return next((item for item in self.calendar if item > day), None)

    def visible_daily(self, symbol: str, as_of: datetime) -> list[Bar]:
        return [bar for bar in self.daily.get(symbol, []) if bar.is_complete and bar.timestamp <= as_of and bar.available_at <= as_of]

    def visible_intraday(self, symbol: str, as_of: datetime) -> list[Bar]:
        return [bar for bar in self.intraday.get(symbol, []) if bar.is_complete and bar.timestamp <= as_of and bar.available_at <= as_of]


@dataclass(frozen=True)
class Advice:
    signal_id: str
    symbol: str
    name: str
    strategy: str
    timeframe: str
    as_of: datetime
    data_time: datetime
    action: str  # BUY_CANDIDATE / WATCH
    score: float
    entry_low: float
    entry_high: float
    stop_price: float
    target_price: float
    suggested_weight: float
    valid_until: datetime
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    allow_paper_entry: bool = False
    sector: str = "UNKNOWN"


def signal_key(symbol: str, strategy: str, data_time: datetime) -> str:
    return hashlib.sha256(f"{symbol}|{strategy}|{data_time.isoformat()}".encode()).hexdigest()[:24]


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(jsonable(value), sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
