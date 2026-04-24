"""Referral CHECK constraints + jobs.updated_at NOT NULL (align with schema.sql).

Revision ID: 007
Revises: 006
Create Date: 2026-04-21

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE public.profiles SET referred_by_user_id = NULL "
        "WHERE referred_by_user_id IS NOT NULL AND referred_by_user_id = id"
    )
    op.execute(
        "DELETE FROM public.referral_attributions "
        "WHERE referrer_user_id = referee_user_id"
    )
    op.create_check_constraint(
        "chk_profiles_referral_not_self",
        "profiles",
        "referred_by_user_id IS NULL OR referred_by_user_id <> id",
    )
    op.create_check_constraint(
        "chk_referral_attributions_distinct_users",
        "referral_attributions",
        "referrer_user_id <> referee_user_id",
    )
    op.execute("UPDATE public.jobs SET updated_at = created_at WHERE updated_at IS NULL")
    op.alter_column(
        "jobs",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )


def downgrade() -> None:
    op.alter_column(
        "jobs",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=sa.text("NOW()"),
    )
    op.drop_constraint(
        "chk_referral_attributions_distinct_users",
        "referral_attributions",
        type_="check",
    )
    op.drop_constraint("chk_profiles_referral_not_self", "profiles", type_="check")
