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

# --- LLM（9router free） ---
LLM_BASE_URL = os.environ.get("QUANT_LLM_BASE_URL", "http://127.0.0.1:20128/v1")
LLM_API_KEY = os.environ.get("NINEROUTER_API_KEY", "")
LLM_MODEL = os.environ.get("QUANT_LLM_MODEL", "free")

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
