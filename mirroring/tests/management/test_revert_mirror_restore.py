"""Tests for revert_mirror_restore safety gates and helpers."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from mirroring.management.postgres_clone import (
    backout_database_name,
    preswap_database_name,
    revert_preswap_cutover,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from pytest_django.fixtures import SettingsWrapper


@pytest.mark.unit
def test_backout_database_name() -> None:
    assert backout_database_name("staging") == "staging_backout"
    assert preswap_database_name("staging") == "staging_preswap"


@pytest.mark.unit
def test_revert_requires_confirm(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    with pytest.raises(CommandError, match="--confirm"):
        call_command("revert_mirror_restore", stdout=StringIO())


@pytest.mark.unit
def test_revert_requires_allow(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MIRROR_RESTORE_ALLOW", raising=False)
    with pytest.raises(CommandError, match="MIRROR_RESTORE_ALLOW=1"):
        call_command("revert_mirror_restore", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_revert_refuses_blocked_target_host(settings: SettingsWrapper, monkeypatch: MonkeyPatch) -> None:
    settings.MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES = ["prod.example"]
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@db.prod.example:5432/staging")
    with pytest.raises(CommandError, match="blocked host suffix"):
        call_command("revert_mirror_restore", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_revert_confirm_requires_target_allowlist(settings: SettingsWrapper, monkeypatch: MonkeyPatch) -> None:
    settings.MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES = []
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@staging.example:5432/staging")
    with pytest.raises(CommandError, match="MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES"):
        call_command("revert_mirror_restore", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_revert_dry_run(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@staging.example:5432/staging")

    def fake_exists(server_url: str, dbname: str, *, run_capture: object) -> bool:
        return dbname in {"staging", "staging_preswap"}

    with (
        patch("mirroring.management.commands.revert_mirror_restore.shutil.which", return_value="/bin/true"),
        patch(
            "mirroring.management.commands.revert_mirror_restore.postgres_database_exists",
            side_effect=fake_exists,
        ),
    ):
        out = StringIO()
        call_command("revert_mirror_restore", "--dry-run", stdout=out)
    output = out.getvalue()
    assert "Dry run complete" in output
    assert "rename 'staging' → 'staging_backout'" in output
    assert "'staging_preswap' → 'staging'" in output
    assert "Exists 'staging_preswap': True" in output


@pytest.mark.unit
def test_revert_preswap_cutover_sequence() -> None:
    labels: list[str] = []
    existing = {"staging", "staging_preswap"}

    def run_capture(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> str:
        sql = cmd[-1]
        for name in ("staging", "staging_preswap", "staging_backout"):
            if f"datname = '{name}'" in sql:
                return "1\n" if name in existing else "\n"
        raise AssertionError(f"unexpected capture SQL: {sql}")

    def run_checked(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
        labels.append(label)
        sql = cmd[-1]
        if "DROP DATABASE IF EXISTS staging_backout" in sql:
            existing.discard("staging_backout")
        if "RENAME TO staging_backout" in sql:
            existing.discard("staging")
            existing.add("staging_backout")
        if "RENAME TO staging" in sql and "staging_preswap" in sql:
            existing.discard("staging_preswap")
            existing.add("staging")

    revert_preswap_cutover(
        "postgres://u:p@db.example:5432/staging",
        run_checked=run_checked,
        run_capture=run_capture,
    )
    assert labels.index("psql park live database as _backout") < labels.index("psql promote _preswap to live")
    assert labels[-1] == "psql drop database staging_backout"


@pytest.mark.unit
def test_revert_preswap_cutover_requires_preswap() -> None:
    def run_capture(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> str:
        sql = cmd[-1]
        if "datname = 'staging'" in sql:
            return "1\n"
        return "\n"

    def run_checked(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
        raise AssertionError("should not mutate without preswap")

    with pytest.raises(ValueError, match="No 'staging_preswap'"):
        revert_preswap_cutover(
            "postgres://u:p@db.example:5432/staging",
            run_checked=run_checked,
            run_capture=run_capture,
        )


@pytest.mark.unit
def test_revert_rolls_back_backout_when_preswap_promote_fails() -> None:
    labels: list[str] = []

    def run_capture(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> str:
        sql = cmd[-1]
        if "datname = 'staging_backout'" in sql:
            return "\n"
        return "1\n"

    def run_checked(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
        labels.append(label)
        if label == "psql promote _preswap to live":
            raise RuntimeError("preswap promotion failed")

    with pytest.raises(RuntimeError, match="preswap promotion failed"):
        revert_preswap_cutover(
            "postgres://u:p@db.example:5432/staging",
            run_checked=run_checked,
            run_capture=run_capture,
        )

    assert labels == [
        "psql park live database as _backout",
        "psql promote _preswap to live",
        "psql rollback parked live database",
    ]
