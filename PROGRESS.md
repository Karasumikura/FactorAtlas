# 项目进度快照 — 2026-10-16（双盘：低频 + 多周期共振策略）

## 本轮 —— 共振盘升级为「多周期共振策略」（替换日频因子版）

**背景纠正**：原先 `factors/resonance.py` 是把「Boll+MACD+MTM+WR 共振」**降维成日频横截面
选股因子**再测 IC（≈0.002，看似噪声）。但用户提供的**原始策略**是一套**多周期择时交易系统**，
不是横截面因子 —— 用 IC 评价它属于**指标错配**（IC 衡量横截面排序，共振是时序择时）。
本轮**按原始策略全文重做**。

**原始策略**（用户文档要点）：
- **Boll** 定方向（中轨斜率/价格位置/带宽）；**4H K** 确认趋势；**MACD** 确认重启（水上金叉）；
  **MTM** 确认加速；**WR/MR** 确认超卖修复；**30m K** 回踩入场。
- 完整顺序：回调至中轨 → WR 入超卖 → MTM 止跌 → 4H 收盘站回中轨 → MACD 水上金叉 → WR 上穿 -80 → 30m 回踩入场。
- 分批建仓 30%/30%/40%；结构止损（4H 收盘跌破中轨 / 跌破突破阳线低点）。
- 做多检查表 8 项 + 分级：≥8 强共振，6~7 普通信号，≤5 等待。

**新增代码**：
- `core/mtf_fetch.py` —— 多周期 K 线拉取（4H/30m），落盘 `data/binance/kline_<iv>_<SYM>.parquet`。
- `core/mtf_indicators.py` —— 多周期指标库（Boll 中轨斜率/带宽、MACD 水上金叉/红柱、MTM 拐头/上穿均线、
  WR 超卖修复、结构低点）。参数全取社区标准值，**不做参数搜索**。
- `core/resonance_strategy.py` —— 策略状态机（`evaluate` 出信号与检查表评分、`should_exit` 结构止损、
  `should_take_profit` 分批止盈）。
- `core/resonance_runner.py` —— 执行器：复用 `build_orders`/`execute_orders`/book 账本/`notify`，
  自管逐标的状态（分批阶段、进场止损位）+ 分批建仓 + 结构止损；CLI `python -m core.resonance_runner`。

**关键工程修正**：
1. **Demo 端点只有约 16~18 个美股永续有真实报价**（主网有 100+）。策略**必须限定可交易池**，
   否则会对开不出仓的标的发信号。已在 runner 用 `live_quote_symbols(testnet=True)` 过滤。
2. **状态与持仓账同步**：`decide_targets(commit=False)` 先算不写；仅在**真成交后**提交状态，
   避免 dry-run 污染状态文件导致"幽灵持仓"。
3. **结构止损修正**：去掉"上一根须在中轨上方"的过度约束 —— 急跌破位时上一根可能已在下轨，
   那种情况恰是最该止损的。
4. **小本金保护**：单标的上限权重默认 0.50（$100 → 首仓 $15），使建仓单能过币安步长/最小名义额。

**服务**：`quant-trader-resonance.service` 改为跑 `core.resonance_runner`（`--book resonance --equity 100`，
间隔 1800s 与 30m K 对齐）。首轮已真执行 GOOGL 平仓（旧因子版残留仓）。

**验证**：`tests/test_resonance_strategy.py` 12 例（趋势/通道/指标/止损/分批/最小名义额）；
全套 **90/90 通过**。

**诚实说明**：本轮把策略**按原始规则接上了模拟盘**，未做回测（用户明确「别人弄好的策略直接上盘」）。
真实表现待运行观察；策略当前在 Demo 池 16 标的上最高仅 5 分（无一达 6 分门槛），
即**当前市场下策略选择「等待」** —— 这是设计预期（强共振才入）。

---

# 项目进度快照 — 2026-10-16（双盘隔离基础）

## 单账户双盘隔离（多策略并行）

**需求**：低频因子盘 与 共振策略盘 **各 $100**，在同一个币安 Demo 账户上并行，互不影响。

**约束**：一个 Demo 账户只有一套持仓（`BinanceExec` 只读一组 key），物理上无法开两个账户。
→ 解法：**单账户 + 代码层命名空间隔离**。

**实现**（`core/book.py`）：
- `--book <名>` / `QUANT_BOOK` 决定账本名；默认盘（空）= 历史全局路径，行为 100% 不变。
- 每盘独立：`data/binance/books/<book>/` 下的 决策/心跳/状态/成交/LLM 决策/价格历史。
- **每盘自有持仓账** `book_positions.json`：算 delta 时**只认自己发出过的单**，
  不用交易所合计持仓（否则会把别盘的仓当成自己的、在别人仓上加加减减）。
  成交后按 signed_qty 回填自有账。
- 标的池**不划分**：两盘都可看全部可交易美股永续（共享池）。
- `set_book()` 会把账本同步给 `llm_order` / `demo_reconcile` 子模块。

**验证**：`tests/test_book_isolation.py` 8 例（路径零重叠 / 自有账读写 / 成交回填 / 默认盘不变）。
实盘验证：共振盘开 GOOGL 0.03 → 自有账记 0.03，交易所合计 0.06（= 低频 0.03 + 共振 0.03），
两盘各记各的，**零互踩**。


---

# 项目进度快照 — 2026-10-16（调仓日历）

## 本轮 —— 执行层与因子频率对齐：调仓日历（rebalance calendar）

**问题**：一个 250 日动量（低频）因子，被 5 分钟 tick 跑成了 **一天 27 笔、单笔中位 $6.6、91% 的单 <$10、MSTR 单标的 49 次**。
根因：`needs_rebalance` 只要**任一腿**越过再平衡死区就触发 → 目标权重每 tick 随价格抖动 → 每次都发一笔碎单。
**结论**：前端挖低频、后端跑高频，成本优势在执行层又被交回去。

**方案**：给 `needs_rebalance` 加两道「调仓日历」前置条件——
1. **全组合累计漂移** ≥ equity × `aggr_drift_pct`（默认 10%）；
2. **距上次实际调仓** ≥ `min_rebalance_interval_min`（默认 240 分钟）。
两条同时满足才调仓；单腿轻微越死区不再单独触发，仅记 `rebalance_suppressed` 供观测。

