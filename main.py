"""A 股日线、盘中与复盘入口。只输出建议；所有 API 都是行情读取。"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sys
import time

from config import QuantConfig
from models import CN_TZ, Advice, DataSet, jsonable, parse_time, stable_hash, validate_symbol
from data_loader import load_dataset, save_dataset
from detector import evaluate_daily, evaluate_intraday, diagnose_universe, market_regime
from demo_data import make_demo_dataset
from replay import run_replay
from reporting import ADVICE_FIELDS, atomic_text, export_reports, write_csv

LOG = logging.getLogger("A股建议版")
BASE = Path(__file__).resolve().parent


class OutputLease:
    """同一输出目录只容许一个监控进程，锁文件存在不代表锁仍被持有。"""
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.handle = (directory / "watch.lock").open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("此输出目录已有监控进程，请先停止该进程或换独立目录") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def choose_symbols(text: str | None, dataset: DataSet | None, config: QuantConfig) -> list[str]:
    if text:
        symbols = list(dict.fromkeys(validate_symbol(item) for item in text.split(",") if item.strip()))
    elif dataset is not None:
        symbols = [symbol for symbol, instrument in dataset.instruments.items() if instrument.asset_type != "INDEX" and instrument.board in config.allowed_boards]
    else:
        raise ValueError("请用 --symbols 指定你的自选股票代码；第一版不会默认下载全市场")
    if not symbols:
        raise ValueError("自选列表为空")
    return symbols


def research_subset(dataset: DataSet, text: str | None, config: QuantConfig) -> DataSet:
    """统一自选范围；保留明确基准数据供市场过滤，不改写输入数据包。"""
    if not text:
        return dataset
    symbols = choose_symbols(text, dataset, config)
    missing = [symbol for symbol in symbols if symbol not in dataset.instruments]
    if missing:
        raise ValueError("自选证券缺少资料：" + ",".join(missing))
    retained = set(symbols)
    if config.benchmark_symbol in dataset.instruments:
        retained.add(config.benchmark_symbol)
    metadata = dict(dataset.metadata)
    metadata["research_symbols"] = symbols
    metadata["universe_scope"] = "CLI显式自选；基准仅保留用于市场过滤"
    return DataSet({symbol: item for symbol, item in dataset.instruments.items() if symbol in retained},
                   {symbol: rows for symbol, rows in dataset.daily.items() if symbol in retained},
                   {symbol: rows for symbol, rows in dataset.intraday.items() if symbol in retained},
                   dataset.calendar, dataset.source_label, metadata)


def analyze(dataset: DataSet, config: QuantConfig, as_of: datetime, mode: str) -> tuple[list[Advice], dict]:
    daily = evaluate_daily(dataset, config, as_of) if mode in {"daily", "combined"} else []
    minutes = evaluate_intraday(dataset, config, as_of) if mode in {"intraday", "combined"} else []
    advice = sorted(daily + minutes, key=lambda item: (item.action == "BUY_CANDIDATE", item.score), reverse=True)
    diagnostics = {
        "market": market_regime(dataset, config, as_of, timeframe="5m" if mode == "intraday" else "daily"),
        "daily": diagnose_universe(dataset, config, as_of, timeframe="daily") if mode in {"daily", "combined"} else {},
        "intraday": diagnose_universe(dataset, config, as_of, timeframe="5m") if mode in {"intraday", "combined"} else {},
    }
    return advice, diagnostics


def log_result(path: Path, advice: list[Advice]) -> None:
    LOG.info("输出 %d 条建议，其中条件候选 %d 条；由你决定是否交易", len(advice), sum(item.allow_paper_entry for item in advice))
    LOG.info("查看报告：%s", path)
    for item in advice[:8]:
        LOG.info("%s %s | %s | %s | 参考 %.2f–%.2f 元 | 仓位上限 %.2f%%", item.symbol, item.name, item.strategy,
                 "条件候选" if item.allow_paper_entry else "仅观察", item.entry_low, item.entry_high, item.suggested_weight * 100)


def calendar_covers(dataset: DataSet, day: date) -> bool:
    start = dataset.metadata.get("calendar_start")
    end = dataset.metadata.get("calendar_end")
    if not start or not end:
        return False
    return date.fromisoformat(str(start)) <= day <= date.fromisoformat(str(end))


def in_session(now: datetime) -> bool:
    clock = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= clock < 11 * 60 + 30 or 13 * 60 <= clock < 14 * 60 + 57


def clear_current_advice(output: Path, status: str, message: str = "") -> None:
    """初始化失败、停止或暂停时也不得把先前报告当作当前建议。"""
    payload = {"as_of": datetime.now(CN_TZ).isoformat(), "healthy": False, "active": False,
               "status": status, "advisory_only": True, "advice": []}
    if message:
        payload["error"] = message
    atomic_text(output / "monitor_status.json", json.dumps(payload, ensure_ascii=False))
    atomic_text(output / "report.json", json.dumps(payload, ensure_ascii=False))
    write_csv(output / "advice.csv", [], ADVICE_FIELDS)
    heading = {"STARTING": "监控正在初始化", "STOPPED": "建议监控已停止", "PAUSED": "本轮暂停生成建议", "FAILED": "监控启动或运行失败"}.get(status, "当前没有建议")
    atomic_text(output / "report.html", '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + heading + '</title><h1>' + heading + '</h1><p>本页没有当前建议。请查看 monitor_status.json 与 system.log。历史提醒保存在 alerts.jsonl，只能按当时的数据时间和有效期审查。启动 --watch 并恢复行情后才会更新。</p></html>')


def watch(args: argparse.Namespace, config: QuantConfig, output: Path) -> None:
    state_path = output / "watch_state.json"
    identity = stable_hash({"data_dir": str(args.data_dir.resolve()), "config": config, "symbols": args.symbols,
                            "online_minute": args.online_minute, "minute_time_semantics": args.minute_time_semantics})
    state = {"identity": identity, "seen": {}, "last_emitted": {}, "alerts": []}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        if state.get("identity") != identity:
            raise ValueError("输出目录的监控配置已变更，请使用新的输出目录保留原先记录")
        if not isinstance(state.get("seen"), dict) or not isinstance(state.get("last_emitted"), dict) or not isinstance(state.get("alerts"), list):
            raise ValueError("监控状态损坏，拒绝自动清空历史")
    client = None
    if args.online_minute:
        if args.minute_time_semantics is None:
            raise ValueError("在线分钟接口的 time 起止含义未确认；请核对提供商后显式指定 --minute-time-semantics start 或 end")
        from tushare_client import TushareClient
        client = TushareClient(config=config)
    LOG.info("建议监控已启动：每 %d 秒读取一次已完成五分钟数据；这不是逐笔或秒级信号", config.watch_poll_seconds)
    LOG.info("Ctrl+C 停止；提醒写入控制台和 alerts.jsonl，不向外部通讯软件发送消息")
    prior_error = None
    try:
        while True:
            started = time.monotonic()
            now = datetime.now(CN_TZ)
            try:
                dataset = load_dataset(args.data_dir)
                if dataset.metadata.get("synthetic"):
                    raise ValueError("演示数据不能用于当前盘中监控；请改为真实行情数据目录")
                if not calendar_covers(dataset, now.date()):
                    raise ValueError("交易日历没有覆盖今天；请刷新行情包和未来交易日历，不能按工作日猜交易日")
                active = now.date() in dataset.calendar and in_session(now)
                if active and client is not None:
                    symbols = choose_symbols(args.symbols, dataset, config)
                    if len(symbols) > config.minute_max_symbols:
                        raise ValueError(f"分钟自选最多 {config.minute_max_symbols} 只，请缩小 --symbols")
                    dataset = client.fetch_intraday(dataset, symbols, now, config, timestamp_semantics=args.minute_time_semantics)
                    # 保存首次观察/修订时刻，重启后继续审计同一批分钟数据。
                    save_dataset(dataset, args.data_dir)
                    # 数据可得时间使用响应收取时刻，分析时钟随后更新。
                    now = datetime.now(CN_TZ)
                research_data = research_subset(dataset, args.symbols, config)
                advice, diagnostics = analyze(research_data, config, now, "combined" if active else "daily")
                notices = [] if active else ["当前不在连续交易监控时段，未发起分钟网络请求；日线数据仍需满足新鲜度条件。"]
                for item in advice:
                    if not active or not item.allow_paper_entry or item.signal_id in state["seen"]:
                        continue
                    group = f"{item.symbol}|{item.timeframe}"
                    prior = state["last_emitted"].get(group)
                    if prior and now - parse_time(prior) < timedelta(minutes=config.intraday_cooldown_minutes):
                        continue
                    record = {"emitted_at": now.isoformat(), "advice": jsonable(item), "advisory_only": True}
                    with (output / "alerts.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    state["seen"][item.signal_id] = now.isoformat()
                    state["last_emitted"][group] = now.isoformat()
                    state["alerts"].append(record)
                    LOG.info("新建议 %s %s %s，参考 %.2f–%.2f，失效 %.2f；请自行复核", item.symbol, item.name, item.strategy,
                             item.entry_low, item.entry_high, item.stop_price)
                state["alerts"] = state["alerts"][-config.watch_history_limit:]
                if len(state["seen"]) > config.watch_history_limit * 5:
                    state["seen"] = dict(sorted(state["seen"].items(), key=lambda item: item[1])[-config.watch_history_limit * 5:])
                atomic_text(state_path, json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False))
                diagnostics["monitor"] = {"active": active, "recent_alert_count": len(state["alerts"]), "latest_alerts": state["alerts"][-20:]}
                export_reports(output, research_data, config, now, advice, diagnostics, mode="watch", notices=notices)
                atomic_text(output / "monitor_status.json", json.dumps({"as_of": now.isoformat(), "healthy": True, "active": active, "advisory_only": True}, ensure_ascii=False))
                if prior_error:
                    LOG.info("行情监控已恢复")
                prior_error = None
            except Exception as error:
                # 仅输出受控错误信息；客户端保证不把 Token/响应正文拼入异常。
                message = str(error)
                if message != prior_error:
                    LOG.error("本轮暂停生成建议：%s", message)
                prior_error = message
                # 覆盖当前建议，避免用户继续读到旧的买入候选。
                clear_current_advice(output, "PAUSED", message)
            time.sleep(max(0.1, config.watch_poll_seconds - (time.monotonic() - started)))
    except KeyboardInterrupt:
        LOG.info("已停止建议监控，未调用任何券商交易接口")
    finally:
        if client is not None:
            client.close()
        clear_current_advice(output, "STOPPED")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A 股日线+盘中建议与本地模拟复盘；Python标准库")
    modes = parser.add_mutually_exclusive_group(required=True)
    for flag, help_text in (("demo", "合成数据展示日线/盘中/复盘流程"), ("daily", "收盘日线建议"),
                            ("intraday", "当前五分钟数据建议"), ("combined", "日线与盘中合并建议"),
                            ("watch", "持续读取CSV/可选Tushare分钟接口并输出建议"), ("fetch-daily", "从Tushare读取自选股票历史行情包"),
                            ("check-data", "读取并诊断CSV数据包")):
        modes.add_argument("--" + flag, action="store_true", help=help_text)
    modes.add_argument("--replay", choices=("daily", "intraday", "combined"), help="历史模拟复盘模式")
    parser.add_argument("--data-dir", type=Path, default=BASE / "data")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=BASE / "config.example.json")
    parser.add_argument("--as-of", help="分析时刻，例如 2026-10-12T10:35:05+08:00")
    parser.add_argument("--symbols", help="自选代码逗号分隔，例如600000.SH,000001.SZ；示例代码不是推荐")
    parser.add_argument("--start", help="下载历史起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="下载历史结束日期 YYYY-MM-DD；默认最近已可得日期")
    parser.add_argument("--online-minute", action="store_true", help="watch模式从Tushare拉取五分钟数据，需要单独权限")
    parser.add_argument("--minute-time-semantics", choices=("start", "end"), help="提供商time字段是K线起点还是终点，须自行核实")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    mode = "demo" if args.demo else args.replay or next(name for name in ("daily", "intraday", "combined", "watch", "fetch_daily", "check_data") if getattr(args, name))
    output = (args.output_dir or BASE / "runtime" / ("demo" if args.demo else mode)).resolve()
    output.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(output / "system.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s：%(message)s", handlers=handlers)
    try:
        if args.watch:
            with OutputLease(output):
                clear_current_advice(output, "STARTING")
                try:
                    config = QuantConfig.load(args.config)
                    if args.as_of:
                        raise ValueError("持续监控使用当前北京时间，不能以历史 --as-of 冒充实时")
                    watch(args, config, output)
                except Exception as error:
                    clear_current_advice(output, "FAILED", str(error))
                    raise
            return 0
        config = QuantConfig.load(args.config)
        if args.online_minute:
            raise ValueError("--online-minute 仅适用于 --watch")
        if args.fetch_daily:
            from tushare_client import fetch_daily_dataset
            now = datetime.now(CN_TZ)
            symbols = choose_symbols(args.symbols, None, config)
            end = date.fromisoformat(args.end) if args.end else now.date() - timedelta(days=0 if now.hour >= 18 else 1)
            start = date.fromisoformat(args.start) if args.start else end - timedelta(days=240)
            if start > end or end > now.date():
                raise ValueError("下载日期范围错误；不能读取未来行情")
            dataset = fetch_daily_dataset(symbols, start, end, config)
            save_dataset(dataset, args.data_dir)
            LOG.info("行情包已保存至 %s；数据权限/时间限制请看metadata.json", args.data_dir.resolve())
        elif args.demo:
            dataset = make_demo_dataset()
            save_dataset(dataset, BASE / "runtime" / "demo_data")
        else:
            dataset = load_dataset(args.data_dir)
        dataset = research_subset(dataset, args.symbols, config)
        if args.as_of:
            as_of = parse_time(args.as_of)
        elif args.demo:
            # 只看演示末日10:35的数据，不用后来完成的K线生成该时点建议。
            last = max(bar.day for bars in dataset.intraday.values() for bar in bars)
            as_of = datetime.combine(last, datetime.strptime("10:35:02", "%H:%M:%S").time(), CN_TZ)
        else:
            as_of = datetime.now(CN_TZ)
        report_mode = "combined" if args.demo or args.replay or args.fetch_daily or args.check_data else mode
        advice, diagnostics = analyze(dataset, config, as_of, report_mode)
        replay = run_replay(dataset, config, mode=args.replay or "combined") if args.demo or args.replay else None
        notices = ["当前命令仅做CSV数据诊断，输出的信号仍需按数据时点审查。"] if args.check_data else []
        path = export_reports(output, dataset, config, as_of, advice, diagnostics, replay, mode=mode, notices=notices)
        log_result(path, advice)
        if replay:
            print(json.dumps(jsonable(replay.get("summary", {})), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        LOG.info("已取消，无券商订单")
        return 130
    except Exception as error:
        LOG.error("任务停止：%s", str(error))
        LOG.info("请检查数据文件、日历范围与接口权限；密钥不用发到聊天")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
