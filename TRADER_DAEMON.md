# 量化交易主脑（Demo 事件驱动调度器）

> `core/trader_daemon.py` —— 不需要人盯着敲命令，自己能跑的那部分。

## LLM 介入（三处，不纯脚本）

**1) 宏观事件介入**（`core/macro_watch.py`）
- 官方 BLS 日历判定 CPI/PPI/NFP 发布日（无前视）
- 当天或近 1 天有重大数据 → 抓真实新闻（Tavily）→ 喂 LLM
- LLM 判断 `RISK_ON / NEUTRAL / RISK_OFF` → 输出 `exposure_mult`（0.4~1.2）
- 该系数**缩放全部目标权重**（risk_off → 主动降敞口）
- LLM 失败降级：按新闻情绪关键词打分（兜底不摆烂）

**2) 触发式介入**（`core/trigger_engine.py`）
- 5 类触发：偏离 / **价格异动(z-score)** / 浮亏 / 例行日 / **LLM 主动评估(定时)**
- 只有触发才唤醒 LLM，平时静默（省 token）

**3) LLM 直接下单**（`core/llm_order.py`，`--llm-orders` 开启）
- LLM **能看到**：当前持仓、每只标的的交易所规则（minNotional/step/minQty）、
  可用额度、实时价格、因子参考目标、宏观判断
- LLM **自己决定**：下几笔、每笔多少、多空、开/加/减/平、是否单边/集中/不交易
- LLM 输出订单意图（`{"orders":[{"symbol","side","notional_usd"或"qty","reduce_only"}]}`）
- **校验器**逐条把关：精度吸附、minNotional 门槛、单笔上限、总敞口(gross cap)、
  可交易性、reduce_only 不翻转 —— 只剔除物理上无法成交的腿，不改变 LLM 的自由度
- 校验通过 → 走**同一执行通道**（`bridge_us_binance.execute_orders`，仍然 Demo-only + 硬闸）
- 节流：默认每 60 分钟至少主动评估一次（`llm_review_interval_min`）

**优先级 / 降级链（LLM 优先，脚本兜底）** —— 见 `rec["order_source"]`：

| LLM 情况 | order_source | 行为 |
|---|---|---|
| 给了**可成交订单** | `llm` | 用 LLM 的单（因子目标仅作参考） |
| 成功但**决定不下单**（空 orders，有理由） | `llm_empty` | 尊重 LLM，不发单 |
| 报了单但**全被交易所门槛剔** | `factor_fallback(llm_orders_all_invalid)` | **退回脚本因子订单** |
| **不可用/失败/超时/解析不出** | `factor_fallback(llm_unavailable)` | **退回脚本因子订单** |
| LLM 路径整体异常 | `factor_fallback(llm_exception)` | **退回脚本因子订单** |

> 判据是 `llm_decide_orders` 返回的 `_ok`：True=LLM 真的给了判断，False=不可用。
> 分工：因子给「参考目标」，LLM 是「最终决策者」；LLM 挂掉时脚本顶上，不会什么都不做。
> 两者都不绕过硬闸（Demo/杠杆/敞口）。

## 当前配置（$100 小资金）

**100 美元额度**，只看这 100 的盈亏。可满仓（非强制）。滚动复利。

**仓位分配是"决策"不是"定死"** —— 两项**唯一真实约束**：
1. **总额 ≤ equity**（$100）
2. **单笔 ≥ 交易所 minNotional**（当前 $5，从 exchangeAPI 动态读）

其余全是自由的（**没有任何硬编码的腿数/每腿金额/分位数**）：
- **几只？** 不预设 —— 贪心预算分配：从最强标的开始，分给它的钱 ≥ 门槛就下，直到预算花完
- **每只多少？** 按信号强度（排名倒数 1,1/2,1/3…），强信号拿大头
- **多空？** 因子正做多、因子负做空；**不对称、单边、单只满仓都允许**
- **总敞口？** 由信号整体强度决定（不强求满仓）

实测：$100 → 信号强时 1~4 腿（可能单边单只满仓）；越分散每腿越小，到 <$5 自动截止。
宏观 RISK_OFF 缩仓后，碎腿（<minNotional）自动剔除（少持有而非下碎单）。

## 一句话

**静默轮询 → 触发才唤醒 LLM → 决策 → 在 Demo 下单。**

不是定时无脑跑，而是**事件驱动**：平时安静，只有出现"值得判断的情况"才叫 LLM。

## 工作流

```
┌─ 静默轮询（每 5 分钟；只读价格/持仓/算目标，不调 LLM）
│     ↓
├─ 触发引擎 core/trigger_engine.py 判断（任一命中）
│     ① needs_rebalance  持仓偏离目标，真的需要调仓（受「调仓日历」约束，见下）
│     ② price_anomaly    单标的 z-score 超阈值（异动）
│     ③ drawdown_alert   浮亏超本金 5%（风控）
│     ④ rebalance_due    距上次调仓 ≥5 天（例行）
│     ↓ 命中
├─ 唤醒 LLM（9Router free）：打包证据 → 要它判断该不该动
│     ↓
├─ 决策合并：LLM 建议 + 4 道硬闸
│     ↓
└─ 执行（Demo）+ 台账 + 心跳留痕
```

