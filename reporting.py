"""生成离线 HTML、JSON 与 CSV；不发消息、不访问券商。"""
from __future__ import annotations

import csv
import html
import io
import json
import os
from pathlib import Path
from typing import Any

from models import Advice, DataSet, jsonable
from config import QuantConfig

ADVICE_FIELDS = ["signal_id", "symbol", "name", "strategy", "timeframe", "as_of", "data_time", "action", "score", "score_is_probability", "entry_low", "entry_high", "stop_price", "target_price", "suggested_weight_pct", "reference_budget_cny", "valid_until", "allow_paper_entry", "reasons", "warnings", "metrics"]


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        cleaned = {}
        for key in fields:
            value = jsonable(row.get(key, ""))
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, allow_nan=False)
            # CSV 在表格软件打开时不把外部名称/原因解释成公式。
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            cleaned[key] = value
        writer.writerow(cleaned)
    atomic_text(path, "\ufeff" + buffer.getvalue())


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return f"{value:,.{digits}f}"
    return html.escape(str(value))


def advice_row(advice: Advice, config: QuantConfig) -> dict[str, Any]:
    row = jsonable(advice)
    row["suggested_weight_pct"] = advice.suggested_weight * 100
    # 仅预算示意：实际成交仍需用户复核价格、交易单位、费用与账户可用资金。
    row["reference_budget_cny"] = None if config.reference_capital_cny is None else config.reference_capital_cny * advice.suggested_weight
    row["score_is_probability"] = False
    return row