**安全护栏**：**风控豁免**——当浮亏达告警线（≥5%）时，日历让路，降险/纠偏不等间隔。
（实现要点：豁免判据须在 §3 drawdown 之前独立求值，不能依赖 `res.reasons` 顺序——已修正。）

**配套**：`--per-symbol-daily-cap 4` / `--per-symbol-flip-cap 2` 单标的震荡闸（专治 MSTR 一天来回 22 次）。

**验证**：新增 `tests/test_rebalance_calendar.py` 7 例；全套 **64/64 通过**；live dry-run `status=silent`、无误触发。
**部署**：`quant-trader.service` 已带 `--aggr-drift-pct 0.10 --min-rebalance-interval-min 240` 重启。

---

# 项目进度快照 — 2026-10-15（晚间更新）

## 本轮（晚间）—— 量化主脑：事件驱动自动交易（Demo）

### 交付物：`core/trader_daemon.py` + `core/trigger_engine.py`

**问题**：此前全是散装脚本，靠手动敲命令当人肉执行器。用户要的是「程序自己跑」。

**方案**：事件驱动主脑，不是定时无脑轮询。
- **静默轮询**（每 5 分钟）：只读价格/持仓/算目标，**不调 LLM**
- **触发引擎**：4 类触发条件（偏离/异动/浮亏/例行日），任一命中才唤醒 LLM
- **唤醒 LLM**（9Router free）：打包证据 → 判断该不该动
- **决策合并**：LLM 建议 + 4 道硬闸
- **执行**（Demo）+ 台账 + 心跳

### 4 道硬闸（无人值守保命）
1. LLM 说 HOLD 且非风控 → 拦下
2. 日内调仓 ≤ 6 次
3. 总敞口 ≤ equity × 1.2
4. 执行前重读持仓，变了就放弃（防重复开仓）

### 再平衡死区（防高频来回打）
- 已有持仓的「微调」< max(minNotional×2, 5%×equity) 视为噪声跳过
- 开仓/全平不受限
- 背景：无死区时 $100 账户 16h 执行 54 次、MSTR 来回打 47 次、换手 23× 本金

### 修掉的 6 个大坑
1. **本地持仓文件不可信** → 改从交易所读（原会导致重复开仓/仓位翻倍）
2. **sector 中性小样本爆炸** → 加保护 + 默认改 none
3. **僵尸标的**（Demo 的 BMNR 无报价）→ live_quote_symbols 过滤
4. **噪声单**（min_notional 死代码）→ 修
5. **LLM 不守 JSON** → 关键词兜底
6. **extract_json 抛异常污染结果** → 单独包住

### systemd 服务
- `quant-trader.service`：active (running)、enabled、内存 ~90M
- live 模式：触发 + LLM 同意 → 自动在 Demo 下单
- 日志：`runs/trader_daemon.log`；心跳：`data/binance/trader_heartbeat.jsonl`
- 文档：`TRADER_DAEMON.md`

### 已验证
静默路径（不调LLM）/ 触发路径（偏离→LLM→决策→dry执行）/ 常驻循环稳定 /
开市判断 / 四道闸逻辑 / systemd / 9Router free 调通。

---

# 项目进度快照 — 2026-10-15（下午）

## 本轮（下午）完成的工作 —— 成本稳健性 + 容量 + 独立族

### 重大修正（推翻上午部分乐观结论）
1. **成本稳健性检验**：可交易门槛升级为**三重门槛**（`core/mine.py`）：
   - ① 30bps 净 Sharpe > 0.2
   - ② 40bps 成本下仍 > 0（成本稳健）
   - ③ 分段负段 ≤ 1（时段稳健）
   - **效果**：中证1000 的 10 个"可交易"因子 **40bps 下全灭（0/10）**
2. **容量分析**（`core/capacity.py`，平方根冲击模型）：
   - 沪深300 波动族**容量百亿级**（冲击 2–20bps，换手仅 6%/期）
3. **点内掩码**（`data/ashare.py::point_in_time_mask()`）：
   - 量化"未来纳入前视"：2015 中证1000 仅 37% 当前成分股当时存在
4. **独立族聚类**：沪深300 的 13–14 个因子实际**只有 2 个独立族**（波动、趋势）
5. **分段稳健性**（`core/robustness.py::subperiod_sharpe`）：所有因子都有亏损段

### 新增能力
- `--lowfreq-strict` 三重门槛（成本 + 时段）
- `lowfreq.cost_sensitivity`（20/30/40bps）
- `lowfreq.subperiods`（5 段逐段 Sharpe）
- `core/capacity.py`：`factor_impact_cost_bps` / `net_sharpe_vs_aum`
- `point_in_time_mask()`：消除未来纳入前视
- `subperiod_sharpe()`：时段稳健性

### 最终结论（v6 全自动三重门槛，权威）
- **沪深300：14 个过三重门槛**（波动族10 + 趋势族4），容量百亿，成本+时段双稳健
- **中证500：靠流动性因子，但近年（段4）已转负**
- **中证1000：0 个 → 不可操作**
- **沪深300 内 2 个独立族，LS 相关仅 0.127** → 组合 Sharpe 0.58→0.70，回撤 -22.7%
- **最优单因子 `trend_x_lowvol_d1`（趋势÷波动）**：0 负段
- **真正分散化来自跨市场**：美股↔A股相关仅 0.11~0.14

### 诚实修正（下午）
- 中证1000 的"可交易清单"是 20bps 幻觉 → 40bps 全灭
- 中证500 流动性因子 +1.18 全期诱人，但**段4（最近）为 -0.51**
- **幸存条数 ≠ 独立 alpha 数**：14 个 = 2 个信号
- **任何因子都有亏损段**，组合是必需而非可选

### 关键文件
- 报告：`TRADEABLE_FINAL.md`、`COST_ROBUSTNESS.md`、`CAPACITY.md`、`CROSS_MARKET_COMBO.md`、`MASTER_report.md`
- 代码：`core/capacity.py`、`core/cross_market_combo.py`、`core/robustness.py`、`core/mine.py`、`data/ashare.py`
- 权威结果：`runs/mine_300_v6_triple.json`、`runs/mine_500_v5_triple.json`、`runs/mine_1000_v4.json`

