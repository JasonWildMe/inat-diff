"""
Configuration settings for the iNat Invasives web application.
"""
import os
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
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production-to-random-string")
BASE_URL = os.getenv("BASE_URL", "https://invasives.wildme.org")

# Report generation settings
DEFAULT_LOOKBACK_YEARS = 20
DEFAULT_RATE_LIMIT = 1.2  # seconds between iNaturalist API calls
REPORT_STORAGE_DIR = BASE_DIR / "reports"

# Ensure report directory exists
REPORT_STORAGE_DIR.mkdir(exist_ok=True)

# Celery Beat schedule (monthly reports)
CELERY_BEAT_SCHEDULE = {
    'generate-monthly-reports': {
        'task': 'webapp.tasks.generate_scheduled_reports',
        'schedule': {
            'day_of_month': 1,  # First day of each month
            'hour': 6,
            'minute': 0,
        },
    },
    'generate-weekly-reports': {
        'task': 'webapp.tasks.generate_scheduled_reports',
        'schedule': {
            'day_of_week': 0,  # Monday
            'hour': 6,
            'minute': 0,
        },
        'kwargs': {'frequency': 'weekly'},
    },
}

# Rate limiting for web form submissions
MAX_SUBSCRIPTIONS_PER_EMAIL = 10
MAX_SUBSCRIPTIONS_PER_DAY = 50

# Token expiry for magic links
MAGIC_LINK_EXPIRY_HOURS = 48
