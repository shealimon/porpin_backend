"""Rename plan slug pay_as_you_go -> payg (short PayG tier)

Revision ID: 006
Revises: 005
Create Date: 2026-04-20

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE profiles SET plan = 'payg' WHERE LOWER(plan) IN ('pay_as_you_go', 'pay-as-you-go')"
    )


def downgrade() -> None:
    op.execute("UPDATE profiles SET plan = 'pay_as_you_go' WHERE plan = 'payg'")
