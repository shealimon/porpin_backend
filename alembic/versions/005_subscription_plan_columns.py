"""subscription period columns + widen profiles.plan for payg / monthly / yearly

Revision ID: 005
Revises: 004
Create Date: 2026-04-20

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column("subscription_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "profiles",
        sa.Column("subscription_period_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "profiles",
        sa.Column("subscription_contract_end", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "profiles",
        sa.Column("pending_subscription_kind", sa.String(16), nullable=True),
    )
    op.alter_column(
        "profiles",
        "plan",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "profiles",
        "plan",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
    op.drop_column("profiles", "pending_subscription_kind")
    op.drop_column("profiles", "subscription_contract_end")
    op.drop_column("profiles", "subscription_period_start")
    op.drop_column("profiles", "subscription_started_at")
