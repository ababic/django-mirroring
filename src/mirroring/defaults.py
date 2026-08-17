"""Reusable defaults and env helpers for database mirror refresh and staging restore."""

from __future__ import annotations

import os

from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model


MIRROR_EXCLUDED_SCHEMA: list[str] = []
MIRROR_EXCLUDED_TABLES: list[str] = []

# Projects override in settings; each entry is {"table", "column", "cascades"?}.
MIRROR_ROW_RETAIN: list[dict] = []


def pg_public_table_names(*table_names: str) -> list[str]:
    """Qualify bare Postgres table names as ``public.<name>`` for ``pg_dump`` flags."""
    qualified: list[str] = []
    for name in table_names:
        qualified.append(name if "." in name else f"public.{name}")
    return qualified


def build_mirror_excluded_table_data(*table_name_groups: list[str]) -> list[str]:
    """Merge table name lists into sorted unique ``public.*`` qualified names."""
    merged: set[str] = set()
    for group in table_name_groups:
        merged.update(pg_public_table_names(*group))
    return sorted(merged)


def mirror_dumpling_config_path(*, default: str | Path | None = None) -> Path:
    """Resolve Dumpling policy path from ``MIRROR_DUMPLING_CONFIG`` or an explicit default."""
    raw = os.environ.get("MIRROR_DUMPLING_CONFIG") or (str(default) if default is not None else "")
    if not raw:
        raise ValueError("MIRROR_DUMPLING_CONFIG is not set and no default was provided")
    return Path(raw).resolve()


def mirror_retain_months() -> int:
    raw = os.environ.get("MIRROR_RETAIN_MONTHS", "18").strip()
    return int(raw) if raw.isdigit() else 18


def mirror_restore_staff_email_domains() -> list[str]:
    return [
        domain.strip().lower()
        for domain in os.environ.get("MIRROR_RESTORE_STAFF_EMAIL_DOMAINS", "").split(",")
        if domain.strip()
    ]


def mirror_restore_user_match_field() -> str:
    return os.environ.get("MIRROR_RESTORE_USER_MATCH_FIELD", "username")


def mirror_restore_allowed_target_host_suffixes() -> list[str]:
    """Return target host suffixes; restore/revert fail closed when this is empty."""
    return [
        suffix.strip().lower()
        for suffix in os.environ.get("MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES", "").split(",")
        if suffix.strip()
    ]


def mirror_restore_blocked_target_host_suffixes() -> list[str]:
    return [
        suffix.strip().lower()
        for suffix in os.environ.get(
            "MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES",
            "",
        ).split(",")
        if suffix.strip()
    ]


def auth_user_db_table() -> str:
    """Return the qualified public auth user table for Dumpling rules and post-restore SQL."""
    configured = getattr(settings, "MIRROR_AUTH_USER_DB_TABLE", None)
    if configured:
        return configured if "." in configured else f"public.{configured}"
    table = get_user_model()._meta.db_table
    return table if "." in table else f"public.{table}"
