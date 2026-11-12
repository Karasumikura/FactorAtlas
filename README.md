# FactorAtlas

**用大模型挖因子，用多重检验防止自欺。**

一个可复现的量化研究框架：LLM 提出因子假设，程序做回测验证，BH-FDR 校正
对抗「挖得越多、假阳性越多」的问题。全部跑在**免费数据源**上，适合入门和
个人研究。

---

## 为什么做这个

LLM 很会编故事。你让它提一个因子，它能给你一段听起来无懈可击的经济学逻辑，
然后这个因子在样本外一文不值。

**这个项目的全部价值在于「怎么不骗自己」**，而不是「怎么挖更多因子」：

| 问题 | 做法 |
|---|---|
| 挖得越多越容易撞上假阳性 | 所有尝试汇入**同一个** BH-FDR 池，挖越多门槛越严 |
| 用 IC 筛因子会选出一堆假货 | 幸存判定用**已对冲 beta 的多空组合 Sharpe**，不用横截面 IC |
| 样本内漂亮、样本外崩 | 前 70% 训练 / 后 30% 只评估**一次** |
| 高收益掩盖高回撤 | 净 Sharpe 必须配**最大回撤** |
| A 股日频被成本碾碎 | 用真实调仓法做**降频检查**，加 `--lowfreq` 自动执行 |
| 因子间高度相关却各算一个 | 用**多空收益相关**判独立性，不用因子值 rank 均值 |

详见 [`OVERFIT_GUARD.md`](OVERFIT_GUARD.md)。

---

## 免费数据源

整套框架不依赖任何付费行情。入门门槛极低，个人也能跑起来：

| 数据源 | 用途 | 免费额度 |
|---|---|---|
| [Alpaca](https://alpaca.markets/) | 美股日线 OHLCV | 免费，够跑几百只票多年历史 |
| [FMP](https://financialmodelingprep.com/) | 美股基本面、行业分类 | 免费档有限，够做年度基本面因子 |
| [Marketaux](https://www.marketaux.com/) | 个股新闻 + 实体情绪分 | 100 请求/天 |
| [Binance](https://www.binance.com/) | 加密 K 线、资金费率 | 公共接口免费 |
| [Tavily](https://tavily.com/) | 宏观市场新闻 | 免费档够日常用 |

**不用的**：FINNHUB、TwelveData、AlphaVantage —— `config.py` 里留了字段但没接入，
免得你以为需要去申请。

> 免费档的限制是真实的：Marketaux 一天 100 次，按 90 只票分，每只票一天只能
> 拉一次。框架里做了额度闸门（低于 10 次就跳过），不会把额度烧光。

---

## 架构

```
core/mine.py       因子挖掘主循环（系统扫描 + LLM 提案）
core/promote.py    晋升闸门：可重复性 / DSR / PBO / 冗余度
core/backtest.py   回测引擎（含真实调仓法、成本三重门槛）
core/zoo.py        因子动物园（试验记录）
core/factor_library.py  正式因子库（已通过全部闸门的少数幸存者）

llm/proposer*.py   LLM 提案：单算子 / 复合因子 / 批量并行
factors/           因子算子实现（价量、基本面、消息面）
data/              数据接入与面板构建
scripts/           运维脚本（挖掘循环、数据刷新、新闻清洗）
```

**两条策略线**：

- **低频因子盘** —— 日频横截面选股，靠因子库合成信号
- **多周期共振盘** —— 30m 入场 / 4H 定方向 / 日线定趋势，Boll+MACD+MTM+WR 多指标共振

---

## 快速开始

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # 填入你的免费 API key

# 拉数据
python -m data.history --years 6

# 挖一轮因子
python -m core.mine --market us --llm-total 150

# 看晋升结果
python -m core.promote --glob 'runs/mine*.json'
```

---

## 现状（诚实版）

- 累计试验 **82,663** 次 → 正式因子库 **14** 个
- 晋升率约 **0.017%**

这个数字不是失败，是**过滤器在工作**。绝大多数候选被 BH-FDR、DSR、PBO、
成本门槛、冗余判定层层拦下——拦下来的那些，正是「看起来能赚钱」的假货。

当前面板：**657 只美股 × 226 个交易日**（数据可重新拉长）。
样本还小，所以库里的因子只能算「通过初步检验」，不能算「已验证有效」。

---

## 免责声明

纯研究用途。所有交易默认走 **Demo（测试网）**，不碰实盘。
因子挖掘出来的东西**不构成投资建议**，历史有效不代表未来有效。
真金白银的决策请自己负责。

---

## 成熟挖掘对标与复用的轮子（2026-11-09）

调研结论：本项目防过拟合闸门（BH-FDR + OOS + DSR/PBO + 跨会话一致）已经比多数
2026 年论文更严，真正的短板是**搜索没有记忆、评估只有单一维度、算力占空比低**。
按「先找轮子，别造轮子」逐项对标后，只搬可复用的部分：

| 复用的东西 | 来源 | 协议 | 怎么用的 |
|---|---|---|---|
| 五维评估指标 PPS/RRE/PFS/DIV/LOGIC | [AlphaEval, KDD 2026](https://arxiv.org/abs/2508.13174) · [dhj74/AlphaEval](https://github.com/dhj74/AlphaEval) | MIT | 原仓库依赖 Qlib A 股数据格式，搬不动 → 只用 numpy 在 `core/alpha_eval5.py` 重实现五个指标；DIV 改用日度 IC 序列（截面均值会被市场共同漂移主导）。LOGIC 用结构规则代替 LLM 打分，因为执行机零 LLM |
| 多模型横评的任务分解（**仅调研，未实施**） | [AlphaBench, ICLR 2026](https://github.com/CityU-MLO/AlphaBench) · [AlphaForgeBench, KDD 2026](https://www3.ntu.edu.sg/home/boan/papers/KDD26_AlphaForgeBench.pdf) | MIT | 只记录 T1/T2/T4 的任务定义。**未写横评脚本、未发起任何模型请求** —— 主动发起模型请求须先请示（最高指令） |
| 多臂老虎机方向调度 | [R&D-Agent-Quant](https://arxiv.org/abs/2505.15155) · [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) | MIT | 只借调度设计，在 `core/mine_bandit.py` 用 UCB1 选 market；**不部署框架**（需 Docker + LLM，违反执行机零 LLM 约束，69 MB / 2 核 1.8 G 也吃不下） |
| 数据/回测底座 | [microsoft/qlib](https://github.com/microsoft/qlib) | MIT | **评估后未部署**：PyPI 0.9.7 只有 cp310/cp311 wheel，本机是 cp312；`lightgbm` 同样无 cp312 manylinux wheel，需现场编译，且 `mlflow`/`redis`/`pymongo` 一堆服务依赖。按小主机纪律做成可选后端，不预装 |
| 已排除 | [DulyHao/AlphaForge](https://github.com/DulyHao/AlphaForge) | 无 license | 最后 push 2024-09-01，三年没动等于没有 |

### 本机实测（2026-11-09，深圳机）

- 硬件：2 核 Xeon / 1.8 GiB / 可用 1.5 GiB / 30 G 磁盘；cgroup `MemoryHigh=1000M`。
- 吞吐：单进程 `evaluate_with_split` **79.3 候选/分钟**，`ls_nav_true_rebalance` 34.4/分钟；
  2 进程 **163.1/分钟，加速 2.11×**；单进程峰值 RSS 371 MB。
- 改造前：冷却 7200 s、每轮实算 2~21 分钟，占空比 4~13%；
  `quant-robust.timer` 每 20 分钟跑 68 秒体检，再加约 5.7%。
- 改造：每轮 us/crypto/正交 三个回测 **2 并行**（`MINE_PARALLEL=1` 可退回串行），
  promote 仍严格串行（峰值曾 1.59 G）；冷却期在已有 `robust_idle.sh` 后面追加五维评估，
  **只做评估、不新增候选 → 不抬 `overfit_guard` 的累计试验数 N**；
  市场轮换由固定 round-robin 换成 UCB1，hold 仍跟提案池走（`HOLD="$POOL_HOLD"` 对齐不动）。
- 同算力预算实测（各 100 候选、同一 FDR+gate 标尺）：固定网格 5 个过 gate、
  随机采样 0、轨迹变异 0。盲堆候选不产生 alpha（FDR ceiling），
  所以算力花在搜索结构 + 验证维度 + 数据维度上，不是堆数量。

### 相关论文（2026，按 arXiv 提交日）

`2611.07751` 公式语法认证 · `2611.04555` CosmosAlpha 图搜索 ·
`2610.29500` **指标溯源审计**（和本项目修 `sharpe_basis` 的坑完全对上） ·
`2610.15488` AlphaPilot 多轮工具 · `2608.01789` 进化计算统一视角 ·
`2603.16365` FactorEngine 程序级因子 · `2604.09601` Hubble DSL+AST 沙箱 ·
`2602.14670` FactorMiner 经验记忆 · `2602.07085` QuantaAlpha 轨迹级变异 ·
`2505.11122` LLM-MCTS。完整对标与选型见技能
`factor-research-pipeline/references/mature-alpha-mining-landscape-2026.md`。

---

## License

MIT

## 抄来的轮子 / 许可证（2026-11-12）

本项目为私人学习用途，以下开源代码已直接 vendored / 移植，按协议保留来源与许可证：

| 来源 | 协议 | 落地位置 | 用途 |
|---|---|---|---|
| [eslazarev/purged-cross-validation](https://github.com/eslazarev/purged-cross-validation) v0.1.12 | MIT | `core/_vendor/purgedcv/`（含 `LICENSE`） | CPCV 组合折 + purge + embargo；`core/cpcv_eval.py` 接到本项目 `[date x symbol]` panel，产出**多路径 OOS Sharpe 分布** |
| [gyx09212214-prog/worldquant-harness](https://github.com/gyx09212214-prog/worldquant-harness) | MIT | `core/rejection_memory.py` | 抄 `docs/AGENT_HARNESS_CONTRACT.md` 的 hypotheses / review_decisions / memory_delta 分层思路，用轻量 JSONL 实现「失败假设 → 下一轮约束」 |
| [quantskills/registry](https://github.com/quantskills/registry) | GPL-3.0 | 仅设计参考 | `skill-walk-forward-validator` / `skill-llm-alpha-generator` 的三层校验与 warm-start 思路；**未复制代码** |

**vendored 改动**：purgedcv 只保留 splitter / PBO 子集；`_cpcv.py` 里 `sklearn.base.clone`
换成 `copy.deepcopy`（quant-lab venv 无 sklearn，本项目只用 `split()` 拿索引），
`__init__.py` 收窄导出以避开 diagnostics / optuna 集成。

**为什么抄这两块**（论坛依据，2026-11-11 检索）：
- [Quant.SE 54927](https://quant.stackexchange.com/questions/54927/multivariate-combinatorial-purged-cross-validation)：k-fold / walk-forward 只给**一个** Sharpe，CPCV 给的是 **Sharpe 分布**。本项目此前只有一次 70/30 split。
- worldquant-harness README：*"Most AI quant workflows stop at idea → expression → backtest"*，把 rejection memory 留在系统外 → *"Failures become structured memory and next-round constraints"*。本项目 30 轮 0 晋升、每轮重撒网，正是这个病。
- 知乎《[量化干货｜如何优化因子挖掘模型](https://zhuanlan.zhihu.com/p/688817370)》：遗传规划 + 样本外/换手率/风格暴露检验。

实测（`mom_n250`, hold=5, C(6,2)=15 折, us 面板 1471×655）：
`sharpe_mean -0.1948 / std 0.9099 / min -1.8271 / max 1.0852 / 46.7% 路径为负` —— 单次 split 的正 Sharpe 不足以说明问题，这就是加这一层的理由。

## EDGAR 申报事件因子（2026-11-12，实验性，未进生产）

数据源：`data/edgar/filings.parquet`（92,271 份 8-K/10-Q/10-K，615 ticker，
2001-2026，自带精确 `filing_date`）。此前只有 `data/edgar.py::event_dates()`
和一个归档脚本在用，**没有任何因子算子消费它**。
落地：`factors/edgar_event.py` 六个算子（申报密度 / 8-K 密度 / 修订件占比 /
披露滞后 / 财报间隔 / form 熵），注册进 `OPS`，next-trading-day 对齐严格无前视。

**结论：接上了，但当前口径下没有可交易增量，暂不进生产 FDR 池。**

- 单次 70/30 split 曾报 OOS Sharpe **+4.6 ~ +4.7**，看着很亮；但 hold=5 下
  `evaluate_with_split` 有 ~√hold 膨胀。用本轮引入的 CPCV（C(6,2)=15 条路径、
  真实调仓）复核：`edgar_filing_count` **15/15 路径为负**（mean −1.69），
  `edgar_8k_count` **15/15 为负**（mean −1.49），对照 `mom` 为 46.7% 负。
  方向与单次 split 相反 → 口径失真，不是 alpha。
- 途中修掉一个真对齐 bug：面板开始前的 2001-2020 申报被 `searchsorted` 全压到
  首日，造成 2021 申报密度 **15.9 vs 2022 起 3.3** 的 5 倍脉冲；丢弃面板前事件后
  修为 3.1~4.2 平稳。
- 40/655 标的无 SEC 申报 → 加覆盖度掩码设 NaN（6.1%），避免恒零值污染截面分位数。
- 因此 `generate_grid()` 默认排除 `edgar_*`（生产候选维持 334），
  `include_experimental=True` 才出 370 个供研究。不拿 +4.7 邀功，也不把 36 个
  疑似伪信号塞进生产池抬高所有人的 FDR 门槛。

**纪律**：这一轮「上数据维度」交付的是「接上了 + 修了 bug + 用 CPCV 证伪」，
不是「找到新因子」。0 增量是正确结果，不是失败掩饰。

## 另类数据：FINRA 空头量 + Wikipedia 关注度（2026-11-12，实验性，未进生产）

**来源（全部免费）**
- FINRA Reg SHO 日频空头量：`cdn.finra.org/equity/regsho/daily/CNMSshvolYYYYMMDD.txt`
  （官方、无 key；实测 2021-01-04 / 2024-01-02 / 2026-11-10 全 200）。
- **换通道**：[LuxAlgo/market-trackers-data](https://github.com/LuxAlgo/market-trackers-data)
  **CC0-1.0**，从 FINRA / Wikimedia 归集、每行带 `provenance` 深链，
  `archives.json` 提供按年打包的 release asset。
- Wikimedia Pageviews REST（无 key，1 年/请求，实测 200；5 年范围会 429）。

**为什么换通道**：官方日文件逐日拉 1471 次实测只有 **16 个/分钟（≈88 分钟）**；
年度归档 6 个文件一次下载，流式解析 gz（不把解压结果读进 1.9G 内存）。

**落地**
- `data/finra.py`（官方日文件增量拉取器，每 200 天 checkpoint）、
  `data/wikipedia.py`（SEC 公司名 → Wikipedia 文章名批量解析，命中 493/652）、
  `scripts/fetch_alt_archives.py`（**实际数据入口**，只保留 655 标的）。
- `factors/alt_data.py`：6 个算子（空头比率 / 空头异动 / 豁免占比 / 关注度水平 /
  关注度变化 / 关注度波动），next-trading-day 无前视、覆盖度 NaN 掩码、
  稀疏观测 `ffill(limit=10)`；注册后 OPS 共 68 个算子。
- **实验性隔离**：`generate_grid()` 默认排除 `edgar_/finra_/wiki_`，
  生产候选维持 **334**；`include_experimental=True` 才出 **406**。

**数据实况（如实）**
- FINRA 年度归档只有 **321 天观测**（2021-01 → 2026-03）、644 标的，
  不是完整日频；ffill 后因子覆盖率约 **47%**。
- Wikipedia 归档是 curated map，只覆盖 **90 个标的**，因子覆盖率约 **10%**。

**CPCV 证伪**（15 路径 / 真实调仓 / hold=5 / panel 截到 2026-03-05，
产物 `runs/cpcv_alt_scan.json`）：

| 因子 | Sharpe 均值 | 负路径 |
|---|---|---|
| finra_short_ratio | −1.41 | **15/15** |
| finra_short_surge | −2.46 | **15/15** |
| finra_short_exempt | −1.19 | **15/15** |
| wiki_attention | −1.96 | **15/15** |
| wiki_attention_chg | −1.72 | **15/15** |
| wiki_attention_vol | −0.50 | 8/15 |
| mom（对照） | −0.07 | 6/15 |

→ **当前数据覆盖下没有可交易增量，不进生产 FDR 池。**
沿用 EDGAR 那次的纪律：不拿单次 split 的高 Sharpe 邀功，也不把疑似伪信号塞进生产池。

⚠️ **待验证（未写成结论）**：Wikipedia 只有 90 个大盘标的、FINRA 覆盖率 47%，
高 NaN 会让分位数分层落在少数标的上；结构性负 Sharpe **可能**是覆盖度偏差
而非因子本身无效。要下这个判断需要补齐日频全市场数据后重跑 CPCV。

## 统一数据 API 接入层（2026-11-12，纯程序化，不涉 LLM）

`data/api_hub.py`：5 个源统一注册，key 从项目 `.env` 读（**显式 `override=True`**，
避免串到 Hermes 网关注入的同名变量——假 -2015 那次的教训），通用重试/限速，
落 `data/api/*.parquet`。CLI：`venv/bin/python -m data.api_hub --source all --smoke`。

| 源 | 官方端点 | 免费档 | smoke 实测 | 备注 |
|---|---|---|---|---|
| Alpaca News | `data.alpaca.markets/v1beta1/news` | 免费 200/min，Benzinga，历史回溯 2015（官方 blog） | 50 行 | 补新闻密度；技能里 Marketaux 只有 234 行/78 标的，撑不起截面文本因子 |
| Finnhub insider | `finnhub.io/api/v1/stock/insider-transactions` | 免费档；**官方 rate-limit 页本次抓取失败，限额未核实** | 511 行 | Form 4 结构化 |
| Twelve Data | `api.twelvedata.com/time_series` | Basic $0：**8 credits/min、800/天、仅 US**（官方 pricing.md） | 9 行 | 备用日线，adapter 守 8s 间隔 |
| Alpha Vantage `INSIDER_TRANSACTIONS` | `alphavantage.co/query` | 多数端点免费；**`NEWS_SENTIMENT` 实测返回 "premium endpoint"** | 14513 行 | 日限额本次未抓到明确数字，不把 25/天当铁律 |
| Binance 多空账户比 | `fapi.binance.com/futures/data/globalLongShortAccountRatio` | 公开无 key | 90 行 | 用 `BTCUSDT/ETHUSDT/SOLUSDT`，不复用美股 symbols |

**实测 200 但没硬接**（对 655 美股日频截面因子无直接映射，留配置不拉数据）：
Hyperliquid `/info`、CoinGecko、USAspending、openFDA、BLS public API、SEC submissions、
ClinicalTrials.gov v2、Yahoo chart（技能里「Yahoo Unauthorized」是旧结论，该端点现已通）。
**被挡/限流不接**：Nasdaq Data Link 403、Stooq 返回 JS challenge HTML、CBOE VIX 307 CF、
GDELT 429「5 秒一次」、EIA DEMO_KEY 404。

**状态（如实）**：smoke 只拉了 3 个标的，够验证链路、**不够做截面因子**。
全量拉取要按各源免费限额排期（Twelve Data 800/天、AV 限额未核实、Finnhub 未核实），
拉完照旧走 CPCV 15 路径 + 真实调仓证伪，再决定进不进生产池。
本轮**未新增任何候选到生产网格**（`generate_grid()` 仍是 334）。

## LLM 部分：一项都没执行，等逐项审批

用户 2026-11-12 指令：「也可以涉llm，只是llm部分需要我具体审批」。
待审批清单见 `state/llm_approval_queue.md`（4 项，全部 `PENDING`）。
边界不因这句放宽：仅 **Magpie 当前主模型**（`auto/hy4free`）、**并发 ≤ 2**、
**产生费用需请示**、禁止模型切换 / 多模型横评 / fallback。
本轮没有发起任何模型请求、没有改 `llm/client.py`、没有动提案池 prompt。
Bright Data SERP / Web Unlocker 可能产生费用，同样没碰。

