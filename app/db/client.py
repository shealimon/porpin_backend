"""Reusable database access (Supabase PostgreSQL).

Set ``SUPABASE_DATABASE_URL`` (or ``DATABASE_URL``) to your Transaction pooler URI from the Supabase dashboard.
"""

from __future__ import annotations

from app.db.session import (
    create_all_tables,
    get_db,
    get_engine,
    get_session_factory,
)

__all__ = [
    "create_all_tables",
    "get_db",
    "get_engine",
    "get_session_factory",
]
