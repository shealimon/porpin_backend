"""profiles: first_name, last_name, mobile, city, country

Revision ID: 002
Revises: 001
Create Date: 2026-04-01

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("first_name", sa.String(100), nullable=True))
    op.add_column("profiles", sa.Column("last_name", sa.String(100), nullable=True))
    op.add_column("profiles", sa.Column("mobile", sa.String(32), nullable=True))
    op.add_column("profiles", sa.Column("city", sa.String(120), nullable=True))
    op.add_column("profiles", sa.Column("country", sa.String(120), nullable=True))


def downgrade() -> None:
    op.drop_column("profiles", "country")
    op.drop_column("profiles", "city")
    op.drop_column("profiles", "mobile")
    op.drop_column("profiles", "last_name")
    op.drop_column("profiles", "first_name")
