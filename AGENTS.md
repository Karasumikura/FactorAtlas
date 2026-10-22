# AGENTS.md — 测试与改动硬规则

## 测试必须轻量化（内存占用尽量 < 1G）

本机总内存只有 **1.9G**（+2G swap），Hermes 网关是 user unit，`oom_score_adj=100`，
最容易被 OOM killer 选中。历史上已有**两次**因为跑测试把网关杀掉。

### 禁止

```bash
pytest tests/            # ✗ 绝对禁止
pytest tests/ -k "xxx"   # ✗ 也禁止 —— -k 只筛测试用例，不筛 import
```

`-k` 过滤不会减少模块加载：`tests/` 被收集时 torch 等重量级依赖照样 import。

### 允许

```bash
./venv/bin/python -m pytest tests/test_promote.py tests/test_factor_library.py -q
./venv/bin/python -c "from core.promote import f; print(f(...))"
./venv/bin/python -m py_compile core/promote.py
```

只用项目内的 `./venv/bin/python`（系统 python3 没装依赖）。

需要跑全量测试时，**先问用户**。

## 不准修改硬编码

不要为了绕过问题写死枚举/映射/白名单/期望值。测试失败先查是否自己的改动引入真实回归，
不许改期望值让它变绿。
