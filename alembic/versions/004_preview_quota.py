"""preview quota columns on profiles

Revision ID: 004
Revises: 003
Create Date: 2026-04-14

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("preview_quota_utc_date", sa.Date(), nullable=True))
    op.add_column(
        "profiles",
        sa.Column("preview_quota_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("profiles", "preview_quota_count")
    op.drop_column("profiles", "preview_quota_utc_date")
