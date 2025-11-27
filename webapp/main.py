"""
FastAPI web application for iNaturalist Invasives Monitor.
"""
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from email_validator import validate_email, EmailNotValidError

from webapp.config import BASE_URL, REPORT_STORAGE_DIR
from webapp.models import (
    init_db, get_db, Subscription, Report, Token,
    Frequency, ReportStatus
)
from webapp.tasks import (
    send_verification_email_task,
    generate_report_task,
    generate_scheduled_reports
)
from webapp.email_service import email_service
from webapp.auth import get_current_admin
from webapp.csrf import generate_csrf_token, validate_csrf_token

# Import inat_diff for place validation
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from inat_diff import iNatClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Initialize app
app = FastAPI(
    title="iNaturalist Invasives Monitor",
    description="Automated invasive species monitoring reports from iNaturalist",
    version="1.0.0"
)

# Add rate limiter to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Mount static files and templates
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# =============================================================================
# Security Middleware
# =============================================================================

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Only add HSTS in production (when using HTTPS)
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# =============================================================================
# Helper Functions
# =============================================================================

def validate_report_path(file_path: str) -> bool:
    """Validate that a report file path is within the allowed directory."""
    if not file_path:
        return False
    try:
        # Resolve to absolute path and check it's within REPORT_STORAGE_DIR
        resolved = Path(file_path).resolve()
        storage_dir = REPORT_STORAGE_DIR.resolve()
        return str(resolved).startswith(str(storage_dir)) and resolved.exists()
    except Exception:
        return False


# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    init_db()
    logger.info("Database initialized")


