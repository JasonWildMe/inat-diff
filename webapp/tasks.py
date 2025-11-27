"""
Celery tasks for background report generation and email delivery.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import sys

from celery import Celery
from sqlalchemy.orm import Session

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp.config import REDIS_URL, REPORT_STORAGE_DIR, DEFAULT_LOOKBACK_YEARS, DEFAULT_RATE_LIMIT
from webapp.models import SessionLocal, Subscription, Report, Token, ReportStatus, Frequency
from webapp.email_service import email_service

# Import the inat_diff library
from inat_diff import SpeciesQuery
from inat_diff.visualize import generate_html

logger = logging.getLogger(__name__)

# Initialize Celery
celery_app = Celery(
    'webapp.tasks',
    broker=REDIS_URL,
    backend=REDIS_URL
)

celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=7200,  # 2 hour hard limit
    task_soft_time_limit=6600,  # 1 hour 50 min soft limit
)

# Apply beat schedule for automated report generation
from webapp.celery_config import configure_celery
configure_celery(celery_app)


@celery_app.task(bind=True, max_retries=3)
def generate_report_task(self, report_id: int):
    """
    Generate a single report for a subscription.

    This task:
    1. Fetches subscription details
    2. Runs the iNaturalist query
    3. Generates HTML report
    4. Sends email with report
    """
    db = SessionLocal()
    try:
        # Get report and subscription
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"Report {report_id} not found")
            return {"error": "Report not found"}

        subscription = report.subscription
        if not subscription.is_active or not subscription.is_verified:
            logger.warning(f"Subscription {subscription.id} is not active/verified")
            report.status = ReportStatus.FAILED
            report.error_message = "Subscription is not active"
            db.commit()
            return {"error": "Subscription not active"}

        # Update report status
        report.status = ReportStatus.RUNNING
        report.started_at = datetime.utcnow()
        db.commit()

        logger.info(f"Generating report {report_id} for {subscription.region}")

        # Initialize query
        query = SpeciesQuery()

        # Calculate time period
        period_days = (report.period_end - report.period_start).days

        # Run the query
        try:
            results = query.find_all_new_species_in_period(
                time_period=f"last {period_days} days",
                region=subscription.region,
                lookback_years=subscription.lookback_years or DEFAULT_LOOKBACK_YEARS,
                rate_limit=DEFAULT_RATE_LIMIT
            )
        except Exception as e:
            logger.error(f"Query failed for report {report_id}: {e}")
            report.status = ReportStatus.FAILED
            report.error_message = str(e)
            db.commit()
            raise self.retry(exc=e, countdown=300)  # Retry in 5 minutes

        # Save JSON results
        json_filename = f"{report.report_uuid}.json"
        json_path = REPORT_STORAGE_DIR / json_filename
        with open(json_path, 'w') as f:
            json.dump(results, f, indent=2, default=str)

        # Generate HTML report
        html_filename = f"{report.report_uuid}.html"
        html_path = REPORT_STORAGE_DIR / html_filename

        report_html = generate_html(results)
        with open(html_path, 'w') as f:
            f.write(report_html)

        # Update report with results
        report.status = ReportStatus.COMPLETED
        report.completed_at = datetime.utcnow()
        report.json_path = str(json_path)
        report.html_path = str(html_path)
        report.total_species = results.get('total_species_in_period', 0)
        report.new_species_count = results.get('new_species_count', 0)
        db.commit()

        logger.info(f"Report {report_id} completed: {report.new_species_count} new species")

        # Send email
        send_report_email_task.delay(report_id)

        return {
            "status": "completed",
            "report_id": report_id,
            "new_species_count": report.new_species_count,
            "total_species": report.total_species
        }

    except Exception as e:
        logger.error(f"Error generating report {report_id}: {e}")
        if report:
            report.status = ReportStatus.FAILED
            report.error_message = str(e)
            db.commit()
        raise
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3)
def send_report_email_task(self, report_id: int):
    """Send the generated report via email."""
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report or report.status != ReportStatus.COMPLETED:
            return {"error": "Report not ready"}

        subscription = report.subscription

        # Read the HTML report
        with open(report.html_path, 'r') as f:
            report_html = f.read()

        # Get or create unsubscribe token
        unsub_token = db.query(Token).filter(
            Token.subscription_id == subscription.id,
            Token.token_type == "unsubscribe"
        ).first()

        if not unsub_token:
            unsub_token = Token.generate(subscription.id, "unsubscribe")
            unsub_token.expires_at = datetime.utcnow() + timedelta(days=365)  # Long-lived
            db.add(unsub_token)
            db.commit()

        # Send email
        success = email_service.send_report_email(
            to_email=subscription.email,
            region=subscription.place_display_name or subscription.region,
            report_html=report_html,
            new_species_count=report.new_species_count,
            total_species=report.total_species,
            report_uuid=report.report_uuid,
            unsubscribe_token=unsub_token.token
        )

        if success:
            report.email_sent = True
            report.email_sent_at = datetime.utcnow()
            subscription.last_report_at = datetime.utcnow()
            db.commit()
            logger.info(f"Report email sent to {subscription.email}")
            return {"status": "sent"}
        else:
            raise Exception("Email sending failed")

    except Exception as e:
        logger.error(f"Error sending report email {report_id}: {e}")
        raise self.retry(exc=e, countdown=60)
    finally:
        db.close()


@celery_app.task
def generate_scheduled_reports(frequency: str = "monthly"):
    """
    Generate reports for all active subscriptions with the given frequency.
    Called by Celery Beat scheduler.
    """
    db = SessionLocal()
    try:
        # Get active subscriptions with matching frequency
        freq_enum = Frequency.MONTHLY if frequency == "monthly" else Frequency.WEEKLY
        subscriptions = db.query(Subscription).filter(
            Subscription.is_active == True,
            Subscription.is_verified == True,
            Subscription.frequency == freq_enum
        ).all()

        logger.info(f"Generating {frequency} reports for {len(subscriptions)} subscriptions")

        # Calculate period
        now = datetime.utcnow()
        if frequency == "monthly":
            # Last month
            period_end = now.replace(day=1) - timedelta(days=1)
            period_start = period_end.replace(day=1)
        else:
            # Last week
            period_end = now - timedelta(days=now.weekday() + 1)
            period_start = period_end - timedelta(days=6)

        reports_created = 0
        for subscription in subscriptions:
            # Create report record
            report = Report(
                subscription_id=subscription.id,
                report_uuid=str(uuid.uuid4()),
                period_start=period_start,
                period_end=period_end,
                status=ReportStatus.PENDING
            )
            db.add(report)
            db.commit()

            # Queue the generation task
            generate_report_task.delay(report.id)
            reports_created += 1

        logger.info(f"Queued {reports_created} {frequency} reports for generation")
        return {"reports_queued": reports_created}

    finally:
        db.close()


@celery_app.task
def send_verification_email_task(subscription_id: int):
    """Send verification email for a new subscription."""
    db = SessionLocal()
    try:
        subscription = db.query(Subscription).filter(
            Subscription.id == subscription_id
        ).first()

        if not subscription:
            return {"error": "Subscription not found"}

        # Generate verification token
        token = Token.generate(subscription_id, "verify")
        db.add(token)
        db.commit()

        # Send email
        success = email_service.send_verification_email(
            to_email=subscription.email,
            token=token.token,
            region=subscription.region
        )

        if success:
            logger.info(f"Verification email sent to {subscription.email}")
            return {"status": "sent"}
        else:
            return {"error": "Email sending failed"}

    finally:
        db.close()