## 安全边界（硬编码 + 硬闸）
| 闸门 | 规则 | 作用 |
|---|---|---|
| 环境 | **只允许 Demo(testnet)**，mainnet 拒绝启动 | 零真实风险 |
| 杠杆 | 默认 1 | 不加杠杆 |
| 单标的名义 | ≤ 500 USDT | 防单腿爆仓 |
| 日内调仓次数 | ≤ 6 次 | 防震荡市来回打（`--max-daily-rebalances`，默认 6） |
| 再平衡死区 | 微调 < max(minNotional×2, 5% equity) 跳过 | 防追着抖动目标做碎单 |
| **调仓日历** | `needs_rebalance` 须同时满足：①全组合累计漂移 ≥ equity×10%；②距上次实际调仓 ≥ 240 分钟。风控（回撤/异动）豁免本闸 | **根治「前端挖低频、后端跑高频」**：低频因子的持有期不应被 5 分钟 tick 跑成碎单（`--aggr-drift-pct`/`--min-rebalance-interval-min`） |
| **单标的震荡闸** | 单标的日内被交易 > 4 次 或 方向反转 > 2 次 → 仅剔除该标的订单 | 专治 MSTR 一天来回 22 次（`--per-symbol-daily-cap`/`--per-symbol-flip-cap`） |
| **最大回撤熔断** | 浮亏 ≥ 本金 10% → **主动降险**：把总敞口压到 `gross_cap×equity×(1-回撤)` 以下，只产出减仓单 | 硬止损，不依赖 LLM（`--max-drawdown-pct`，默认 0.10） |
| **日内亏损熔断** | 当日已实现亏损 ≥ 本金 3% → 禁止加仓 | 防亏损后加仓摊平（`--daily-loss-limit-pct`，默认 0.03） |
| **日内换手上限** | 当日累计成交名义额 ≥ equity × 3 → 停手 | 防「每次只调一点」累计被手续费抽干（`--max-daily-turnover-x`，默认 3.0） |
| **资金费率闸门** | 腿的年化持有成本 > 30% → 剔除该腿 | 永续独有持仓成本，防止高费率腿持续侵蚀 alpha（`--funding-max-ann`，默认 0.30） |
| 总敞口 | ≤ equity × 1.2 | 防仓位失控 |
| 执行前重读 | 下单前重读交易所持仓，变了就放弃 | 防重复开仓 |
| **自动对账** | 每 tick 比对交易所 vs 本地持仓，漂移即告警并同步 | 防状态脱节导致重复开仓/漏平 |
| LLM 权限 | 只能"建议"，HOLD 且非风控时直接拦下 | LLM 不能绕过门槛 |

> **三道硬风控的意义**：原系统只有 `drawdown_alert`「提醒 LLM」，若 LLM 回 HOLD 就不降险，无人值守时等于没有止损。
> 新增的 4/5/6 号闸门是**代码级硬规则**，判定只看数字、不看 LLM。
> 其中 **闸4 是「主动降险」而非「被动拦截」**：回撤超限时不仅拦加仓，还会用
> `force_derisk` 主动把敞口按回撤比例砍下去 —— 浮亏越深、敞口越薄。

## 三种运行模式

```bash
cd /opt/quant-lab

# 1) 单次 dry（看它会判断成什么，不发单，会调 LLM）
./venv/bin/python -m core.trader_daemon --once --force

# 2) 单次 dry，跳过 LLM（纯规则）
./venv/bin/python -m core.trader_daemon --once --force --no-llm

# 3) 常驻（生产用；见下方 systemd）
./venv/bin/python -m core.trader_daemon --loop --interval 300 --live
```

## systemd 服务（已安装）

```bash
systemctl status  quant-trader     # 看状态
systemctl stop    quant-trader     # 暂停自动交易
systemctl start   quant-trader     # 恢复
systemctl disable quant-trader     # 取消开机自启
journalctl -u quant-trader -f      # 实时日志（或看 runs/trader_daemon.log）
```

配置见 `/etc/systemd/system/quant-trader.service`。
当前参数：`mom250` / equity=1000 / quantile=0.2 / **neutralize=none** / 杠杆1 / 名义≤500 / 日内≤3 / gross≤1.2。

## 留痕文件

| 文件 | 内容 |
|---|---|
| `data/binance/trader_heartbeat.jsonl` | **每次 tick 都写**（无论是否下单）→ 可审计 |
| `data/binance/trader_decisions.jsonl` | 触发+有决策的 tick（含 LLM 判断、闸门结果） |
| `data/binance/trader_state.json` | 最近一次状态快照 |
| `data/binance/demo_exec_ledger.jsonl` | 真实成交台账（交给 demo_reconcile 对账） |
| `runs/trader_daemon.log` | systemd stdout/stderr |

