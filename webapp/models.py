"""
Database models for the iNat Invasives web application.
"""
import secrets
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, ForeignKey, Enum as SQLEnum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

from webapp.config import DATABASE_URL, MAGIC_LINK_EXPIRY_HOURS

Base = declarative_base()

# Create engine with connection pooling
# SQLite doesn't support pool_size, so we check the database type
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False}  # Allow multi-threaded access
    )
else:
    # PostgreSQL/MySQL with connection pooling
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_size=5,          # Number of persistent connections
        max_overflow=10,      # Extra connections when pool is exhausted
        pool_timeout=30,      # Seconds to wait for a connection
        pool_recycle=1800,    # Recycle connections after 30 minutes
        pool_pre_ping=True    # Verify connections before using
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Frequency(str, Enum):
    MONTHLY = "monthly"
    WEEKLY = "weekly"


class ReportStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Subscription(Base):
    """A user's subscription to receive invasive species reports."""
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), index=True, nullable=False)

    # Region configuration
    region = Column(String(255), nullable=False)  # User-entered region name
    place_id = Column(Integer, nullable=True)  # Resolved iNaturalist place ID
    place_name = Column(String(255), nullable=True)  # Resolved place name
    place_display_name = Column(String(500), nullable=True)  # Full display name

    # Optional taxon filter
    taxon_filter = Column(String(255), nullable=True)
    taxon_id = Column(Integer, nullable=True)

    # Subscription settings
    frequency = Column(SQLEnum(Frequency), default=Frequency.MONTHLY, nullable=False)
    lookback_years = Column(Integer, default=20)

    # Status
    is_verified = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)
    last_report_at = Column(DateTime, nullable=True)

    # Relationships
    reports = relationship("Report", back_populates="subscription", cascade="all, delete-orphan")
    tokens = relationship("Token", back_populates="subscription", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Subscription {self.id}: {self.email} - {self.region}>"


class Report(Base):
    """A generated report for a subscription."""
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False)

    # Report metadata
    report_uuid = Column(String(64), unique=True, index=True)  # For public URLs
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)

    # Status
    status = Column(SQLEnum(ReportStatus), default=ReportStatus.PENDING)
    error_message = Column(Text, nullable=True)

    # Results
    total_species = Column(Integer, nullable=True)
    new_species_count = Column(Integer, nullable=True)
    json_path = Column(String(500), nullable=True)
    html_path = Column(String(500), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Email tracking
    email_sent = Column(Boolean, default=False)
    email_sent_at = Column(DateTime, nullable=True)

    # Relationships
    subscription = relationship("Subscription", back_populates="reports")

    def __repr__(self):
        return f"<Report {self.id}: {self.status} - {self.new_species_count} new species>"


class Token(Base):
    """Magic link tokens for email verification and subscription management."""
    __tablename__ = "tokens"

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False)

    token = Column(String(64), unique=True, index=True, nullable=False)
    token_type = Column(String(50), nullable=False)  # "verify", "manage", "unsubscribe"

    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)

    # Relationships
    subscription = relationship("Subscription", back_populates="tokens")

    @classmethod
    def generate(cls, subscription_id: int, token_type: str) -> "Token":
        """Generate a new token with default expiry."""
        return cls(
            subscription_id=subscription_id,
            token=secrets.token_urlsafe(32),
            token_type=token_type,
            expires_at=datetime.utcnow() + timedelta(hours=MAGIC_LINK_EXPIRY_HOURS)
        )

    @property
    def is_valid(self) -> bool:
        """Check if token is valid (not expired and not used)."""
        return self.used_at is None and datetime.utcnow() < self.expires_at

    def __repr__(self):
        return f"<Token {self.token_type}: {self.token[:8]}...>"


class AdminUser(Base):
    """Admin users for the dashboard."""
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)


def init_db():
    """Initialize the database tables."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """Get database session for dependency injection."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
