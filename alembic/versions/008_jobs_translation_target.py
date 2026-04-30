"""Add jobs.translation_target (hinglish vs hindi).

Revision ID: 008
Revises: 007
Create Date: 2026-04-27

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "translation_target",
            sa.String(16),
            nullable=False,
            server_default="hinglish",
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "translation_target")
