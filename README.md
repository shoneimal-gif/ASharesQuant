# A 股量化建议版

日线候选、盘中五分钟确认和本地历史模拟复盘使用同一套数据与风控规则。运行环境为 Python 3.10+，全部使用标准库，不需要 pip。输出建议供你自行判断。

## 1. 先看演示

在本目录双击 `run_ashare.cmd`。它使用你电脑已有的 Codex Python，生成合成数据报告并在浏览器打开。没有密钥也可以使用；演示不是当前行情，也不是策略真实业绩。

在此目录的 PowerShell 中也可以运行：

```powershell
.\run_ashare.cmd --demo
Start-Process .\runtime\demo\report.html
```

启动器默认在当前用户目录寻找 Codex Python；可用环境变量 ASHARE_PYTHON 指定其他 Python 3.10+，或直接运行 python -X utf8 main.py --demo。.cmd 入口不需要修改 PowerShell 执行策略。

本交付只做源码静态检查，没有执行应用、演示或真实接口调用。运行验证状态见 [VALIDATION.md](VALIDATION.md)。

## 2. 集成的四类信号

| 周期 | 代码 | 规则用途 |
| --- | --- | --- |
| 日线 | DAILY_VOLUME_BREAKOUT | 趋势过滤后，放量越过前期价格区间 |
| 日线 | DAILY_PULLBACK_CONFIRM | 上升趋势中，回踩均线后重新确认 |
| 五分钟 | INTRADAY_OPENING_RANGE_BREAKOUT | 越过开盘前30分钟区间，并有成交量确认 |
| 五分钟 | INTRADAY_VWAP_RECLAIM | 当日完整分钟数据下，重新站上成交量加权均价 |

市场过滤参考沪深300日线的20/60日均线关系。分数是规则排序，不是上涨概率。量价只能描述观测到的成交，不能据此认定“主力建仓”。第一版没有财务因子、新闻解析、逐笔订单流或板块成分的历史重建。

每条建议包含数据时间、有效期、参考买入区间、失效价、目标价、仓位比例、理由及限制。`BUY_CANDIDATE` 表示条件候选；`WATCH` 表示只观察，模拟引擎也不会据此开仓。

## 3. 默认研究参数

真实参考资金 `reference_capital_cny` 为 `null`，符合你“资金未定”的设置。模拟初始现金为 **100,000元人民币虚拟资金**，与 Alpaca 美元账户独立。

| 项目 | 默认值 |
| --- | --- |
| 研究范围 | 沪深主板股票；CSV可提供明确标识的股票ETF |
| 排除条件 | ST、停牌、上市不足180自然日、价格低于3元、近20日平均成交额低于5000万元、资料不完整 |
| 历史最低要求 | 至少65根已可得的日线 |
| 单标的名义仓位上限 | 5% |
| 单笔计划风险预算 | 模拟权益的0.5%（不是实际损失保证） |
| 总名义仓位 / 同行业上限 | 30% / 15% |
| 最多同时持仓 | 6只 |
| 初始失效距离 | 基于6%、2倍ATR及结构价取较远者，超过12%则只观察 |
| 保本 / 追踪激活 / 硬止盈 | 净收益8% / 12% / 20% |
| 追踪回撤 | 最高价格回撤6%，只对之后的K线生效 |
| 最长持有 | 20个交易日 |
| 模拟滑点 / 量参与率上限 | 单边0.1% / 单根K线成交量1% |

参数在 `config.example.json` 中；尚未用真实样本优化。创业板需显式配置并自行核对交易权限。科创板、北交所的特殊申报规则第一版未实现，因此拒绝启用。

费用采用可修改的研究假设：佣金万2.5且每笔最低5元、股票卖出印花税0.05%、股票双边过户费0.001%、ETF卖出印花税0。实际券商收费和适用税费应以你的账户为准；任何默认值都不是费用报价。股票卖出印花税减半的依据见[财政部、税务总局公告](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html)。

