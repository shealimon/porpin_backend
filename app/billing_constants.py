"""Signup free word allowance — keep in sync with public pricing config / marketing copy."""

FREE_CREDITS_INITIAL = 10_000

# Profile.plan — marketing / billing tier (VARCHAR widened in migrations for longer slugs).
PLAN_FREE = "free"
# Short slug (display name e.g. "PayG" in UI).
PLAN_PAYG = "payg"
PLAN_MONTHLY = "monthly"
PLAN_YEARLY = "yearly"


def is_high_priority_plan(plan: str | None) -> bool:
    """Paid queue + higher daily job cap (legacy ``paid`` + subscription plans)."""
    p = (plan or "").lower()
    return p in ("paid", PLAN_MONTHLY, PLAN_YEARLY)
