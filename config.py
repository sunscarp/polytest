"""Configuration constants for the weather NO trading simulator."""

# ── Bankroll ──────────────────────────────────────────────────────────────
SHARED_BANKROLL = 10.0
MIN_BET = 1.0
MAX_BET = 3.0

# ── Entry Strategy ────────────────────────────────────────────────────────
DISTANCE_MIN = 2.0   # minimum °C between weather.com high and bucket threshold
DISTANCE_MAX = 4.0   # maximum °C
NOISE_THRESHOLD = 0.5  # °C — weather.com vs METAR diff below this is noise

# ── Monitoring ────────────────────────────────────────────────────────────
METAR_POLL_SECONDS = 45       # 45 seconds
WEATHER_POLL_SECONDS = 600    # 10 minutes
STOP_LOSS_PCT = -0.20         # sell at -20% of bet
PROXIMITY_THRESHOLD = 10.0    # only trigger exit actions when METAR within this many degrees of bucket
METAR_CLOSE_READINGS = 2      # need this many consecutive METAR readings trending toward bucket

# ── Market Discovery ──────────────────────────────────────────────────────
LOOK_AHEAD_DAYS = 2           # today + tomorrow (covers IST midnight gap)
MIN_NO_PRICE = 0.50           # only buy NO if YES price < this (NO is cheap)
MAX_NO_PRICE = 0.90           # skip if NO price > this (too expensive, low upside)
MIN_VOLUME = 100              # skip low-volume markets

# ── HTTP ──────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = (5, 15)     # (connect, read) seconds
MAX_RETRIES = 3
RETRY_DELAY = 3               # seconds between retries

# ── Paths ─────────────────────────────────────────────────────────────────
import pathlib
BASE_DIR = pathlib.Path(__file__).parent
STATIONS_FILE = BASE_DIR / "stations.json"
LOGS_DIR = BASE_DIR / "logs"
