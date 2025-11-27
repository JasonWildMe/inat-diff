"""
Celery configuration with Beat schedule for automated report generation.
"""
from celery.schedules import crontab

# Celery Beat Schedule
beat_schedule = {
    # Monthly reports - runs on the 1st of each month at 6 AM UTC
    'generate-monthly-reports': {
        'task': 'webapp.tasks.generate_scheduled_reports',
        'schedule': crontab(
            minute=0,
            hour=6,
            day_of_month=1
        ),
        'kwargs': {'frequency': 'monthly'},
    },
    # Weekly reports - runs every Monday at 6 AM UTC
    'generate-weekly-reports': {
        'task': 'webapp.tasks.generate_scheduled_reports',
        'schedule': crontab(
            minute=0,
            hour=6,
            day_of_week=1  # Monday
        ),
        'kwargs': {'frequency': 'weekly'},
    },
}

# Apply configuration
def configure_celery(app):
    """Apply beat schedule to Celery app."""
    app.conf.beat_schedule = beat_schedule
    app.conf.timezone = 'UTC'
    return app