# =============================================================================
# Public Routes
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page with subscription form."""
    csrf_token = generate_csrf_token()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "csrf_token": csrf_token
    })


@app.post("/subscribe")
@limiter.limit("10/hour")  # Max 10 subscription attempts per hour per IP
async def create_subscription(
    request: Request,
    email: str = Form(...),
    region: str = Form(...),
    frequency: str = Form("monthly"),
    taxon_filter: Optional[str] = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db)
):
    """Create a new subscription (requires email verification)."""
    # Validate CSRF token
    if not validate_csrf_token(csrf_token):
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_title": "Form Expired",
                "error_message": "Your form submission has expired. Please go back and try again."
            },
            status_code=403
        )

    # Validate email format
    try:
        validated = validate_email(email, check_deliverability=False)
        email = validated.normalized
    except EmailNotValidError as e:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_title": "Invalid Email",
                "error_message": "Please enter a valid email address."
            },
            status_code=400
        )

    # Validate input lengths
    if len(region) > 200:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_title": "Invalid Region",
                "error_message": "Region name is too long. Please use a shorter name."
            },
            status_code=400
        )

    # Validate region by resolving with iNaturalist
    client = iNatClient()
    try:
        place_id, place_info = client.resolve_place_with_info(region)
    except Exception as e:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_title": "Invalid Region",
                "error_message": "Could not find this region in iNaturalist. Please try a different region name (e.g., 'Oregon', 'Florida', 'California')."
            },
            status_code=400
        )

    # Check for existing subscription
    existing = db.query(Subscription).filter(
        Subscription.email == email.lower(),
        Subscription.region == region,
        Subscription.is_active == True
    ).first()

    if existing:
        if existing.is_verified:
            return templates.TemplateResponse(
                "message.html",
                {
                    "request": request,
                    "title": "Already Subscribed",
                    "message": f"You already have an active subscription for {region}. Check your email for past reports."
                }
            )
        else:
            # Resend verification
            send_verification_email_task.delay(existing.id)
            return templates.TemplateResponse(
                "message.html",
                {
                    "request": request,
                    "title": "Verification Pending",
                    "message": "We've resent the verification email. Please check your inbox."
                }
            )

    # Create new subscription
    freq_enum = Frequency.WEEKLY if frequency == "weekly" else Frequency.MONTHLY

    subscription = Subscription(
        email=email.lower().strip(),
        region=region.strip(),
        place_id=place_id,
        place_name=place_info.get('name'),
        place_display_name=place_info.get('display_name'),
        frequency=freq_enum,
        taxon_filter=taxon_filter.strip() if taxon_filter else None,
        is_verified=False,
        is_active=True
    )

    db.add(subscription)
    db.commit()

    # Send verification email
    send_verification_email_task.delay(subscription.id)

    logger.info(f"New subscription created: {email} for {region}")

    return templates.TemplateResponse(
        "message.html",
        {
            "request": request,
            "title": "Check Your Email",
            "message": f"We've sent a verification email to {email}. Please click the link to confirm your subscription."
        }
    )


@app.get("/verify/{token}")
async def verify_subscription(
    request: Request,
    token: str,
    db: Session = Depends(get_db)
):
    """Verify email and activate subscription."""
    token_obj = db.query(Token).filter(
        Token.token == token,
        Token.token_type == "verify"
    ).first()

    if not token_obj or not token_obj.is_valid:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_title": "Invalid Link",
                "error_message": "This verification link is invalid or has expired. Please subscribe again."
            },
            status_code=400
        )

    subscription = token_obj.subscription

    # Mark as verified
    subscription.is_verified = True
    subscription.verified_at = datetime.utcnow()
    token_obj.used_at = datetime.utcnow()
    db.commit()

    # Generate manage token for future use
    manage_token = Token.generate(subscription.id, "manage")
    manage_token.expires_at = datetime.utcnow() + timedelta(days=365)
    db.add(manage_token)
    db.commit()

    # Send confirmation email
    email_service.send_subscription_confirmed_email(
        to_email=subscription.email,
        region=subscription.place_display_name or subscription.region,
        frequency=subscription.frequency.value,
        manage_token=manage_token.token
    )

    logger.info(f"Subscription verified: {subscription.email} for {subscription.region}")

    return templates.TemplateResponse(
        "verified.html",
        {
            "request": request,
            "subscription": subscription,
            "manage_token": manage_token.token
        }
    )


@app.get("/unsubscribe/{token}")
async def unsubscribe(
    request: Request,
    token: str,
    db: Session = Depends(get_db)
):
    """Unsubscribe from reports."""
    token_obj = db.query(Token).filter(
        Token.token == token,
        Token.token_type == "unsubscribe"
    ).first()

    if not token_obj:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_title": "Invalid Link",
                "error_message": "This unsubscribe link is invalid."
            },
            status_code=400
        )

    subscription = token_obj.subscription
    subscription.is_active = False
    db.commit()

    logger.info(f"Unsubscribed: {subscription.email} from {subscription.region}")

    return templates.TemplateResponse(
        "message.html",
        {
            "request": request,
            "title": "Unsubscribed",
            "message": f"You've been unsubscribed from {subscription.region} reports. You can resubscribe anytime at {BASE_URL}."
        }
    )


@app.get("/manage/{token}", response_class=HTMLResponse)
async def manage_subscription(
    request: Request,
    token: str,
    db: Session = Depends(get_db)
):
    """View and manage subscription."""
    token_obj = db.query(Token).filter(
        Token.token == token,
        Token.token_type == "manage"
    ).first()

    if not token_obj or not token_obj.is_valid:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_title": "Invalid Link",
                "error_message": "This management link is invalid or has expired."
            },
            status_code=400
        )

    subscription = token_obj.subscription

    # Get recent reports
    reports = db.query(Report).filter(
        Report.subscription_id == subscription.id
    ).order_by(Report.created_at.desc()).limit(10).all()

    # Get or create unsubscribe token
    unsub_token = db.query(Token).filter(
        Token.subscription_id == subscription.id,
        Token.token_type == "unsubscribe"
    ).first()

    if not unsub_token:
        unsub_token = Token.generate(subscription.id, "unsubscribe")
        unsub_token.expires_at = datetime.utcnow() + timedelta(days=365)
        db.add(unsub_token)
        db.commit()

    return templates.TemplateResponse(
        "manage.html",
        {
            "request": request,
            "subscription": subscription,
            "reports": reports,
            "token": token,
            "unsubscribe_token": unsub_token.token
        }
    )


@app.get("/report/{report_uuid}", response_class=HTMLResponse)
async def view_report(
    request: Request,
    report_uuid: str,
    db: Session = Depends(get_db)
):
    """View a generated report."""
    report = db.query(Report).filter(Report.report_uuid == report_uuid).first()

    if not report or report.status != ReportStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Report not found")

    # Validate file path is within allowed directory (prevent path traversal)
    if not validate_report_path(report.html_path):
        logger.error(f"Invalid report path attempted: {report.html_path}")
        raise HTTPException(status_code=404, detail="Report not found")

    return FileResponse(report.html_path, media_type="text/html")


@app.get("/report/{report_uuid}/json")
async def download_report_json(
    report_uuid: str,
    db: Session = Depends(get_db)
):
    """Download report as JSON."""
    report = db.query(Report).filter(Report.report_uuid == report_uuid).first()

    if not report or report.status != ReportStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Report not found")

    # Validate file path is within allowed directory (prevent path traversal)
    if not validate_report_path(report.json_path):
        logger.error(f"Invalid report path attempted: {report.json_path}")
        raise HTTPException(status_code=404, detail="Report not found")

    return FileResponse(
        report.json_path,
        media_type="application/json",
        filename=f"invasives-report-{report_uuid}.json"
    )


# =============================================================================
# Admin Routes
# =============================================================================

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    admin = Depends(get_current_admin)
):
    """Admin dashboard showing subscriptions and reports (requires authentication)."""
    # Get stats
    total_subscriptions = db.query(Subscription).count()
    active_subscriptions = db.query(Subscription).filter(
        Subscription.is_active == True,
        Subscription.is_verified == True
    ).count()
    total_reports = db.query(Report).count()
    pending_reports = db.query(Report).filter(
        Report.status.in_([ReportStatus.PENDING, ReportStatus.RUNNING])
    ).count()

    # Get recent subscriptions
    recent_subscriptions = db.query(Subscription).order_by(
        Subscription.created_at.desc()
    ).limit(20).all()

    # Get recent reports
    recent_reports = db.query(Report).order_by(
        Report.created_at.desc()
    ).limit(20).all()

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "stats": {
                "total_subscriptions": total_subscriptions,
                "active_subscriptions": active_subscriptions,
                "total_reports": total_reports,
                "pending_reports": pending_reports
            },
            "recent_subscriptions": recent_subscriptions,
            "recent_reports": recent_reports
        }
    )


@app.post("/admin/trigger-reports/{frequency}")
async def trigger_reports(
    frequency: str,
    db: Session = Depends(get_db),
    admin = Depends(get_current_admin)
):
    """Manually trigger report generation for testing (requires authentication)."""
    if frequency not in ["monthly", "weekly"]:
        raise HTTPException(status_code=400, detail="Invalid frequency")

    generate_scheduled_reports.delay(frequency)

    return {"message": f"Triggered {frequency} report generation"}


@app.post("/admin/generate-report/{subscription_id}")
async def generate_single_report(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin = Depends(get_current_admin)
):
    """Generate a report for a specific subscription (requires authentication)."""
    subscription = db.query(Subscription).filter(
        Subscription.id == subscription_id
    ).first()

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Determine period based on frequency
    now = datetime.utcnow()
    if subscription.frequency == Frequency.MONTHLY:
        period_end = now
        period_start = now - timedelta(days=30)
    else:
        period_end = now
        period_start = now - timedelta(days=7)

    # Create report
    report = Report(
        subscription_id=subscription.id,
        report_uuid=str(uuid.uuid4()),
        period_start=period_start,
        period_end=period_end,
        status=ReportStatus.PENDING
    )
    db.add(report)
    db.commit()

    # Queue generation
    generate_report_task.delay(report.id)

    return {
        "message": "Report generation started",
        "report_id": report.id,
        "report_uuid": report.report_uuid
    }


@app.post("/admin/resend-verification/{subscription_id}")
async def resend_verification(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin = Depends(get_current_admin)
):
    """Resend verification email for an unverified subscription."""
    subscription = db.query(Subscription).filter(
        Subscription.id == subscription_id
    ).first()

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if subscription.is_verified:
        return {"message": "Subscription already verified", "status": "already_verified"}

    # Queue verification email
    send_verification_email_task.delay(subscription.id)

    return {"message": f"Verification email sent to {subscription.email}"}


# =============================================================================
# Health Check
# =============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