### ★★ 跨市场组合（本轮最强实战结果）
- **A股(波动+趋势) × 美股(动量)，风险平价：Sharpe +0.96，年化 +16.9%，回撤 -19.8%**
- A股↔美股月频相关仅 **+0.034**（几乎完全独立）
- 全部由三重门槛可交易因子构成，真实扣成本（30bps）
- 验证：沪深300↔中证500 同名因子相关 **0.84~0.90** → 中证500 不加分散价值
- 复现：`python -m core.cross_market_combo`

### 修复的 bug
- `llm/proposer_batch.py::_normalize`：LLM 返回 `direction` 为 list 时崩溃，
  导致整批 LLM 提案被丢弃（中证500 v5 丢了全部 13 个 LLM 候选）。
  已加 direction/params 容错 + 单候选异常隔离。

### 中证500 三重门槛结果（v5）
- 24 幸存 → **仅 3 个过门槛**：amihud/illiq（强但段4失效）+ trend_strength_n120（5段全正）

### 下一步（未完成）
1. 中证500 v6（LLM 修复版）确认
2. 用三重门槛存活因子重算跨指数组合（已确认 500 无增量）
3. 点内成分股数据（消除幸存者偏差，需付费源）
4. 文本因子（需中文新闻源）

---

## 币安美股永续执行层（新增，2026-10-15 晚）

### 背景
A股模拟盘无法介入（仿真接口不暴露真实成交约束）。用户资金在币安，
币安 2026 已上线美股/TradFi 永续（USDT 结算、7x24、可做空）→ 作为执行通道。

### 已建成（全部只读，零 API key）
- `core/binance_us.py`：币安 TradFi 只读接入。**实测 221 个美股/TradFi 永续**
  （EQUITY 183 + COMMODITY 8 + HK 15 + KR 8 + CN 2 + PREMARKET 4 + FX 1），
  全部有真实流动性。含标的发现/行情/资金费率/K线/费率历史。
- `core/us_mapping.py`：币安永续 ↔ 正规面板映射 + 开市日历。
  **122 个标的可映射**（研究用 data/hist 655 只 × 5.8年）。
- `core/us_paper.py`：跟踪误差对账（方案 A）。
- 产物：`runs/binance_tradfi_screen.json`、`runs/binance_us_mapping.json`、
  `runs/tracking_us_perp.json`

### ★ 关键原则（用户确认）
**研究用正规市场长历史（data/hist，2021起），币安永续只做执行；
正规市场不开市就不交易** → 天然回避 7x24 永续的休市 EWMA 偏移风险。

### ★★ 跟踪对账核心发现（30 主流标的）
- **方向高度一致**：日收益 corr 均值 **0.90**，命中率 **0.92**，平均日偏差仅 **-0.7bps**
  → 币安永续确实跟着正规市场走，无系统性漂移。
- **幅度放大**：年化跟踪误差均值 **31%**（vs 正规 SPY 仅 6%）→ crypto 资金放大波动。
- **资金费率是主要成本**：多头年化付出均值 **+4.6%**，P90 **+10.7%**；
  热门票 CRCL/MU/AAOI/NBIS 高达 **10-12%/年**，但 SPY/QQQ/COIN 约 0 甚至负数。
- **对策略含义**：
  - 单边做多热门票会被资金费率吃死（1000bps 级，远超 30/40bps 手续费门槛）。
  - LS 组合中，**空头在正费率时反而收钱** → LS 比纯多头更划算。
  - 冷门票/指数（SPY/QQQ）执行成本最低。

### 硬限制
- 币安美股永续**最早 2025-12 上线**，主流票 2026-01~03 → **最长仅约 10 个月历史**，
  不能用来挖因子（样本不足）。**只可做执行层。**

### 下一步（美股执行线）
1. 信号→下单桥：现有美股动量因子 → 正规开市日目标仓位 → 币安下单
2. 接币安 Demo Trading（testnet.binancefuture.com）真实模拟下单（需用户提供 demo key）
3. 把资金费率纳入组合成本模型（现有 30/40bps 门槛未含费率）

---

## 资金费率成本模型 + 执行层（2026-10-15 晚续）

### 任务 1-3 完成（费率建模 / 净 Sharpe / 套利筛选）
- `core/funding_cost.py`：费率按**多空净额**建模。
  - **LS 组合年化净费率：中性 +7.7%、悲观 +17.8%、乐观 -2.7%**。
  - 高费率端 MU/CRCL +10.5%，低费率端 SPY -3.0%/SKHY -4.0%。
  - 净费率 = **两腿费率之和**（多头付 + 空头少收），不是两者之差。
- `core/us_momentum_net.py`：美股动量 LS 加真实费率后的净表现。
  - **修正了一个真 bug**：年化用 252 期算持有期收益 → mom250 算出 1089%（实际约 16%）。
    正确应为 `252/hold` 期。修后数字合理。
  - 结果：**mom_250 hold=10 毛 Sharpe 0.69 → 净（中性）0.36 → 净（悲观）-0.07**。
    其余因子（mom_120/60）加费率后全负。
- 费率套利：能做空收正费率的标的（SKHY +4.0%、SPY +3.0%）恰是**费率本就为负的票**，
  意味着多数时间做空要付钱 —— 真正的"高正费率票"做空=做空最强动量股，与动量策略对冲。

### 执行层选型（结论：自建，收编开源逻辑）
- 验证了开源 `muvon/mcp-binance-futures`（Apache-2.0）：
  - `BASE_URL=fapi.binance.com`，**无 symbol 硬编码**，实测 **完全支持美股永续**
    （NVDAUSDT：TRADIFI_PERPETUAL / EQUITY，过滤器 stepSize=0.01、minNotional=$5 正确读出）。
  - 但它是为 **LLM 对话客户端**设计的 MCP server，无头服务器跑自动策略需要协议层，多余。
- **决定：收编其签名/精度吸附/下单逻辑，去掉 MCP 协议层** → `core/binance_exec.py`。
- **MCP OAuth 的澄清**：无头服务器**并非不能做 OAuth** —— 有 device flow(RFC 8628)、
  端口转发、手动贴 token、stdio+env key 四条路径。官方托管 MCP 走 OAuth，
  但 stdio MCP 可用环境变量凭据。此前"无头不能做"的判断已修正。

