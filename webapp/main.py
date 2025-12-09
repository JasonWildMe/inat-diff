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

import httpx
from webapp.config import BASE_URL, REPORT_STORAGE_DIR, PROCAPTCHA_SITEKEY, PROCAPTCHA_SECRET
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

@app.get("/api/places/autocomplete")
async def places_autocomplete(q: str = ""):
    """
    Autocomplete endpoint for iNaturalist places.
    Returns matching places for the search query.
    """
    if len(q) < 2:
        return {"results": []}

    client = iNatClient()
    try:
        # Search for places matching the query
        # search_places returns a list directly, not a dict
        results = client.search_places(q)

        # Format results for autocomplete
        places = []
        for place in results[:10]:  # Limit to 10 results
            places.append({
                "id": place.get("id"),
                "name": place.get("name"),
                "display_name": place.get("display_name", place.get("name")),
                "place_type": place.get("place_type_name", ""),
                "bbox": place.get("bounding_box_geojson")
            })

        return {"results": places}
    except Exception as e:
        logger.error(f"Place autocomplete error: {e}")
        return {"results": [], "error": str(e)}


@app.get("/api/taxa/autocomplete")
async def taxa_autocomplete(q: str = ""):
    """
    Autocomplete endpoint for iNaturalist taxa.
    Returns matching taxa for the search query.
    """
    if len(q) < 2:
        return {"results": []}

    client = iNatClient()
    try:
        # Search for taxa matching the query
        results = client.search_taxa(q)

        # Format results for autocomplete
        taxa = []
        for taxon in results[:10]:  # Limit to 10 results
            taxa.append({
                "id": taxon.get("id"),
                "name": taxon.get("name"),
                "common_name": taxon.get("preferred_common_name", ""),
                "rank": taxon.get("rank", "").capitalize(),
                "iconic_taxon": taxon.get("iconic_taxon_name", ""),
            })

        return {"results": taxa}
    except Exception as e:
        logger.error(f"Taxa autocomplete error: {e}")
        return {"results": [], "error": str(e)}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page with subscription form."""
    csrf_token = generate_csrf_token()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "csrf_token": csrf_token,
        "procaptcha_sitekey": PROCAPTCHA_SITEKEY
    })


async def verify_procaptcha(token: str) -> bool:
    """Verify Procaptcha token with Prosopo API."""
    if not PROCAPTCHA_SECRET:
        # If no secret configured, skip verification (for development)
        logger.warning("PROCAPTCHA_SECRET not configured, skipping captcha verification")
        return True

    if not token:
        logger.warning("Procaptcha token is empty")
        return False

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.prosopo.io/siteverify",
                json={
                    "secret": PROCAPTCHA_SECRET,
                    "token": token
                },
                headers={"Content-Type": "application/json"},
                timeout=10.0
            )
            result = response.json()
            verified = result.get("verified", False)
            logger.info(f"Procaptcha verification result: {result}")
            return verified
    except Exception as e:
        logger.error(f"Procaptcha verification error: {e}")
        return False


@app.post("/subscribe")
@limiter.limit("10/hour")  # Max 10 subscription attempts per hour per IP
async def create_subscription(
    request: Request,
    email: str = Form(...),
    region: str = Form(...),
    frequency: str = Form("monthly"),
    taxon_filter: Optional[str] = Form(None),
    taxon_id: Optional[str] = Form(None),
    place_id: Optional[str] = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db)
):
    """Create a new subscription (requires email verification)."""
    # Get procaptcha response from form data
    form_data = await request.form()
    procaptcha_response = form_data.get("procaptcha-response", "")

    # Verify captcha first
    if PROCAPTCHA_SECRET:
        captcha_valid = await verify_procaptcha(procaptcha_response)
        if not captcha_valid:
            return templates.TemplateResponse(
                "error.html",
                {
                    "request": request,
                    "error_title": "Verification Failed",
                    "error_message": "Please complete the human verification challenge and try again."
                },
                status_code=400
            )

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
    resolved_place_id = None
    place_info = {}

    # If place_id provided from autocomplete, use it directly
    if place_id and place_id.strip():
        try:
            resolved_place_id = int(place_id.strip())
            # Fetch place info to verify it exists
            place_info = client.get_place_by_id(resolved_place_id)
            if not place_info:
                raise ValueError("Place not found")
        except (ValueError, Exception) as e:
            # Fall back to text search if place_id is invalid
            resolved_place_id = None

    # Fall back to text search if no valid place_id
    if not resolved_place_id:
        try:
            resolved_place_id, place_info = client.resolve_place_with_info(region)
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

    # Resolve taxon_id if provided (before duplicate check)
    resolved_taxon_id = None
    if taxon_id and taxon_id.strip():
        try:
            resolved_taxon_id = int(taxon_id.strip())
        except ValueError:
            resolved_taxon_id = None

    # Check for existing subscription (including taxon filter)
    existing = db.query(Subscription).filter(
        Subscription.email == email.lower(),
        Subscription.region == region,
        Subscription.taxon_id == resolved_taxon_id,
        Subscription.is_active == True
    ).first()

    if existing:
        if existing.is_verified:
            # Build message with taxon filter info if present
            taxon_msg = f" (filtered to {existing.taxon_filter})" if existing.taxon_filter else ""
            return templates.TemplateResponse(
                "message.html",
                {
                    "request": request,
                    "title": "Already Subscribed",
                    "message": f"You already have an active subscription for {region}{taxon_msg}. Check your email for past reports."
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
        place_id=resolved_place_id,
        place_name=place_info.get('name'),
        place_display_name=place_info.get('display_name'),
        frequency=freq_enum,
        taxon_filter=taxon_filter.strip() if taxon_filter else None,
        taxon_id=resolved_taxon_id,
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

    # Check if already verified (handle duplicate clicks/prefetch)
    if subscription.is_verified:
        logger.info(f"Subscription already verified: {subscription.email}")
        # Find existing manage token or create one
        manage_token = db.query(Token).filter(
            Token.subscription_id == subscription.id,
            Token.token_type == "manage"
        ).first()
        if not manage_token:
            manage_token = Token.generate(subscription.id, "manage")
            manage_token.expires_at = datetime.utcnow() + timedelta(days=365)
            db.add(manage_token)
            db.commit()

        return templates.TemplateResponse(
            "verified.html",
            {
                "request": request,
                "subscription": subscription,
                "manage_token": manage_token.token
            }
        )

    # Mark as verified and active
    subscription.is_verified = True
    subscription.is_active = True
    subscription.verified_at = datetime.utcnow()

    # Only set used_at on first actual use
    if token_obj.used_at is None:
        token_obj.used_at = datetime.utcnow()

    db.commit()

    logger.info(f"Subscription verified: {subscription.email} for {subscription.region}")

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
        manage_token=manage_token.token,
        taxon_filter=subscription.taxon_filter
    )

    return templates.TemplateResponse(
        "verified.html",
        {
            "request": request,
            "subscription": subscription,
            "manage_token": manage_token.token
        }
    )


@app.get("/unsubscribe/{token}")
async def unsubscribe_confirm(
    request: Request,
    token: str,
    db: Session = Depends(get_db)
):
    """Show unsubscribe confirmation page (two-step unsubscribe for security scanners)."""
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

    # If already unsubscribed, show message
    if not subscription.is_active:
        return templates.TemplateResponse(
            "message.html",
            {
                "request": request,
                "title": "Already Unsubscribed",
                "message": f"You've already been unsubscribed from {subscription.place_display_name or subscription.region} reports."
            }
        )

    # Show confirmation page (GET request does NOT unsubscribe)
    return templates.TemplateResponse(
        "unsubscribe_confirm.html",
        {
            "request": request,
            "subscription": subscription,
            "token": token
        }
    )


@app.post("/unsubscribe/{token}")
async def unsubscribe(
    request: Request,
    token: str,
    db: Session = Depends(get_db)
):
    """Actually perform the unsubscribe (requires POST from confirmation page)."""
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
            "message": f"You've been unsubscribed from {subscription.place_display_name or subscription.region} reports. You can resubscribe anytime at {BASE_URL}."
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


@app.post("/admin/delete-subscription/{subscription_id}")
async def delete_subscription(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin = Depends(get_current_admin)
):
    """Delete a subscription (requires authentication)."""
    subscription = db.query(Subscription).filter(
        Subscription.id == subscription_id
    ).first()

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    email = subscription.email
    region = subscription.region

    # Delete associated tokens
    db.query(Token).filter(Token.subscription_id == subscription_id).delete()

    # Delete the subscription
    db.delete(subscription)
    db.commit()

    logger.info(f"Admin deleted subscription {subscription_id}: {email} for {region}")

    return {"message": f"Subscription for {email} ({region}) deleted"}


@app.post("/admin/activate-subscription/{subscription_id}")
async def activate_subscription(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin = Depends(get_current_admin)
):
    """Activate an inactive subscription (requires authentication)."""
    subscription = db.query(Subscription).filter(
        Subscription.id == subscription_id
    ).first()

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if subscription.is_active:
        return {"message": f"Subscription for {subscription.email} is already active"}

    subscription.is_active = True
    db.commit()

    logger.info(f"Admin activated subscription {subscription_id}: {subscription.email} for {subscription.region}")

    return {"message": f"Subscription for {subscription.email} ({subscription.place_display_name or subscription.region}) activated"}


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
