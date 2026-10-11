"""严格 CSV 数据契约；仅标准库，未知交易状态保持未知。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
import csv
import hashlib
import io
import json
import logging
import math
import os
import tempfile

from models import Bar, CN_TZ, DataSet, Instrument, validate_symbol

LOGGER = logging.getLogger(__name__)
INSTRUMENT_FIELDS = tuple(item.name for item in fields(Instrument))
BAR_FIELDS = tuple(item.name for item in fields(Bar))
CSV_FILES = ("instruments.csv", "daily.csv", "intraday.csv", "calendar.csv")
_MAX_FILE_BYTES = 128 * 1024 * 1024
_NULL = {"", "null", "none", "unknown"}


class DataValidationError(ValueError):
    """缺字段、冲突数据或未知日历覆盖，不能据此生成可执行建议。"""


def _day(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        raise DataValidationError(f"{label}必须是日期，不是时间")
    if isinstance(value, date):
        return value
    try:
        text = str(value).strip()
        if len(text) != 10:
            raise ValueError
        return date.fromisoformat(text)
    except (ValueError, TypeError):
        raise DataValidationError(f"{label}须为YYYY-MM-DD") from None


def _stamp(value: str, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(CN_TZ)
    except (ValueError, TypeError, AttributeError):
        raise DataValidationError(f"{label}必须是带时区的ISO时间，例如2026-10-12T09:35:00+08:00") from None


def _flag(value: Any, label: str, *, nullable: bool = False) -> bool | None:
    text = str(value).strip().lower()
    if nullable and (value is None or text in _NULL):
        return None
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    raise DataValidationError(f"{label}仅接受0/1或true/false；未知可空的字段请留空")


def _number(value: Any, label: str, *, nullable: bool = False) -> float | None:
    if nullable and (value is None or str(value).strip().lower() in _NULL):
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError):
        raise DataValidationError(f"{label}必须为有限数值") from None


def _calendar(rows: list[dict[str, Any]], metadata: dict[str, Any]) -> tuple[tuple[date, ...], dict[str, Any]]:
    states: dict[date, int] = {}
    for row in rows:
        day = _day(row.get("date"), "calendar.date")
        raw = row.get("is_open")
        if str(raw).strip() not in {"0", "1"}:
            raise DataValidationError("calendar.is_open须逐自然日明确写0或1，不能空缺或猜周末/节假日")
        state = int(raw)
        if day in states and states[day] != state:
            raise DataValidationError(f"日历{day}存在冲突的开闭市状态")
        states[day] = state
    if not states:
        raise DataValidationError("日历为空，无法确认交易日或休市日")
    first = _day(metadata.get("calendar_start", min(states)), "metadata.calendar_start")
    last = _day(metadata.get("calendar_end", max(states)), "metadata.calendar_end")
    if first > last or min(states) != first or max(states) != last:
        raise DataValidationError("日历实际起止日期与metadata覆盖区间不一致")
    if len(states) != (last - first).days + 1:
        cursor = first
        while cursor <= last:
            if cursor not in states:
                raise DataValidationError(f"日历覆盖不完整：缺少{cursor}的明确0/1状态；请补全日历，系统不会猜节假日")
            cursor += timedelta(days=1)
    normalized_rows = [{"date": day.isoformat(), "is_open": states[day]} for day in sorted(states)]
    if "calendar_rows" in metadata:
        supplied = metadata["calendar_rows"]
        if not isinstance(supplied, list):
            raise DataValidationError("metadata.calendar_rows必须为完整日历行列表")
        shadow = dict(metadata)
        shadow.pop("calendar_rows")
        _, comparison = _calendar(supplied, shadow)
        if comparison["calendar_rows"] != normalized_rows:
            raise DataValidationError("calendar.csv与metadata.calendar_rows不一致")
    result = dict(metadata)
    result.update(calendar_start=first.isoformat(), calendar_end=last.isoformat(), calendar_rows=normalized_rows)
    return tuple(day for day in sorted(states) if states[day] == 1), result


def validate_dataset(dataset: DataSet) -> DataSet:
    """返回排序、去除完全重复行后的新数据集；冲突重复与未证实日历拒绝。"""
    if not isinstance(dataset, DataSet) or not isinstance(dataset.metadata, dict):
        raise DataValidationError("必须提供DataSet和JSON对象metadata")
    rows = dataset.metadata.get("calendar_rows")
    if not isinstance(rows, list):
        raise DataValidationError("metadata必须提供calendar_rows全自然日0/1日历，不能从开放日列表反推休市日")
    calendar, metadata = _calendar(rows, dataset.metadata)
    if tuple(dataset.calendar) != calendar:
        raise DataValidationError("DataSet.calendar须与完整日历中的开放日按日期排序一致")
    open_days = set(calendar)
    instruments: dict[str, Instrument] = {}
    if not dataset.instruments:
        raise DataValidationError("instruments.csv没有证券资料")
    for key, instrument in dataset.instruments.items():
        if not isinstance(instrument, Instrument) or key != validate_symbol(key) or instrument.symbol != key:
            raise DataValidationError("证券字典键、symbol必须是相同的标准交易所代码")
        valid_boards = {"STOCK": {"MAIN", "CHINEXT", "STAR", "BSE"}, "ETF": {"ETF"}, "INDEX": {"INDEX"}}
        if instrument.board not in valid_boards[instrument.asset_type] or not instrument.name.strip():
            raise DataValidationError(f"{key}证券名称/类型/板块不一致")
        if not isinstance(instrument.t_plus_one, bool) or isinstance(instrument.lot_size, bool):
            raise DataValidationError(f"{key}交易单位/T+1状态必须明确")
        _day(instrument.list_date, "list_date")
        if instrument.delist_date is not None and _day(instrument.delist_date, "delist_date") < instrument.list_date:
            raise DataValidationError(f"{key}退市日期早于上市日期")
        instruments[key] = instrument
    groups: dict[str, dict[str, list[Bar]]] = {}
    for timeframe, table in (("daily", dataset.daily), ("5m", dataset.intraday)):
        normalized: dict[str, list[Bar]] = {symbol: [] for symbol in instruments}
        for symbol, bars in table.items():
            if symbol not in instruments:
                raise DataValidationError(f"{symbol}K线缺少证券元数据")
            unique: dict[datetime, Bar] = {}
            for bar in bars:
                if not isinstance(bar, Bar) or bar.symbol != symbol or bar.timeframe != timeframe:
                    raise DataValidationError(f"{symbol}K线字段与所在文件/字典不一致")
                if not isinstance(bar.is_complete, bool) or not isinstance(bar.suspended, bool):
                    raise DataValidationError(f"{symbol}K线完整性/停牌状态须为明确布尔值")
                if any(value is not None and not isinstance(value, bool) for value in (bar.is_st, bar.has_price_limit)):
                    raise DataValidationError(f"{symbol}ST/涨跌停状态只能为布尔值或未知None")
                local = bar.timestamp.astimezone(CN_TZ)
                if local.second or local.microsecond:
                    raise DataValidationError(f"{symbol}K线结束时刻必须对齐整分钟")
                minute = local.hour * 60 + local.minute
                if timeframe == "daily" and minute != 15 * 60:
                    raise DataValidationError(f"{symbol}日线timestamp须为15:00的K线结束时刻")
                if timeframe == "5m" and (minute % 5 or not (575 <= minute <= 690 or 785 <= minute <= 900)):
                    raise DataValidationError(f"{symbol}5分钟K线结束时刻应在09:35–11:30或13:05–15:00，禁止跨午休")
                instrument = instruments[symbol]
                if bar.day not in open_days:
                    raise DataValidationError(f"{symbol}在{bar.day}的K线不在明确开放日历内；先补充calendar.csv覆盖")
                if bar.day < instrument.list_date or (instrument.delist_date and bar.day > instrument.delist_date):
                    raise DataValidationError(f"{symbol}K线日期在上市/退市范围之外")
                if bar.timestamp in unique and unique[bar.timestamp] != bar:
                    raise DataValidationError(f"{symbol}在{bar.timestamp.isoformat()}存在冲突的重复K线")
                unique[bar.timestamp] = bar
            normalized[symbol] = sorted(unique.values(), key=lambda bar: bar.timestamp)
        groups[timeframe] = normalized
    metadata.setdefault("synthetic", None)
    metadata.setdefault("source", dataset.source_label)
    metadata.setdefault("survivorship_bias", "用户CSV未声明历史证券池覆盖，可能存在幸存者偏差")
    metadata.setdefault("point_in_time", False)
    metadata.setdefault("availability_caveat", "available_at由数据提供方声明；未提供可核查PIT入库日志")
    metadata.update(volume_unit="shares_or_etf_units", amount_unit="CNY", timestamp_semantics="bar_end")
    return DataSet(instruments, groups["daily"], groups["5m"], calendar, dataset.source_label, metadata)


def _read_bytes(path: Path) -> bytes:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise DataValidationError(f"{path.name}超过128MiB，请拆分数据集")
        return path.read_bytes()
    except OSError:
        raise DataValidationError(f"无法读取必需数据文件{path.name}") from None


def _rows(raw: bytes, name: str, required: set[str], allowed: set[str]) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        headers = reader.fieldnames
        if not headers or len(set(headers)) != len(headers) or required - set(headers) or set(headers) - allowed:
            missing = sorted(required - set(headers or ()))
            raise DataValidationError(f"{name}列不符合契约，缺失={','.join(missing)}，请使用明确字段名")
        result = []
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise DataValidationError(f"{name}:{line}列数不完整或多出列")
            if any(value.strip() for value in row.values()):
                result.append(row)
        return result
    except (UnicodeError, csv.Error):
        raise DataValidationError(f"{name}须为合法UTF-8 CSV") from None


def load_dataset(directory: Path) -> DataSet:
    directory = Path(directory)
    metadata_bytes = _read_bytes(directory / "metadata.json")
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8-sig"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(metadata, dict):
            raise ValueError
    except (ValueError, UnicodeError):
        raise DataValidationError("metadata.json须为有限数值的UTF-8 JSON对象") from None
    payloads = {name: _read_bytes(directory / name) for name in CSV_FILES}
    manifest = metadata.get("file_sha256")
    if manifest is not None:
        if not isinstance(manifest, dict) or set(manifest) != set(CSV_FILES):
            raise DataValidationError("metadata文件校验清单缺失或多出文件")
        for name, raw in payloads.items():
            if hashlib.sha256(raw).hexdigest() != manifest[name]:
                raise DataValidationError(f"{name}与metadata校验值不符：可能保存中断、并发改写或人工修改；拒绝混合批次")
    if metadata_bytes != _read_bytes(directory / "metadata.json"):
        raise DataValidationError("读取期间数据批次发生变化，请重新读取")
    instrument_rows = _rows(payloads["instruments.csv"], "instruments.csv", set(INSTRUMENT_FIELDS) - {"delist_date"}, set(INSTRUMENT_FIELDS))
    instruments: dict[str, Instrument] = {}
    for row in instrument_rows:
        symbol = validate_symbol(row["symbol"])
        lot = _number(row["lot_size"], "lot_size")
        if lot <= 0 or lot != int(lot):
            raise DataValidationError(f"{symbol}lot_size必须是正整数")
        instrument = Instrument(symbol, row["name"].strip(), row["asset_type"].strip().upper(), row["board"].strip().upper(),
                                row["sector"].strip() or "UNKNOWN", _day(row["list_date"], "list_date"), int(lot),
                                _flag(row["t_plus_one"], "t_plus_one"), _day(row["delist_date"], "delist_date") if row.get("delist_date", "").strip() else None)
        if symbol in instruments and instruments[symbol] != instrument:
            raise DataValidationError(f"{symbol}存在冲突证券资料")
        instruments[symbol] = instrument
    optional = {"adj_factor", "prev_close", "limit_up", "limit_down", "is_st", "has_price_limit"}
    tables: list[dict[str, list[Bar]]] = []
    for name, timeframe in (("daily.csv", "daily"), ("intraday.csv", "5m")):
        table: dict[str, list[Bar]] = {}
        for row in _rows(payloads[name], name, set(BAR_FIELDS) - optional, set(BAR_FIELDS)):
            symbol = validate_symbol(row["symbol"])
            if row["timeframe"].strip() != timeframe:
                raise DataValidationError(f"{name}包含不符的timeframe")
            values: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe,
                                      "timestamp": _stamp(row["timestamp"], "timestamp"), "available_at": _stamp(row["available_at"], "available_at")}
            for field_name in ("open", "high", "low", "close", "volume", "amount", "adj_factor", "prev_close", "limit_up", "limit_down"):
                values[field_name] = _number(row.get(field_name), field_name, nullable=field_name in optional)
            for field_name in ("suspended", "is_st", "has_price_limit", "is_complete"):
                values[field_name] = _flag(row.get(field_name), field_name, nullable=field_name in optional)
            table.setdefault(symbol, []).append(Bar(**values))
        tables.append(table)
    calendar_rows = _rows(payloads["calendar.csv"], "calendar.csv", {"date", "is_open"}, {"date", "is_open"})
    calendar, metadata = _calendar(calendar_rows, metadata)
    result = validate_dataset(DataSet(instruments, tables[0], tables[1], calendar, str(metadata.get("source_label", metadata.get("source", "用户CSV"))), metadata))
    LOGGER.info("已载入%s：%d只证券、%d条日线、%d条5分钟线，日历覆盖%s至%s；未知状态不自动补齐", directory.name, len(result.instruments), sum(map(len, result.daily.values())), sum(map(len, result.intraday.values())), metadata["calendar_start"], metadata["calendar_end"])
    return result


@contextmanager
def _write_lease(directory: Path) -> Iterator[None]:
    with (directory / ".dataset.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DataValidationError("同一目录已有数据写入进程，请等待保存完成") from None
        yield


def _csv_bytes(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: ("" if row.get(key) is None else int(row[key]) if isinstance(row[key], bool) else row[key].isoformat() if isinstance(row[key], (date, datetime)) else row[key]) for key in columns})
    return target.getvalue().encode("utf-8")


def save_dataset(dataset: DataSet, directory: Path) -> None:
    normalized = validate_dataset(dataset)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        "instruments.csv": _csv_bytes(INSTRUMENT_FIELDS, [asdict(item) for _, item in sorted(normalized.instruments.items())]),
        "daily.csv": _csv_bytes(BAR_FIELDS, [asdict(bar) for symbol in sorted(normalized.daily) for bar in normalized.daily[symbol]]),
        "intraday.csv": _csv_bytes(BAR_FIELDS, [asdict(bar) for symbol in sorted(normalized.intraday) for bar in normalized.intraday[symbol]]),
        "calendar.csv": _csv_bytes(("date", "is_open"), normalized.metadata["calendar_rows"]),
    }
    metadata = dict(normalized.metadata)
    metadata.update(source_label=normalized.source_label, schema_version=1, file_sha256={name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()})
    try:
        payloads["metadata.json"] = (json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        raise DataValidationError("metadata必须是可序列化JSON，不能含NaN/无穷值") from None
    staged: dict[str, Path] = {}
    with _write_lease(directory):
        try:
            for name, raw in payloads.items():
                if len(raw) > _MAX_FILE_BYTES:
                    raise DataValidationError(f"{name}超过128MiB，请拆分数据集")
                with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=".dataset-", suffix=".tmp", delete=False) as stream:
                    staged[name] = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            # metadata最后提交；中断时旧manifest会拒绝任何混合CSV批次。
            for name in (*CSV_FILES, "metadata.json"):
                os.replace(staged[name], directory / name)
                staged.pop(name)
        except OSError:
            raise DataValidationError("数据集原子保存失败；保留旧清单或拒绝不完整批次，请检查磁盘/权限") from None
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
    LOGGER.info("已原子保存CSV与metadata：%s；日历和数据批次具有SHA256校验", directory.name)