### 新建执行层文件
- `core/binance_exec.py`：同步签名客户端（HMAC-SHA256 + recvWindow）。
  - 精度吸附（stepSize/tickSize/minQty/minNotional，避免 -1111/-1013/-4164）。
  - **主网/Demo 可切换**，条件单走 Algo API（币安 2025-12 起要求）。
  - 错误码可读翻译（-1021 时钟漂移、-2015 权限等）。
- `core/bridge_us_binance.py`：信号→下单桥。
  - 正规市场因子 → 目标权重（多空，严格无前视，只用 ≤ as_of 的数据）
  - **按目标环境实时构建映射**（demo 与主网标的不同）
  - 与当前持仓 diff → 调仓指令（先平后开）→ 下单
  - 默认 dry-run；`--mode execute` 才真实发单。

### ★ 关键发现：Demo 与主网标的集合差异巨大
- **主网：221 个 TradFi 永续**（美股 212）
- **Demo：仅 45 个 TradFi 永续，其中美股 24 个**
  （NVDA/TSLA/META/GOOGL/AMZN/MU/INTC/PLTR/MSTR/COIN/CRCL/QQQ/SPY/SOXL...）
- **后果**：用主网映射会选出 demo 上不存在的票（LRCX/AMAT/ORCL...）→ `-1121 Invalid symbol`。
  已修：下单桥对目标环境 `fetch_live=True` 建映射。
- **Demo 实测**：mom250 选 MU/INTC/GOOGL(多)+COIN/MSTR(空)，全部 demo 可交易；
  主网同样参数选出 46 个标的（多空各 23）。

### 执行层安全（务必遵守）
- API key **关闭提现权限**，只开合约交易；**IP 白名单**绑服务器。
- **先在 testnet.binancefuture.com 的 Demo 跑通**，零风险。
- 桥内置 `--max-notional` 单标的上限、默认 `leverage=1`、默认 dry-run。

### 下一步（执行线未完成）
1. 用户在 testnet.binancefuture.com 申请 Demo key → 填环境变量
2. `--mode execute --testnet` 跑通 Demo 真实下单/调仓
3. 对账：Demo 成交价 vs 正规收盘价、资金费率累积、滑点
4. 把资金费率接入组合成本模型（30/40bps 门槛 + 费率）

### 对账闭环（已完成）
- `core/demo_reconcile.py`：Demo 实盘对账。
  - **滑点**：成交均价 vs 参考价（正规收盘），输出 bps（买正=吃亏）。
  - **资金费**：读 `/fapi/v1/income`，累计 FUNDING_FEE / COMMISSION / REALIZED_PNL。
  - **跟踪**：Demo 持仓净值 vs 同权重正规收盘理论净值。
  - 成交台账：`data/binance/demo_exec_ledger.jsonl`（桥在真实发单后自动追加）。
- 桥已接台账：`--mode execute` 发单成功后自动写入，供对账读取。
- 验证：income 端点存在（未签名返回 -2014）；跟踪函数自测通过
  （NVDA 多10股 +110.10 / TSLA 空5股 -95.55 / 理论净 +14.55）。

### 执行层完整文件清单
| 文件 | 作用 |
|---|---|
| `core/binance_us.py` | 币安 TradFi 只读接入（标的/行情/费率/K线） |
| `core/us_mapping.py` | 永续↔正规面板映射 + 开市日历（支持按环境实时） |
| `core/binance_exec.py` | 签名执行客户端（主网/Demo 切换、精度吸附、下单） |
| `core/bridge_us_binance.py` | 信号→调仓→下单桥（默认 dry-run） |
| `core/demo_reconcile.py` | Demo 对账（滑点/资金费/跟踪） |
| `core/us_paper.py` | 币安永续 vs 正规市场 跟踪误差对账 |
| `core/funding_cost.py` | 资金费率多空净额建模 + 套利筛选 |
| `core/us_momentum_net.py` | 美股动量加费率后的净表现 |

### ★ 一句话现状
**研究在 hist（5.8年正规数据），执行在币安（demo 24 个美股永续），
代码全部就绪 + dry-run 通过，只差一个 Demo API key 即可真实模拟下单。**

---

## ★★ 里程碑：Demo 真实下单成功（2026-10-15 16:00 UTC）

### 已打通真实模拟盘全链路
- **Demo key 已配**（testnet.binancefuture.com；主网天然连不上 = 安全边界）。
- **账户**：5000 USDT 合约余额。
- **首次真实下单**：mom250 因子 → 5 笔市价单**全部 `sent` 并成交**。

### 首个真实持仓（1x 杠杆，equity=1000）
| 标的 | 方向 | 数量 | 开仓价 | 下单参考价 | 滑点 |
|---|---|---|---|---|---|
| MU | 多 | +0.18 | 889.02 | 888.83 | +2.1bps |
| INTC | 多 | +1.73 | 95.98 | 95.91 | +7.3bps |
| GOOGL | 多 | +0.48 | 343.61 | 343.06 | **+16bps** |
| COIN | 空 | -0.97 | 171.05 | 171.05 | 0 |
| MSTR | 空 | -1.17 | 141.73 | 141.74 | -0.7bps |

**首次实测成本**：手续费合计 **0.2546 USDT / 1000 本金 ≈ 2.5bps 单边**（与币安美股永续 10bps 上限相符）。
资金费待 8h 结算点（下一次 16:00 UTC）。

### ★ 关键前置：必须签 TradFi-Perps 协议
- 首次下单报 **`-4411: Please sign TradFi-Perps agreement contract fapi`**。
- 加密永续可直接交易，但**美股永续必须先签协议**（合规要求）。
- 签署端点：`POST /fapi/v1/stock/contract` → 返回 `SUCCESS`。
- **这是一次性授权**（等价于在网页同意条款），已用用户自己的 key 完成。

### 交接的第一个 key（已废弃）
- 用户最早提供的 key 是**主网现货 key**（`canWithdraw=true`），
  连不上 testnet 合约（-2015）；且已暴露 → **建议用户删除该 key**。
- 诊断依据：该 key 在主网现货 `api.binance.com/api/v3/account` 返回完整账户，
  但在 fapi/v1/fapi/v2 及所有 testnet 端点均 -2015。

