"""确定性合成数据用于展示操作；没有任何真实历史行情或真实业绩。"""
from __future__ import annotations

from datetime import date, timedelta
import math

from models import Bar, DataSet, Instrument, cn_time


def make_demo_dataset() -> DataSet:
    sessions = []
    cursor = date(2025, 1, 6)
    while len(sessions) < 146:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor += timedelta(days=1)
    instruments = {
        "600000.SH": Instrument("600000.SH", "合成主板甲（非真实行情）", "STOCK", "MAIN", "演示行业A", date(2000, 1, 1)),
        "000001.SZ": Instrument("000001.SZ", "合成主板乙（非真实行情）", "STOCK", "MAIN", "演示行业B", date(2000, 1, 1)),
        "510300.SH": Instrument("510300.SH", "合成股票ETF（非真实行情）", "ETF", "ETF", "演示宽基", date(2005, 1, 1)),
        "000300.SH": Instrument("000300.SH", "合成基准（非真实行情）", "INDEX", "INDEX", "指数", date(2005, 1, 1)),
    }
    daily: dict[str, list[Bar]] = {}
    for k, (symbol, instrument) in enumerate(instruments.items()):
        values = []
        previous = 3000.0 if instrument.asset_type == "INDEX" else 8.0 + k * 6
        factor = 1.0
        for i, day in enumerate(sessions[:140]):
            trend = 0.0019 if k == 3 else 0.0025
            wave = 0.003 * math.sin(i * 0.4 + k)
            ret = trend + wave
            if k != 3 and i % 26 == 17:
                ret = 0.035
            elif k != 3 and i % 26 in {19, 20, 21}:
                ret = -0.012
            # 倒数第二交易日造一个明确的放量趋势样例，便于展示日线提示。
            if k == 0 and i == 138:
                ret = 0.045
            opening = round(previous * (1 + 0.001 * math.sin(i)), 2)
            close = round(previous * (1 + ret), 2)
            high = round(max(opening, close) * 1.004, 2)
            low = round(min(opening, close) * 0.996, 2)
            volume = 12_000_000 * (2.8 if ret > 0.025 else 0.85 + 0.15 * (1 + math.sin(i * 0.8)))
            amount = volume * (opening + close) / 2
            is_index = instrument.asset_type == "INDEX"
            values.append(Bar(symbol, cn_time(day, 15), cn_time(day, 18), "daily", opening, high, low, close,
                              volume, amount, None if is_index else factor, previous,
                              None if is_index else round(previous * 1.10, 2), None if is_index else round(previous * .90, 2),
                              False, None if is_index else False, not is_index, True))
            previous = close
        daily[symbol] = values
    intraday: dict[str, list[Bar]] = {}
    for k, (symbol, instrument) in enumerate(instruments.items()):
        if instrument.asset_type == "INDEX":
            continue
        values = []
        for index in range(137, 140):
            day = sessions[index]
            prior = daily[symbol][index - 1].close
            previous = prior
            day_values = []
            ends = [cn_time(day, 9, 30) + timedelta(minutes=5 * i) for i in range(1, 25)]
            ends += [cn_time(day, 13) + timedelta(minutes=5 * i) for i in range(1, 25)]
            for step, end in enumerate(ends):
                opening = previous
                ret = 0.0004 + 0.0005 * math.sin(step * .9 + k)
                if index == 139 and step == 12:
                    ret = 0.012 + k * .003
                if index == 138 and k == 0 and step == 12:
                    ret = 0.045
                if index == 138 and 24 <= step <= 27:
                    ret = -0.008
                close = round(opening * (1 + ret), 2)
                high = round(max(opening, close) * 1.001, 2)
                low = round(min(opening, close) * .999, 2)
                volume = (650_000 if index == 138 and k == 0 else 350_000) * (3.0 if step == 12 else 1.0 + .15 * math.sin(step))
                bar = Bar(symbol, end, end + timedelta(seconds=1), "5m", opening, high, low, close,
                                  volume, volume * (opening + close) / 2, 1.0, prior, round(prior * 1.10, 2),
                                  round(prior * .90, 2), False, False, True, True)
                values.append(bar)
                day_values.append(bar)
                previous = close
            # 同一交易日的演示日线由分钟数据聚合，避免两种时间周期互相矛盾。
            daily[symbol][index] = Bar(symbol, cn_time(day, 15), cn_time(day, 18), "daily",
                                      day_values[0].open, max(bar.high for bar in day_values), min(bar.low for bar in day_values),
                                      day_values[-1].close, sum(bar.volume for bar in day_values), sum(bar.amount for bar in day_values),
                                      1.0, prior, round(prior * 1.10, 2), round(prior * .90, 2), False, False, True, True)
        intraday[symbol] = values
    session_set = set(sessions)
    calendar_rows = [{"date": (sessions[0] + timedelta(days=offset)).isoformat(),
                      "is_open": int(sessions[0] + timedelta(days=offset) in session_set)}
                     for offset in range((sessions[-1] - sessions[0]).days + 1)]
    return DataSet(instruments, daily, intraday, tuple(sessions), "人工合成演示 · 不是实时或真实历史行情", {
        "synthetic": True, "calendar_start": sessions[0].isoformat(), "calendar_end": sessions[-1].isoformat(), "calendar_rows": calendar_rows,
        "calendar_source": "合成工作日历；没有模拟法定节假日", "point_in_time_metadata": True,
        "survivorship_bias": "演示固定标的池，不能用于评价全市场表现", "price_basis": "raw",
        "daily_availability_assumption": "演示日线18:00可得", "data_purpose": "操作示例，不是业绩验证",
    })
