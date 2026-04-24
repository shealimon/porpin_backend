"""referral columns on profiles + referral_attributions

Revision ID: 003
Revises: 002
Create Date: 2026-04-02

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("referral_code", sa.String(32), nullable=True))
    op.add_column(
        "profiles",
        sa.Column("referred_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "profiles",
        sa.Column("referral_bonus_words", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "profiles",
        sa.Column(
            "referral_words_earned_total",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index("ix_profiles_referral_code", "profiles", ["referral_code"], unique=True)
    op.create_foreign_key(
        "fk_profiles_referred_by_user_id",
        "profiles",
        "profiles",
        ["referred_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "referral_attributions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("referee_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("referrer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("words_credited_to_referrer", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["referee_user_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["referrer_user_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referee_user_id"),
    )
    op.create_index(
        "ix_referral_attributions_referrer",
        "referral_attributions",
        ["referrer_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_referral_attributions_referrer", table_name="referral_attributions")
    op.drop_table("referral_attributions")
    op.drop_constraint("fk_profiles_referred_by_user_id", "profiles", type_="foreignkey")
    op.drop_index("ix_profiles_referral_code", table_name="profiles")
    op.drop_column("profiles", "referral_words_earned_total")
    op.drop_column("profiles", "referral_bonus_words")
    op.drop_column("profiles", "referred_by_user_id")
    op.drop_column("profiles", "referral_code")
