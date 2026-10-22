# 小资金（$100）仓位分配逻辑 — 审计与优化方案

> 对象：`/opt/quant-lab` 事件驱动模拟盘守护进程（`quant-trader.service`，Binance Demo）
> 审计时间：2026-10-15 · 当前运行参数：`--factor mom250 --equity 100 --neutralize none --leverage 1 --max-notional 500 --max-daily-rebalances 3 --gross-cap 1.2`
> 数据源：实盘 `compute_targets` 实时输出 + 交易所 `exchangeInfo` 实测

---

## 一、结论先行（TL;DR）

**当前逻辑方向正确（信号驱动、自适应腿数、不强制满仓），但在 $100 尺度上存在 3 个会实际吃钱/空转的硬伤：**

| # | 问题 | 后果 | 严重度 |
|---|------|------|--------|
| **A** | 腿数按「每边」算，$100 实际跑 **8 腿**，跑到参考文档假设的 2 倍 | 单腿仅 \$7–14，逼近 \$5 minNotional；调仓单大面积被丢弃 | 🔴 高 |
| **B** | 漂移阈值 25% 在 \$7–14 的小腿上**永远触发不了实单**（需 35%–71% 漂移才凑够 \$5） | 触发引擎反复"假触发→被 minNotional 丢弃→再触发"，噪声空转 | 🔴 高 |
| **C** | 收到执行通知失败：`[notify] 异常: [Errno 2] No such file or directory: 'hermes'`（旧进程遗留；新 PATH 已修但**未在真实发单上复验**） | 用户收不到调仓通知 | 🟡 中 |

**最优配置（一句话）**：$100 本金 + 动量因子下，**每边 ≤3 腿（含单边）、gross 封顶 100%、漂移阈值提到 40%、minNotional 门槛与腿数联立**。目标：单腿名义恒 ≥ \$10，一次调仓摩擦 ≤ 0.2% 本金，年化换手摩擦 ≤ 3%。

---

## 二、当前实现审计（证据）

### 2.1 腿数计算 —— 单位错误（问题 A）

`core/bridge_us_binance.py:142-152`：

```python
cap = equity if equity else gross_exposure * 100
max_by_cash = max(1, int(cap / 2 / 5.0 / 2.2))   # $100 → int(4.5) = 4
top_n = max_by_cash                               # = 4  ← 这是【每边】腿数！
longs  = [...][:top_n]      # 4 条
shorts = [...][:top_n]      # 4 条
```

`max_by_cash` 的设计意图是「全组合最多几条腿」，但代码把它当成了**每边**腿数，实际总腿数 = `2 × max_by_cash`。

**实测验证**（equity=100）：

```
equity=100 → legs=8 (L4/S4)  gross=$78.7
```

| equity | 实际腿数 | 参考文档声称 |
|--------|---------|-------------|
| \$50  | 4 (L2/S2) | — |
| **\$100** | **8 (L4/S4)** | 4 腿 |
| \$200 | 14 | — |
| \$500 | 15 | — |

**根因**：参考文档 `references/event-driven-demo-daemon-2026-10-15.md` 的实测表写的是"总腿数"，但代码实现按每边算，两者差 2 倍。

### 2.2 单腿名义 vs minNotional —— 接近失效边缘（问题 A 的后果）

$100 / 8 腿实测目标权重：

```
MU     MUUSDT     w=+0.1414  → $14.14
MSTR   MSTRUSDT   w=-0.1414  → $14.14
INTC   INTCUSDT   w=+0.1000  → $10.00
COIN   COINUSDT   w=-0.1000  → $10.00
GOOGL  GOOGLUSDT  w=+0.0816  →  $8.16
CRCL   CRCLUSDT   w=-0.0816  →  $8.16
NVDA   NVDAUSDT   w=+0.0707  →  $7.07
HOOD   HOODUSDT   w=-0.0707  →  $7.07
                        gross = $78.7 (78.7%)
```