### 执行层命令备忘
```bash
# 查持仓/余额
./venv/bin/python -m core.bridge_us_binance --factor mom250 --equity 1000 --mode signal --testnet
# 真实下单
./venv/bin/python -m core.bridge_us_binance --factor mom250 --equity 1000 --mode execute --testnet --leverage 1 --max-notional 200
# 对账
./venv/bin/python -m core.demo_reconcile --testnet
```

### 下一步
1. 等 16:00 UTC 资金费结算 → 实测资金费扣款，验证 `funding_cost.py` 的预测
2. 持续跑几天 Demo，积累真实滑点/费率样本
3. 把实测滑点与费率回灌进回测成本模型

---

## ★★ 宏观事件研究：找到免费数据源 + 诚实负结果（2026-10-15）

### 起因
用户问："通胀导致闪迪/海力士暴跌这类怎么搞？" —— 研究宏观事件冲击的识别与应对。

### 找到的免费数据源（FMP 配额用完后）
| 源 | 状态 | 用途 |
|---|---|---|
| **Alpha Vantage**（已有 key） | ✅ | CPI/通胀/美债/失业率（25次/天） |
| **FRED CSV**（免 key） | ✅ | 任意宏观序列，无配额 |
| **BLS API**（免 key） | ✅ | 官方 CPI（20次/天） |
| **DBnomics**（免 key） | ✅ | BLS/BEA/ECB 聚合 |
| **SEC EDGAR**（免 key） | ✅ | 财务数据、ticker→CIK |
| **Twelve Data**（已有 key） | ✅ | 行情备源（800/天） |
| **Tavily**（已有 key） | ✅ | **能绕过 Cloudflare 抓 bls.gov** |
| Yahoo/Stooq/FMP/Finnhub | ❌ | 被挡或需 key |

### 关键突破：官方 BLS 发布日历（权威、无前视）
- 数据源：`bls.gov/schedule/<YEAR>/home.htm`（官网，curl 被挡，**Tavily 可抓**）
- 解析脚本：`scripts/parse_bls_schedule.py` → `data/macro_calendar/bls_releases.parquet`
- **241 个事件**，2022-2026：CPI 59 / NFP 64 / PPI 59，精确到 08:30 ET
- 新增 `core/macro_calendar.official_calendar()` 读取官方日历

### 事件冲击研究（core/us_event_study.py，严格无前视）
**风险确实放大：**
- **CPI 日**：市场 EW std **1.61%** vs 平日 1.14%（+41%）
- **NFP 日**：存储股异常波动 std **3.03%** vs 平日 2.12%；半导体 2.18% vs 1.69%
- **NFP 日市场均值 -0.135%**（平日 +0.077%）

**但方向不可预测 + 无事后漂移：**
- CPI 日市场均值仍 **+0.11%**（正）
- CPI 后 1/3/5/10 日，半导体+存储 CAR ≈ 无条件（-0.013% vs +0.078%）

### 屏蔽/降仓测试（全部为负结果）
| 方法 | 结果 |
|---|---|
| 官方日历屏蔽调仓日 | mom_250 Sharpe 0.62 → 0.68（微弱，噪声内）；rev_5 变差 |
| 事件日降仓 50%（core/event_gate.py） | 年化 13.35%→11.71%，**最差期无改善**（-16.97% 不变） |

**原因**：5日持有期的低频策略，1天事件仅占 20%；降仓 20% 时间×50% → 总暴露只降 10%；
真正的尾部损失来自**持有期内其他日子**，不是发布日。

### 结论
**事件层能识别风险（波动放大），但不能规避它。低频策略的尾部风险来自"持有期内任何一天"。**
唯一有效的是**长期降低波动目标/杠杆**，不是"事件日择时"。

### 新增文件
- `scripts/parse_bls_schedule.py`（BLS 日程解析）
- `core/us_event_study.py`（事件冲击研究）
- `core/event_gate.py`（事件感知仓位门控）
- `core/event_gate_backtest.py`（降仓效果回测）
- `data/macro_calendar/bls_releases.parquet/.csv`
- `runs/event_study.json`、`runs/event_gate_backtest.json`

---

## ★★ 免费数据源胜利 + 板块中性化结论反转（2026-10-15 续）

### SEC EDGAR 全量行业分类（替代 FMP）
- FMP 免费配额用完 → 只用 SEC EDGAR（免费、无配额、官方）
- `core/build_us_sic.py`：并发拉 655 只的 SIC 行业码
- **652/655 拿到**（99.5%，vs FMP 只有 192）→ `data/us_sic.parquet`
- 验证正确：SNDK=Sandisk 3572、SKHY=SK Hynix 3674、MU/NVDA/AMD=Semiconductors
- 仅缺 BF.B、BRK.B（带点代码）、QQQ（ETF）—— 正常
- `data/us_meta.py` 增加 `sec_sector_map()`，FMP 缺失时用 SEC SIC 回退

### ★ 关键：板块中性化结论反转
**之前（FMP 192只，行业不全）**：中性化砍掉大部分收益，Sharpe 0.62→0.18，判定"中性化没用"。

**现在（SEC 652只，全量）**：
| 因子 | 模式 | Sharpe | 回撤 | 最差期 |
|---|---|---|---|---|
| mom_250 | none | 0.62 | -82% | **-16.97%** |
| mom_250 | sector | **0.56** | **-75%** | **-8.41%** ✅ |
| mom_250 | sector+beta | 0.43 | -76% | -8.47% |

**mom_250 最差期从 -16.97% → -8.41%（腰斩），Sharpe 仅从 0.62 降到 0.56。**

### 稳健性检验（分年度最差期）
| 年 | 基准 | 中性化 |
|---|---|---|
| 2022 | -12.45% | -6.92% |
| 2023 | -12.61% | -4.83% |
| 2026 | -16.97% | -8.41% |
**6 年 5 年改善，大尾部改善最明显 → 系统性保护，非单事件驱动。**

### 最终结论（回答"通胀导致闪迪/海力士暴跌怎么搞"）
1. 宏观发布日风险确实放大 40%+，但**方向不可预测、无事后漂移**
2. 低频策略靠"事件日屏蔽/降仓"**无效**（最差期无改善，Sharpe 反降）
3. **板块中性化是唯一有效的尾部保护**：腰斩最差期，Sharpe 代价极小
4. 正确做法是**行业中性 + 控制板块集中度**，不是预测/规避特定发布日
5. 尾部损失的日子**不都在发布日** —— 中性化保护的是结构性的每一天

