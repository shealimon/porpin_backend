"""SQLAlchemy models for users, translation jobs (async / scale-out)."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.billing_constants import FREE_CREDITS_INITIAL
from app.db.base import Base


class UserTier(str, enum.Enum):
    FREE = "free"
    PAID = "paid"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    AWAITING_PAYMENT = "awaiting_payment"
    PROCESSING = "processing"
    PREVIEW_READY = "preview_ready"
    COMPLETED = "completed"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    api_key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    tier: Mapped[str] = mapped_column(String(16), default=UserTier.FREE.value)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    jobs: Mapped[list["TranslationJob"]] = relationship(back_populates="user")


class TranslationJob(Base):
    __tablename__ = "translation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING.value)
    input_filename: Mapped[str] = mapped_column(String(512))
    input_relpath: Mapped[str] = mapped_column(String(1024))
    export_format: Mapped[str] = mapped_column(String(16), default="docx")
    output_relpath: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped["User"] = relationship(back_populates="jobs")


# --- Supabase-scale schema (profiles / jobs / usage / transactions) ---


class Profile(Base):
    """Maps 1:1 to Supabase auth.users.id (application-enforced)."""

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    plan: Mapped[str] = mapped_column(String(32), default=UserTier.FREE.value)
    credits_inr_balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    free_credits: Mapped[int] = mapped_column(default=FREE_CREDITS_INITIAL)
    subscription_active: Mapped[bool] = mapped_column(default=False)
    subscription_credits: Mapped[int] = mapped_column(default=0)
    subscription_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    subscription_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    subscription_contract_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_subscription_kind: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    subscription_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    razorpay_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    referral_code: Mapped[str | None] = mapped_column(
        String(32), unique=True, nullable=True, index=True
    )
    referred_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    referral_bonus_words: Mapped[int] = mapped_column(default=0)
    referral_words_earned_total: Mapped[int] = mapped_column(default=0)
    preview_quota_utc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preview_quota_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    document_jobs: Mapped[list["DocumentJob"]] = relationship(
        "DocumentJob",
        primaryjoin="Profile.id == DocumentJob.user_id",
        foreign_keys="DocumentJob.user_id",
        back_populates="profile",
    )
    usage_rows: Mapped[list["UsageRecord"]] = relationship(back_populates="profile")
    transactions: Mapped[list["PaymentTransaction"]] = relationship(
        back_populates="profile"
    )


class ReferralAttribution(Base):
    """One row per referee; pending → verified (email) → completed (referee's first payment)."""

    __tablename__ = "referral_attributions"
    __table_args__ = (Index("ix_referral_attributions_device_hash", "device_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    referee_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("profiles.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    referrer_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    claim_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    device_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    words_credited_to_referrer: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CreditTransaction(Base):
    """Ledger for translation word-credit grants (referral bonuses, etc.)."""

    __tablename__ = "credit_transactions"
    __table_args__ = (Index("ix_credit_transactions_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    referral_attribution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("referral_attributions.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentJob(Base):
    """Async translation job (queue worker updates this row).

    Supabase: ``user_id`` should reference ``auth.users(id)`` (same UUID as ``profiles.id``).
    The ORM does not emit that FK so local SQLite and Supabase both work; enforce in DB.

    Indexes ``idx_jobs_user_id`` and ``idx_jobs_status`` match Supabase DDL.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_user_id", "user_id"),
        Index("idx_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING.value)
    input_filename: Mapped[str] = mapped_column(String(512), default="")
    input_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    export_format: Mapped[str] = mapped_column(String(16), default="docx")
    output_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    file_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_used: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_inr: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    processing_time_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # PAYG deferred: client pays for this job before enqueue; `quoted_payg_inr` is the agreed order amount.
    quoted_payg_inr: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    translation_attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    translation_target: Mapped[str] = mapped_column(
        String(16), default="hinglish", server_default="hinglish"
    )

    profile: Mapped["Profile"] = relationship(
        "Profile",
        primaryjoin="Profile.id == DocumentJob.user_id",
        foreign_keys=[user_id],
        back_populates="document_jobs",
    )


class UsageRecord(Base):
    __tablename__ = "usage"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tokens_used: Mapped[int] = mapped_column(default=0)
    cost_inr: Mapped[float] = mapped_column(Numeric(12, 4), default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    profile: Mapped["Profile"] = relationship(back_populates="usage_rows")


class PaymentTransaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    amount_inr: Mapped[float] = mapped_column(Numeric(12, 4))
    provider: Mapped[str] = mapped_column(String(32), default="stripe")
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    kind: Mapped[str] = mapped_column(String(32), default="wallet_topup", server_default="wallet_topup")
    razorpay_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    profile: Mapped["Profile"] = relationship(back_populates="transactions")