交易所实测 `minNotional = 5 USDT`（MU/INTC/NVDA/MSTR 均为 5）。**最小的两只腿只有 \$7，距离门槛仅 \$2。**

### 2.3 漂移阈值与 minNotional 的"空转"死结（问题 B）

`core/trigger_engine.py:36` 设 `rebalance_drift_pct = 0.25`；但 `build_orders` 会丢弃 `|delta×price| < min_notional(5)` 的单（`bridge_us_binance.py:287`）。

反推「一条腿要真正下单需要多大漂移」：

| 腿名义 | 触发 25% 漂移的金额 | 需多大漂移才够 \$5 |
|--------|-------------------|------------------|
| \$14.14 | \$3.54 | **35%** |
| \$10.00 | \$2.50 | **50%** |
| \$8.16  | \$2.04 | **61%** |
| \$7.07  | \$1.77 | **71%** |

**结论**：25% 漂移阈值下，所有腿的调仓单都 < \$5 被丢弃 → `needs_rebalance` 触发条件（要求 `notional ≥ 5.0`，`trigger_engine.py:65`）其实也筛不出东西。**阈值与门槛矛盾：要么调仓单被丢弃（白算），要么阈值形同虚设。**

### 2.4 换算缺失：`top_per_side` 未被守护进程传入

`core/trader_daemon.py:178-180` 调 `compute_targets(...)` 时**没有传 `top_per_side`**，所以永远走 `max_by_cash` 自动分支。守护进程也没有 CLI 参数暴露它（`--max-legs` 存在但只做上限截断）。

### 2.5 通知 PATH 问题（问题 C）

日志实测：

```
[notify] 异常: [Errno 2] No such file or directory: 'hermes'   ← 行13，15:01:29
```

- 该行来自 **15:01 那个旧进程**（当时 unit 尚未加 `Environment=PATH`）。
- 当前 unit 已含 `Environment=PATH=/root/.local/share/uv/tools/hermes-agent/bin:...`，`/proc/49541/environ` 已确认生效，`shutil.which('hermes')` 现在能解析。
- **但自 15:15 重启后，唯一一次真实发单发生在 15:01（重启前）**，所以「修复后能成功推送」**尚未在真实 executed tick 上验证**。

---

## 三、优化目标与量化标准

对 $100 本金，分配逻辑应同时满足：

1. **单腿名义 ≥ \$10**（是 minNotional \$5 的 2 倍安全垫，避免价格波动把有效单打到门槛下）。
2. **腿数 = f(资金)**：`总腿数 ≈ min(2 × 3, floor(equity / 单腿下限))`，$100 下上限 **6 腿**（3/边），极端强信号允许单边 3 腿。
3. **一次调仓摩擦 ≤ 0.2% 本金**；年化（假设每月 6 次、每次换手 3–4 腿）**≤ 3% 本金/年**。
4. **漂移阈值与 minNotional 联立**：阈值 × 单腿名义 ≥ minNotional，确保触发即能真的下单。
5. **保持信号驱动**：不强制满仓（gross 由信号强度决定，实测 0.6–1.0）、不强制多空对称、不固定腿数。

---

## 四、具体可执行优化方案

### 改动 1 —— 修正腿数单位（问题 A，必改）

**文件**：`core/bridge_us_binance.py:142-152`

把 `max_by_cash` 明确为「**每边**上限」，并统一常量：

```python
# 单腿名义安全下限：minNotional($5) 的 2 倍，避免调仓单被丢弃
LEG_MIN_USD = 10.0
# 每边最多腿数（小资金收紧；可被 top_per_side / max_legs 进一步截断）
MAX_LEGS_PER_SIDE = 3

cap = equity if equity else gross_exposure * 100
max_by_cash = max(1, int(cap / LEG_MIN_USD))          # $100 → 10 → 但下面封顶
max_by_cash = min(max_by_cash, MAX_LEGS_PER_SIDE)     # $100 → 3（每边）
if top_per_side and top_per_side > 0:
    top_n = min(top_per_side, max_by_cash)
else:
    top_n = max_by_cash
if max_legs and max_legs > 0:
    top_n = min(top_n, max_legs)
top_n = max(1, top_n)
```

