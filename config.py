"""Configuration constants for the weather NO trading simulator."""

import os
import pathlib
from dotenv import load_dotenv

# Load .env from project root
_env_path = pathlib.Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# ── Polymarket Credentials ────────────────────────────────────────────────
POLY_FUNDER_ADDRESS = os.getenv("POLY_FUNDER_ADDRESS", "")

# ── Email (SMTP) ──────────────────────────────────────────────────────────
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "githubsanskar@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "qyat gutg rkvg rgsg")
SMTP_RECIPIENT = os.getenv("SMTP_RECIPIENT", "githubsanskar@gmail.com")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# ── Mode ──────────────────────────────────────────────────────────────────
RECOMMENDATION_MODE = True  # True = recommend only, False = live CLOB execution
PAPER_TRADING = True        # True = paper trade with virtual bankroll, no emails
                            # False = recommend-only (or live CLOB if RECOMMENDATION_MODE=False)

# ── Bankroll ──────────────────────────────────────────────────────────────
SHARED_BANKROLL = 10.0
MIN_BET = 1.0
MAX_BET = 1.0
MAX_OPEN_PAPER = 10         # max concurrent paper positions

# ── Entry Strategy ────────────────────────────────────────────────────────
DISTANCE_MIN = 2.0
DISTANCE_MAX = 4.0
NOISE_THRESHOLD = 0.5

# ── Monitoring ────────────────────────────────────────────────────────────
METAR_POLL_SECONDS = 45
WEATHER_POLL_SECONDS = 600
STOP_LOSS_PCT = -0.20
PROXIMITY_THRESHOLD = 10.0
METAR_CLOSE_READINGS = 2
FORECAST_DRIFT_THRESHOLD = 1.0

# ── Market Discovery ──────────────────────────────────────────────────────
LOOK_AHEAD_DAYS = 2
MIN_NO_PRICE = 0.50
MAX_NO_PRICE = 0.90
MIN_VOLUME = 100

# ── HTTP ──────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = (5, 15)
MAX_RETRIES = 3
RETRY_DELAY = 3

# ── Paths ─────────────────────────────────────────────────────────────────
BASE_DIR = pathlib.Path(__file__).parent
STATIONS_FILE = BASE_DIR / "stations.json"
LOGS_DIR = BASE_DIR / "logs"
