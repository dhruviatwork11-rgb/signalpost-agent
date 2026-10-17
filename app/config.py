"""Application configuration settings."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load variables from .env file if present
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Brønnøysund Register Centre API base URL
BRREG_API_BASE_URL: str = os.getenv(
    "BRREG_API_BASE_URL",
    "https://data.brreg.no/enhetsregisteret/api",
).rstrip("/")

# SQLite database path (defaults to data/signalpost.db)
_db_env = os.getenv("DATABASE_PATH", "data/signalpost.db")
DATABASE_PATH: Path = (
    Path(_db_env) if Path(_db_env).is_absolute() else BASE_DIR / _db_env
)

# HTTP client configuration
REQUEST_TIMEOUT_SECONDS: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10.0"))
USER_AGENT: str = os.getenv("USER_AGENT", "SignalpostAgent/0.1.0")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