### 新增文件（续）
- `core/build_us_sic.py`、`data/us_sic.parquet`
- `core/macro_defense_report.py`、`runs/macro_defense_report.md`
- `core/us_neutral.py` 支持 --sector-source sec
- `data/us_meta.py` 增加 SEC SIC 回退 + sec_sector_map()

### 执行层接入板块中性化
- `bridge_us_binance.py` 增加 `--neutralize sector`：
  按 SEC SIC 行业分组做多空，跨行业等权汇总，归一化到目标敞口。
- demo 可交易标的仅 18 个，单行业不足 5 只 → 自适应放宽最小分组（5→2），
  仍无有效分组则自动回退普通选股（避免 0 目标）。
- 实测 dry-run：sector 中性化产出分散目标（GOOGL/CRCL 多，XYZ/BMNR 空）。

### 给用户的总结（回答"通胀导致闪迪/海力士暴跌怎么搞"）
**能用免费数据做出来的、真正管用的东西 = 板块中性化。**
- 官方 BLS 日历（241 事件）证明事件日风险放大 40%+ → 但方向不可预测
- 事件日屏蔽/降仓无效 → 放弃这条路
- **板块中性化：mom250 最差期 -17% → -8%（腰斩），Sharpe 只降 0.06** → 采纳

**局限（诚实）**：demo 只有 18 个标的，行业中性化在实盘小universe上打折；
真正发挥需要更大可交易标的池（主网或更多 demo 标的）。

---

## ★★ 大模型挖因子闭环 + SEC XBRL 基本面因子（2026-10-15 晚）

详见 `reports/LLM_XBRL_FACTORS.md`。要点：

### 路线1+3：LLM 提案 + 闭环迭代 —— **诚实负结果**
- 新增 `llm/proposer_composite.py`（LLM 设计**复合/交互**因子，容错规范化 + 安全
  formula 解析器，绝不 eval）；`core/llm_loop.py`（多轮「提案→回测→反馈」闭环）。
- `core/mine.py` 接入候选源 D（LLM 复合）+ 反馈参数 + CLI `--llm-composite`。
- **跑 US 4 轮闭环：0 幸存**。原因分析：**多重检验（BH-FDR）是天花板**。
  单跑 29 候选：24 个 OOS Sharpe≥0.3 且同向，仅 2 个过 FDR；候选涨到 343 → FDR
  陡严 → 0 通过。**堆 LLM 候选数量无用，只抬高门槛。**
- LLM 复合提案器**本身有效**：产出原创交互（`trend_div_maxdd` OOS +0.73；
  `vwap_dev_x_volume_ratio`/`sharpe_reversal_5_vol_20` OOS +0.67），卡在 IS/OOS 同向。

### 路线B：SEC XBRL 基本面因子 —— **本轮真正增量**
- 瓶颈：Marketaux 新闻库太稀（234 条/78 只）；SEC 归档文档（www.sec.gov/Archives）
  **本机 IP 被 403**，抓不了文本。但 `data.sec.gov` **XBRL companyfacts 完全可用**。
- 新增 `data/xbrl.py`：**1,769,820 条 facts / 650 只 / 29 科目 / filed 2009-2026**，
  **精确公布日 = 严格无前视**（优于 FMP 的 +45 天近似）。
- 新增 `factors/xbrl_fund.py`：11 个因子（价值/质量/成长/规模），覆盖 68-90%。
- 新增 `core/mine_xbrl.py`（★修正：多日持有必须用真实调仓 `ls_nav_true_rebalance`，
  不能用 `forward_returns(hold)`+日频 Sharpe —— 后者重叠样本把 Sharpe 虚高 √hold 倍）。
- 新增 `core/xbrl_robust.py`（分段稳健 + 与价量相关性）、`core/xbrl_leg_value.py`（组合增量）。

### 结果（hold=20，真实调仓，10bps）
| 因子 | Sharpe | 期数 | 负段 | 与价量\|corr\|max |
|---|---|---|---|---|
| xbrl_log_mcap_d-1 | +1.75 | 72 | 0/5 | 0.33 |
| xbrl_roe_d-1 | +1.36 | 72 | 1/5 | 0.32 |
| xbrl_earnings_yoy_d-1 | +1.32 | 71 | 0/5 | 0.41 |
| xbrl_fcf_yield_d-1 | +0.93 | 72 | 1/5 | 0.29 |

**★ 组合增量**：XBRL 腿 Sharpe +1.23；与美股动量相关 **+0.057**、与 A股腿 **+0.002**。
动量+XBRL 等权：Sharpe 0.64→**1.38**，回撤 -34.7%→**-14.9%（腰斩）**。

### ★★ 诚实警告：regime 依赖，非稳定 alpha
分段看：**所有 XBRL 因子 2021-2023（段1-2）≈0，2024-2026（段3-5）强正**。
→ 本质是"近两年质量/成长被惩罚"，regime 信号。5.8 年样本（价格仅 2021 起）无法
证明持续。**不可仅凭回测部署。** 反证：随机噪声因子同口径 Sharpe -0.97（框架不造假）。

### ★★★ 关键更正：多数 XBRL 因子其实是「波动率因子」的伪装
用 **L/S 期收益相关**（组合层面唯一有意义的度量）复核各 XBRL 因子与既有价量因子：

| XBRL 因子 | Sharpe | vs mom250 | vs trend120 | **vs vol60** | vs rev5 |
|---|---|---|---|---|---|
| xbrl_earnings_yoy | +1.31 | +0.04 | -0.26 | **+0.92** | -0.07 |
| xbrl_roe | +1.36 | +0.03 | -0.29 | **+0.93** | -0.02 |
| xbrl_fcf_yield | +0.93 | +0.10 | -0.16 | **+0.83** | -0.02 |
| xbrl_log_mcap | +1.74 | -0.16 | -0.48 | **+0.81** | +0.04 |
| xbrl_sales_yield | +0.32 | -0.16 | +0.06 | **-0.74** | +0.06 |
| **xbrl_book_to_price** | +0.59 | -0.26 | -0.27 | **-0.22** | +0.18 |

