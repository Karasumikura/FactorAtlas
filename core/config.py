"""Quant Lab 配置：复用 PRO profile 的数据接口密钥。"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- 数据接口（复用 PRO profile） ---
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_FEED = os.environ.get("ALPACA_DATA_FEED", "sip")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
FMP_KEY = os.environ.get("FMP_API_KEY", "")
TWELVE_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
MARKETAUX_KEY = os.environ.get("MARKETAUX_API_KEY", "")
ALPHAVANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")

# --- LLM：请求目标强制跟随 Hermes 当前主模型 ---
# 不保留项目级模型 slug / QUANT_LLM_MODEL 覆盖，避免请求偷偷固定到旧模型。
# endpoint 与 key 仍沿用当前 Magpie 路由。
LLM_BASE_URL = os.environ.get("QUANT_LLM_BASE_URL", "http://127.0.0.1:3425/v1")
LLM_API_KEY = os.environ.get("MAGPIE_API_KEY", "")


HERMES_BIN_DEFAULT = "/root/.hermes/hermes-agent/.hermes/bin/hermes"


def _live_main_model() -> str | None:
    """本机装了 hermes CLI 就直接读主模型；读不到返回 None（交给文件兜底）。"""
    import subprocess

    hermes = os.environ.get("HERMES_CLI", HERMES_BIN_DEFAULT)
    if not Path(hermes).exists():
        return None
    env = os.environ.copy()
    # 服务进程的 HERMES_HOME 指向 pro-profile（systemd 里就是这么设的），
    # 但主模型口径固定为 default profile —— 与导出脚本保持同一来源。
    env["HERMES_HOME"] = "/root/.hermes"
    env["HERMES_REAL_HOME"] = "/root"
    try:
        result = subprocess.run(
            [hermes, "config", "get", "model.default"],
            check=True, capture_output=True, text=True, timeout=5, env=env,
        )
    except Exception:  # noqa: BLE001 — CLI 不可用/超时都退回文件
        return None
    lines = [x.strip() for x in result.stdout.strip().splitlines() if x.strip()]
    model = lines[-1].strip('"').strip("'") if lines else ""
    return model or None


def get_main_model() -> str:
    """每次读取 Hermes 主模型；不在进程启动时缓存，支持主模型切换后立即跟随。

    取数顺序（2026-11-08 修正）：
      1. 本机有 hermes CLI → 直接读（香港主控机走这条，永不过期）；
      2. 没有 CLI（深圳挖矿机）→ 读 ``QUANT_MODEL_FILE`` 快照，>48h 判过期报错。

    原来文件优先：只要 .env 里设了 QUANT_MODEL_FILE 就无条件读文件，
    连装着一整套 hermes 的主控机也在读 6 小时前的快照 —— 切了主模型
    仍在打旧模型（2026-11-07 的 hy4free 事故就是这个原因）。
    """
    import time

    live = _live_main_model()
    if live:
        return live

    model_file = os.environ.get("QUANT_MODEL_FILE")
    if not model_file:
        raise RuntimeError("既没有 hermes CLI 也没有 QUANT_MODEL_FILE，无法确定主模型，拒绝发送请求")

    p = Path(model_file)
    if not p.exists():
        raise RuntimeError(f"QUANT_MODEL_FILE 不存在: {model_file}")
    age_h = (time.time() - p.stat().st_mtime) / 3600.0
    if age_h > 48:
        raise RuntimeError(
            f"主模型名文件已过期 {age_h:.1f}h（{model_file}），"
            "请确认主控机的 model 导出 cron 还在跑"
        )
    model = p.read_text().strip()
    if not model:
        raise RuntimeError(f"主模型名文件为空: {model_file}")
    return model

# --- PRO 现成资产 ---
PRO_SCRIPTS = Path("/root/.hermes/profiles/pro-analysis/scripts")
PRO_DAILY_REFERENCE = PRO_SCRIPTS / "multi_stock_paper_daily_reference.json"
PRO_LIQUIDITY_UNIVERSE = PRO_SCRIPTS / "multi_stock_paper_liquidity_universe.json"

# --- 本地缓存 ---
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
REPORTS_DIR = ROOT / "reports"
for d in (DATA_DIR, RUNS_DIR, REPORTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def has_alpaca() -> bool:
    return bool(ALPACA_KEY and ALPACA_SECRET)


def has_llm() -> bool:
    return bool(LLM_API_KEY)
