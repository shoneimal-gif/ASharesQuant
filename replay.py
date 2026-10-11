"""按数据可得时刻推进本地模拟，日线和5m共用一份现金与持仓。"""
from __future__ import annotations

from datetime import datetime, timedelta
from itertools import groupby
from typing import Any

from config import QuantConfig
from models import Bar, DataSet, cn_time, jsonable, parse_time
from paper_trader import ASharePaperTrader


def run_replay(dataset: DataSet, config: QuantConfig, mode: str = "daily") -> dict[str, Any]:
    if mode not in {"daily", "intraday", "combined"}:
        raise ValueError("mode须为daily、intraday或combined")
    from detector import evaluate_daily, evaluate_intraday

    trader = ASharePaperTrader(config, dataset.instruments, dataset.calendar)
    all_daily = [bar for rows in dataset.daily.values() for bar in rows if bar.is_complete]
    all_minute = [bar for rows in dataset.intraday.values() for bar in rows if bar.is_complete]
    minute_days = {(bar.symbol, bar.day) for bar in all_minute}
    execution_keys: set[tuple[str, str, datetime]] = set()
    fallback_keys: set[tuple[str, str, datetime]] = set()
    event_queue: list[tuple[datetime, str, Bar | Any]] = []

    for bar in all_daily:
        event_queue.append((bar.available_at, "VISIBLE_DAILY", bar))
        if mode in {"daily", "combined"}:
            event_queue.append((max(bar.available_at, cn_time(bar.day, 18)), "DAILY_ANALYSIS", bar))
        instrument = dataset.instruments.get(bar.symbol)
        if instrument is None or instrument.asset_type not in {"STOCK", "ETF"}:
            continue
        if mode == "daily":
            execution_keys.add(trader.key(bar))
            event_queue.append((bar.open_time, "OPEN", bar))
        elif (bar.symbol, bar.day) not in minute_days:
            # 只处理已有仓位，不在缺分钟数据的日期补造新的盘中买入。
            fallback_keys.add(trader.key(bar))
            event_queue.append((bar.open_time, "FALLBACK_OPEN", bar))
    if mode != "daily":
        for bar in all_minute:
            event_queue.append((bar.available_at, "VISIBLE_INTRADAY", bar))
            instrument = dataset.instruments.get(bar.symbol)
            if instrument and instrument.asset_type in {"STOCK", "ETF"}:
                execution_keys.add(trader.key(bar))
                event_queue.append((bar.open_time, "OPEN", bar))

    end_time = max((item[0] for item in event_queue), default=None)
    arrivals_by_day: dict[Any, list[datetime]] = {}
    for bar in all_daily + (all_minute if mode != "daily" else []):
        arrivals_by_day.setdefault(bar.day, []).append(bar.available_at)
    for day in dataset.calendar:
        # 有晚到数据时，在其可得之后再做收盘完整性检查；不会把数据提前到15:00。
        arrivals = arrivals_by_day.get(day, [])
        check_time = max([cn_time(day, 15, 5), *arrivals])
        if end_time is not None and check_time <= end_time:
            event_queue.append((check_time, "SESSION_CHECK", day))

    event_queue.sort(key=lambda item: (item[0], item[1], getattr(item[2], "symbol", "")))
    if event_queue:
        trader.record_equity(event_queue[0][0], "INITIAL_CASH")
    daily_evaluations = intraday_evaluations = total_advice = entry_advice = 0
    fallback_logged: set[tuple[str, Any]] = set()
    for now, iterator in groupby(event_queue, key=lambda item: item[0]):
        group = list(iterator)
        visible = [item for item in group if item[1].startswith("VISIBLE_")]
        # 同时可见的跨标的先全部更新，再生成一次该时刻的建议。
        for _, kind, bar in sorted(visible, key=lambda item: (item[2].timestamp, item[2].symbol, item[1])):
            key = trader.key(bar)
            if key in execution_keys:
                trader.on_bar(bar)
            elif key in fallback_keys and bar.symbol in trader.positions:
                identity = (bar.symbol, bar.day)
                if identity not in fallback_logged:
                    fallback_logged.add(identity)
                    trader.incomplete_reasons.add("持仓缺5m，使用显式日线fallback，无法验证盘中路径")
                    trader.event(now, "DAILY_EXECUTION_FALLBACK_NO_MINUTE_DATA", bar.symbol, session=bar.day, limitation="仅处理已有仓位，日线OHLC无法恢复盘中路径")
                trader.on_bar(bar)
            else:
                trader.mark_only(bar)
        if mode in {"daily", "combined"} and any(kind == "DAILY_ANALYSIS" for _, kind, _ in group):
            advices = evaluate_daily(dataset, config, now)
            daily_evaluations += 1
            for advice in advices:
                if advice.as_of > now or advice.data_time > now:
                    raise ValueError("detector返回尚不可得的日线建议，拒绝回放")
                total_advice += 1
                entry_advice += int(advice.allow_paper_entry)
                trader.queue_advice(advice)
        if mode in {"intraday", "combined"} and any(kind == "VISIBLE_INTRADAY" for _, kind, _ in visible):
            advices = evaluate_intraday(dataset, config, now)
            intraday_evaluations += 1
            for advice in advices:
                if advice.as_of > now or advice.data_time > now:
                    raise ValueError("detector返回尚不可得的盘中建议，拒绝回放")
                total_advice += 1
                entry_advice += int(advice.allow_paper_entry)
                trader.queue_advice(advice)

        # 观察时刻与下一bar开盘相等仍不成交：on_open要求open_time严格>as_of。
        opens = [item for item in group if item[1] in {"OPEN", "FALLBACK_OPEN"}]
        def priority(item: tuple[datetime, str, Bar]) -> tuple[float, str]:
            advice = trader.advice_queue.get(item[2].symbol)
            return (-(advice.score if advice else float("-inf")), item[2].symbol)
        for _, kind, bar in sorted(opens, key=priority):
            trader.on_open(bar, allow_entry=kind == "OPEN")
        for _, kind, day in group:
            if kind == "SESSION_CHECK":
                for symbol in list(trader.positions):
                    trader.mark_missing(symbol, day, now)
        if visible or any(item[1] == "SESSION_CHECK" for item in group):
            trader.record_equity(now)

    if mode != "daily" and event_queue:
        # 任意一根分钟数据不代表完整交易日；只核对实际/预留持仓区间。
        minute_ends: dict[tuple[str, Any], set[datetime]] = {}
        for bar in all_minute:
            minute_ends.setdefault((bar.symbol, bar.day), set()).add(bar.timestamp)
        intervals = [(item["symbol"], parse_time(item["entry_time"]), parse_time(item["exit_bar_end"])) for item in trader.trades]
        final_time = event_queue[-1][0]
        intervals.extend((item.symbol, item.entry_time, final_time) for item in trader.positions.values())
        intervals.extend((item.advice.symbol, item.entry_time, final_time) for item in trader.entry_intents.values())
        required: dict[tuple[str, Any], set[datetime]] = {}
        for symbol, start, end in intervals:
            for day in dataset.calendar:
                if not start.date() <= day <= end.date():
                    continue
                for hour, minute, count in ((9, 35, 24), (13, 5, 24)):
                    segment_start = cn_time(day, hour, minute)
                    for offset in range(count):
                        stamp = segment_start + timedelta(minutes=5 * offset)
                        if start < stamp <= end:
                            required.setdefault((symbol, day), set()).add(stamp)
        for (symbol, day), expected in sorted(required.items()):
            missing = sorted(expected - minute_ends.get((symbol, day), set()))
            if missing:
                trader.incomplete_reasons.add("持仓/预留期间5m覆盖有缺口，盘中退出和峰值路径不完整")
                trader.event(final_time, "INTRADAY_EXPOSURE_COVERAGE_GAP", symbol, session=day, missing_bars=len(missing), missing_bar_ends=missing, handling="保留原账本，不补造盘中成交")
        trader.record_equity(final_time, "FINAL_OBSERVED_MARK_NO_FORCED_FLATTEN")
    summary = trader.summary()
    summary.update({
        "mode": mode, "source_label": dataset.source_label,
        "data_metadata": jsonable(dataset.metadata),
        "start_time": event_queue[0][0].isoformat() if event_queue else None,
        "end_time": event_queue[-1][0].isoformat() if event_queue else None,
        "daily_evaluations": daily_evaluations, "intraday_evaluations": intraday_evaluations,
        "advice_records_seen": total_advice, "entry_eligible_advice_records_seen": entry_advice,
        "unique_signals_seen": len(trader.seen_signals),
        "daily_fallback_sessions": len(fallback_logged),
        "execution_mode_rule": "daily使用日线；intraday/combined同标的同日有5m时不重复日线执行，无5m只为已有仓位显式日线fallback",
        "signal_timing_rule": "只在available_at观察；日线等后续可交易日开盘，5m等open_time严格大于as_of的下一可用bar",
        "volume_timing_rule": "开盘按已知预算预留，整根bar总量仅在可得后用作事后模拟成交约束，未成交预留当时才释放",
        "broker_connected": False, "orders_submitted": False,
    })
    if not event_queue:
        summary["valid_performance"] = False
        summary["performance_complete"] = False
        summary["incomplete_reasons"] = ["无完整可用bar，不能评估策略绩效"]
    return jsonable({"summary": summary, "trades": trader.trades, "equity_curve": trader.equity_curve, "events": trader.events})
