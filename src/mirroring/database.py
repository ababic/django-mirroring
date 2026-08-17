"""Temporary Django DB aliases for mirror refresh/restore against shadow URLs."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import dj_database_url

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections


if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def temporary_database_alias(database_url: str, *, alias: str) -> Iterator[str]:
    """Register ``database_url`` as ``alias`` for the duration of the context.

    Refuses to overwrite an existing alias. Always closes and removes the alias
    afterwards so settings ``DATABASES`` are left unchanged.

    SSL is controlled by the URL (e.g. ``?sslmode=require``); this helper does
    not infer SSL from the hostname.
    """
    apps.check_apps_ready()
    if alias in connections.databases:
        raise CommandError(f"Database alias {alias!r} is already registered; refusing to overwrite.")
    connections.databases[alias] = dj_database_url.parse(
        database_url,
        conn_max_age=0,
    )
    try:
        yield alias
    finally:
        connections[alias].close()
        connections.databases.pop(alias, None)
        try:
            del connections[alias]
        except KeyError:
            pass


def migrate_database_url(
    database_url: str,
    *,
    alias: str,
    app_labels: list[str] | None = None,
    verbosity: int = 1,
) -> None:
    """Run ``migrate`` against ``database_url`` via a temporary DB alias."""
    with temporary_database_alias(database_url, alias=alias) as db_alias:
        args: list[Any] = list(app_labels or [])
        call_command(
            "migrate",
            *args,
            database=db_alias,
            interactive=False,
            run_syncdb=False,
            verbosity=verbosity,
        )
