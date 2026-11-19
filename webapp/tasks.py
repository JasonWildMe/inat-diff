"""
Celery tasks for background report generation and email delivery.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
import sys

from celery import Celery
from celery.exceptions import Retry
from sqlalchemy import func, or_, and_
from sqlalchemy.orm import Session

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp.config import REDIS_URL, REPORT_STORAGE_DIR, DEFAULT_LOOKBACK_YEARS, DEFAULT_RATE_LIMIT
from webapp.models import SessionLocal, Subscription, Report, Token, ReportStatus, Frequency, get_or_create_token
from webapp.email_service import email_service

# Import the inat_diff library
from inat_diff import SpeciesQuery
from inat_diff.visualize import generate_html

logger = logging.getLogger(__name__)

TASK_TIME_LIMIT = 7200  # seconds

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
    task_time_limit=TASK_TIME_LIMIT,  # 2 hour hard limit
    task_soft_time_limit=6600,  # 1 hour 50 min soft limit
)

# A RUNNING report older than the hard time limit (+ grace) belongs to a dead worker.
STALE_RUNNING_AFTER = timedelta(seconds=TASK_TIME_LIMIT + 900)
# A delivery claim older than this (worker died mid-send) may be retried.
STALE_SEND_CLAIM_AFTER = timedelta(minutes=15)

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
    report = None
    claim_started_at = None  # set once this attempt owns the RUNNING claim
    try:
        # Get report and subscription
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            logger.error(f"Report {report_id} not found")
            return {"error": "Report not found"}
        if report.status == ReportStatus.COMPLETED:
            # Duplicate/redelivered task: don't regenerate or re-send.
            logger.info(f"Report {report_id} already completed; skipping")
            return {"status": "already_completed", "report_id": report_id}

        subscription = report.subscription
        if not subscription.is_active or not subscription.is_verified:
            logger.warning(f"Subscription {subscription.id} is not active/verified")
            # Only a not-yet-claimed report; never touch a live worker's RUNNING row.
            db.query(Report).filter(
                Report.id == report_id,
                Report.status.in_([ReportStatus.PENDING, ReportStatus.FAILED])
            ).update({Report.status: ReportStatus.FAILED,
                      Report.error_message: "Subscription is not active"},
                     synchronize_session=False)
            db.commit()
            return {"error": "Subscription not active"}

        # Atomically claim the report so two workers never generate it concurrently
        claim_started_at = datetime.utcnow()
        claimed = db.query(Report).filter(
            Report.id == report_id,
            or_(
                Report.status.in_([ReportStatus.PENDING, ReportStatus.FAILED]),
                and_(Report.status == ReportStatus.RUNNING,
                     Report.started_at < datetime.utcnow() - STALE_RUNNING_AFTER)
            )
        ).update({
            Report.status: ReportStatus.RUNNING,
            Report.started_at: claim_started_at
        }, synchronize_session=False)
        db.commit()
        if not claimed:
            claim_started_at = None
            logger.info(f"Report {report_id} is already being generated; skipping")
            return {"status": "already_running", "report_id": report_id}
        db.refresh(report)

        logger.info(f"Generating report {report_id} for {subscription.region}")

        # Initialize query
        query = SpeciesQuery()

        # Query exactly the stored period (inclusive dates), not "last N days"
        # relative to whenever this task happens to run or retry.
        time_period = f"{report.period_start:%Y-%m-%d} to {report.period_end:%Y-%m-%d}"

        # Run the query
        try:
            results = query.find_all_new_species_in_period(
                time_period=time_period,
                region=subscription.region,
                lookback_years=subscription.lookback_years or DEFAULT_LOOKBACK_YEARS,
                rate_limit=DEFAULT_RATE_LIMIT,
                taxon_id=subscription.taxon_id,
                place_id=subscription.place_id
            )
        except Exception as e:
            logger.error(f"Query failed for report {report_id}: {e}")
            db.rollback()
            # Only if we still own the claim (it may have been reclaimed as stale).
            db.query(Report).filter(
                Report.id == report_id,
                Report.status == ReportStatus.RUNNING,
                Report.started_at == claim_started_at
            ).update({Report.status: ReportStatus.FAILED, Report.error_message: str(e)},
                     synchronize_session=False)
            db.commit()
            claim_started_at = None  # released; outer handler must not write again
            raise self.retry(exc=e, countdown=300)  # Retry in 5 minutes

        # Save JSON results
        json_filename = f"{report.report_uuid}.json"
        json_path = REPORT_STORAGE_DIR / json_filename
        with open(json_path, 'w') as f:
            json.dump(results, f, indent=2, default=str)

        # Generate HTML report with quality grades
        html_filename = f"{report.report_uuid}.html"
        html_path = REPORT_STORAGE_DIR / html_filename

        report_html = generate_html(results, include_quality=True, rate_limit=DEFAULT_RATE_LIMIT)
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

    except Retry:
        raise
    except Exception as e:
        logger.error(f"Error generating report {report_id}: {e}")
        if report is not None and claim_started_at is not None:
            db.rollback()
            db.query(Report).filter(
                Report.id == report_id,
                Report.status == ReportStatus.RUNNING,
                Report.started_at == claim_started_at
            ).update({Report.status: ReportStatus.FAILED, Report.error_message: str(e)},
                     synchronize_session=False)
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
        if report.email_sent:
            # Duplicate/retried task after a successful send: never mail twice.
            return {"status": "already_sent"}

        subscription = report.subscription
        if not subscription.is_active or not subscription.is_verified:
            # Unsubscribed between generation and delivery.
            logger.info(f"Skipping report {report_id} email: subscription {subscription.id} inactive")
            return {"status": "skipped_inactive"}


        # Read the HTML report
        with open(report.html_path, 'r') as f:
            report_html = f.read()

        unsub_token = get_or_create_token(db, subscription.id, "unsubscribe")

        # Re-check right before sending: the user may have unsubscribed meanwhile.
        db.refresh(subscription)
        if not subscription.is_active or not subscription.is_verified:
            return {"status": "skipped_inactive"}

        # Atomic delivery claim (email_sent_at doubles as an in-progress marker) so
        # two concurrent tasks can't both send.
        now = datetime.utcnow()
        claimed = db.query(Report).filter(
            Report.id == report_id,
            Report.status == ReportStatus.COMPLETED,
            Report.subscription.has(and_(Subscription.is_active == True,
                                         Subscription.is_verified == True)),
            Report.email_sent == False,
            or_(Report.email_sent_at.is_(None),
                Report.email_sent_at < now - STALE_SEND_CLAIM_AFTER)
        ).update({Report.email_sent_at: now}, synchronize_session=False)
        db.commit()
        if not claimed:
            return {"status": "send_in_progress"}

        # Send email
        success = email_service.send_report_email(
            to_email=subscription.email,
            region=subscription.place_display_name or subscription.region,
            report_html=report_html,
            new_species_count=report.new_species_count,
            total_species=report.total_species,
            report_uuid=report.report_uuid,
            unsubscribe_token=unsub_token.token,
            frequency=subscription.frequency.value,
            taxon_filter=subscription.taxon_filter
        )

        if success:
            report.email_sent = True
            report.email_sent_at = datetime.utcnow()
            subscription.last_report_at = datetime.utcnow()
            db.commit()
            logger.info(f"Report email sent to {subscription.email}")
            return {"status": "sent"}
        # Provider definitively rejected: release the claim so the retry can send.
        db.query(Report).filter(Report.id == report_id, Report.email_sent == False,
                                Report.email_sent_at == now) \
            .update({Report.email_sent_at: None}, synchronize_session=False)
        db.commit()
        raise Exception("Email sending failed")

    except Retry:
        raise
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

        # Calculate period as whole UTC calendar days (inclusive)
        today = datetime.utcnow().date()
        if frequency == "monthly":
            # Previous calendar month
            last_day = today.replace(day=1) - timedelta(days=1)
            first_day = last_day.replace(day=1)
        else:
            # Previous Monday..Sunday
            last_day = today - timedelta(days=today.weekday() + 1)
            first_day = last_day - timedelta(days=6)
        period_start = datetime.combine(first_day, dtime.min)
        period_end = datetime.combine(last_day, dtime.min)

        reports_created = 0
        skipped = 0
        for subscription in subscriptions:
            # Idempotency: Beat re-runs or a manual trigger must not create a
            # second report (and second email) for the same period.
            # Compare calendar dates: pre-upgrade rows carry a time of day.
            existing = db.query(Report).filter(
                Report.subscription_id == subscription.id,
                func.date(Report.period_start) == first_day,
                func.date(Report.period_end) == last_day
            ).first()
            if existing:
                if existing.status == ReportStatus.PENDING:
                    # Earlier queue publish may have failed; the task's atomic
                    # claim makes a duplicate enqueue harmless.
                    try:
                        generate_report_task.delay(existing.id)
                    except Exception as e:
                        logger.error(f"Failed to requeue report {existing.id}: {e}")
                skipped += 1
                continue

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

            # Queue the generation task; one broker hiccup must not skip everyone after it
            try:
                generate_report_task.delay(report.id)
                reports_created += 1
            except Exception as e:
                logger.error(f"Failed to queue report {report.id} for subscription {subscription.id}: {e}")

        logger.info(f"Queued {reports_created} {frequency} reports ({skipped} already existed)")
        return {"reports_queued": reports_created, "skipped_existing": skipped}

    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3)
def send_verification_email_task(self, subscription_id: int):
    """Send verification email for a new subscription."""
    db = SessionLocal()
    try:
        subscription = db.query(Subscription).filter(
            Subscription.id == subscription_id
        ).first()

        if not subscription:
            return {"error": "Subscription not found"}
        if subscription.is_verified:
            return {"status": "already_verified"}

        # Generate verification token
        token = Token.generate(subscription_id, "verify")
        db.add(token)
        db.commit()

        # Send email
        success = email_service.send_verification_email(
            to_email=subscription.email,
            token=token.token,
            region=subscription.region,
            frequency=subscription.frequency.value,
            taxon_filter=subscription.taxon_filter
        )

        if success:
            logger.info(f"Verification email sent to {subscription.email}")
            return {"status": "sent"}
        raise self.retry(exc=Exception("Verification email sending failed"), countdown=120)

    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3)
def send_confirmation_email_task(self, subscription_id: int):
    """Send the 'subscription confirmed' email with the manage link."""
    db = SessionLocal()
    try:
        subscription = db.query(Subscription).filter(
            Subscription.id == subscription_id
        ).first()
        if not subscription or not subscription.is_verified or not subscription.is_active:
            return {"status": "skipped"}

        manage_token = get_or_create_token(db, subscription.id, "manage")
        success = email_service.send_subscription_confirmed_email(
            to_email=subscription.email,
            region=subscription.place_display_name or subscription.region,
            frequency=subscription.frequency.value,
            manage_token=manage_token.token,
            taxon_filter=subscription.taxon_filter
        )
        if success:
            return {"status": "sent"}
        raise self.retry(exc=Exception("Confirmation email sending failed"), countdown=120)
    finally:
        db.close()
