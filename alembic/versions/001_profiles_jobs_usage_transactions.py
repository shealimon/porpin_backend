"""profiles, jobs, usage, transactions

Revision ID: 001
Revises:
Create Date: 2026-03-30

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("plan", sa.String(16), nullable=False, server_default="free"),
        sa.Column(
            "credits_inr_balance",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_profiles_email", "profiles", ["email"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_filename", sa.String(512), nullable=False),
        sa.Column("input_file_path", sa.String(2048), nullable=False),
        sa.Column("export_format", sa.String(16), nullable=False),
        sa.Column("output_file_path", sa.String(2048), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"], unique=False)

    op.create_table(
        "usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cost_inr",
            sa.Numeric(12, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_user_id", "usage", ["user_id"], unique=False)
    op.create_index("ix_usage_job_id", "usage", ["job_id"], unique=False)

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_inr", sa.Numeric(12, 4), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="stripe"),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_transactions_user_id", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_usage_job_id", table_name="usage")
    op.drop_index("ix_usage_user_id", table_name="usage")
    op.drop_table("usage")
    op.drop_index("ix_jobs_user_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_profiles_email", table_name="profiles")
    op.drop_table("profiles")
