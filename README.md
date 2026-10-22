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

## License

MIT
