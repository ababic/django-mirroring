"""Tests for restore_from_mirror safety gates and helpers."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from mirroring.management.commands.restore_from_mirror import Command
from mirroring.management.postgres_clone import (
    database_identity,
    looks_like_managed_heroku_postgres,
    replace_database_name,
    shadow_database_name,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from pytest_django.fixtures import SettingsWrapper


@pytest.mark.unit
def test_looks_like_managed_heroku_postgres() -> None:
    assert looks_like_managed_heroku_postgres("postgres://u:p@ec2-1-2-3-4.compute-1.amazonaws.com:5432/d")
    assert not looks_like_managed_heroku_postgres("postgres://u:p@localhost:5432/d")


@pytest.mark.unit
def test_replace_database_name_preserves_credentials() -> None:
    url = "postgres://alice:s3cret@db.example:5432/staging?sslmode=require"
    replaced_url = replace_database_name(url, "staging_tmp")
    assert database_identity(replaced_url) == (
        "db.example",
        "5432",
        "staging_tmp",
    )
    parsed = urlsplit(replaced_url)
    assert parsed.username == "alice"
    assert parsed.password == "s3cret"
    assert parsed.query == "sslmode=require"


@pytest.mark.unit
def test_refresh_requires_confirm(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    with pytest.raises(CommandError, match="--confirm"):
        call_command("restore_from_mirror", stdout=StringIO())


@pytest.mark.unit
def test_refresh_requires_refresh_allow(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MIRROR_RESTORE_ALLOW", raising=False)
    with pytest.raises(CommandError, match="MIRROR_RESTORE_ALLOW=1"):
        call_command("restore_from_mirror", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_refresh_refuses_same_source_and_target(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@db.example:5432/anon")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@db.example:5432/anon")
    with pytest.raises(CommandError, match="same host/port/database"):
        call_command("restore_from_mirror", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_refresh_refuses_production_database_url_as_target(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@prod.example:5432/live")
    monkeypatch.setenv("PRODUCTION_DATABASE_URL", "postgres://u:p@prod.example:5432/live")
    with pytest.raises(CommandError, match="PRODUCTION_DATABASE_URL"):
        call_command("restore_from_mirror", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_refresh_refuses_blocked_target_host(settings: SettingsWrapper, monkeypatch: MonkeyPatch) -> None:
    settings.MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES = ["prod.example"]
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@db.prod.example:5432/staging")
    with pytest.raises(CommandError, match="blocked production host"):
        call_command("restore_from_mirror", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_refresh_refuses_partial_suffix_false_positive(settings: SettingsWrapper, monkeypatch: MonkeyPatch) -> None:
    settings.MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES = ["staging.example.com"]
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    # Would incorrectly match a naive endswith("staging.example.com") check.
    monkeypatch.setenv(
        "MIRROR_RESTORE_TARGET_DATABASE_URL",
        "postgres://u:p@notstaging.example.com:5432/staging",
    )
    with patch(
        "mirroring.management.commands.restore_from_mirror.shutil.which",
        return_value="/bin/true",
    ):
        out = StringIO()
        call_command("restore_from_mirror", "--dry-run", stdout=out)
    assert "Dry run complete" in out.getvalue()


@pytest.mark.unit
def test_refresh_confirm_requires_target_allowlist(settings: SettingsWrapper, monkeypatch: MonkeyPatch) -> None:
    settings.MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES = []
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@staging.example:5432/staging")
    with pytest.raises(CommandError, match="MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES"):
        call_command("restore_from_mirror", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_refresh_dry_run(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@staging.example:5432/staging")
    with patch(
        "mirroring.management.commands.restore_from_mirror.shutil.which",
        return_value="/bin/true",
    ):
        out = StringIO()
        call_command("restore_from_mirror", "--dry-run", stdout=out)
    output = out.getvalue()
    assert "Dry run complete" in output
    assert "DROP/CREATE 'staging_tmp'" in output
    assert "rename 'staging' → 'staging_preswap'" in output
    assert "refuse if present" in output


@pytest.mark.unit
def test_refresh_dry_run_delete_preswap(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@staging.example:5432/staging")
    with patch(
        "mirroring.management.commands.restore_from_mirror.shutil.which",
        return_value="/bin/true",
    ):
        out = StringIO()
        call_command("restore_from_mirror", "--dry-run", "--delete-preswap", stdout=out)
    assert "will DROP before cutover (--delete-preswap)" in out.getvalue()


@pytest.mark.unit
def test_recreate_shadow_database_drops_then_creates() -> None:
    from mirroring.management.postgres_clone import recreate_shadow_database

    calls: list[list[str]] = []

    def run_checked(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
        calls.append(cmd)

    shadow_url = recreate_shadow_database(
        "postgres://u:p@db.example:5432/mirror",
        run_checked=run_checked,
    )
    assert database_identity(shadow_url) == ("db.example", "5432", "mirror_tmp")
    assert len(calls) == 1
    c_args = [calls[0][i + 1] for i, arg in enumerate(calls[0]) if arg == "-c"]
    assert len(c_args) == 3
    assert "pg_terminate_backend" in c_args[0]
    assert c_args[1].strip() == "DROP DATABASE IF EXISTS mirror_tmp;"
    assert "CREATE DATABASE mirror_tmp WITH TEMPLATE template0;" in c_args[2]


@pytest.mark.unit
def test_dump_line_filter_preserves_copy_payload_resembling_set() -> None:
    from mirroring.management.postgres_clone import DumpLineFilter

    filt = DumpLineFilter()
    stream = (
        b"SET transaction_timeout = 0;\n"
        b"COPY public.t (note) FROM stdin;\n"
        b"SET transaction_timeout = 0;\n"
        b"\\.\n"
        b"SET client_encoding = 'UTF8';\n"
    )
    kept = b"".join(line for line in stream.splitlines(keepends=True) if filt.keep(line))
    assert b"SET transaction_timeout = 0;\n" not in kept.split(b"COPY", 1)[0]
    assert b"COPY public.t (note) FROM stdin;\nSET transaction_timeout = 0;\n\\.\n" in kept
    assert b"SET client_encoding = 'UTF8';\n" in kept


@pytest.mark.unit
def test_host_matches_suffix_requires_dot_boundary() -> None:
    from mirroring.management.postgres_clone import host_matches_suffix

    assert host_matches_suffix("db.staging.example.com", "staging.example.com")
    assert host_matches_suffix("staging.example.com", "staging.example.com")
    assert not host_matches_suffix("notstaging.example.com", "staging.example.com")


@pytest.mark.unit
def test_cutover_preswap_refuses_existing_without_delete() -> None:
    from mirroring.management.postgres_clone import cutover_by_rename

    def run_checked(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
        raise AssertionError("should not rename when preswap exists")

    def run_capture(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> str:
        return "1\n"

    with pytest.raises(ValueError, match="already exists.*--delete-preswap"):
        cutover_by_rename(
            "postgres://u:p@db.example:5432/staging",
            "postgres://u:p@db.example:5432/staging_tmp",
            run_checked=run_checked,
            run_capture=run_capture,
            retired_name="staging_preswap",
            delete_existing_retired=False,
        )


@pytest.mark.unit
def test_cutover_preswap_deletes_existing_when_opted_in() -> None:
    from mirroring.management.postgres_clone import cutover_by_rename

    labels: list[str] = []

    def run_checked(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
        labels.append(label)

    def run_capture(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> str:
        return "1\n"

    retired = cutover_by_rename(
        "postgres://u:p@db.example:5432/staging",
        "postgres://u:p@db.example:5432/staging_tmp",
        run_checked=run_checked,
        run_capture=run_capture,
        retired_name="staging_preswap",
        delete_existing_retired=True,
    )
    assert retired == "staging_preswap"
    assert "psql drop database staging_preswap" in labels
    assert labels.index("psql park live database") < labels.index("psql promote shadow to live")
    assert "psql promote shadow to live" in labels


@pytest.mark.unit
def test_cutover_rolls_parked_database_back_when_promote_fails() -> None:
    from mirroring.management.postgres_clone import cutover_by_rename

    labels: list[str] = []

    def run_checked(cmd: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
        labels.append(label)
        if label == "psql promote shadow to live":
            raise RuntimeError("promotion failed")

    with pytest.raises(RuntimeError, match="promotion failed"):
        cutover_by_rename(
            "postgres://u:p@db.example:5432/staging",
            "postgres://u:p@db.example:5432/staging_tmp",
            run_checked=run_checked,
            retired_name="staging_preswap",
        )

    assert labels == [
        "psql park live database",
        "psql promote shadow to live",
        "psql rollback parked live database",
    ]


@pytest.mark.unit
def test_sql_literal_escapes_quotes() -> None:
    assert Command.sql_literal("O'Brien") == "'O''Brien'"
    assert Command.sql_literal("t") == "TRUE"
    assert Command.sql_literal("f") == "FALSE"


@pytest.mark.unit
def test_shadow_and_preswap_database_names() -> None:
    from mirroring.management.postgres_clone import preswap_database_name

    assert shadow_database_name("reskinned_inventory") == "reskinned_inventory_tmp"
    assert preswap_database_name("reskinned_inventory") == "reskinned_inventory_preswap"


@pytest.mark.unit
def test_stamp_restored_at_dry_run(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@staging.example:5432/staging")
    out = StringIO()
    call_command("restore_from_mirror", "--stamp-restored-at", "--dry-run", stdout=out)
    assert "Dry run complete" in out.getvalue()
    assert "Stamp MirrorDatabaseState.restored_at" in out.getvalue()


@pytest.mark.unit
def test_stamp_restored_at_requires_allow(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MIRROR_RESTORE_ALLOW", raising=False)
    with pytest.raises(CommandError, match="MIRROR_RESTORE_ALLOW=1"):
        call_command("restore_from_mirror", "--stamp-restored-at", stdout=StringIO())


@pytest.mark.unit
def test_restore_soft_fails_watermark_after_cutover(
    settings: SettingsWrapper,
    monkeypatch: MonkeyPatch,
) -> None:
    """Cutover success must not become a failed restore if watermark write fails."""
    settings.MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES = ["staging.example"]
    monkeypatch.setenv("MIRROR_RESTORE_ALLOW", "1")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    monkeypatch.setenv("MIRROR_RESTORE_TARGET_DATABASE_URL", "postgres://u:p@staging.example:5432/staging")

    with (
        patch("mirroring.management.commands.restore_from_mirror.shutil.which", return_value="/bin/true"),
        patch.object(Command, "snapshot_staff_credentials", return_value=[]),
        patch(
            "mirroring.management.commands.restore_from_mirror.recreate_shadow_database",
            return_value="postgres://u:p@staging.example:5432/staging_tmp",
        ),
        patch.object(Command, "reset_public_schema"),
        patch.object(Command, "stream_dump_to_psql"),
        patch.object(Command, "migrate_database"),
        patch.object(Command, "rematerialise_staff_credentials", return_value=0),
        patch(
            "mirroring.management.commands.restore_from_mirror.cutover_by_rename",
            return_value="staging_preswap",
        ),
        patch.object(Command, "record_restore_completion", side_effect=RuntimeError("alias boom")),
    ):
        out = StringIO()
        call_command("restore_from_mirror", "--confirm", "--skip-migrate", stdout=out)

    output = out.getvalue()
    assert "Cut over by rename" in output
    assert "Restore watermark still pending" in output
    assert "--stamp-restored-at" in output
