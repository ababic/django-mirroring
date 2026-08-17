"""Tests for pinned dependency / external tool version checks."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from django.core.management.base import CommandError

from mirroring.versions import (
    PINNED_DUMPLING_CLI,
    PINNED_POSTGRES_CLIENT_MAJOR,
    assert_compatible_release,
    minimum_postgres_client_major,
    parse_postgres_client_major,
    require_dumpling,
    require_executable,
    require_postgres_clients,
)


if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from pytest_django.fixtures import SettingsWrapper


@pytest.mark.unit
def test_assert_compatible_release_accepts_same_series() -> None:
    assert_compatible_release("0.9.0", "0.9.0", label="dumpling")
    assert_compatible_release("0.9.3", "0.9.0", label="dumpling")


@pytest.mark.unit
def test_assert_compatible_release_rejects_other_minor() -> None:
    with pytest.raises(CommandError, match="incompatible with pinned"):
        assert_compatible_release("0.10.0", "0.9.0", label="dumpling")


@pytest.mark.unit
def test_assert_compatible_release_rejects_older_patch() -> None:
    with pytest.raises(CommandError, match="older than pinned minimum"):
        assert_compatible_release("0.9.0", "0.9.2", label="dumpling")


@pytest.mark.unit
def test_parse_postgres_client_major() -> None:
    assert parse_postgres_client_major("pg_dump (PostgreSQL) 15.13") == 15
    assert parse_postgres_client_major("psql (PostgreSQL) 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)") == 16


@pytest.mark.unit
def test_require_executable_raises_when_missing() -> None:
    with patch("mirroring.versions.shutil.which", return_value=None):
        with pytest.raises(CommandError, match="not found on PATH"):
            require_executable("definitely-not-a-real-binary")


@pytest.mark.unit
def test_require_dumpling_accepts_pinned_series() -> None:
    with (
        patch("mirroring.versions.shutil.which", return_value="/usr/bin/dumpling"),
        patch("mirroring.versions._run_version", return_value=f"dumpling {PINNED_DUMPLING_CLI}"),
    ):
        assert require_dumpling() == "dumpling"


@pytest.mark.unit
def test_require_dumpling_rejects_other_series() -> None:
    with (
        patch("mirroring.versions.shutil.which", return_value="/usr/bin/dumpling"),
        patch("mirroring.versions._run_version", return_value="dumpling 0.8.0"),
    ):
        with pytest.raises(CommandError, match="incompatible with pinned"):
            require_dumpling()


@pytest.mark.unit
def test_require_dumpling_honours_explicit_binary() -> None:
    with (
        patch("mirroring.versions.shutil.which", return_value="/opt/dumpling"),
        patch("mirroring.versions._run_version", return_value=f"dumpling {PINNED_DUMPLING_CLI}") as version_mock,
    ):
        assert require_dumpling("/opt/dumpling") == "/opt/dumpling"
    assert version_mock.call_args.args[0] == "/opt/dumpling"


@pytest.mark.unit
def test_require_postgres_clients_accepts_newer_client() -> None:
    with (
        patch("mirroring.versions.shutil.which", return_value="/usr/bin/pg_dump"),
        patch("mirroring.versions._run_version", return_value="pg_dump (PostgreSQL) 16.14"),
    ):
        details = require_postgres_clients("pg_dump")
    assert details["minimum_major"] == PINNED_POSTGRES_CLIENT_MAJOR
    assert details["tools"]["pg_dump"]["major"] == 16


@pytest.mark.unit
def test_require_postgres_clients_rejects_older_client() -> None:
    with (
        patch("mirroring.versions.shutil.which", return_value="/usr/bin/pg_dump"),
        patch("mirroring.versions._run_version", return_value="pg_dump (PostgreSQL) 14.11"),
    ):
        with pytest.raises(CommandError, match="requires at least major"):
            require_postgres_clients("pg_dump")


@pytest.mark.unit
def test_minimum_postgres_client_major_setting_override(settings: SettingsWrapper) -> None:
    settings.MIRRORING_POSTGRES_CLIENT_MAJOR = 17
    assert minimum_postgres_client_major() == 17


@pytest.mark.unit
def test_minimum_postgres_client_major_env_override(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRRORING_POSTGRES_CLIENT_MAJOR", "16")
    assert minimum_postgres_client_major() == 16


@pytest.mark.unit
def test_minimum_postgres_client_major_rejects_garbage(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRRORING_POSTGRES_CLIENT_MAJOR", "not-a-number")
    with pytest.raises(CommandError, match="Invalid MIRRORING_POSTGRES_CLIENT_MAJOR"):
        minimum_postgres_client_major()
