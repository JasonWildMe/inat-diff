"""
Authentication utilities for admin dashboard.
"""
import logging
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from webapp.models import get_db, AdminUser

logger = logging.getLogger(__name__)
security = HTTPBasic()


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password_bytes, salt).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        password_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except Exception:
        return False


def get_current_admin(
    credentials: HTTPBasicCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> AdminUser:
    """
    Dependency to verify admin credentials.

    Usage:
        @app.get("/admin")
        def admin_page(admin: AdminUser = Depends(get_current_admin)):
            ...
    """
    # Look up user
    admin = db.query(AdminUser).filter(
        AdminUser.username == credentials.username
    ).first()

    if not admin:
        logger.warning(f"Failed login attempt: unknown user '{credentials.username}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Verify password
    if not verify_password(credentials.password, admin.password_hash):
        logger.warning(f"Failed login attempt: wrong password for user '{credentials.username}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    logger.info(f"Successful admin login: {credentials.username}")
    return admin


def create_admin_user(db: Session, username: str, password: str) -> AdminUser:
    """Create a new admin user."""
    # Check if user exists
    existing = db.query(AdminUser).filter(AdminUser.username == username).first()
    if existing:
        raise ValueError(f"Admin user '{username}' already exists")

    admin = AdminUser(
        username=username,
        password_hash=hash_password(password)
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin
