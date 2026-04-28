"""Tiny additive-only schema migrator for SQLite.

We only ever add columns; never rename or drop. SQLite is lenient about
NOT NULL when adding a column with a default, so each migration here is
safe to run on a fresh DB and on an existing one.
"""
from sqlalchemy import inspect, text

from .db import engine


def _add_column(table: str, column_def: str) -> None:
    col_name = column_def.split()[0]
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return  # create_all hasn't run yet, or table renamed; nothing to do here
    cols = {c["name"] for c in insp.get_columns(table)}
    if col_name in cols:
        return
    with engine.connect() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def}"))
        conn.commit()


def run() -> None:
    _add_column("admins", "auth_source VARCHAR(16) DEFAULT 'local' NOT NULL")
    _add_column("admins", "display_name VARCHAR(128) DEFAULT '' NOT NULL")
    _add_column("admins", "last_login DATETIME")
    # v0.3 → v0.4: extended hardware inventory + package hash + per-agent maintenance window.
    _add_column("agents", "manufacturer VARCHAR(128) DEFAULT '' NOT NULL")
    _add_column("agents", "product_name VARCHAR(128) DEFAULT '' NOT NULL")
    _add_column("agents", "serial_number VARCHAR(128) DEFAULT '' NOT NULL")
    _add_column("agents", "bios_version VARCHAR(64) DEFAULT '' NOT NULL")
    _add_column("agents", "gpu_model VARCHAR(255) DEFAULT '' NOT NULL")
    _add_column("agents", "mac_address VARCHAR(32) DEFAULT '' NOT NULL")
    _add_column("agents", "packages_hash VARCHAR(64) DEFAULT '' NOT NULL")
    _add_column("agents", "packages_updated_at DATETIME")
    _add_column("agents", "maintenance_window_start VARCHAR(5)")
    _add_column("agents", "maintenance_window_end VARCHAR(5)")
