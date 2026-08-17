"""Tests for MirrorDatabaseState singleton and command watermark helpers."""

from __future__ import annotations

import json

from contextlib import contextmanager
from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from django.core.management.base import CommandError
from django.db import connections
from django.utils import timezone

from mirroring.database import temporary_database_alias
from mirroring.management.commands.refresh_database_mirror import Command as RefreshCommand
from mirroring.management.commands.restore_from_mirror import Command as RestoreCommand
from mirroring.models import MirrorDatabaseState


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.mark.django_db
@pytest.mark.unit
def test_mirror_database_state_is_singleton() -> None:
    first = MirrorDatabaseState.get_solo()
    second = MirrorDatabaseState.get_solo()
    assert first.pk == MirrorDatabaseState.SINGLETON_PK
    assert second.pk == first.pk
    assert MirrorDatabaseState.objects.count() == 1


@pytest.mark.unit
def test_load_dumpling_report_reads_json(tmp_path: Path) -> None:
    report_path = tmp_path / "dumpling-report.json"
    report_path.write_text(
        json.dumps({"dumpling_version": "0.9.0", "config_sha256": "abc123"}),
        encoding="utf-8",
    )
    payload = RefreshCommand().load_dumpling_report(report_path)
    assert payload == {"dumpling_version": "0.9.0", "config_sha256": "abc123"}


@pytest.mark.unit
def test_load_dumpling_report_missing_returns_none(tmp_path: Path) -> None:
    assert RefreshCommand().load_dumpling_report(tmp_path / "missing.json") is None


@pytest.mark.unit
def test_load_dumpling_report_rejects_invalid_json(tmp_path: Path) -> None:
    report_path = tmp_path / "dumpling-report.json"
    report_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CommandError, match="not valid JSON"):
        RefreshCommand().load_dumpling_report(report_path)


@contextmanager
def _default_alias(_database_url: str, *, alias: str) -> Iterator[str]:
    yield "default"


@pytest.mark.django_db
@pytest.mark.unit
def test_record_mirror_generation_upserts_singleton() -> None:
    command = RefreshCommand()
    with (
        patch("mirroring.management.commands.refresh_database_mirror.migrate_database_url"),
        patch(
            "mirroring.management.commands.refresh_database_mirror.temporary_database_alias",
            side_effect=_default_alias,
        ),
        patch.object(command, "info"),
    ):
        command.record_mirror_generation(
            "postgres://u:p@unused.example:5432/shadow",
            source_url="postgres://u:p@follower.example:5432/prod",
            retain_cutoff=date(2026, 1, 15),
            dumpling_report={
                "dumpling_version": "0.9.0",
                "config_sha256": "deadbeef" * 8,
            },
        )

    state = MirrorDatabaseState.get_solo()
    assert state.generated_at is not None
    assert state.source_host == "follower.example"
    assert state.source_database == "prod"
    assert state.retain_cutoff == date(2026, 1, 15)
    assert state.dumpling_version == "0.9.0"
    assert state.dumpling_config_sha256 == "deadbeef" * 8
    assert state.dumpling_report is not None
    assert state.dumpling_report["dumpling_version"] == "0.9.0"
    assert state.restored_at is None


@pytest.mark.django_db
@pytest.mark.unit
def test_record_restore_completion_sets_restored_at() -> None:
    state = MirrorDatabaseState.get_solo()
    state.generated_at = timezone.now()
    state.source_host = "follower.example"
    state.source_database = "prod"
    state.restored_at = None
    state.save()

    command = RestoreCommand()
    with (
        patch(
            "mirroring.management.commands.restore_from_mirror.temporary_database_alias",
            side_effect=_default_alias,
        ),
        patch.object(command, "info"),
    ):
        command.record_restore_completion("postgres://u:p@unused.example:5432/staging")

    state.refresh_from_db()
    assert state.restored_at is not None
    assert state.source_host == "follower.example"
    assert state.source_database == "prod"


@pytest.mark.unit
def test_temporary_database_alias_refuses_overwrite() -> None:
    connections.databases["mirroring_test_alias"] = dict(connections.databases["default"])
    try:
        with pytest.raises(CommandError, match="already registered"):
            with temporary_database_alias("postgres://u:p@localhost:5432/db", alias="mirroring_test_alias"):
                pass
    finally:
        connections.databases.pop("mirroring_test_alias", None)