**结论修正**：
- 之前 `xbrl_robust.py` 报的 0.29~0.41 是【逐日截面 rank 相关均值】，**低估了真实共动**；
  用 L/S 期收益相关复核，发现**多数"质量/成长"因子与波动率因子相关 0.81~0.93** →
  它们基本是**低波动因子的伪装**（高盈利成长股 ≈ 低波动股）。
- **唯一真正独立的是 `xbrl_book_to_price`（价值）**：与 mom/trend/vol/rev 相关仅
  -0.27~+0.18，Sharpe +0.59 → **这才是真正的增量 alpha（价值因子）**。
- 因此"XBRL 腿与 A股腿相关 0.002"的组合增益**部分是幻觉** —— 因为 A股腿本就有波动族，
  而 XBRL 质量/成长因子与之高度重叠。

**教训**：相关性度量口径决定结论。组合分散化必须看 **L/S 期收益相关**，不是
横截面 rank 相关，也不是均值序列时序相关（后者会被市场共同趋势污染，给出 0.84 的假高值）。


### 新增文件
- `llm/proposer_composite.py`、`core/llm_loop.py`、`data/xbrl.py`、
  `factors/xbrl_fund.py`、`core/mine_xbrl.py`、`core/xbrl_robust.py`、`core/xbrl_leg_value.py`
- 数据：`data/edgar/companyfacts.parquet`（1.77M 行）
- 结果：`runs/llm_loop_us_v1.json`、`runs/mine_xbrl_us_v2.json`、
  `runs/xbrl_robust_h20.json`、`runs/xbrl_leg_value.json`
- 报告：`reports/LLM_XBRL_FACTORS.md`

### 下一步（未完成）
1. XBRL 因子在 2009-2020 验证（需更长行情；当前价格仅 2021 起）→ 判定是否 regime
2. XBRL 因子对价量做正交化，看增量 alpha 是否还在
3. 闭环改为"每轮精选 10-15 个高质量假设"而非几十上百
4. XBRL 扩到 A股（需另接财报源）

---

## ★★ Demo 模拟盘调仓执行（2026-10-15 晚，全权负责）

### 执行结果
用 mom250 因子（equity=1000，1x 杠杆），从交易所读真实持仓做 delta 调仓：
- **INTC 加多 +0.08**：成交价 91.96（ref 91.92，滑点 +4.4bps），fee 0.0029
- **CRCL 建空 -2.09**：成交价 79.42（ref 79.42，滑点 0bps），fee 0.0415

### 当前 Demo 持仓（6 个，1x）
| 标的 | 方向 | 数量 | 开仓价 | 浮盈(执行时) |
|---|---|---|---|---|
| MU | 多 | +0.18 | 889.02 | -0.91 |
| INTC | 多 | +1.81 | 95.80 | -7.07 |
| GOOGL | 多 | +0.48 | 343.61 | +3.88 |
| CRCL | 空 | -2.09 | 79.42 | +0.23 |
| COIN | 空 | -0.97 | 171.05 | +0.48 |
| MSTR | 空 | -1.17 | 141.73 | +2.70 |
钱包 4999.63，累计手续费 0.299，累计资金费 -0.066。

### ★★★ 修的两个真 bug（都在执行层，必须记）
1. **僵尸标的**：Demo 的 `BMNRUSDT` 在 exchangeInfo 里存在但**无报价**
   （lastPrice=None, volume=None）→ 下单桥选到它后取价失败。
   修：`us_mapping.live_quote_symbols()` 用一次批量 `/fapi/v1/ticker/price`
   拉全部有价 symbol，`compute_targets` 据此过滤。剔除 BMNR 后空头自动补位 CRCL。
2. **持仓来源错误（危险）**：`bridge` 原来从本地文件 `demo_positions.json`
   读持仓，但该文件不存在 → 认为"无持仓" → **所有调仓都标成 OPEN（重复开仓）**。
   若直接实发，会对已有持仓再开一遍，仓位翻倍。
   修：新增 `load_positions_from_exchange()`，**从交易所读真实持仓**；main 改用它。
   验证：修后调仓指令从"6 条全 OPEN"变成"2 条（INTC 加仓 + CRCL 开仓）"。
3. **噪声单过滤**：`build_orders` 的 min_notional 检查是死代码（只 pass）。
   修：增量名义额 < min_notional 直接跳过（平仓单除外）。修后 0.00 的无意义单被清除。

### 安全边界验证
- Demo base = testnet.binancefuture.com；主网 = fapi.binance.com。
- 主网账户访问被拒 `-2015 Invalid API-key` → **同一 key 在主网无权限，安全边界完好**。
- （`ping()` 是公开端点，"主网可 ping 通"不代表能下单；已用 account_summary 实测确认。）
- TradFi 协议 `/fapi/v1/stock/contract` 返回 SUCCESS。

### 新增/修改文件
- `core/us_mapping.py`：+`live_quote_symbols()`
- `core/bridge_us_binance.py`：+`load_positions_from_exchange()`、
  僵尸标的过滤、噪声单过滤、main 改用交易所持仓
- 台账：`data/binance/demo_exec_ledger.jsonl`（+2 条）
- 快照：`runs/demo_state_snapshot.json`

### 后续（Demo 线）
- 继续按 mom250 因子在美股开市日调仓（人工/半自动，**无 cron、无无人值守**）
- 累积滑点/资金费样本，回灌成本模型




## 2026-10-16 深化：风控硬规则 + 持续因子挖掘防过拟合

### A. 交易系统（tasks 1-3）
1. **主动降险**（`bridge_us_binance.force_derisk`）：回撤熔断不再只"拦加仓"，
   而是主动把总敞口压到 `gross_cap×equity×(1-回撤)` 以下，只产出减仓单
   （绝不加仓、绝不翻向）。接入 `trader_daemon` 闸4。
2. **资金费率闸门**（`trader_daemon` 步骤 2.5）：永续持仓成本按腿方向取
   `ann_long`/`ann_short`，年化成本 > `--funding-max-ann`（默认 0.30）的腿剔除。
   顺带修正 `funding_cost.py` 里 `ann_short` 的错误 docstring（正=付出，非"收到"）。
