"""A股研究信号与建议；评分用于排序，禁止把评分当成上涨概率。

所有计算先过滤K线结束时间和可得时间。日线保守地按18:00可得处理；
复权只使用本次计算已经可得的历史因子，并归一到当前可见价格基准。
本模块不连接券商，不提交订单，缺少必要交易元数据时不产生开仓候选。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
import math
from typing import Any

from config import QuantConfig
from models import Advice, Bar, CN_TZ, DataSet, Instrument, cn_time, signal_key

LOGGER = logging.getLogger(__name__)


def _now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("策略计算时点as_of必须是带时区的datetime")
    return value.astimezone(CN_TZ)


def _calendar(dataset: DataSet) -> list[date]:
    return sorted(set(dataset.calendar))


def _calendar_covers(dataset: DataSet, day: date) -> bool:
    """仅接受明确声明的覆盖范围，不把日历最后一日之后猜成休市。"""
    try:
        first = date.fromisoformat(str(dataset.metadata["calendar_start"]))
        last = date.fromisoformat(str(dataset.metadata["calendar_end"]))
    except (KeyError, TypeError, ValueError):
        return False
    return first <= day <= last


def _next_session(dataset: DataSet, day: date) -> date | None:
    return next((item for item in _calendar(dataset) if item > day and _calendar_covers(dataset, item)), None)


def _expected_daily(dataset: DataSet, as_of: datetime, *, intraday: bool = False) -> date | None:
    if not _calendar_covers(dataset, as_of.date()):
        return None
    completed = [day for day in _calendar(dataset)
                 if day < as_of.date() or (not intraday and day == as_of.date() and as_of >= cn_time(day, 18))]
    return completed[-1] if completed else None


def _daily(dataset: DataSet, symbol: str, as_of: datetime, *, before_today: bool = False) -> list[Bar]:
    return sorted((bar for bar in dataset.visible_daily(symbol, as_of)
                   if bar.symbol == symbol and bar.timeframe == "daily" and cn_time(bar.day, 18) <= as_of
                   and (not before_today or bar.day < as_of.date())), key=lambda bar: bar.timestamp)


def _minute_end(bar: Bar) -> bool:
    stamp = bar.timestamp.astimezone(CN_TZ)
    wall = stamp.time().replace(tzinfo=None)
    return (stamp.second == 0 and stamp.microsecond == 0 and stamp.minute % 5 == 0
            and (time(9, 35) <= wall <= time(11, 30) or time(13, 5) <= wall <= time(15)))


def _minutes(dataset: DataSet, symbol: str, as_of: datetime) -> list[Bar]:
    return sorted((bar for bar in dataset.visible_intraday(symbol, as_of)
                   if bar.symbol == symbol and bar.timeframe == "5m" and _minute_end(bar)), key=lambda bar: bar.timestamp)


def _active_session(as_of: datetime) -> bool:
    wall = as_of.time().replace(tzinfo=None)
    return time(9, 30) <= wall < time(11, 30) or time(13) <= wall < time(14, 57)


def _mean(values: list[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


@dataclass(frozen=True)
class _Price:
    bar: Bar
    open: float
    high: float
    low: float
    close: float


def _normalized(bars: list[Bar], reference_factor: float) -> list[_Price]:
    """历史原始价格×当时因子/当前可见因子；不会访问未来因子。"""
    return [_Price(bar, bar.open * (float(bar.adj_factor) / reference_factor),
                   bar.high * (float(bar.adj_factor) / reference_factor),
                   bar.low * (float(bar.adj_factor) / reference_factor),
                   bar.close * (float(bar.adj_factor) / reference_factor)) for bar in bars]


def _atr(prices: list[_Price], period: int) -> float:
    if len(prices) < period + 1:
        return 0.0
    ranges = [max(current.high - current.low, abs(current.high - previous.close),
                  abs(current.low - previous.close)) for previous, current in zip(prices, prices[1:])]
    return _mean(ranges[-period:])


def _valid_factor(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _duplicate_times(bars: list[Bar], *, daily: bool = False) -> bool:
    identities = [bar.day if daily else bar.timestamp for bar in bars]
    return len(identities) != len(set(identities))


def _metadata_reasons(bar: Bar, label: str) -> list[tuple[str, str]]:
    reasons: list[tuple[str, str]] = []
    if bar.is_st is None or not isinstance(bar.is_st, bool):
        reasons.append(("MISSING_ST_STATUS", f"{label}缺少明确is_st状态"))
    elif bar.is_st:
        reasons.append(("ST", f"{label}为ST状态"))
    if not isinstance(bar.suspended, bool):
        reasons.append(("MISSING_SUSPENSION_STATUS", f"{label}停牌状态不明确"))
    elif bar.suspended:
        reasons.append(("SUSPENDED", f"{label}停牌"))
    if not isinstance(bar.has_price_limit, bool):
        reasons.append(("MISSING_LIMIT_STATUS", f"{label}缺少明确has_price_limit状态"))
    elif bar.has_price_limit and (bar.limit_up is None or bar.limit_down is None):
        reasons.append(("MISSING_LIMIT_PRICES", f"{label}实行价格限制但缺少官方涨跌停价"))
    elif (bar.limit_up is None) != (bar.limit_down is None):
        reasons.append(("INCOMPLETE_LIMIT_PRICES", f"{label}涨跌停价只提供一侧，不能猜测另一侧"))
    elif bar.has_price_limit is False and bar.limit_up is not None:
        reasons.append(("INCONSISTENT_LIMIT_METADATA", f"{label}不限幅标记与提供的涨跌停价矛盾，拒绝自行选用一方"))
    if not _valid_factor(bar.adj_factor):
        reasons.append(("MISSING_ADJUSTMENT_FACTOR", f"{label}缺少有效复权因子"))
    return reasons


@dataclass
class _Inspection:
    instrument: Instrument
    daily: list[Bar]
    minutes: list[Bar]
    latest: Bar | None
    prices: list[_Price]
    reasons: list[tuple[str, str]]
    watch_reasons: list[str]
    expected_day: date | None
    fresh: bool
    amount20: float


def _inspect(dataset: DataSet, instrument: Instrument, config: QuantConfig,
             as_of: datetime, timeframe: str) -> _Inspection:
    is_minute = timeframe == "5m"
    history = _daily(dataset, instrument.symbol, as_of, before_today=is_minute)
    minute_history = _minutes(dataset, instrument.symbol, as_of)
    current_minutes = [bar for bar in minute_history if bar.day == as_of.date()]
    latest = (current_minutes[-1] if current_minutes else None) if is_minute else (history[-1] if history else None)
    reasons: list[tuple[str, str]] = []
    watch_reasons: list[str] = []
    selection = dataset.metadata.get("research_symbols")
    if selection is not None and (not isinstance(selection, list) or instrument.symbol not in selection):
        reasons.append(("NOT_IN_RESEARCH_SELECTION", "此证券仅保留供基准过滤，不在CLI显式研究自选中"))
    if instrument.asset_type not in {"STOCK", "ETF"}:
        reasons.append(("NOT_TRADABLE_UNIVERSE", "指数仅用于市场状态过滤，不作为股票/ETF候选"))
    if instrument.board not in config.allowed_boards:
        reasons.append(("BOARD_NOT_ALLOWED", "证券板块未在研究许可范围内"))
    if ((instrument.asset_type == "ETF" and instrument.board != "ETF") or
            (instrument.asset_type == "STOCK" and instrument.board in {"ETF", "INDEX"})):
        reasons.append(("INCONSISTENT_ASSET_METADATA", "证券类型与板块元数据矛盾"))
    if not isinstance(instrument.list_date, date) or instrument.list_date == date(1900, 1, 1):
        reasons.append(("MISSING_LIST_DATE", "上市日期未明确提供"))
    elif (as_of.date() - instrument.list_date).days < config.min_listing_calendar_days:
        reasons.append(("LISTING_TOO_RECENT", f"上市不足{config.min_listing_calendar_days}个日历日"))
    if instrument.delist_date is not None and instrument.delist_date <= as_of.date():
        reasons.append(("DELISTED", "截至计算时点已退市"))
    if not isinstance(instrument.t_plus_one, bool):
        reasons.append(("MISSING_SETTLEMENT_RULE", "证券交易制度未明确提供"))
    required = max(config.min_history_bars, config.daily_breakout_days + 1, config.atr_period + 1, 64)
    if len(history) < required:
        reasons.append(("INSUFFICIENT_DAILY_HISTORY", f"可得完整日线{len(history)}根，需至少{required}根"))
    if _duplicate_times(history, daily=True):
        reasons.append(("DUPLICATE_DAILY_BARS", "日线存在重复交易日，不能重复计入指标窗口"))
    if _duplicate_times(minute_history):
        reasons.append(("DUPLICATE_MINUTE_BARS", "5分钟线存在重复结束时刻"))
    relevant = history[-required:]
    if any(not _valid_factor(bar.adj_factor) for bar in relevant):
        reasons.append(("MISSING_HISTORICAL_ADJUSTMENT", "指标历史窗口缺少复权因子，拒绝推测复权价格"))
    amount20 = _mean([bar.amount for bar in history[-20:]]) if len(history) >= 20 else 0.0
    if amount20 < config.min_daily_amount_cny:
        reasons.append(("LOW_LIQUIDITY", f"近20日平均成交额低于{config.min_daily_amount_cny:,.0f}元"))
    if latest is None:
        reasons.append(("NO_CURRENT_MINUTE_DATA" if is_minute else "NO_VISIBLE_DAILY_DATA",
                        "没有当前交易日可得完整5分钟线" if is_minute else "没有可得完整日线"))
    else:
        reasons.extend(_metadata_reasons(latest, "最新行情"))
        if latest.close < config.min_price_cny:
            reasons.append(("LOW_PRICE", f"未复权价格低于{config.min_price_cny:.2f}元"))
    if is_minute and history:
        reasons.extend(_metadata_reasons(history[-1], "前一完整交易日日线"))
    expected = _expected_daily(dataset, as_of, intraday=is_minute)
    fresh = expected is not None and bool(history) and history[-1].day == expected
    if not _calendar_covers(dataset, as_of.date()):
        watch_reasons.append("交易日历声明的覆盖范围不包含计算日期；不能把旧日历当作当前交易日依据")
    if expected is None:
        watch_reasons.append("交易日历不能确认最新应有完整日线，建议仅供观察")
    elif not fresh:
        watch_reasons.append(f"日线过期或交易日不匹配：最新应为{expected.isoformat()}，不能允许模拟入场")
    if is_minute:
        if as_of.date() not in _calendar(dataset):
            watch_reasons.append("交易日历未确认当前交易日")
        if not _active_session(as_of):
            watch_reasons.append("当前不在连续交易时段；午休/收盘不生成新入场候选")
        if latest is not None and (as_of - latest.timestamp).total_seconds() > config.max_intraday_age_seconds:
            watch_reasons.append(f"最新5分钟线超过{config.max_intraday_age_seconds}秒新鲜度上限")
        if latest is not None and len([bar for bar in minute_history if bar.timestamp < latest.timestamp]) < config.intraday_volume_lookback:
            watch_reasons.append(f"5分钟量比前置窗口不足{config.intraday_volume_lookback}根，暂不评估入场")
    prices: list[_Price] = []
    if latest is not None and _valid_factor(latest.adj_factor) and relevant and all(
            _valid_factor(bar.adj_factor) for bar in relevant):
        prices = _normalized(relevant, float(latest.adj_factor))
    return _Inspection(instrument, history, minute_history, latest, prices, reasons, watch_reasons,
                       expected, fresh, amount20)


def market_regime(dataset: DataSet, config: QuantConfig, as_of: datetime,
                  timeframe: str = "daily") -> dict[str, Any]:
    """仅用可得基准日线；RISK_ON是研究门槛标签，不是市场预测。"""
    as_of = _now(as_of)
    if timeframe not in {"daily", "5m"}:
        raise ValueError("timeframe仅支持daily或5m")
    bars = _daily(dataset, config.benchmark_symbol, as_of, before_today=timeframe == "5m")
    identity = dataset.instruments.get(config.benchmark_symbol)
    official_index = identity is not None and identity.asset_type == "INDEX" and identity.board == "INDEX"
    calendar_covered = _calendar_covers(dataset, as_of.date())
    expected = _expected_daily(dataset, as_of, intraday=timeframe == "5m")
    fresh = bool(bars) and expected is not None and bars[-1].day == expected
    report: dict[str, Any] = {
        "status": "UNKNOWN", "allow_entries": not config.require_market_filter,
        "filter_required": config.require_market_filter, "benchmark_symbol": config.benchmark_symbol,
        "as_of": as_of.isoformat(), "data_time": bars[-1].timestamp.isoformat() if bars else None,
        "expected_daily_session": expected.isoformat() if expected else None,
        "visible_bars": len(bars), "fresh": fresh, "close": None, "sma20": None, "sma60": None,
        "calendar_coverage_ok": calendar_covered,
        "calendar_start": dataset.metadata.get("calendar_start"), "calendar_end": dataset.metadata.get("calendar_end"),
        "price_basis": "raw_official_index_points" if official_index else "adjusted_latest_visible_prices",
        "price_unit": "index_points" if official_index else "CNY",
        "adjustment_applicable": not official_index,
        "reasons": [], "score_is_probability": False,
    }
    if len(bars) < 60:
        report["reasons"].append("基准可得完整日线不足60根")
    elif identity is None:
        report["reasons"].append("基准证券身份缺失，不能按代码猜成不需要复权因子的指数")
    elif _duplicate_times(bars, daily=True):
        report["reasons"].append("基准存在重复交易日")
    elif not official_index and any(not _valid_factor(bar.adj_factor) for bar in bars[-60:]):
        report["reasons"].append("股票/ETF基准缺少历史复权因子；指数例外不能用于股票/ETF")
    else:
        # 官方指数点位本身就是指数编制结果，不伪造factor=1、不套用股票公司行动复权。
        prices = ([_Price(bar, bar.open, bar.high, bar.low, bar.close) for bar in bars[-60:]]
                  if official_index else _normalized(bars[-60:], float(bars[-1].adj_factor)))
        closes = [item.close for item in prices]
        sma20, sma60 = _mean(closes[-20:]), _mean(closes[-60:])
        positive = sma20 > sma60 and closes[-1] >= sma20
        report.update(close=closes[-1], sma20=sma20, sma60=sma60,
                      status="RISK_ON" if positive else "RISK_OFF")
        report["allow_entries"] = not config.require_market_filter or (positive and fresh)
        report["reasons"].append("基准SMA20>SMA60且收盘不低于SMA20" if positive
                                  else "基准未通过SMA20/SMA60与收盘位置门槛")
    if not fresh:
        report["reasons"].append("基准日线缺失/过期，不能按过期基准确认当前市场状态")
        if config.require_market_filter:
            report["allow_entries"] = False
    if not config.require_market_filter:
        report["reasons"].append("配置显式关闭市场状态过滤，仍保留市场数据诊断")
    if not calendar_covered:
        report["allow_entries"] = False
        report["reasons"].append("交易日历范围未覆盖计算日期，必须更新日历后才能允许模拟入场")
    return report


def diagnose_universe(dataset: DataSet, config: QuantConfig, as_of: datetime,
                      timeframe: str = "daily") -> dict[str, Any]:
    as_of = _now(as_of)
    if timeframe not in {"daily", "5m"}:
        raise ValueError("timeframe仅支持daily或5m")
    regime = market_regime(dataset, config, as_of, timeframe)
    rows: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    eligible_count = watch_only = 0
    for symbol, instrument in sorted(dataset.instruments.items()):
        check = _inspect(dataset, instrument, config, as_of, timeframe)
        reasons = [message for _, message in check.reasons] + check.watch_reasons
        codes = list(dict.fromkeys(code for code, _ in check.reasons))
        counts.update(codes)
        eligible = not check.reasons
        next_day = _next_session(dataset, as_of.date() if timeframe == "5m" else
                                 check.latest.day if check.latest is not None else as_of.date())
        if next_day is None:
            reasons.append("下一交易日未被输入交易日历确认")
        expired = timeframe == "daily" and next_day is not None and cn_time(next_day, 15) <= as_of
        if expired:
            reasons.append("日线建议下一交易日15:00有效期已到")
        if not regime["allow_entries"]:
            reasons.append("市场过滤未通过，研究信号降为WATCH")
        ready = eligible and not check.watch_reasons and next_day is not None and not expired and regime["allow_entries"]
        if eligible:
            eligible_count += 1
            watch_only += int(not ready)
        rows[symbol] = {
            "name": instrument.name, "asset_type": instrument.asset_type, "board": instrument.board,
            "eligible": eligible, "paper_entry_data_ready": ready,
            "status": "REJECTED" if not eligible else "ELIGIBLE" if ready else "WATCH_ONLY",
            "reasons": reasons, "reason_codes": codes,
            "visible_daily_bars": len(check.daily),
            "latest_daily_time": check.daily[-1].timestamp.isoformat() if check.daily else None,
            "visible_intraday_bars": len(check.minutes),
            "latest_intraday_time": check.minutes[-1].timestamp.isoformat() if check.minutes else None,
            "mean_daily_amount_20_cny": check.amount20,
        }
    return {"as_of": as_of.isoformat(), "timeframe": timeframe, "total_instruments": len(rows),
            "eligible_count": eligible_count, "rejected_count": len(rows) - eligible_count,
            "watch_only_count": watch_only, "rejection_counts": dict(sorted(counts.items())),
            "calendar_coverage_ok": _calendar_covers(dataset, as_of.date()),
            "market": regime, "by_symbol": rows}


def _trend(prices: list[_Price]) -> dict[str, float | bool]:
    closes = [item.close for item in prices]
    sma20, sma60 = _mean(closes[-20:]), _mean(closes[-60:])
    old20 = _mean(closes[-23:-3])
    return {"sma20": sma20, "sma60": sma60, "sma20_three_bars_ago": old20,
            "uptrend": len(closes) >= 63 and sma20 > sma60 and sma20 > old20 and closes[-1] >= sma20}


def _score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _make_advice(dataset: DataSet, config: QuantConfig, check: _Inspection,
                 regime: dict[str, Any], as_of: datetime, *, strategy: str,
                 timeframe: str, atr: float, structural_stop: float, score: float,
                 reasons: list[str], metrics: dict[str, Any], entry_floor: float = 0.0) -> Advice:
    latest = check.latest
    if latest is None:
        raise ValueError("生成建议前必须有可见最新K线")
    close = latest.close
    entry_low = max(close - 0.25 * atr, entry_floor, close * 0.001)
    entry_high = close + 0.25 * atr
    stop = max(close * 0.001, min(close * (1 - config.initial_stop_pct),
                                close - config.atr_stop_multiple * atr, structural_stop))
    risk_fraction = (entry_high - stop) / entry_high
    weight = min(config.suggested_weight_cap, config.risk_per_trade_fraction / risk_fraction)
    watch = list(check.watch_reasons)
    if not regime["allow_entries"]:
        watch.append("市场过滤缺失/弱势，信号仅观察，不允许模拟开仓")
    next_day = _next_session(dataset, latest.day)
    if next_day is None:
        watch.append("下一交易日未被交易日历确认，不允许模拟开仓")
    if timeframe == "daily":
        valid_until = cn_time(next_day, 15) if next_day else as_of
    else:
        segment_end = cn_time(as_of.date(), 11, 30) if as_of.time().replace(tzinfo=None) < time(11, 30) else cn_time(as_of.date(), 15)
        valid_until = min(latest.available_at.astimezone(CN_TZ) + timedelta(minutes=config.intraday_signal_valid_minutes), segment_end)
    if valid_until <= as_of:
        watch.append("建议有效期已到；不得回填历史时点模拟成交")
    if risk_fraction > config.max_stop_pct:
        watch.append(f"建议区间上缘至止损距离{risk_fraction:.2%}超过研究上限{config.max_stop_pct:.2%}")
    if latest.has_price_limit and latest.limit_up is not None and close >= latest.limit_up - 1e-8:
        watch.append("最新价已触及涨停价，不把排队不确定性当作可立即买入")
    if latest.has_price_limit and latest.limit_down is not None and close <= latest.limit_down + 1e-8:
        watch.append("最新价已触及跌停价，拒绝生成新买入候选")
    warnings = ["研究评分仅用于排序，不是上涨概率或收益承诺。",
                "涨跌停、停牌、排队和滑点可能使入场或止损价格无法成交。",
                "本信号使用价格和成交量，不代表真实主力、北向资金或主动买卖方向。",
                "历史成交量使用原始股/份数，公司行动可能使量比失真。"]
    if check.instrument.t_plus_one:
        warnings.append("本品种按T+1元数据处理：当日新买仓位不能依靠盘中卖出来保证止损。")
    else:
        warnings.append("本品种按提供的T+0元数据处理；交易制度及交易单位须与行情来源核对。")
    if config.reference_capital_cny is None:
        warnings.append("参考资金尚未确认，仅给出比例；模拟账本资金是研究配置值。")
    action = "WATCH" if watch else "BUY_CANDIDATE"
    metrics = {**metrics, "atr": atr, "stop_distance_pct": risk_fraction * 100,
               "mean_daily_amount_20_cny": check.amount20, "score_is_probability": False,
               "price_basis": "raw_current_cny", "history_price_basis": "historical_factor_over_latest_visible_factor",
               "volume_basis": "raw_shares_or_units", "market_status": regime["status"],
               "expected_daily_session": check.expected_day.isoformat() if check.expected_day else None,
               "daily_assumed_available_time": "18:00 Asia/Shanghai",
               "unblocked_suggested_weight": weight,
               "suggested_amount_cny": (weight * config.reference_capital_cny
                                         if not watch and config.reference_capital_cny is not None else None)}
    return Advice(signal_id=signal_key(latest.symbol, strategy, latest.timestamp),
                  symbol=latest.symbol, name=check.instrument.name, strategy=strategy,
                  timeframe=timeframe, as_of=as_of, data_time=latest.timestamp, action=action,
                  score=_score(score), entry_low=round(entry_low, 4), entry_high=round(entry_high, 4),
                  stop_price=round(stop, 4), target_price=round(entry_high + config.target_r_multiple * (entry_high - stop), 4),
                  suggested_weight=weight if not watch else 0.0, valid_until=valid_until,
                  reasons=tuple(reasons + watch), warnings=tuple(warnings), metrics=metrics,
                  allow_paper_entry=not watch, sector=check.instrument.sector)


def _best_per_symbol(advices: list[Advice], limit: int) -> list[Advice]:
    result: dict[str, Advice] = {}
    for advice in sorted(advices, key=lambda item: (-item.score, item.symbol, item.strategy)):
        result.setdefault(advice.symbol, advice)
    return list(result.values())[:limit]


def evaluate_daily(dataset: DataSet, config: QuantConfig, as_of: datetime) -> list[Advice]:
    as_of = _now(as_of)
    regime = market_regime(dataset, config, as_of)
    result: list[Advice] = []
    for _, instrument in sorted(dataset.instruments.items()):
        check = _inspect(dataset, instrument, config, as_of, "daily")
        if check.reasons or check.latest is None:
            LOGGER.debug("%s 日线拒绝：%s", instrument.symbol, "; ".join(message for _, message in check.reasons))
            continue
        prices = check.prices
        trend = _trend(prices)
        atr = _atr(prices, config.atr_period)
        if not trend["uptrend"] or atr <= 0:
            continue
        current, previous = prices[-1], prices[-2]
        baseline_volume = _mean([item.bar.volume for item in prices[-21:-1]])
        if baseline_volume <= 0:
            continue
        volume_ratio = current.bar.volume / baseline_volume
        prior_high = max(item.high for item in prices[-config.daily_breakout_days - 1:-1])
        metrics = {**trend, "volume_ratio": volume_ratio, "volume_lookback": 20,
                   "breakout_window": config.daily_breakout_days, "prior_high": prior_high}
        trend_strength = max(0.0, (float(trend["sma20"]) / float(trend["sma60"]) - 1) * 100)
        if current.close > prior_high and volume_ratio >= config.daily_volume_ratio:
            score = 55 + min(20, (volume_ratio - 1) * 10) + min(10, trend_strength) + min(10, (current.close - prior_high) / atr * 5)
            result.append(_make_advice(dataset, config, check, regime, as_of, strategy="DAILY_VOLUME_BREAKOUT",
                          timeframe="daily", atr=atr, structural_stop=prior_high - 0.5 * atr,
                          score=score, entry_floor=prior_high,
                          reasons=[f"收盘突破此前{config.daily_breakout_days}根日线最高价",
                                   f"当日成交量为此前20日均量的{volume_ratio:.2f}倍",
                                   "SMA20高于SMA60且SMA20抬升"], metrics=metrics))
        previous_sma20 = _mean([item.close for item in prices[-21:-1]])
        prior_volume_baseline = _mean([item.bar.volume for item in prices[-22:-2]])
        pullback_ratio = previous.bar.volume / prior_volume_baseline if prior_volume_baseline > 0 else math.inf
        touched = previous.low <= previous_sma20 + 0.5 * atr and previous.close >= previous_sma20 - 0.5 * atr
        confirmed = (current.close > previous.high and current.close > current.open
                     and current.bar.volume >= previous.bar.volume * 1.1)
        if touched and pullback_ratio <= 0.85 and confirmed:
            score = 55 + min(15, (1 - pullback_ratio) * 25) + min(15, (current.close - previous.high) / atr * 10) + min(10, trend_strength)
            result.append(_make_advice(dataset, config, check, regime, as_of, strategy="DAILY_PULLBACK_CONFIRM",
                          timeframe="daily", atr=atr, structural_stop=min(previous.low, current.low) - 0.25 * atr,
                          score=score, reasons=["上涨趋势中前一日缩量回踩SMA20附近",
                          f"回踩日量比{pullback_ratio:.2f}，当前收盘重新突破回踩日高点",
                          "确认日收阳且成交量较回踩日增加"], metrics={**metrics,
                          "pullback_volume_ratio": pullback_ratio, "previous_sma20": previous_sma20}))
    return _best_per_symbol(result, config.top_candidates)


def _complete_day_prefix(bars: list[Bar], latest: Bar) -> bool:
    expected: list[datetime] = []
    for begin, end in ((cn_time(latest.day, 9, 35), cn_time(latest.day, 11, 30)),
                       (cn_time(latest.day, 13, 5), cn_time(latest.day, 15))):
        cursor = begin
        while cursor <= min(end, latest.timestamp):
            expected.append(cursor)
            cursor += timedelta(minutes=5)
    return [bar.timestamp for bar in bars] == expected


def evaluate_intraday(dataset: DataSet, config: QuantConfig, as_of: datetime) -> list[Advice]:
    as_of = _now(as_of)
    # 午休/收盘不新造建议；保留的既有建议由watch主循环管理失效/冷却。
    if not _active_session(as_of):
        return []
    regime = market_regime(dataset, config, as_of, "5m")
    result: list[Advice] = []
    for _, instrument in sorted(dataset.instruments.items()):
        check = _inspect(dataset, instrument, config, as_of, "5m")
        if check.reasons or check.latest is None:
            LOGGER.debug("%s 分钟拒绝：%s", instrument.symbol, "; ".join(message for _, message in check.reasons))
            continue
        current_bars = [bar for bar in check.minutes if bar.day == as_of.date()]
        if len(current_bars) < 2 or any(not _valid_factor(bar.adj_factor) for bar in current_bars):
            continue
        current, previous = current_bars[-1], current_bars[-2]
        lookback = config.intraday_volume_lookback
        preceding = [bar for bar in check.minutes if bar.timestamp < current.timestamp][-lookback:]
        if len(preceding) < lookback:
            continue
        baseline = _mean([bar.volume for bar in preceding])
        if baseline <= 0:
            continue
        ratio = current.volume / baseline
        trend = _trend(check.prices)
        atr = _atr(check.prices, config.atr_period)
        if not trend["uptrend"] or current.close < float(trend["sma20"]) or atr <= 0 or ratio < config.intraday_volume_ratio:
            continue
        metrics = {**trend, "volume_ratio": ratio, "volume_lookback": lookback,
                   "volume_comparison": "当前完整5m/此前完整5m均量；可跨上一交易日，并非同时间段季节性比较",
                   "previous_daily_time": check.daily[-1].timestamp.isoformat(),
                   "current_day_bar_count": len(current_bars)}
        score_base = 55 + min(20, (ratio - 1) * 8) + min(10, max(0.0, (float(trend["sma20"]) / float(trend["sma60"]) - 1) * 100))
        opening = [bar for bar in current_bars if cn_time(as_of.date(), 9, 35) <= bar.timestamp <= cn_time(as_of.date(), 10)]
        opening_times = [cn_time(as_of.date(), 9, 35) + timedelta(minutes=5 * index) for index in range(6)]
        if len(opening) == 6 and [bar.timestamp for bar in opening] == opening_times and current.timestamp > cn_time(as_of.date(), 10):
            high, low = max(bar.high for bar in opening), min(bar.low for bar in opening)
            if previous.close <= high < current.close:
                result.append(_make_advice(dataset, config, check, regime, as_of,
                              strategy="INTRADAY_OPENING_RANGE_BREAKOUT", timeframe="5m", atr=atr,
                              structural_stop=low - 0.25 * atr, entry_floor=high,
                              score=score_base + min(10, (current.close - high) / atr * 10),
                              reasons=["完整09:35–10:00六根5分钟线定义开盘30分钟区间",
                                       "最新完整5分钟收盘从区间内越过区间高点",
                                       f"该根成交量为此前{lookback}根完整5分钟均量的{ratio:.2f}倍"],
                              metrics={**metrics, "opening_range_high": high, "opening_range_low": low}))
        # VWAP须有从开盘起的完整当日成交额/股数；不能用缺口数据冒充全天VWAP。
        if not _complete_day_prefix(current_bars, current):
            continue
        total_volume = math.fsum(bar.volume for bar in current_bars)
        prior_volume = math.fsum(bar.volume for bar in current_bars[:-1])
        if total_volume <= 0 or prior_volume <= 0:
            continue
        vwap = math.fsum(bar.amount for bar in current_bars) / total_volume
        previous_vwap = math.fsum(bar.amount for bar in current_bars[:-1]) / prior_volume
        retested = current.low <= previous_vwap and current.close > vwap
        recovered = (previous.close <= previous_vwap or current.low <= previous_vwap) and current.close > current.open and current.close >= previous.close
        if retested and recovered:
            result.append(_make_advice(dataset, config, check, regime, as_of,
                          strategy="INTRADAY_VWAP_RECLAIM", timeframe="5m", atr=atr,
                          structural_stop=min(current.low, previous.low, previous_vwap) - 0.25 * atr,
                          score=score_base + min(10, (current.close - vwap) / atr * 10),
                          reasons=["当前交易日完整价量数据计算VWAP（成交额/股数）",
                                   "最新完整5分钟线回踩此前VWAP后重新收于当日VWAP上方",
                                   f"价量确认：此前{lookback}根均量比{ratio:.2f}，前一日趋势通过"],
                          metrics={**metrics, "vwap": vwap, "previous_vwap": previous_vwap}))
    return _best_per_symbol(result, config.top_candidates)