## 大坑记录（都已在代码里修掉）

1. **本地持仓文件不可信**：`load_positions()` 读本地文件，文件缺失时返回空 → 桥以为"没持仓" → 所有调仓标成 OPEN → **重复开仓**。改用 `load_positions_from_exchange()` 从交易所读真实持仓。

2. **sector 中性在小样本池上爆炸**：Demo 可交易池小，sector 分组可能只选出 2 只 → 单腿名义爆到 330-480。已加小样本保护（覆盖 <60% 或 <10 只就退回 none）。**默认改用 none**。

3. **僵尸标的**：Demo 的 `BMNRUSDT` 在 exchangeInfo 里有符号但**无报价**。用 `live_quote_symbols()` 过滤掉无行情的标的。

4. **噪声单**：`build_orders` 里 min_notional 检查曾是死代码（只 `pass`），0.00 的单也发。已修，但平仓单仍放行。

5. **LLM 不守 JSON 格式**：free 模型经常回自由文本。已加 `_fallback_parse` 关键词兜底（REDUCE_RISK/REBALANCE/HOLD），解析失败退回纯规则，绝不崩。

6. **`extract_json` 会抛异常**：从自由文本抠 JSON 时可能崩，被 `_ask_llm` 捕获后异常串会污染结果。已单独包住解析。

## 调参

```bash
--factor            mom250 | mom120 | trend120
--equity            资金（默认 1000）
--quantile          选股分位（默认 0.2，即前后 20%）
--neutralize        none（默认）| sector（小样本会自动退回 none）
--leverage          杠杆（默认 1）
--max-notional      单标的名义上限（默认 500）
--max-daily-rebalances  日内调仓上限（默认 6）
--gross-cap         总敞口上限（默认 1.2）
--max-drawdown-pct      最大回撤熔断阈值（默认 0.10，0 关闭）
--daily-loss-limit-pct  日内亏损熔断阈值（默认 0.03，0 关闭）
--max-daily-turnover-x  日内换手上限倍数（默认 3.0，0 关闭）
--derisk-floor      主动降险时单腿最小占比（默认 0=可压到平）
--funding-max-ann   资金费率闸门：年化持有成本超此值剔腿（默认 0.30，负值关闭）
--book              账本名（多盘隔离）：空=默认盘；非空则台账落在 data/binance/books/<book>/
--aggr-drift-pct    调仓日历：全组合累计漂移门槛（默认 0.10）
--min-rebalance-interval-min  调仓日历：两次调仓最小间隔分钟（默认 240）
--per-symbol-daily-cap / --per-symbol-flip-cap  单标的日内次数/反转上限（默认 4 / 2）
```

## 多盘隔离（--book）

**场景**：同一台机、同一个 Demo 账户，跑多套独立策略（如低频盘 + 共振盘）。

**机制**（`core/book.py`）：
- 账本名来自 `--book <名>`；**空 = 默认盘 = 历史全局路径，行为不变**。
- 每盘的 决策/心跳/状态/成交/LLM决策/价格历史 落在 `data/binance/books/<book>/`，**零重叠**。
- **每盘自有持仓账** `book_positions.json`：算 delta 只认**本盘发出过的单**，
  不用交易所合计持仓 —— 否则会把别盘的仓当自己的、在别人仓上加加减减（互踩）。
  成交后按 signed_qty 回填自有账。
- **标的池不划分**：各盘都看全部可交易标的。
- 风控计数（调仓/换手/单标的震荡）按各盘台账分别统计。

**注意**：单向持仓模式下，交易所每标的只有一个合计持仓；
但各盘只认自有账，故两盘可并行而互不干扰（合计 = 各盘叠加）。

**已验证**：`tests/test_book_isolation.py`（8 例）；实盘 GOOGL 双盘叠加验证通过。

## 已验证

- [x] 静默路径：无触发时完全不调 LLM（省 token）
- [x] 触发路径：偏离 → 唤醒 LLM → 拿到决策 → dry 执行
- [x] 常驻循环：连续多次 tick 稳定，心跳持续写入
- [x] 开市判断：非开市日自动跳过
- [x] 四道硬闸：逻辑单测通过
- [x] **三道硬风控（回撤/日内亏损/换手）**：`tests/test_risk_gates.py` 9 项单测通过
- [x] **磁盘满不再拖垮交易**：`_append`/`_write_state` 写盘失败降级告警（历史教训：满盘导致进程崩 62 次）
- [x] systemd：active (running)、enabled、内存 ~90M
- [x] 9Router free：`chat()` 调用正常（模型 `free`，base `127.0.0.1:20128/v1`）
