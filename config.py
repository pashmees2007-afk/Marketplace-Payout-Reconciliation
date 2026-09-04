"""
Central configuration for the AI Finance Controller.

Every tunable constant used by more than one module lives here so the
demo can be re-tuned (tolerance, window, scale) from a single place, and
so the README's documented defaults are guaranteed to match the code.

Values can be overridden with environment variables (see .env.example)
without editing code -- useful for demoing a different scale live.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv is a convenience, not a hard requirement: if it's not
    # installed, real environment variables (export FOO=bar) still work.
    pass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

ORDERS_CSV = DATA_DIR / "orders.csv"
PAYOUTS_CSV = DATA_DIR / "payouts.csv"
GROUND_TRUTH_JSON = DATA_DIR / "ground_truth.json"

RECONCILIATION_RESULTS_JSON = OUTPUT_DIR / "reconciliation_results.json"
METRICS_JSON = OUTPUT_DIR / "metrics.json"
EXCEPTIONS_CSV = OUTPUT_DIR / "exceptions.csv"
AUDIT_LOG_JSONL = OUTPUT_DIR / "audit_log.jsonl"

# --------------------------------------------------------------------------
# Synthetic data generation
# --------------------------------------------------------------------------
RANDOM_SEED = _env_int("RANDOM_SEED", 42)
NUM_SELLERS = _env_int("NUM_SELLERS", 20)          # 15-25 per spec
MIN_ORDERS_PER_SELLER = _env_int("MIN_ORDERS_PER_SELLER", 10)
MAX_ORDERS_PER_SELLER = _env_int("MAX_ORDERS_PER_SELLER", 30)
ORDER_SPAN_DAYS = _env_int("ORDER_SPAN_DAYS", 16)   # orders spread over this many days

# Intentional scenario mix for payouts generated from real order batches.
SCENARIO_WEIGHTS = {
    "exact_match": 0.70,
    "close_match": 0.15,
    "timing_mismatch": 0.10,
    "unresolved": 0.05,
}

# --------------------------------------------------------------------------
# Reconciliation engine
# --------------------------------------------------------------------------
# Tolerance below which a non-exact subset is still considered "explained".
TOLERANCE_PAISE = _env_int("TOLERANCE_PAISE", 3000)  # ₹30, per plan example

# Only orders dated on/before the payout date, and no more than this many
# days before it, are eligible for that payout (date-window filtering).
ELIGIBILITY_WINDOW_DAYS = _env_int("ELIGIBILITY_WINDOW_DAYS", 30)

# Subset-sum search space guards (see reconcile.py for why these are safe).
FULL_SEARCH_MAX_ELIGIBLE = _env_int("FULL_SEARCH_MAX_ELIGIBLE", 16)
CAPPED_SUBSET_SIZE = _env_int("CAPPED_SUBSET_SIZE", 8)

# --------------------------------------------------------------------------
# AI explanation layer
# --------------------------------------------------------------------------
LLM_MODE = os.getenv("LLM_MODE", "mock").strip().lower()  # "mock" or "live"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 400)
LLM_TIMEOUT_SECONDS = _env_float("LLM_TIMEOUT_SECONDS", 20.0)
