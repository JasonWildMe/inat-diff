"""
FastAPI web application for iNaturalist Invasives Monitor.
"""
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from email_validator import validate_email, EmailNotValidError

import httpx
from webapp.config import BASE_URL, REPORT_STORAGE_DIR, PROCAPTCHA_SITEKEY, PROCAPTCHA_SECRET
from webapp.models import (
    init_db, get_db, Subscription, Report, Token,
    Frequency, ReportStatus, get_or_create_token
)
from webapp.tasks import (
    send_verification_email_task,
    send_confirmation_email_task,
    generate_report_task,
    generate_scheduled_reports
)
from webapp.auth import get_current_admin, require_admin_csrf_header
from webapp.csrf import generate_csrf_token, validate_csrf_token

# Import inat_diff for place validation
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from inat_diff import iNatClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Minimum gap between verification emails for the same pending subscription
VERIFY_RESEND_COOLDOWN_MINUTES = 15

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Initialize app
app = FastAPI(
    title="iNaturalist Invasives Monitor",
    description="Automated invasive species monitoring reports from iNaturalist",
    version="1.0.0",
    # No public API consumers: don't expose interactive docs / schema.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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
    # Bearer-token URLs (/verify, /manage, /unsubscribe) must not leak via Referer
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith(("/verify/", "/manage/", "/unsubscribe/", "/admin")):
        response.headers["Cache-Control"] = "no-store"
    if request.url.path.startswith("/report/"):
        # Generated reports are static HTML with inline styles and no scripts.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
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
        return resolved.is_relative_to(REPORT_STORAGE_DIR.resolve()) and resolved.is_file()
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
@limiter.limit("60/minute")
def places_autocomplete(request: Request, q: str = ""):
    """
    Autocomplete endpoint for iNaturalist places.
    Returns matching places for the search query.
    """
    if len(q) < 2 or len(q) > 100:
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
        return JSONResponse({"results": [], "error": "Place search is temporarily unavailable"}, status_code=502)


@app.get("/api/taxa/autocomplete")
@limiter.limit("60/minute")
def taxa_autocomplete(request: Request, q: str = ""):
    """
    Autocomplete endpoint for iNaturalist taxa.
    Returns matching taxa for the search query.
    """
    if len(q) < 2 or len(q) > 100:
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
        return JSONResponse({"results": [], "error": "Taxon search is temporarily unavailable"}, status_code=502)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Home page with subscription form."""
    csrf_token = generate_csrf_token()
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "csrf_token": csrf_token,
        "procaptcha_sitekey": PROCAPTCHA_SITEKEY
    })


def verify_procaptcha(token: str) -> bool:
    """Verify Procaptcha token with Prosopo API."""
    if not PROCAPTCHA_SECRET:
        # If no secret configured, skip verification (for development)
        logger.warning("PROCAPTCHA_SECRET not configured, skipping captcha verification")
        return True

    if not token:
        logger.warning("Procaptcha token is empty")
        return False

    try:
        response = httpx.post(
            "https://api.prosopo.io/siteverify",
            json={
                "secret": PROCAPTCHA_SECRET,
                "token": token
            },
            timeout=10.0
        )
        result = response.json()
        verified = result.get("verified") is True
        logger.info(f"Procaptcha verification result: verified={verified}")
        return verified
    except Exception as e:
        logger.error(f"Procaptcha verification error: {e}")
        return False


@app.post("/subscribe")
@limiter.limit("10/hour")  # Max 10 subscription attempts per hour per IP
def create_subscription(
    request: Request,
    email: str = Form(...),
    region: str = Form(...),
    frequency: str = Form("monthly"),
    taxon_filter: Optional[str] = Form(None),
    taxon_id: Optional[str] = Form(None),
    place_id: Optional[str] = Form(None),
    csrf_token: str = Form(...),
    procaptcha_response: str = Form("", alias="procaptcha-response"),
    db: Session = Depends(get_db)
):
    """Create a new subscription (requires email verification)."""
    region = region.strip()
    taxon_filter = taxon_filter.strip() if taxon_filter else None

    # Verify captcha first
    if PROCAPTCHA_SECRET:
        captcha_valid = verify_procaptcha(procaptcha_response)
        if not captcha_valid:
            return templates.TemplateResponse(
                request, "error.html",
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
            request, "error.html",
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
            request, "error.html",
            {
                "request": request,
                "error_title": "Invalid Email",
                "error_message": "Please enter a valid email address."
            },
            status_code=400
        )

    # Validate input lengths
    if not region or len(region) > 200 or len(taxon_filter or "") > 200:
        return templates.TemplateResponse(
            request, "error.html",
            {
                "request": request,
                "error_title": "Invalid Region",
                "error_message": "Please enter a region name (up to 200 characters) and a taxon filter of at most 200 characters."
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
                request, "error.html",
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
        if resolved_taxon_id is not None and resolved_taxon_id <= 0:
            resolved_taxon_id = None
    if taxon_filter and resolved_taxon_id is None:
        # Free text without a selected taxon would silently produce an
        # all-species report labelled as filtered.
        return templates.TemplateResponse(
            request, "error.html",
            {
                "request": request,
                "error_title": "Invalid Taxon Filter",
                "error_message": "Please choose a taxon from the suggestions list, or leave the taxon filter empty."
            },
            status_code=400
        )

    # Same response for new / pending / already-subscribed, so the form can't be
    # used to discover who is subscribed to what.
    check_email_response = templates.TemplateResponse(
        request, "message.html",
        {
            "request": request,
            "title": "Check Your Email",
            "message": f"If this subscription isn't already active, we've sent a verification email to {email}. Please click the link to confirm your subscription."
        }
    )

    # Check for existing subscription (same email, resolved place and taxon filter)
    # Text match only for legacy rows saved without a place ID, so two different
    # places with the same name (e.g. Portland OR / ME) stay distinct.
    place_match = or_(
        Subscription.place_id == resolved_place_id,
        and_(Subscription.place_id.is_(None), Subscription.region == region)
    ) if resolved_place_id else (Subscription.region == region)
    existing = db.query(Subscription).filter(
        Subscription.email == email.lower(),
        place_match,
        Subscription.taxon_id == resolved_taxon_id,
        Subscription.is_active == True
    ).first()

    if existing:
        if not existing.is_verified:
            # Resend verification, at most once per cooldown window per subscription
            # (prevents using the form to mail-bomb a third party).
            cooldown_start = datetime.utcnow() - timedelta(minutes=VERIFY_RESEND_COOLDOWN_MINUTES)
            recent = db.query(Token.id).filter(
                Token.subscription_id == existing.id,
                Token.token_type == "verify",
                Token.created_at >= cooldown_start
            ).first()
            if not recent:
                send_verification_email_task.delay(existing.id)
        return check_email_response

    # Create new subscription
    freq_enum = Frequency.WEEKLY if frequency == "weekly" else Frequency.MONTHLY

    subscription = Subscription(
        email=email.lower().strip(),
        region=region,
        place_id=resolved_place_id,
        place_name=place_info.get('name'),
        place_display_name=place_info.get('display_name'),
        frequency=freq_enum,
        taxon_filter=taxon_filter,
        taxon_id=resolved_taxon_id,
        is_verified=False,
        is_active=True
    )

    db.add(subscription)
    db.commit()

    # Send verification email
    send_verification_email_task.delay(subscription.id)

    logger.info(f"New subscription created: {email} for {region}")

    return check_email_response


@app.get("/verify/{token}")
def verify_subscription(
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
            request, "error.html",
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
        manage_token = get_or_create_token(db, subscription.id, "manage")

        return templates.TemplateResponse(
            request, "verified.html",
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

    manage_token = get_or_create_token(db, subscription.id, "manage")

    # Send confirmation email in the background (retries on provider failure)
    send_confirmation_email_task.delay(subscription.id)

    return templates.TemplateResponse(
        request, "verified.html",
        {
            "request": request,
            "subscription": subscription,
            "manage_token": manage_token.token
        }
    )


@app.get("/unsubscribe/{token}")
def unsubscribe_confirm(
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
            request, "error.html",
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
            request, "message.html",
            {
                "request": request,
                "title": "Already Unsubscribed",
                "message": f"You've already been unsubscribed from {subscription.place_display_name or subscription.region} reports."
            }
        )

    # Show confirmation page (GET request does NOT unsubscribe)
    return templates.TemplateResponse(
        request, "unsubscribe_confirm.html",
        {
            "request": request,
            "subscription": subscription,
            "token": token
        }
    )


@app.post("/unsubscribe/{token}")
def unsubscribe(
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
            request, "error.html",
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
        request, "message.html",
        {
            "request": request,
            "title": "Unsubscribed",
            "message": f"You've been unsubscribed from {subscription.place_display_name or subscription.region} reports. You can resubscribe anytime at {BASE_URL}."
        }
    )


@app.get("/manage/{token}", response_class=HTMLResponse)
def manage_subscription(
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
            request, "error.html",
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

    unsub_token = get_or_create_token(db, subscription.id, "unsubscribe")

    return templates.TemplateResponse(
        request, "manage.html",
        {
            "request": request,
            "subscription": subscription,
            "reports": reports,
            "token": token,
            "unsubscribe_token": unsub_token.token
        }
    )


@app.get("/report/{report_uuid}", response_class=HTMLResponse)
def view_report(
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
def download_report_json(
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
def admin_dashboard(
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

    # Get all subscriptions (searchable/scrollable in UI)
    recent_subscriptions = db.query(Subscription).order_by(
        Subscription.created_at.desc()
    ).all()

    # Get reports from last 90 days (searchable/scrollable in UI)
    ninety_days_ago = datetime.utcnow() - timedelta(days=90)
    recent_reports = db.query(Report).filter(
        Report.created_at >= ninety_days_ago
    ).order_by(
        Report.created_at.desc()
    ).all()

    return templates.TemplateResponse(
        request, "admin/dashboard.html",
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


@app.post("/admin/trigger-reports/{frequency}", dependencies=[Depends(require_admin_csrf_header)])
def trigger_reports(
    frequency: str,
    db: Session = Depends(get_db),
    admin = Depends(get_current_admin)
):
    """Manually trigger report generation for testing (requires authentication)."""
    if frequency not in ["monthly", "weekly"]:
        raise HTTPException(status_code=400, detail="Invalid frequency")

    generate_scheduled_reports.delay(frequency)

    return {"message": f"Triggered {frequency} report generation"}


@app.post("/admin/generate-report/{subscription_id}", dependencies=[Depends(require_admin_csrf_header)])
def generate_single_report(
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


@app.post("/admin/resend-verification/{subscription_id}", dependencies=[Depends(require_admin_csrf_header)])
def resend_verification(
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


@app.post("/admin/delete-subscription/{subscription_id}", dependencies=[Depends(require_admin_csrf_header)])
def delete_subscription(
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


@app.post("/admin/activate-subscription/{subscription_id}", dependencies=[Depends(require_admin_csrf_header)])
def activate_subscription(
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
def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
