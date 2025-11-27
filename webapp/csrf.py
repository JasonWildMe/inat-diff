"""
CSRF protection for form submissions.
"""
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, HTTPException, status
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from webapp.config import SECRET_KEY

# Token serializer
_serializer = URLSafeTimedSerializer(SECRET_KEY)

# Token validity period (1 hour)
CSRF_TOKEN_MAX_AGE = 3600


def generate_csrf_token() -> str:
    """Generate a new CSRF token."""
    # Include random data to make each token unique
    data = secrets.token_hex(16)
    return _serializer.dumps(data, salt="csrf-token")


def validate_csrf_token(token: str) -> bool:
    """
    Validate a CSRF token.

    Returns True if valid, False otherwise.
    """
    if not token:
        return False

    try:
        _serializer.loads(token, salt="csrf-token", max_age=CSRF_TOKEN_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


async def get_csrf_token(request: Request) -> str:
    """
    Get or create a CSRF token for the current request.

    Stores token in request state for use in templates.
    """
    if not hasattr(request.state, "csrf_token"):
        request.state.csrf_token = generate_csrf_token()
    return request.state.csrf_token


async def verify_csrf_token(request: Request, token: Optional[str] = None) -> None:
    """
    Verify the CSRF token from a form submission.

    Raises HTTPException if token is invalid.
    """
    if token is None:
        # Try to get from form data
        form = await request.form()
        token = form.get("csrf_token")

    if not validate_csrf_token(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired form submission. Please refresh and try again."
        )