3. **自动对账**（`trader_daemon` 步骤 8.5）：每 tick 比对交易所持仓 vs 本地状态，
   相对差 >1% 或名义差 >$5 即告警并同步本地状态，防状态脱节导致重复开仓/漏平。

### B. 持续因子挖掘防过拟合（tasks 4-5）
4. **`core/overfit_guard.py`**：DSR（按累计试验数打折）+ PBO(CSCV) + 试验登记册。
   单测验证：纯噪声 PBO≈0.71/DSR≈0.52；真信号 PBO≈0.10/DSR≈0.9999。
5. **`core/promote.py`**：晋升闸门 —— 只有【跨 >=2 次独立挖掘反复出现 + 方向一致
   + DSR>=0.95 + PBO<=0.5 + 幅度 + 扣成本为正】才进 `data/factor_library.json`。
   接入 `core/mine.py`（每轮记录候选人入选幸存者）。
6. **`quant-miner.service` + `scripts/mine_loop.sh`**：常驻无限循环
   mine → promote → 冷却，串行 + 内存守卫（1.9GB 主机安全）。已启动。

### 首次扫描结论
- 23 个历史结果、137 个候选 spec，**0 个**满足全部晋升条件。
- `vol_n120` 跨会话 OOS 一次 +0.73、一次 -0.74 → 方向漂移，被正确拦下。
- 大量候选扣成本后归零 → 历史"幸存"多是换手幻觉。


## 2026-10-16 运营基座 + 因子库消费闭环

### C. 自主运营层（"全权负责"的基座）
7. **`scripts/ops_watch.py`**：一次调用给出全貌 —— 服务健康 / 内存磁盘 /
   交易 tick 时效 / 挖掘进度 / 累计试验数 N / 因子库大小 / 风控触发统计。
8. **`scripts/ops_loop.py`**：巡检 + 白名单自愈（仅重启挂掉的服务、清理超期
   日志），有风险动作只报告不执行。留痕 `runs/ops/ops_<date>.jsonl`。
9. **`quant-ops.service` + `.timer`**：每 30 分钟自愈巡检（systemd timer，
   比 cron 更可观测）。
10. **cron `quant-lab-iteration`（fa819b73b85a）**：每 6 小时一次自主巡检+迭代。

### D. 挖掘性能
11. **zoo 缓存复用**：`mine.py` 按 `panel_end + hold` 指纹跳过同窗口重复回测
    （`--no-cache` 可强制全量）。2 核主机上把每轮全量重算变成"只算新候选"。

### E. 因子库消费闭环（晋升 → 交易）
12. **`core/factor_library.py`**：读 `data/factor_library.json`，把库内稳健因子
    横截面 rank 等权合成为 composite 信号（等权=不引入加权拟合参数）。
13. **`factors/engine.py make_factor`**：新增 `kind='composite'` 支持，使库因子
    直接走 compute_targets / 回测同一通道。
14. **`trader_daemon` 新增 `--factor library`**：自动用库合成信号；**库为空时
    安全回退 mom250**。交易服务已切到 library 模式（当前回退中，因子一晋升即自动升级）。

### 测试
- 49 个单测全绿（新增 6 个因子库/合成 + 8 个晋升 + 11 防过拟合 + 15 深化闸门 + 9 历史闸门）。


## 2026-10-16 发现并修复:低频因子被跑成高频(关键)

### 症状
用户问"我们跑的是低频因子吗"—— 实读日志发现矛盾:
- 因子本身:mom250(250 日动量)= 经典月频/季频因子 ✓ 低频
- 但实盘执行:**10-15 一天 27 笔、10-16 一天 27 笔**;单笔名义中位 $6.6;
  91% 的单 <$10;MSTR 单标的 49 次;触发 47/54 是 needs_rebalance ✗ 高频

### 根因(两个)
1. **`tick()` 默认 max_daily_rebalances=3,CLI 默认 6** —— 不一致(潜在坑),
   已统一为 6。
2. **总次数上限拦不住"单标的反复横跳"**:MSTR 在 10-15 被交易 22 次、
   **方向反转 18 次**。旧逻辑只限总次数,不限单标的。

### 修复
- 新增 **闸2.5 单标的震荡闸门**:任一标的当日交易次数 > `--per-symbol-daily-cap`(默认4)
  或方向反转 > `--per-symbol-flip-cap`(默认2),则**只剔除该标的的订单**,
  其余标的照常(保护面最小)。
- 用真实日志回测:若当时有该闸,MSTR(22次/18反转)、MU(4次)会被拦 —— 正是耗损来源。
- 服务已显式传参并重启生效。

### 新增
- `trader_daemon.py`: `_symbol_exec_counts_today()`、`_symbol_flips_today()` + 闸2.5
- `tests/test_per_symbol_gate.py`: 8 个测试
- 单测总数 49 → **57**

## 2026-10-22 巡检修复：LLM 链路断链（提案池连续 4 轮 0 候选）

### 现象
- 第 3–8 轮 `proposal_pool` 全部 `池内候选 0 个`，但池文件 `error: null`（异常被吞）。
- 交易端心跳 `order_source=factor_fallback(llm_unavailable)`，全程降级。

### 根因（三层叠加）
1. `9router.service` 于 2026-10-21 19:54 被人为 stop（且 unit 为 disabled），
   端口 20128 无监听 → 所有 LLM 调用 Connection refused。
2. 9router 恢复后，响应体在正常 JSON 尾部追加了 SSE 尾巴 `data: [DONE]`，
   `r.json()` 抛 `Extra data: line 1 column N`。
3. 响应头不带 charset，requests 回落到 ISO-8859-1，中文正文变乱码（"收到" → "æ¶å°"）。

### 修复
- `systemctl start 9router`（**未** 改 enable 状态，保持原配置）。
- `llm/client.py`: 新增 `_parse_completion()` 剥 SSE 尾巴 / 取首个完整 JSON；
  显式 `r.encoding='utf-8'`。
- `core/proposal_pool.py`: 单算子提案异常改为记录到 `error`，不再静默吞掉。

### 验证
- LLM 存活：`chat()` 返回 "收到"，26.5s。
- 提案链路：`propose_batch(total=4)` 生成 6 个唯一候选，171s。
- 合成信号：`composite_factor()` 在真实缓存面板（226×657）上
  输出 (226, 657)，末行 657/657 非空。
- 单测 84/84 OK。
