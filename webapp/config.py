"""
Configuration settings for the iNat Invasives web application.
"""
import logging
import os
import secrets
from pathlib import Path

# Base paths
BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent

# Database
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/invasives.db")

# Redis (for Celery task queue)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Mandrill Email Configuration
MANDRILL_API_KEY = os.getenv("MANDRILL_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "reports@invasives.wildme.org")
FROM_NAME = os.getenv("FROM_NAME", "iNaturalist Invasives Monitor")

# Application settings
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "change-this-in-production-to-random-string":
    # Never fall back to a publicly known key. A per-process random key keeps
    # the app working (CSRF tokens just don't survive a restart).
    logging.getLogger(__name__).warning("SECRET_KEY not set; using a random per-process key")
    SECRET_KEY = secrets.token_hex(32)
BASE_URL = os.getenv("BASE_URL", "https://invasives.wildme.org")

# Prosopo Procaptcha (bot protection)
PROCAPTCHA_SITEKEY = os.getenv("PROCAPTCHA_SITEKEY", "")
PROCAPTCHA_SECRET = os.getenv("PROCAPTCHA_SECRET", "")

# Report generation settings
DEFAULT_LOOKBACK_YEARS = 20
DEFAULT_RATE_LIMIT = 1.2  # seconds between iNaturalist API calls
REPORT_STORAGE_DIR = BASE_DIR / "reports"

# Ensure report directory exists
REPORT_STORAGE_DIR.mkdir(exist_ok=True)

# Celery Beat schedule lives in webapp/celery_config.py (the one actually loaded).

# Token expiry for magic links
MAGIC_LINK_EXPIRY_HOURS = 48
