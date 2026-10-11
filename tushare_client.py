"""Tushare只读HTTPS适配：日线/ST/复权/涨跌停与显式分钟时间契约。

官方文档：
HTTP JSON协议 https://tushare.pro/document/1?doc_id=130
daily / stock_basic / trade_cal / adj_factor 的doc_id为27/25/26/28。
stk_limit https://tushare.pro/document/2?doc_id=183
stock_st https://tushare.pro/document/2?doc_id=397
rt_min https://tushare.pro/document/2?doc_id=374
index_basic/index_daily 的doc_id为94/95。
仅查询数据；没有交易、券商或自动下单接口。
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Any
from urllib import error, request
import hashlib
import json
import logging
import math
import os
import ssl
import threading
import time

from config import QuantConfig
from data_loader import validate_dataset
from models import Bar, CN_TZ, DataSet, Instrument, cn_time, validate_symbol

LOGGER = logging.getLogger(__name__)
_URL = "https://api.tushare.pro"
_MAX_BYTES = 16 * 1024 * 1024
_FIELDS = {
    "stock_basic": "ts_code,symbol,name,industry,market,exchange,list_status,list_date,delist_date",
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "stk_limit": "ts_code,trade_date,up_limit,down_limit",
    "stock_st": "ts_code,trade_date,type,type_name",
    "trade_cal": "exchange,cal_date,is_open",
    "rt_min": "ts_code,time,open,high,low,close,vol,amount",
    "index_basic": "ts_code,name,market,list_date,exp_date",
    "index_daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
}
# 已公开的硬行限；未公开行限的接口另外以单证券/短日期块和覆盖校验防截断。
_ROW_LIMITS = {"stock_basic": 6000, "daily": 6000, "stk_limit": 5800,
               "stock_st": 1000, "rt_min": 1000, "index_basic": 8000}


class TushareError(RuntimeError):
    """只输出静态错误原因/接口/数值状态，绝不包含token或远程错误正文。"""


class TusharePermissionError(TushareError):
    pass


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class _Gate:
    def __init__(self, rpm: int):
        self.rpm = rpm
        self.starts: deque[float] = deque()
        self.last_start = float("-inf")
        self.blocked_until = 0.0
        self.lock = threading.Lock()


_GATES: dict[str, _Gate] = {}
_GATES_LOCK = threading.Lock()


def _date(value: Any, label: str) -> date:
    try:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        text = str(value).strip()
        return datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 else date.fromisoformat(text)
    except (ValueError, TypeError):
        raise TushareError(f"{label}缺少有效日期") from None


def _num(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or (positive and result <= 0):
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError):
        raise TushareError(f"{label}缺少{'正的' if positive else ''}有限数值，不能补造数据") from None


def _chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    result = []
    current = start
    while current <= end:
        last = min(end, current + timedelta(days=days - 1))
        result.append((current, last))
        current = last + timedelta(days=1)
    return result


def _keyed(rows: list[dict[str, Any]], symbol: str, start: date, end: date, api: str) -> dict[date, dict[str, Any]]:
    result: dict[date, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("ts_code", "")).strip().upper() != symbol:
            raise TushareError(f"{api}返回了请求以外的证券，拒绝混合数据")
        day = _date(row.get("trade_date"), api + ".trade_date")
        if not start <= day <= end:
            raise TushareError(f"{api}返回日期超出请求范围")
        if day in result and result[day] != row:
            raise TushareError(f"{api}返回冲突重复行")
        result[day] = row
    return result


class TushareClient:
    def __init__(self, token: str | None = None, config: QuantConfig | None = None):
        self.config = config or QuantConfig()
        token = os.environ.get("TUSHARE_TOKEN") if token is None else token
        if not isinstance(token, str) or not token.strip():
            raise TusharePermissionError("请在本机环境设置TUSHARE_TOKEN；不要把凭据粘贴到日志或命令参数")
        if any(character.isspace() for character in token.strip()):
            raise TusharePermissionError("TUSHARE_TOKEN格式无效；凭据内容不会输出")
        if not 1 <= self.config.api_max_rpm <= 40 or not 0 < self.config.api_timeout_seconds <= 30 or not 0 <= self.config.api_retries <= 3:
            raise TushareError("客户端要求1–40RPM、超时不超过30秒、网络重试最多3次")
        self._token = token.strip()
        self._cancel = threading.Event()
        self._opener = request.build_opener(_NoRedirect(), request.HTTPSHandler(context=ssl.create_default_context()))
        fingerprint = hashlib.sha256(self._token.encode()).hexdigest()
        with _GATES_LOCK:
            gate = _GATES.setdefault(fingerprint, _Gate(self.config.api_max_rpm))
            gate.rpm = min(gate.rpm, self.config.api_max_rpm)
        self._gate = gate
        self._day_status: dict[tuple[str, date], tuple[bool, float, float, float]] = {}
        self._st_days: dict[date, set[str]] = {}
        self._cache_lock = threading.RLock()
        self.request_counts: dict[str, int] = {}

    def _wait(self, seconds: float) -> None:
        if self._cancel.wait(max(0.0, seconds)):
            raise TushareError("Tushare客户端已停止，取消等待")

    def _slot(self) -> None:
        while True:
            if self._cancel.is_set():
                raise TushareError("Tushare客户端已关闭")
            with self._gate.lock:
                now = time.monotonic()
                while self._gate.starts and self._gate.starts[0] <= now - 60:
                    self._gate.starts.popleft()
                delay = max(0.0, self._gate.last_start + 60 / self._gate.rpm - now, self._gate.blocked_until - now)
                if len(self._gate.starts) >= self._gate.rpm:
                    delay = max(delay, self._gate.starts[0] + 60.00001 - now)
                if delay <= 0:
                    self._gate.starts.append(now)
                    self._gate.last_start = now
                    return
            self._wait(delay)

    @staticmethod
    def _retry_after(value: Any) -> float:
        try:
            return max(0.0, min(300.0, float(value)))
        except (ValueError, TypeError):
            try:
                stamp = parsedate_to_datetime(str(value))
                return max(0.0, min(300.0, (stamp - datetime.now(stamp.tzinfo)).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                return 1.0

    def _query(self, api: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if api not in _FIELDS:
            raise TushareError("客户端只允许已声明的只读数据接口")
        body = json.dumps({"api_name": api, "token": self._token, "params": params, "fields": _FIELDS[api]}, separators=(",", ":"), allow_nan=False).encode("utf-8")
        response_data: bytes | None = None
        for attempt in range(self.config.api_retries + 1):
            self._slot()
            if self._cancel.is_set():
                raise TushareError("Tushare客户端已停止，未发出请求")
            response = None
            try:
                req = request.Request(_URL, data=body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Ashare-Research-Stdlib/1.0"})
                try:
                    response = self._opener.open(req, timeout=self.config.api_timeout_seconds)
                except error.HTTPError as exc:
                    response = exc
                status = int(response.getcode())
                self.request_counts[api] = self.request_counts.get(api, 0) + 1
                if status in {401, 403}:
                    raise TusharePermissionError(f"{api} HTTP鉴权/权限失败；请检查本机token及接口开通权限")
                if 300 <= status < 400:
                    raise TushareError("Tushare返回重定向；拒绝向其他地址发送token，也不会回退HTTP")
                if status == 429 or 500 <= status <= 599:
                    delay = self._retry_after(response.headers.get("Retry-After")) if status == 429 else min(8.0, 2.0 ** attempt)
                    if status == 429:
                        with self._gate.lock:
                            self._gate.blocked_until = max(self._gate.blocked_until, time.monotonic() + delay)
                    if attempt == self.config.api_retries:
                        raise TushareError(f"{api} HTTP{status}，只读请求重试额度已用完")
                    LOGGER.warning("Tushare %s HTTP%d，%.1f秒后重试只读请求", api, status, delay)
                    response.close()
                    response = None
                    self._wait(delay)
                    continue
                if status != 200:
                    raise TushareError(f"{api} HTTP{status}，请求失败；错误正文不会记录")
                response_data = response.read(_MAX_BYTES + 1)
                if len(response_data) > _MAX_BYTES:
                    raise TushareError(f"{api}响应超过16MiB，拒绝截断数据")
                break
            except ssl.SSLError:
                raise TushareError("Tushare HTTPS证书/TLS验证失败；不会关闭SSL校验或回退HTTP") from None
            except (error.URLError, TimeoutError, OSError, HTTPException):
                if attempt == self.config.api_retries:
                    raise TushareError(f"{api} HTTPS连接或读取失败；重试后仍不可用，凭据和错误正文不输出") from None
                delay = min(8.0, 2.0 ** attempt)
                LOGGER.warning("Tushare %s 网络失败，%.1f秒后重试只读请求", api, delay)
                self._wait(delay)
            finally:
                if response is not None:
                    try:
                        response.close()
                    except (OSError, HTTPException):
                        pass
        try:
            payload = json.loads(response_data.decode("utf-8"))
        except (ValueError, UnicodeError, AttributeError):
            raise TushareError(f"{api}未返回合法JSON数据") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise TushareError(f"{api}缺少明确业务状态码")
        code = payload["code"]
        if code != 0:
            detail = "分钟权限需单独开通，不能降级成假行情" if api == "rt_min" else "历史ST权限不足时请提供有明确ST状态的CSV" if api == "stock_st" else "请核对token、积分及接口权限"
            raise TusharePermissionError(f"{api}业务鉴权/权限或接口失败(code={code})；{detail}。远程msg未输出")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
            raise TushareError(f"{api}缺少fields/items；空数据也必须具备明确响应结构")
        columns, items = data["fields"], data["items"]
        required = set(_FIELDS[api].split(","))
        if any(not isinstance(value, str) for value in columns) or len(set(columns)) != len(columns) or required - set(columns):
            raise TushareError(f"{api}响应缺少所需字段，不能以默认值补齐")
        limit = _ROW_LIMITS.get(api)
        if limit is not None and len(items) >= limit:
            raise TushareError(f"{api}返回达到{limit}行上限，可能截断；请缩短日期范围/证券数量")
        result = []
        for item in items:
            if not isinstance(item, list) or len(item) != len(columns):
                raise TushareError(f"{api}字段和数据列数不一致")
            result.append(dict(zip(columns, item)))
        return result

    def _calendar(self, start: date, end: date) -> tuple[tuple[date, ...], list[dict[str, Any]]]:
        by_day: dict[date, int] = {}
        for first, last in _chunks(start, end, 365):
            rows = self._query("trade_cal", {"exchange": "SSE", "start_date": first.strftime("%Y%m%d"), "end_date": last.strftime("%Y%m%d")})
            for row in rows:
                day = _date(row.get("cal_date"), "trade_cal.cal_date")
                raw = row.get("is_open")
                if row.get("exchange") != "SSE" or not first <= day <= last or str(raw) not in {"0", "1"}:
                    raise TushareError("trade_cal交易所/日期/开闭市状态不完整")
                state = int(raw)
                if day in by_day and by_day[day] != state:
                    raise TushareError("trade_cal有冲突开闭市状态")
                by_day[day] = state
            if any(first + timedelta(days=offset) not in by_day for offset in range((last - first).days + 1)):
                raise TushareError("官方trade_cal未覆盖请求的全部自然日（可能未来日历未发布）；请调整范围，不能猜节假日")
        ordered = sorted(by_day)
        return tuple(day for day in ordered if by_day[day] == 1), [{"date": day.isoformat(), "is_open": by_day[day]} for day in ordered]

    def _stock(self, symbol: str, config: QuantConfig) -> Instrument:
        rows = self._query("stock_basic", {"ts_code": symbol, "list_status": "L"})
        if len(rows) != 1:
            raise TushareError(f"{symbol}当前stock_basic未返回唯一上市股票；ETF/指数须使用显式CSV，不按代码猜类型")
        row = rows[0]
        board = {"主板": "MAIN", "创业板": "CHINEXT"}.get(row.get("market"))
        expected_exchange = {"SH": "SSE", "SZ": "SZSE"}.get(symbol[-2:])
        if (row.get("ts_code") != symbol or row.get("symbol") != symbol[:6] or row.get("list_status") != "L"
                or row.get("exchange") != expected_exchange or board is None or board not in config.allowed_boards):
            raise TushareError(f"{symbol}证券身份/上市状态/板块不在已配置MAIN或CHINEXT范围")
        name = str(row.get("name") or "").strip()
        if not name:
            raise TushareError("stock_basic缺少证券名称")
        delist = row.get("delist_date")
        return Instrument(symbol, name, "STOCK", board, str(row.get("industry") or "UNKNOWN"),
                          _date(row.get("list_date"), "stock_basic.list_date"), 100, True,
                          _date(delist, "stock_basic.delist_date") if delist else None)

    def _benchmark(self, symbol: str) -> Instrument:
        matches = []
        # market是官方明确的发行方枚举；逐查询并只接受返回ts_code精确匹配。
        for market in ("SSE", "CSI", "SZSE"):
            rows = self._query("index_basic", {"ts_code": symbol, "market": market})
            matches.extend(row for row in rows if row.get("ts_code") == symbol)
            if matches:
                break
        if len(matches) != 1 or not str(matches[0].get("name") or "").strip():
            raise TushareError("配置基准未能通过index_basic取得唯一明确指数元数据")
        row = matches[0]
        expiration = row.get("exp_date")
        return Instrument(symbol, str(row["name"]), "INDEX", "INDEX", "BENCHMARK", _date(row.get("list_date"), "index_basic.list_date"), 1, False, _date(expiration, "index_basic.exp_date") if expiration else None)

    def _history_st(self, symbol: str, start: date, end: date) -> set[date]:
        if start < date(2000, 1, 1):
            raise TushareError("stock_st历史仅从2000-01-01起，不能补造更早ST状态")
        days: set[date] = set()
        for first, last in _chunks(start, end, 180):
            rows = self._query("stock_st", {"ts_code": symbol, "start_date": first.strftime("%Y%m%d"), "end_date": last.strftime("%Y%m%d")})
            for day, row in _keyed(rows, symbol, first, last, "stock_st").items():
                if not row.get("type") or not row.get("type_name"):
                    raise TushareError("stock_st历史行缺少风险警示类型")
                days.add(day)
        return days

    def _range(self, api: str, symbol: str, start: date, end: date) -> dict[date, dict[str, Any]]:
        result = {}
        for first, last in _chunks(start, end, 365):
            rows = self._query(api, {"ts_code": symbol, "start_date": first.strftime("%Y%m%d"), "end_date": last.strftime("%Y%m%d")})
            chunk = _keyed(rows, symbol, first, last, api)
            if set(result) & set(chunk):
                raise TushareError(f"{api}跨块日期重复")
            result.update(chunk)
        return result

    def fetch_daily_dataset(self, symbols: list[str], start: date, end: date, config: QuantConfig | None = None) -> DataSet:
        config = config or self.config
        start, end = _date(start, "start"), _date(end, "end")
        if start > end or not symbols:
            raise TushareError("请输入非空证券列表和起止日期正确的日线区间")
        selected = list(dict.fromkeys(validate_symbol(symbol) for symbol in symbols))
        benchmark = validate_symbol(config.benchmark_symbol) if config.require_market_filter else None
        if benchmark and benchmark not in selected:
            selected.append(benchmark)
        coverage_end = max(end, datetime.now(CN_TZ).date()) + timedelta(days=30)
        calendar, calendar_rows = self._calendar(start, coverage_end)
        open_days = set(calendar)
        instruments, daily = {}, {}
        fetched_at = datetime.now(CN_TZ)
        for symbol in selected:
            is_benchmark = symbol == benchmark
            instrument = self._benchmark(symbol) if is_benchmark else self._stock(symbol, config)
            instruments[symbol] = instrument
            rows = self._range("index_daily" if is_benchmark else "daily", symbol, start, end)
            if not rows:
                raise TushareError(f"{symbol}请求区间没有日线数据，不能返回看似完整的空证券")
            factors = {} if is_benchmark else self._range("adj_factor", symbol, start, end)
            limits = {} if is_benchmark else self._range("stk_limit", symbol, start, end)
            st_days = set() if is_benchmark else self._history_st(symbol, start, end)
            bars = []
            for day, row in sorted(rows.items()):
                if day not in open_days:
                    raise TushareError("日线日期与官方开放日历不一致")
                if is_benchmark:
                    factor, up, down, st, limited = None, None, None, None, False
                else:
                    if day not in factors or day not in limits:
                        raise TushareError(f"{symbol}在{day}缺复权因子或官方涨跌停价；禁止补factor=1/默认10%")
                    factor = _num(factors[day].get("adj_factor"), "adj_factor", positive=True)
                    up, down = _num(limits[day].get("up_limit"), "up_limit", positive=True), _num(limits[day].get("down_limit"), "down_limit", positive=True)
                    st, limited = day in st_days, True
                bars.append(Bar(symbol, cn_time(day, 15), cn_time(day, 18), "daily",
                                _num(row.get("open"), "open", positive=True), _num(row.get("high"), "high", positive=True),
                                _num(row.get("low"), "low", positive=True), _num(row.get("close"), "close", positive=True),
                                _num(row.get("vol"), "vol") * 100, _num(row.get("amount"), "amount") * 1000,
                                factor, _num(row.get("pre_close"), "pre_close", positive=True), up, down, False, st, limited, True))
            daily[symbol] = bars
        metadata = {
            "synthetic": False, "source": "Tushare HTTPS historical daily + stock_st + adj_factor + stk_limit + trade_cal",
            "point_in_time": False, "fetched_at": datetime.now(CN_TZ).isoformat(),
            "survivorship_bias": "stock_basic使用当前L上市股票静态信息，存在幸存者偏差；未构建当时完整证券池",
            "availability_caveat": "历史daily.available_at统一18:00为保守研究假设，不是当时可得日志；历史字段可能被事后修订，不是PIT回测",
            "st_status_source": "stock_st历史逐symbol<=180日块完整响应，列表不存在日期按该接口完整列表契约判非ST；不使用当前名称推断历史",
            "st_status_coverage_start": start.isoformat(), "st_status_coverage_end": end.isoformat(),
            "calendar_start": start.isoformat(), "calendar_end": coverage_end.isoformat(), "calendar_rows": calendar_rows,
            "daily_requested_start": start.isoformat(), "daily_requested_end": end.isoformat(),
            "daily_actual_end": max(bar.day for bars in daily.values() for bar in bars).isoformat(),
            "missing_daily_sessions": "daily不提供停牌期间数据；缺日不补K线，也不凭缺行判断停牌原因",
            "unit_conversion": {"daily.vol": "手*100=股", "daily.amount": "千元*1000=元", "index_daily.vol": "手*100", "index_daily.amount": "千元*1000"},
            "benchmark_symbol": benchmark, "benchmark_price_unit": "index_points", "api_request_counts": dict(self.request_counts),
            "fetch_started_at": fetched_at.isoformat(),
        }
        return validate_dataset(DataSet(instruments, daily, {symbol: [] for symbol in instruments}, calendar, "Tushare日线（研究假设、非PIT）", metadata))

    def _current_status(self, symbols: list[str], day: date) -> dict[str, tuple[bool, float, float, float]]:
        # 9:30后当日ST/限价/因子已公布：同一客户端仅按证券/日期缓存，绝不跨日继承。
        with self._cache_lock:
            if day not in self._st_days:
                rows = self._query("stock_st", {"trade_date": day.strftime("%Y%m%d")})
                current = set()
                for row in rows:
                    if _date(row.get("trade_date"), "stock_st.trade_date") != day or not row.get("type") or not row.get("type_name"):
                        raise TushareError("stock_st当日风险警示字段或日期不完整")
                    current.add(validate_symbol(str(row.get("ts_code", ""))))
                self._st_days[day] = current
            result = {}
            for symbol in symbols:
                key = (symbol, day)
                if key not in self._day_status:
                    limits = _keyed(self._query("stk_limit", {"ts_code": symbol, "trade_date": day.strftime("%Y%m%d")}), symbol, day, day, "stk_limit")
                    factors = _keyed(self._query("adj_factor", {"ts_code": symbol, "trade_date": day.strftime("%Y%m%d")}), symbol, day, day, "adj_factor")
                    if day not in limits or day not in factors:
                        raise TushareError(f"{symbol}缺少当日官方涨跌停价/复权因子，不能沿用前日或默认值")
                    self._day_status[key] = (symbol in self._st_days[day], _num(limits[day].get("up_limit"), "当日up_limit", positive=True),
                                             _num(limits[day].get("down_limit"), "当日down_limit", positive=True), _num(factors[day].get("adj_factor"), "当日adj_factor", positive=True))
                result[symbol] = self._day_status[key]
            return result

    def fetch_intraday(self, dataset: DataSet, symbols: list[str], as_of: datetime, config: QuantConfig,
                       *, timestamp_semantics: str | None = None) -> DataSet:
        if timestamp_semantics not in {"start", "end"}:
            raise TushareError("rt_min官方未说明time为K线开始/结束；必须显式--minute-time-semantics start或end，确认数据源语义后再用")
        if not isinstance(as_of, datetime) or as_of.tzinfo is None:
            raise TushareError("分钟as_of必须带时区")
        dataset = validate_dataset(dataset)
        as_of = as_of.astimezone(CN_TZ)
        now = datetime.now(CN_TZ)
        if as_of.date() != now.date() or (as_of - now).total_seconds() > 5 or (now - as_of).total_seconds() > config.max_intraday_age_seconds:
            raise TushareError("rt_min只用于当前交易日监控，不能请求历史as_of并声称实时，也不能使用过时本机时钟")
        day = as_of.date()
        if not _date(dataset.metadata["calendar_start"], "calendar_start") <= day <= _date(dataset.metadata["calendar_end"], "calendar_end"):
            raise TushareError("当日超出明确官方日历覆盖；请重新下载日历，不能猜节假日")
        minute = as_of.hour * 60 + as_of.minute
        if day not in dataset.calendar or not (570 <= minute <= 690 or 780 <= minute <= 905):
            raise TushareError("当前不在明确开放日的连续盘/刚收盘窗口，不把隔夜旧行情当实时")
        selected = list(dict.fromkeys(validate_symbol(symbol) for symbol in symbols))
        if not selected or len(selected) > min(config.minute_max_symbols, 20):
            raise TushareError("分钟批次必须包含1–20只证券且不超过minute_max_symbols")
        for symbol in selected:
            instrument = dataset.instruments.get(symbol)
            if instrument is None or instrument.asset_type != "STOCK" or instrument.board not in {"MAIN", "CHINEXT"} or instrument.board not in config.allowed_boards:
                raise TushareError("rt_min在线仅接入显式MAIN/CHINEXT股票；ETF需独立官方接口或CSV，不能按代码猜类型")
        statuses = self._current_status(selected, day)
        rows = self._query("rt_min", {"ts_code": ",".join(selected), "freq": "5MIN"})
        observed = datetime.now(CN_TZ)
        if observed.date() != day:
            raise TushareError("分钟请求期间跨交易日，拒绝混合旧日快照")
        received: dict[str, dict[datetime, Bar]] = {symbol: {} for symbol in selected}
        skipped_incomplete = 0
        for row in rows:
            symbol = str(row.get("ts_code", "")).strip().upper()
            if symbol not in received:
                raise TushareError("rt_min返回未请求证券")
            try:
                raw_time = datetime.fromisoformat(str(row.get("time", "")).replace("Z", "+00:00"))
                raw_time = raw_time.replace(tzinfo=CN_TZ) if raw_time.tzinfo is None else raw_time.astimezone(CN_TZ)
            except ValueError:
                raise TushareError("rt_min缺少可解析交易时间") from None
            stamp = raw_time + timedelta(minutes=5) if timestamp_semantics == "start" else raw_time
            if stamp.date() != day:
                raise TushareError("rt_min返回历史日期，拒绝称为当前实时行情")
            if stamp > min(as_of, observed):
                skipped_incomplete += 1
                continue
            st, up, down, factor = statuses[symbol]
            # 当前未复权前收不从前日close推断（除权日会不同）；分钟接口未提供则保留None。
            bar = Bar(symbol, stamp, observed, "5m", _num(row.get("open"), "rt_min.open", positive=True),
                      _num(row.get("high"), "rt_min.high", positive=True), _num(row.get("low"), "rt_min.low", positive=True),
                      _num(row.get("close"), "rt_min.close", positive=True), _num(row.get("vol"), "rt_min.vol"), _num(row.get("amount"), "rt_min.amount"),
                      factor, None, up, down, False, st, True, True)
            if stamp in received[symbol] and received[symbol][stamp] != bar:
                raise TushareError("rt_min同批次同一结束时刻出现冲突K线")
            received[symbol][stamp] = bar
        intraday = {symbol: list(bars) for symbol, bars in dataset.intraday.items()}
        for symbol in selected:
            fresh = received[symbol]
            if not fresh:
                raise TushareError(f"{symbol}没有已完成的当日5分钟线；未完成/空数据不会降级成假行情")
            latest = max(fresh)
            if (observed - latest).total_seconds() > config.max_intraday_age_seconds:
                raise TushareError(f"{symbol}分钟源时间过旧，不能仅用当前下载时间冒充实时")
            merged = {bar.timestamp: bar for bar in intraday.get(symbol, [])}
            for stamp, bar in fresh.items():
                old = merged.get(stamp)
                # 未变化的记录保留最早实际观察时刻；变化的修订以本次观察时刻为准。
                if old is not None and replace(old, available_at=bar.available_at) == bar:
                    bar = replace(bar, available_at=min(old.available_at, bar.available_at))
                merged[stamp] = bar
            intraday[symbol] = sorted(merged.values(), key=lambda bar: bar.timestamp)
        metadata = dict(dataset.metadata)
        metadata.update(intraday_source="Tushare rt_min 5MIN（实际HTTPS采集、未做行情延迟保证）",
                        intraday_downloaded_at=observed.isoformat(), intraday_timestamp_semantics=timestamp_semantics,
                        intraday_status_date=day.isoformat(), intraday_status_source="当日stock_st/stk_limit/adj_factor（同进程按日期缓存、不跨日继承）",
                        intraday_available_at_semantics="实际首次观察/修订时刻；策略as_of应在fetch完成后重新取当前时钟",
                        intraday_skipped_unfinished=skipped_incomplete, api_request_counts=dict(self.request_counts))
        return validate_dataset(DataSet(dict(dataset.instruments), {symbol: list(bars) for symbol, bars in dataset.daily.items()}, intraday, dataset.calendar, dataset.source_label, metadata))

    def close(self) -> None:
        self._cancel.set()

    def __enter__(self) -> "TushareClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def fetch_daily_dataset(symbols: list[str], start: date, end: date, config: QuantConfig) -> DataSet:
    """使用本机TUSHARE_TOKEN下载，不接受命令行/URL中的token。"""
    with TushareClient(config=config) as client:
        return client.fetch_daily_dataset(symbols, start, end, config)