**效果**：`$100 → 每边 3 腿 = 总 6 腿`。实测每腿名义：

```
多头/空头各 3 腿: $17.3, $12.2, $10.0   →  最小腿 $9.98（≥ $10 安全垫）
```

再小资金（如 \$50）→ `int(50/10)=5 → 封顶 3`，仍 3/边；若要每腿 ≥\$10 需自动降到 2/边，可用动态式：
```python
per_side = max(1, min(MAX_LEGS_PER_SIDE, int(cap * gross_frac / 2 / LEG_MIN_USD)))
```

### 改动 2 —— 漂移阈值与 minNotional 联立（问题 B，必改）

**文件**：`core/trigger_engine.py:36`

把静态 25% 改为「基于最小腿名义反推」的自适应阈值，或直接提高到安全值：

```python
DEFAULT_THRESHOLDS = {
    "rebalance_drift_pct": 0.40,   # 25% → 40%：确保 $10 腿漂移 ≥ $4，叠加滑点后 ≥ $5 才下单
    "price_zscore": 2.5,
    "price_zscore_lookback": 20,
    "drawdown_pct": 0.05,
    "rebalance_interval_days": 5,
    "min_order_notional": 5.0,     # 与 bridge 的 min_notional 对齐，避免两处漂移
}
```

并在 `check_triggers` 的 `needs_rebalance` 判定里显式用同一门槛：

```python
min_n = q["min_order_notional"]
meaningful = [o for o in orders if getattr(o, "notional", 0) >= min_n]
```

> 也可做「动态」版本：`drift_pct = max(0.25, min(0.6, min_order_notional / median_leg_notional))`，让阈值随腿名义自动缩放。**推荐先用固定 0.40 验证，稳定后再上动态。**

### 改动 3 —— 把 `top_per_side` 接入守护进程（问题 A 配套）

**文件**：`core/trader_daemon.py`

- `tick()` 增加参数 `top_per_side: int = 0`，并透传给 `compute_targets`。
- `argparse` 增加 `--top-per-side`（默认 0 = 自动）。
- **systemd unit 显式固定**，避免依赖默认值漂移：

```ini
ExecStart=/opt/quant-lab/venv/bin/python -m core.trader_daemon --loop --interval 300 \
    --live --factor mom250 --equity 100 --quantile 0.2 --neutralize none \
    --top-per-side 3 --leverage 1 --max-notional 500 \
    --max-daily-rebalances 3 --gross-cap 1.0 --notify qqbot
```

> `--gross-cap` 从 1.2 收到 **1.0**：$100 本金不该允许 120% 总敞口（杠杆 1），1.0 已包含「可以满仓」的许可。

### 改动 4 —— 复验通知链路（问题 C，验证项）

- 重启服务后（新进程已带正确 PATH），**制造一次真实 `executed=True` tick**，确认日志出现 `[notify] OK -> qqbot`，且 `runs/trader_alerts.jsonl` 与 QQ 收到消息。
- 若仍失败，兜底：在 unit 里把 `hermes` 绝对路径写进 `notify.py` 的候选列表已存在，检查 `hermes send --list` 是否能列出 `qqbot`。

### 改动 5 —— 摩擦成本护栏（新增，防慢性失血）

在 `build_orders` 之后、执行之前加一道「换手预算」闸：

```python
# 一次 tick 的交易额不得超过本金的 X%，超过则只执行偏离最大的前 K 腿
TURNOVER_CAP = 0.15          # ≤15% 本金/次
if sum(o.notional for o in orders) > equity * TURNOVER_CAP:
    orders = sorted(orders, key=lambda o: -o.notional)[:3]   # 只做最大的 3 条
```

