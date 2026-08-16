"""Tests for refresh_database_mirror helpers and safety gates."""

from __future__ import annotations

from datetime import date
from io import StringIO
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from mirroring.management.commands.refresh_database_mirror import (
    Command as RefreshCommand,
)
from mirroring.management.commands.refresh_database_mirror import (
    build_row_retain_toml,
    build_staff_username_keep_toml,
    database_identity,
    keep_dump_line,
    libpq_environ,
    strip_unsupported_session_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch
    from pytest_django.fixtures import SettingsWrapper


@pytest.mark.unit
def test_database_identity_ignores_credentials() -> None:
    a = "postgres://user:secret@host.example:5432/appdb"
    b = "postgresql://other:pass@host.example:5432/appdb"
    assert database_identity(a) == database_identity(b)
    assert database_identity(a) != database_identity("postgres://user:secret@host.example:5432/otherdb")


@pytest.mark.unit
def test_libpq_environ_puts_credentials_in_env_not_url() -> None:
    env = libpq_environ(
        "postgres://alice%40crew:s3cret%21@db.example:6543/appdb?sslmode=require",
        base_env={},
    )
    assert env["PGHOST"] == "db.example"
    assert env["PGPORT"] == "6543"
    assert env["PGUSER"] == "alice@crew"
    assert env["PGPASSWORD"] == "s3cret!"
    assert env["PGDATABASE"] == "appdb"
    assert env["PGSSLMODE"] == "require"


@pytest.mark.unit
def test_libpq_environ_defaults_sslmode_for_heroku_hosts() -> None:
    env = libpq_environ("postgres://u:p@ec2-1-2-3-4.compute-1.amazonaws.com:5432/d", base_env={})
    assert env["PGSSLMODE"] == "require"


@pytest.mark.unit
def test_build_row_retain_toml_emits_datetime_gte_null_and_cascade() -> None:
    toml = build_row_retain_toml(
        [
            {
                "table": "public.listing_order",
                "column": "created",
                "cascades": [
                    {
                        "child_table": "public.listing_orderitem",
                        "child_fk": "order_id",
                        "parent_pk": "id",
                    },
                ],
            }
        ],
        cutoff=date(2025, 1, 1),
    )
    assert '[row_filters."public.listing_order"]' in toml
    assert 'op = "gte", value = "2025-01-01", format = "datetime"' in toml
    assert 'op = "is_null"' in toml
    assert '[[row_filters."public.listing_order".cascade]]' in toml
    assert 'child_table = "public.listing_orderitem"' in toml
    assert 'child_fk = "order_id"' in toml
    assert 'parent_pk = "id"' in toml
    assert 'op = "like"' not in toml


@pytest.mark.unit
def test_build_staff_username_keep_toml_uses_configured_domains() -> None:
    toml = build_staff_username_keep_toml(
        ["WEARECREW.COM", "new-staff.example"],
        user_table="public.auth_user",
    )
    assert '[[column_cases."public.auth_user".username]]' in toml
    assert 'value = "%@wearecrew.com"' in toml
    assert 'value = "%@new-staff.example"' in toml


@pytest.mark.unit
def test_build_mirror_excluded_table_data_merges_and_qualifies() -> None:
    from mirroring.defaults import build_mirror_excluded_table_data

    result = build_mirror_excluded_table_data(["django_session"], ["public.foo", "bar"])
    assert result == ["public.bar", "public.django_session", "public.foo"]


@pytest.mark.unit
def test_strip_unsupported_session_settings() -> None:
    assert strip_unsupported_session_settings("SET transaction_timeout = 0;\n") is False
    assert strip_unsupported_session_settings("SET SESSION transaction_timeout = 0;\n") is False
    assert strip_unsupported_session_settings("SET client_encoding = 'UTF8';\n") is True
    assert keep_dump_line(b"SET transaction_timeout = 0;\n") is False
    assert keep_dump_line(b"COPY public.listing_order FROM stdin;\n") is True


@pytest.mark.unit
def test_refresh_database_mirror_requires_confirm(settings: SettingsWrapper) -> None:
    settings.ENV = "production"
    with pytest.raises(CommandError, match="--confirm"):
        call_command("refresh_database_mirror", stdout=StringIO())


@pytest.mark.unit
def test_refresh_database_mirror_blocks_non_production(settings: SettingsWrapper) -> None:
    settings.ENV = "staging"
    with pytest.raises(CommandError, match="ENV='staging'"):
        call_command("refresh_database_mirror", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_refresh_database_mirror_refuses_same_src_and_dst(settings: SettingsWrapper, monkeypatch: MonkeyPatch) -> None:
    settings.ENV = "production"
    monkeypatch.setenv("MIRROR_SOURCE_DATABASE_URL", "postgres://u:p@db.example:5432/live")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@db.example:5432/live")
    monkeypatch.setenv("DUMPLING_GLOBAL_SALT", "unit-test-salt")
    with pytest.raises(CommandError, match="same host/port/database"):
        call_command("refresh_database_mirror", "--confirm", "--allow-non-production", stdout=StringIO())


@pytest.mark.unit
def test_refresh_database_mirror_refuses_live_database_url_as_destination(
    settings: SettingsWrapper, monkeypatch: MonkeyPatch
) -> None:
    settings.ENV = "production"
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db.example:5432/live")
    monkeypatch.setenv("MIRROR_SOURCE_DATABASE_URL", "postgres://u:p@follower.example:5432/live")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@db.example:5432/live")
    monkeypatch.setenv("DUMPLING_GLOBAL_SALT", "unit-test-salt")
    with pytest.raises(CommandError, match="live DATABASE_URL"):
        call_command("refresh_database_mirror", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_refresh_database_mirror_refuses_primary_as_source(settings: SettingsWrapper, monkeypatch: MonkeyPatch) -> None:
    settings.ENV = "production"
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@primary.example:5432/live")
    monkeypatch.setenv("MIRROR_SOURCE_DATABASE_URL", "postgres://u:p@primary.example:5432/live")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@anon.example:5432/anon")
    monkeypatch.setenv("DUMPLING_GLOBAL_SALT", "unit-test-salt")
    with pytest.raises(CommandError, match="live DATABASE_URL as source"):
        call_command("refresh_database_mirror", "--confirm", stdout=StringIO())


@pytest.mark.unit
def test_refresh_database_mirror_dry_run_lints_policy(
    settings: SettingsWrapper, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    settings.ENV = "production"
    config = tmp_path / ".dumplingconf"
    config.write_text(
        'salt = "${DUMPLING_GLOBAL_SALT}"\n[rules."public.t"]\nemail = { strategy = "email" }\n', encoding="utf-8"
    )
    settings.MIRROR_DUMPLING_CONFIG = config

    monkeypatch.setenv("MIRROR_SOURCE_DATABASE_URL", "postgres://u:p@follower.example:5432/src")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@dst.example:5432/dst")
    monkeypatch.setenv("DUMPLING_GLOBAL_SALT", "unit-test-salt")

    with (
        patch("mirroring.management.commands.refresh_database_mirror.shutil.which", return_value="/bin/true"),
        patch("mirroring.management.commands.refresh_database_mirror.subprocess.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=0, stderr="", stdout="")
        out = StringIO()
        call_command("refresh_database_mirror", "--dry-run", stdout=out)

    output = out.getvalue()
    assert "Dry run complete" in output
    assert "DROP/CREATE 'dst_tmp'" in output
    assert run_mock.called
    lint_cmd = run_mock.call_args.args[0]
    assert lint_cmd[0] == "dumpling"
    assert "lint-policy" in lint_cmd


@pytest.mark.unit
def test_refresh_database_mirror_drops_retired_database(settings: SettingsWrapper, monkeypatch: MonkeyPatch) -> None:
    settings.ENV = "production"
    monkeypatch.setenv("MIRROR_SOURCE_DATABASE_URL", "postgres://u:p@follower.example:5432/src")
    monkeypatch.setenv("MIRROR_DATABASE_URL", "postgres://u:p@dst.example:5432/dst")
    monkeypatch.setenv("DUMPLING_GLOBAL_SALT", "unit-test-salt")
    with (
        patch("mirroring.management.commands.refresh_database_mirror.shutil.which", return_value="/bin/true"),
        patch(
            "mirroring.management.commands.refresh_database_mirror.recreate_shadow_database",
            return_value="postgres://u:p@dst.example:5432/dst_tmp",
        ),
        patch(
            "mirroring.management.commands.refresh_database_mirror.cutover_by_rename",
            return_value="dst_old_20260101_000000",
        ),
        patch("mirroring.management.commands.refresh_database_mirror.drop_database_if_exists") as drop_mock,
        patch.object(RefreshCommand, "lint_dumpling_policy"),
        patch.object(RefreshCommand, "reset_destination_schema"),
        patch.object(RefreshCommand, "stream_dump_through_dumpling", return_value={"dumpling_version": "0.9.0"}),
        patch.object(RefreshCommand, "post_restore_fixes"),
        patch.object(RefreshCommand, "record_mirror_generation"),
    ):
        call_command("refresh_database_mirror", "--confirm", stdout=StringIO())
    drop_mock.assert_called_once()
    assert drop_mock.call_args.args == (
        "postgres://u:p@dst.example:5432/dst",
        "dst_old_20260101_000000",
    )
    assert callable(drop_mock.call_args.kwargs["run_checked"])


@pytest.mark.unit
def test_shadow_database_name_is_target_plus_tmp() -> None:
    from mirroring.management.postgres_clone import shadow_database_name

    assert shadow_database_name("reskinned_inventory") == "reskinned_inventory_tmp"
    assert shadow_database_name("Mirror-DB") == "mirror_db_tmp"