## 4. 接入真实行情：两条路径

### 路径A：券商或数据源导出的CSV

按 [DATA_FORMATS.md](DATA_FORMATS.md) 提供五个文件。软件不会凭代码猜ETF类型，不会把“手”当“股”，不会把未知ST状态、复权因子或涨跌停价自动补成正常值。缺这些信息会拒绝读取或降为观察。

```powershell
.\run_ashare.cmd --check-data --data-dir .\data
.\run_ashare.cmd --daily --data-dir .\data
Start-Process .\runtime\daily\report.html
```

盘中由你的行情程序持续更新CSV，再运行：

```powershell
.\run_ashare.cmd --watch --data-dir .\data
```

更新四个CSV和元数据时须保持批次一致。软件自己保存的数据带 SHA256 清单；手工编辑后必须同步更新清单，或在自建数据包中不提供该可选清单。不要删除校验清单来掩盖中断写入。

### 路径B：Tushare只读接口

原来的 YHS Key 是美股数据凭据，不是本项目的A股行情凭据。Tushare Token 也不等于已开通全部数据权限：本适配器需要股票资料、历史ST、日线、复权因子、涨跌停价、交易日历，以及基准指数资料/日线。历史 `stock_st` 当前文档要求3000积分起；分钟 `rt_min` 需要单独权限。请先核对自己已有权限。[历史ST文档](https://tushare.pro/document/2?doc_id=397)、[实时分钟文档](https://tushare.pro/document/2?doc_id=374)、[权限说明](https://tushare.pro/document/2?doc_id=290)。

在**本机 PowerShell**逐行输入以下代码，密钥不会显示在输入框，也不需要发送到聊天：

```powershell
$ashareSecret = Read-Host '请输入 Tushare Token' -AsSecureString
$env:TUSHARE_TOKEN = [System.Net.NetworkCredential]::new('', $ashareSecret).Password.Trim()
Remove-Variable ashareSecret
```

这只设置当前PowerShell及其子进程的环境变量，关闭窗口后需要重新设置。然后下载**你指定的自选股票**。以下代码仅示范代码格式，不是证券推荐：

```powershell
.\run_ashare.cmd --fetch-daily --symbols 600000.SH,000001.SZ --data-dir .\data
.\run_ashare.cmd --daily --data-dir .\data
Start-Process .\runtime\daily\report.html
```

默认下载约240自然日；自动补入基准指数；日历请求覆盖到当前日期/下载截止日之后30天。每天18:00以后重新执行下载命令，再生成日线建议。更长的历史区间用 `--start YYYY-MM-DD --end YYYY-MM-DD`。若未来官方日历尚未发布、接口权限不足或返回达到行上限，系统会停止并说明原因。

在线下载第一版支持显式沪深主板股票，创业板须配置允许；ETF当前采用CSV路径。没有全市场扫描，也没有免费的隐藏备用源。请求固定HTTPS、40 RPM、串行限速、有界重试，不回退明文HTTP。

### 在线五分钟监控

先准备完整日线行情包，并确认分钟权限。Tushare文档的 `time` 字段没有清楚说明是K线起点还是终点，不能直接猜。向提供商核实后，再选择对应参数：

```powershell
# 仅在已经确认time代表K线终点时使用end；若为起点则改成start。
.\run_ashare.cmd --watch --online-minute --minute-time-semantics end --symbols 600000.SH,000001.SZ --data-dir .\data
```

最多20只自选，每30秒读取一次，但信号来自**已完成五分钟K线**。9:30–11:30、13:00–14:57进行监控；仅当官方日历明确当天开市时发起分钟请求。午休和休市不请求分钟行情。首次使用的当日ST、涨跌停价和因子会独立取得，不能继承昨天的状态。

在线模式会保存分钟数据及实际首次观察/修订时间到数据目录。新建议写到控制台、`alerts.jsonl` 和报告；去重状态保存在 `watch_state.json`。浏览器需刷新查看新报告。Ctrl+C停止后清空当前候选；断网、过期或权限失败也暂停输出，不保留旧候选冒充当前建议。换配置请用新的 `--output-dir` 保留旧审计记录。

## 5. 历史模拟复盘

```powershell
.\run_ashare.cmd --replay daily --data-dir .\data
.\run_ashare.cmd --replay intraday --data-dir .\data
.\run_ashare.cmd --replay combined --data-dir .\data
```

历史五分钟数据须从有权限的来源导入CSV；`rt_min`用于当前监控，不作为历史下载器。`combined` 在同标的同日有分钟数据时使用分钟执行路径，不再重复用日线成交；持仓遇到缺分钟日期，只能显式降为日线OHLC退出模型并将绩效标成不完整，不能补造盘中开仓。

模型严格按数据可得时刻推进：日线至少18:00以后才能产生信号，五分钟建议只在可得之后用于后续K线开盘，不在产生信号的那根K线上买入。开盘先预留资金/名额；整根成交量仅用于事后约束模拟成交，不能成为当时的开仓依据。没有订单队列或真实成交深度。

股票买入当天即使止损触发也不能卖出；第一版对ETF也保守按T+1。涨跌停、停牌、无量时不虚造成交。若同根K线同时触及预设止损和止盈，按止损优先的保守路径处理。日内高点新设的追踪止损只能对后续K线生效。

复权因子改变时，缺少分红/送股等公司行动账本，系统停止新入场、保留头寸并将绩效标为无效；不会用复权价格假装完成交易。回放结束不强制平仓，报告保留未平仓和不完整估值。开仓/退出价、佣金、税费、滑点、净盈亏和峰值收益均可审计。

## 6. 输出文件

每个模式默认写到 `runtime/<模式>/`。`--output-dir` 可指定其他目录。

| 文件 | 内容 |
| --- | --- |
| report.html | 离线浏览器报告，支持周期/状态/关键词过滤 |
| report.json | 同一报告的机器可读数据和研究参数 |
| advice.csv | 建议、时间、有效期、依据、仓位比例 |
| trades_history.csv | 已平仓的模拟交易明细；未平仓看报告统计 |
| equity_curve.csv | 模拟权益、现金、估值和回撤轨迹 |
| events.csv | 入场拒绝、T+1阻挡、缺数据等复盘事件 |
| system.log | 中文运行日志 |
| alerts.jsonl / watch_state.json | 仅watch：历史提醒 / 去重与冷却状态 |
| monitor_status.json | 仅watch：当前健康状态和是否在监控 |

不同历史回放和参数比较请用不同输出目录，避免覆盖报告。

## 7. 使用边界

软件目前运行在你的电脑上；盘中监控时电脑、行情源和程序都要保持在线。本项目也可在装有Python的服务器运行，使用相同命令与环境变量，但本次没有部署或创建定时任务。

历史Tushare数据可能被修订；当前股票资料存在幸存者偏差，日线18:00可得只是研究假设。本系统没有构建严格的历史证券池和逐时点元数据。真正评价策略应使用可追溯的数据，在不同年份/行情状态上做样本外比较，并报告费用、未成交、未平仓及缺失数据；演示数字不能支持盈利结论。

交易制度参见[上交所交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)。每日涨跌停价直接来自数据源；模型没有假定所有证券每天统一10%。

## 文件结构

```text
models.py          共享数据契约和北京时间
config.py          参数与配置校验
data_loader.py     严格CSV校验、日历、批次一致性
tushare_client.py  只读行情适配器
detector.py        日线/分钟信号、市场状态和诊断
paper_trader.py    资金、仓位、T+1、费用与退出模型
replay.py          按可得时间推进的事件回放
reporting.py       HTML/JSON/CSV报告
demo_data.py       确定性人工合成数据
main.py            各运行模式入口
run_ashare.cmd     你的Windows启动入口
```