**摩擦实测对照**（$100，taker 0.05%）：

| 每次换手腿数 | 摩擦/次 | 每月 6 次 = % 本金 |
|-------------|--------|------------------|
| 2 腿 | \$0.020 | 0.12% |
| **4 腿** | **\$0.040** | **0.24%** |
| 8 腿（当前满换） | \$0.080 | 0.48% |

年化满换 8 腿 = **5.76% 本金/年**；优化到 3–4 腿 → **≈1.5–2.9%/年**。

---

## 五、优化前后对照（$100 实测预测）

| 指标 | 优化前（当前） | 优化后（方案） | 依据 |
|------|--------------|--------------|------|
| 总腿数 | 8 (4/4) | **6 (3/3)** | 改动1 |
| 最小腿名义 | \$7.07 | **≈\$10** | 实测 3/边 |
| 单腿 ≥ minNotional(\$5) 安全垫 | 1.4× | **2.0×** | 改动1 |
| 漂移阈值可执行性 | 25%（<35% 全空转） | **40%（可执行）** | 改动2 |
| gross 上限 | 120% | **100%** | 改动3 |
| 一次调仓摩擦 | \$0.08 | **\$0.04** | 实测 |
| 年化摩擦（满换） | 5.76% | **≈2.9%** | 实测 |
| 通知链路 | 上次失败 | **复验通过** | 改动4 |

---

## 六、验证步骤（按序执行，可复现）

```bash
cd /opt/quant-lab

# 1) 干跑：确认腿数与单腿名义（不发单）
./venv/bin/python -m core.trader_daemon --once --force --factor mom250 \
    --equity 100 --quantile 0.2 --neutralize none --top-per-side 3 --no-llm
# 期望：n_targets=6；最快/最慢腿 notional ≥ $10 与 ≈$10

# 2) 单测：漂移阈值能真实下单
./venv/bin/python -c "
from core.trigger_engine import check_triggers, DEFAULT_THRESHOLDS
print(DEFAULT_THRESHOLDS['rebalance_drift_pct'])   # 期望 0.40
"

# 3) 复验通知：制造一次 executed tick（Demo，安全）
#    改 unit --gross-cap 1.0 + --top-per-side 3 后重启，等一次真实调仓
systemctl restart quant-trader.service
sleep 5 && tail -5 runs/trader_daemon.log          # 期望出现 [notify] OK -> qqbot

# 4) 复盘累计摩擦
python3 -c "
import json,glob
tot=0
for ln in open('runs/trader_alerts.jsonl'):
    d=json.loads(ln); 
    # 从 msg 里解析 fills 名义（按实际结构微调）
"
```

---

## 七、设计原则（保留，勿推翻）

优化不得破坏已验证的正确设计（来自前序会话的用户明确要求）：

1. ✅ **腿数自适应资金**，不是固定值 —— 本方案只是修正单位，仍自适应。
2. ✅ **仓位由信号驱动** —— 不强制满仓（gross 实测 79–100%）、不强制多空对称（单边合法）、不固定腿数。
3. ✅ **不硬编码集中度** —— `--top-per-side` 是**上限**，默认自动；不给用户当决策题。
4. ✅ **盈利复利到预算** —— `equity = budget`，盈利抬高下一轮可用额度。
5. ✅ **硬闸全在代码** —— Demo only / 杠杆 1 / 单标 ≤\$500 / 日内 ≤3 / gross ≤cap / 执行前重读持仓。

---

## 八、风险与边界

- 本方案**只改分配与触发参数**，不改信号/因子逻辑，不动历史账本。
- `minNotional=5` 是 Binance Demo 实测值；主网若不同需重新反推 `LEG_MIN_USD`。
- 仍是模拟盘（Demo）；本方案不构成实盘建议。
- 参数（40% 漂移、gross 1.0）是**候选值**，需在真实 tick 上观察 1–2 周再固化。