def render_report(payload: dict[str, Any]) -> str:
    esc = html.escape
    synthetic = payload.get("synthetic", False)
    heading = "演示数据 · 不是当前行情" if synthetic else "A 股量化建议与跟踪"
    banner = "本页使用人工合成数据展示操作流程，股票名称、价格和盈亏均不能作为交易依据。" if synthetic else "建议来自已完成且在分析时刻可获得的数据。评分为规则排序分，不是上涨概率；请核对数据时间和交易条件。"
    rows = []
    actions = {"BUY_CANDIDATE": "条件候选", "WATCH": "仅观察"}
    for item in payload.get("advice", []):
        reasons = "<br>".join(esc(str(text)) for text in item.get("reasons", []))
        warnings = "<br>".join(esc(str(text)) for text in item.get("warnings", []))
        metrics = "；".join(f"{key}: {value}" for key, value in item.get("metrics", {}).items())
        action = item.get("action", "WATCH")
        rows.append(f'<tr data-frame="{esc(item.get("timeframe", ""))}" data-action="{esc(action)}">'
                    f'<td><b>{esc(item.get("name", ""))}</b><br><small>{esc(item.get("symbol", ""))}</small></td>'
                    f'<td>{esc(item.get("strategy", ""))}<br><small>{esc(item.get("timeframe", ""))}</small></td>'
                    f'<td>{actions.get(action, esc(action))}<br><small>排序 {fmt(item.get("score"))}</small></td>'
                    f'<td>¥{fmt(item.get("entry_low"))}–{fmt(item.get("entry_high"))}<br><small>止损 ¥{fmt(item.get("stop_price"))} / 目标 ¥{fmt(item.get("target_price"))}</small></td>'
                    f'<td>{fmt(item.get("suggested_weight_pct"))}%<br><small>参考额度 {fmt(item.get("reference_budget_cny"))}</small></td>'
                    f'<td><small>{esc(item.get("data_time", ""))}<br>有效至 {esc(item.get("valid_until", ""))}</small></td>'
                    f'<td>{reasons}<details><summary>条件与限制</summary>{warnings}<p>{esc(metrics)}</p></details></td></tr>')
    if not rows:
        rows.append('<tr><td colspan="7">本时点没有符合条件的信号。数据不足、市场过滤和休市均可能导致空列表，请查看诊断。</td></tr>')
    summary = payload.get("replay", {}).get("summary", {})
    stats = ""
    if summary:
        for key, value in summary.items():
            if isinstance(value, (dict, list)):
                continue
            stats += f"<tr><td>{esc(key)}</td><td>{fmt(value)}</td></tr>"
    notices = "".join(f"<li>{esc(str(item))}</li>" for item in payload.get("notices", []))
    diagnostics = esc(json.dumps(payload.get("diagnostics", {}), ensure_ascii=False, indent=2))
    replay_detail = esc(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    source = esc(payload.get("source_label", ""))
    as_of = esc(payload.get("as_of", ""))
    capital = "资金规模未设定：仅输出比例，未生成真实账户买入数量。" if payload.get("reference_capital_cny") is None else f'参考资金 ¥{fmt(payload["reference_capital_cny"])}：预算为提示值，需复核实际资金。'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{heading}</title>
<style>body{{background:#f4f6f8;color:#172b3a;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;margin:0}}main{{max-width:1500px;margin:auto;padding:24px}}h1{{font-size:28px;margin:0 0 8px}}header,.card{{background:white;border:1px solid #dfe5ea;border-radius:14px;padding:22px;margin-bottom:18px}}.banner{{background:{'#fff1ef' if synthetic else '#edf5fe'};border-left:4px solid {'#c84635' if synthetic else '#286caa'};padding:14px;border-radius:6px}}small{{color:#586a7a}}.controls{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}}input,select{{font:inherit;padding:8px 10px;border:1px solid #bbc9d4;border-radius:6px}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:920px}}th,td{{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #e3e9ee}}th{{background:#f6f9fb;white-space:nowrap}}details{{margin-top:8px}}summary{{cursor:pointer;color:#22669a}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}.stats table{{min-width:0}}.footer{{color:#657586;font-size:13px}}a{{color:#22669a}}</style></head>
<body><main><header><h1>{heading}</h1><div class="banner">{banner}</div><p>分析时点：{as_of}（北京时间）<br>来源：{source}</p><p>{capital}</p><b>运行方式：建议输出与本地模拟复盘；由你决定是否交易。</b></header>
<section class="card"><h2>日线与盘中建议</h2><div class="controls"><select id="frame"><option value="">全部周期</option><option value="daily">日线波段</option><option value="5m">盘中五分钟</option></select><select id="action"><option value="">全部状态</option><option value="BUY_CANDIDATE">条件候选</option><option value="WATCH">仅观察</option></select><input id="query" placeholder="筛选代码、名称或理由" aria-label="筛选代码、名称或理由"></div><div class="scroll"><table id="ideas"><thead><tr><th>证券</th><th>信号</th><th>状态 / 排序分</th><th>参考价格与风控</th><th>仓位上限</th><th>数据时点</th><th>依据</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section class="card"><h2>本次诊断与提醒</h2><ul>{notices}</ul><details><summary>标的过滤与市场状态</summary><pre>{diagnostics}</pre></details></section>
<section class="card stats"><h2>模拟复盘统计</h2>{'<table>'+stats+'</table>' if stats else '<p>本模式尚未运行历史模拟。请使用 --replay 生成统计。</p>'}<details><summary>完整统计与模型限制</summary><pre>{replay_detail}</pre></details><p>CSV：<a href="advice.csv">建议清单</a> · <a href="trades_history.csv">模拟交易</a> · <a href="equity_curve.csv">权益轨迹</a> · <a href="events.csv">事件记录</a></p></section>
<p class="footer">趋势、突破、VWAP 等是可解释的研究规则，未宣称具有超额收益。回测只能描述输入数据和模拟成交假设下的结果；T+1、涨跌停与跳空可使止损无法按计划执行。</p></main>
<script>function filter(){{let f=document.getElementById('frame').value,a=document.getElementById('action').value,q=document.getElementById('query').value.toLowerCase();document.querySelectorAll('#ideas tbody tr[data-frame]').forEach(r=>{{r.style.display=(!f||r.dataset.frame===f)&&(!a||r.dataset.action===a)&&r.textContent.toLowerCase().includes(q)?'':'none'}})}}['frame','action','query'].forEach(id=>document.getElementById(id).addEventListener('input',filter));</script></body></html>'''


def export_reports(directory: Path, dataset: DataSet, config: QuantConfig, as_of: Any,
                   advice: list[Advice], diagnostics: dict[str, Any], replay: dict[str, Any] | None = None,
                   mode: str = "combined", notices: list[str] | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    default_notices = [
        "第一版研究参数尚未做真实样本的样本外检验；没有收益或胜率承诺。",
        "建议只使用允许交易的板块；交易权限请你在券商账户核对。",
        "股票买入当日不可卖出，盘中止损触发可以被 T+1 或跌停阻挡。",
        "每日行情的复权因子用于指标计算，成交和费用始终采用未复权价格。",
        "行业未知的标的在模拟中归为同一行业组，避免绕过集中度限制。",
    ]
    if dataset.metadata.get("synthetic"):
        default_notices.insert(0, "当前全部数据是人工演示数据，演示统计不是策略历史业绩。")
    payload = {
        "mode": mode, "advisory_only": True, "broker_connection": False,
        "as_of": jsonable(as_of), "source_label": dataset.source_label,
        "synthetic": bool(dataset.metadata.get("synthetic", False)),
        "reference_capital_cny": config.reference_capital_cny,
        "paper_initial_cash_cny": config.paper_initial_cash_cny,
        "config": jsonable(config), "data_metadata": jsonable(dataset.metadata),
        "advice": [advice_row(item, config) for item in advice],
        "diagnostics": jsonable(diagnostics), "replay": jsonable(replay or {}),
        "notices": default_notices + (notices or []),
    }
    atomic_text(directory / "report.json", json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    write_csv(directory / "advice.csv", payload["advice"], ADVICE_FIELDS)
    replay = replay or {}
    tables = (
        ("trades_history.csv", "trades", ["position_id", "symbol", "strategy", "signal_id", "entry_time", "entry_price", "quantity", "entry_fees", "exit_time", "exit_price", "exit_fees", "realized_pnl_cny", "realized_roi_pct", "peak_roi_pct", "max_adverse_roi_pct", "exit_reason"]),
        ("equity_curve.csv", "equity_curve", ["timestamp", "cash_cny", "market_value_cny", "equity_cny", "drawdown_pct", "open_positions", "valuation_stale"]),
        ("events.csv", "events", ["timestamp", "symbol", "event", "reason", "details"]),
    )
    for filename, key, defaults in tables:
        records = [jsonable(row) for row in replay.get(key, [])]
        fields = defaults + sorted({field for row in records for field in row} - set(defaults))
        write_csv(directory / filename, records, fields)
    path = directory / "report.html"
    atomic_text(path, render_report(payload))
    return path
