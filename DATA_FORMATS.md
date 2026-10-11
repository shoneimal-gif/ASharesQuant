# CSV 数据格式

一个数据目录必须含 `instruments.csv`、`daily.csv`、`intraday.csv`、`calendar.csv`、`metadata.json`。CSV为UTF-8（可有BOM）；分钟数据没有时仍需一个只有表头的 `intraday.csv`。列名必须准确，未知列拒绝。每个证券、时间周期、结束时刻应唯一；完全相同重复行去重，矛盾重复行拒绝。

## instruments.csv

```csv
symbol,name,asset_type,board,sector,list_date,lot_size,t_plus_one,delist_date
600000.SH,示例股票非推荐,STOCK,MAIN,示例行业,2000-01-01,100,1,
000300.SH,示例基准,INDEX,INDEX,BENCHMARK,2005-04-08,1,0,
```

| 列 | 含义 |
| --- | --- |
| symbol | 六位证券代码加`.SH`/`.SZ`/`.BJ`；交易所后缀必须明确 |
| name | 明确名称；不从代码或价格猜类型 |
| asset_type | `STOCK`、`ETF`、`INDEX` |
| board | 股票为MAIN/CHINEXT/STAR/BSE；ETF为ETF；指数为INDEX |
| sector | 行业/集中度分组；未知填写UNKNOWN，模拟视为同一组 |
| list_date / delist_date | YYYY-MM-DD；退市日可空 |
| lot_size | 交易单位，普通主板股票默认100股 |
| t_plus_one | 0/1或true/false；股票必须为1；本版模拟ETF也保守按T+1 |

静态名称和行业不能代替历史ST状态、历史行业归属或当时证券池。股票ETF也应按数据提供方明确认定，不要把债券、跨境、商品ETF与股票ETF混为一类。

## daily.csv 和 intraday.csv

两者使用相同列。`timeframe` 分别必须为 `daily` / `5m`。

```csv
symbol,timestamp,available_at,timeframe,open,high,low,close,volume,amount,adj_factor,prev_close,limit_up,limit_down,suspended,is_st,has_price_limit,is_complete
```

| 列 | 单位 / 规则 |
| --- | --- |
| timestamp | 带时区的ISO时间，表示K线**结束**；日线为当天15:00+08:00 |
| available_at | 带时区ISO时间，不能早于结束；记录实际入库/第一次可读/修订时刻 |
| open/high/low/close | 未复权价格，人民币元；INDEX例外为官方指数点位，不可交易 |
| volume | 股或ETF份额；**不是手** |
| amount | 人民币元；**不是千元/万元** |
| adj_factor | 当日因子，用于指标历史归一；缺失可空但股票/ETF只观察；INDEX不要求 |
| prev_close | 当日数据源公布的未复权前收，除权日不能直接抄昨天close |
| limit_up / limit_down | 当天适用的官方上限/下限，不能统一计算默认10% |
| suspended | 明确0/1；没有成交K线不能据此猜“正常交易” |
| is_st | 明确0/1；未知留空，不可把未知当作非ST |
| has_price_limit | 明确0/1；0表示经核实当日不限幅，此时上下限应空；未知留空 |
| is_complete | 明确0/1；1仅表示已结束且确认完整 |

`adj_factor`、`prev_close`、`limit_up`、`limit_down`、`is_st`、`has_price_limit`为可空字段，其余必需。数值必须有限，不能NaN或无穷。OHLC须满足低价≤开收价≤高价。价格/因子为正，量额为非负。零量与停牌不会产生模拟成交。

五分钟结束点：09:35、09:40……11:30，以及13:05、13:10……15:00。不接受09:30作为五分钟终点、跨午休K线或未对齐秒数；起点格式须由接入端先加五分钟。VWAP需要当日自开盘起的完整连续数据，缺口则不能产生该类候选。

如果只有复权后的OHLC但没有原始价格/历史因子，请不要伪造 `adj_factor=1`；这种文件不能用来同时评价指标、真实交易价格与费用。公司行动因子变化时，回放会停止新入场，并标记绩效无效，因为第一版没有完整分红送股账本。

## calendar.csv

```csv
date,is_open
2026-10-10,0
2026-10-11,0
2026-10-12,1
```

以上仅为格式示例，导入时应使用官方实际日历。覆盖区间每个**自然日**都要明确0或1，不能仅列开放日后自行把缺失日认定为休市。节假日、休市补班日不能用周一至周五猜测。日历还应覆盖当前分析日期和下一个交易日，否则日线候选会降为观察。K线只能出现在已确认开放日，且不能早于上市/晚于退市。

## metadata.json

以下仅为格式示例；请按数据来源填写真实信息和完整覆盖范围，不要冒充已经核查的PIT数据：

```json
{
  "source_label": "我的券商或授权数据源",
  "synthetic": false,
  "calendar_start": "2026-10-10",
  "calendar_end": "2026-10-12",
  "point_in_time": false,
  "availability_caveat": "说明available_at来自入库日志还是研究假设",
  "survivorship_bias": "说明是否含退市股票、当时证券池与历史静态资料",
  "price_basis": "raw"
}
```

加载CSV后软件根据 `calendar.csv` 生成 `calendar_rows`。直接使用Python调用 `save_dataset(DataSet, path)` 时，metadata必须自行提供完整 `calendar_rows`：`[{"date":"YYYY-MM-DD","is_open":0或1}, ...]`，与 `DataSet.calendar` 开放日序列一致。

软件保存数据时会生成 `schema_version` 与四个CSV的 `file_sha256` 清单。重新读入时校验所有文件和同批元数据；写入中断、混合批次和并发改写会被拒绝。自己的CSV可以不提供清单，但仍需满足上述字段和日历契约。

## 数据时间与复盘

日线策略额外采用不早于当日18:00的保守研究假设，即使CSV写更早可得时间也不会提前使用。真实盘中采集必须记实际可得时间，不能用K线结束时间覆盖它。

历史末次采集文件可能包含当时看不到的后续修订值。单一最终版本CSV即使有available_at，也不能证明严格PIT历史，报告会保留这个限制。历史修订如需严谨回放，应建立多版本数据档案，此版未实现。

数据集包含当前自选股票会产生选择/幸存者偏差。模拟报告描述的是指定样本及成交假设，不能据此直接推断全市场可复现收益。
