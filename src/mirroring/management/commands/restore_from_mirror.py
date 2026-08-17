"""Restore a target Postgres DB from the production database mirror via shadow cutover.

Loads ``MIRROR_DATABASE_URL`` into a shadow database, rematerialises staging staff
credentials, then renames so a failed load never wipes the live target.
Anonymisation already happened when the mirror was refreshed.

Safe replace algorithm
----------------------
1. Preflight — ``MIRROR_RESTORE_ALLOW``, distinct source/target, allow/block host
   suffixes.
2. Snapshot staging staff credentials (allowlisted email domains on the live
   target) and rematerialise onto the shadow by ``USERNAME_FIELD`` (username is
   kept on the database mirror; email/password are scrubbed there).
3. Recreate shadow ``{target}_tmp`` on the same cluster (``DROP`` if it already
   exists, then ``CREATE``) using target credentials (must have ``CREATEDB``).
4. Restore ``pg_dump`` of the mirror into the shadow (no Dumpling — already scrubbed).
   Failure leaves the live target untouched.
5. ``migrate`` against the shadow using this slug's code (staging schema ahead of prod).
   Generation watermark (``MirrorDatabaseState``) arrives with the mirror dump.
6. Just before cutover, rematerialise staging ``email``, ``password``,
   ``first_name``, ``last_name`` (+ staff flags) onto shadow users matched by
   ``USERNAME_FIELD``.
7. Cutover by rename — terminate connections, rename live → ``{target}_preswap``,
   shadow → live name (``DATABASE_URL`` keeps working). The previous live DB is
   kept as ``_preswap`` for rollback; deleting an existing ``_preswap`` from a
   prior run is opt-in via ``--delete-preswap``. Use ``revert_mirror_restore`` to
   swap ``_preswap`` back to live.
8. Stamp ``MirrorDatabaseState.restored_at`` on the live target after cutover.
   If that write fails, cutover is still treated as success; retry with
   ``--stamp-restored-at`` (no dump/rename).

Endpoints (no CLI URL overrides)::

    source = MIRROR_DATABASE_URL                     # production database mirror
    target = MIRROR_RESTORE_TARGET_DATABASE_URL      # staging primary to replace
    shadow = {target_dbname}_tmp                     # derived; drop+recreate each run

Examples::

    python backend/manage.py restore_from_mirror --dry-run
    MIRROR_RESTORE_ALLOW=1 python backend/manage.py restore_from_mirror --confirm
    MIRROR_RESTORE_ALLOW=1 python backend/manage.py restore_from_mirror --confirm --delete-preswap
    MIRROR_RESTORE_ALLOW=1 python backend/manage.py restore_from_mirror --stamp-restored-at
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import tempfile

from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.utils import timezone

from mirroring.base import BaseMirroringCommand
from mirroring.database import migrate_database_url, temporary_database_alias
from mirroring.management.postgres_clone import (
    DumpLineFilter,
    cutover_by_rename,
    database_identity,
    database_name,
    host_matches_suffix,
    libpq_environ,
    preswap_database_name,
    recreate_shadow_database,
    redact_database_url,
    shadow_database_name,
)
from mirroring.models import MirrorDatabaseState
from mirroring.versions import require_postgres_clients


if TYPE_CHECKING:
    from argparse import ArgumentParser

SOURCE_URL_ENV = "MIRROR_DATABASE_URL"
TARGET_URL_ENV = "MIRROR_RESTORE_TARGET_DATABASE_URL"
MIRROR_RESTORE_ALLOW_ENV = "MIRROR_RESTORE_ALLOW"
SUBPROCESS_TIMEOUT_SECONDS = 300

STAFF_SNAPSHOT_COLUMNS = (
    "email",
    "username",
    "password",
    "is_staff",
    "is_superuser",
    "is_active",
    "first_name",
    "last_name",
)


class Command(BaseMirroringCommand):
    help = __doc__

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required to create/load the shadow DB and cut over the staging target.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the planned cutover; do not create DBs or write.",
        )
        parser.add_argument(
            "--skip-migrate",
            action="store_true",
            help="Skip migrate on the shadow DB (tests / already-migrated shadow only).",
        )
        parser.add_argument(
            "--delete-preswap",
            action="store_true",
            help=(
                "If {target}_preswap already exists from a previous swap, drop it before "
                "cutover. Without this flag the command refuses to overwrite it."
            ),
        )
        parser.add_argument(
            "--stamp-restored-at",
            action="store_true",
            help=(
                "Only stamp MirrorDatabaseState.restored_at on the live restore target "
                "(no dump/cutover). Use after a successful cutover if metadata recording failed."
            ),
        )
        parser.add_argument(
            "--keep-work-dir",
            action="store_true",
            help="Keep the temporary work directory after success.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["stamp_restored_at"]:
            self.handle_stamp_restored_at(dry_run=bool(options["dry_run"]))
            return

        dry_run = bool(options["dry_run"])
        confirm = bool(options["confirm"])
        if not dry_run and not confirm:
            raise CommandError("Refusing to replace staging without --confirm (or pass --dry-run).")

        if os.environ.get(MIRROR_RESTORE_ALLOW_ENV) != "1":
            raise CommandError(f"Refusing to run unless {MIRROR_RESTORE_ALLOW_ENV}=1.")

        source_url = self.require_env_url(SOURCE_URL_ENV)
        target_url = self.require_env_url(TARGET_URL_ENV)
        self.assert_safe_endpoints(source_url, target_url, require_allowed_host=not dry_run)
        target_db = database_name(target_url)
        try:
            shadow_name = shadow_database_name(target_db)
            preswap_name = preswap_database_name(target_db)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        delete_preswap = bool(options["delete_preswap"])

        user_model = get_user_model()
        username_field = user_model.USERNAME_FIELD
        staff_domains = list(getattr(settings, "MIRROR_RESTORE_STAFF_EMAIL_DOMAINS", []))
        user_table = user_model._meta.db_table

        require_postgres_clients("pg_dump", "psql")

        self.render_h1("Refresh staging from database mirror" + (" (dry run)" if dry_run else ""))
        self.info(f"Source ({SOURCE_URL_ENV}): {redact_database_url(source_url)}")
        self.info(f"Target ({TARGET_URL_ENV}): {redact_database_url(target_url)}")
        self.info(f"Shadow: DROP/CREATE {shadow_name!r} on target server")
        self.info(f"Cutover: rename {target_db!r} → {preswap_name!r}, then {shadow_name!r} → {target_db!r}")
        if delete_preswap:
            self.info(f"Existing {preswap_name!r}: will DROP before cutover (--delete-preswap)")
        else:
            self.info(f"Existing {preswap_name!r}: refuse if present (pass --delete-preswap to drop)")
        self.info(f"Credential match field: {username_field} (UserModel.USERNAME_FIELD)")
        self.info(f"Staff snapshot email domains: {staff_domains or '(none — no password rematerialise)'}")

        if dry_run:
            self.success("Dry run complete — no database writes.")
            return

        work_dir = Path(tempfile.mkdtemp(prefix="restore_from_mirror_"))
        shadow_url: str | None = None
        try:
            staff_rows = self.snapshot_staff_credentials(
                target_url,
                user_table=user_table,
                username_field=username_field,
                staff_domains=staff_domains,
            )
            self.info(f"Snapshotted {len(staff_rows)} staff credential row(s) from target.")

            try:
                shadow_url = recreate_shadow_database(target_url, run_checked=self.run_checked)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            self.info(f"Recreated shadow database {shadow_name!r}.")

            self.reset_public_schema(shadow_url)
            self.stream_dump_to_psql(
                src_url=source_url,
                dst_url=shadow_url,
                work_dir=work_dir,
            )

            if not options["skip_migrate"]:
                self.migrate_database(shadow_url)

            rematerialised = self.rematerialise_staff_credentials(
                shadow_url,
                staff_rows,
                user_table=user_table,
                username_field=username_field,
            )
            self.info(f"Rematerialised credentials for {rematerialised} user(s).")

            try:
                retired_name = cutover_by_rename(
                    target_url,
                    shadow_url,
                    run_checked=self.run_checked,
                    run_capture=self.run_capture,
                    retired_name=preswap_name,
                    delete_existing_retired=delete_preswap,
                )
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            shadow_url = None
            watermark_pending = False
            try:
                self.record_restore_completion(target_url)
            except Exception as exc:
                # Cutover already succeeded; do not fail the command or force a destructive retry.
                watermark_pending = True
                self.warning(
                    "Database cutover succeeded, but recording MirrorDatabaseState.restored_at failed "
                    f"({exc}). Retry metadata only with:\n"
                    f"  {MIRROR_RESTORE_ALLOW_ENV}=1 python backend/manage.py restore_from_mirror "
                    "--stamp-restored-at"
                )
            message = (
                f"Cut over by rename. Live DB is again {target_db!r}; "
                f"previous target retained as {retired_name!r} "
                f"(not deleted — drop manually or pass --delete-preswap on the next run)."
            )
            if watermark_pending:
                message += " Restore watermark still pending — use --stamp-restored-at."
            self.success(message)
        except Exception:
            if shadow_url:
                self.warning(
                    f"Leaving shadow database {shadow_name!r} in place for inspection "
                    f"({redact_database_url(shadow_url)})."
                )
            raise
        finally:
            if options["keep_work_dir"]:
                self.warning(f"Keeping work dir: {work_dir}")
            else:
                shutil.rmtree(work_dir, ignore_errors=True)

    def handle_stamp_restored_at(self, *, dry_run: bool) -> None:
        """Stamp ``restored_at`` on the live restore target without dump/cutover."""
        if os.environ.get(MIRROR_RESTORE_ALLOW_ENV) != "1":
            raise CommandError(f"Refusing to run unless {MIRROR_RESTORE_ALLOW_ENV}=1.")
        source_url = self.require_env_url(SOURCE_URL_ENV)
        target_url = self.require_env_url(TARGET_URL_ENV)
        # Reuse the same endpoint gates as restore (distinct source/target, allowlisted host).
        self.assert_safe_endpoints(source_url, target_url, require_allowed_host=not dry_run)
        self.render_h1("Stamp MirrorDatabaseState.restored_at" + (" (dry run)" if dry_run else ""))
        self.info(f"Target ({TARGET_URL_ENV}): {redact_database_url(target_url)}")
        if dry_run:
            self.success("Dry run complete — no database writes.")
            return
        self.record_restore_completion(target_url)
        self.success("Recorded restore completion watermark on live target.")

    def require_env_url(self, env_var: str) -> str:
        try:
            value = os.environ[env_var]
        except KeyError as exc:
            raise CommandError(f"Environment variable {env_var} is not set.") from exc
        if not value.strip():
            raise CommandError(f"Environment variable {env_var} is empty.")
        if urlparse(value).scheme not in {"postgres", "postgresql"}:
            raise CommandError(f"{env_var} must be a postgres:// or postgresql:// URL.")
        return value

    def assert_safe_endpoints(
        self,
        source_url: str,
        target_url: str,
        *,
        require_allowed_host: bool,
    ) -> None:
        if database_identity(source_url) == database_identity(target_url):
            raise CommandError(f"{SOURCE_URL_ENV} and {TARGET_URL_ENV} resolve to the same host/port/database.")

        allowed_hosts = list(getattr(settings, "MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES", []))
        target_host = database_identity(target_url)[0]
        if allowed_hosts and not any(host_matches_suffix(target_host, s) for s in allowed_hosts):
            raise CommandError(f"Target host {target_host!r} is not in MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES.")

        blocked = list(getattr(settings, "MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES", []))
        if any(host_matches_suffix(target_host, s) for s in blocked):
            raise CommandError(f"Target host {target_host!r} matches a blocked host suffix.")
        if require_allowed_host and not allowed_hosts:
            raise CommandError("Refusing a destructive restore without MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES.")

    def snapshot_staff_credentials(
        self,
        target_url: str,
        *,
        user_table: str,
        username_field: str,
        staff_domains: list[str],
    ) -> list[dict[str, str]]:
        """Snapshot staff rows from the live target for post-restore rematerialise.

        Selection uses allowlisted email domains on staging (still real there).
        Rematerialise matches on ``UserModel.USERNAME_FIELD``, which Dumpling
        keeps on the database mirror.
        """
        if not staff_domains:
            return []
        if username_field not in STAFF_SNAPSHOT_COLUMNS:
            raise CommandError(
                f"UserModel.USERNAME_FIELD={username_field!r} is not in the staff snapshot columns."
            )
        for domain in staff_domains:
            if not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
                raise CommandError(f"Unsafe staff email domain: {domain!r}")
        cols = ", ".join(STAFF_SNAPSHOT_COLUMNS)
        where = " OR ".join(f"email ILIKE '%@{domain}'" for domain in staff_domains)
        sql = f"COPY (SELECT {cols} FROM {user_table} WHERE {where}) TO STDOUT WITH CSV HEADER"
        result = self.run_capture(
            ["psql", "--quiet", "--echo-errors", "-v", "ON_ERROR_STOP=1", "-c", sql],
            label="psql staff credential snapshot",
            env=libpq_environ(target_url),
        )
        return list(csv.DictReader(io.StringIO(result)))

    def rematerialise_staff_credentials(
        self,
        shadow_url: str,
        staff_rows: list[dict[str, str]],
        *,
        user_table: str,
        username_field: str,
    ) -> int:
        if not staff_rows:
            return 0
        # Just before cutover: restore staging email/password/names onto rows
        # matched by kept USERNAME_FIELD (database mirror still has scrubbed values).
        restore_columns = (
            "email",
            "password",
            "first_name",
            "last_name",
            "is_staff",
            "is_superuser",
            "is_active",
        )
        updated = 0
        for row in staff_rows:
            match_value = row.get(username_field) or ""
            if not match_value:
                continue
            assignments: list[str] = []
            for column in restore_columns:
                if column not in row:
                    continue
                assignments.append(f"{column} = {self.sql_literal(row[column])}")
            if not assignments:
                continue
            sql = (
                f"UPDATE {user_table} SET {', '.join(assignments)} "
                f"WHERE {username_field} = {self.sql_literal(match_value)}"
            )
            matched = self.run_capture(
                [
                    "psql",
                    "--quiet",
                    "--echo-errors",
                    "--tuples-only",
                    "--no-align",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    f"WITH updated AS ({sql} RETURNING 1) SELECT count(*) FROM updated;",
                ],
                label="psql rematerialise staff credentials",
                env=libpq_environ(shadow_url),
            )
            updated += int(matched.strip() or 0)
        return updated

    @staticmethod
    def sql_literal(value: str) -> str:
        """Render a SQL string or boolean literal from CSV text."""
        if value in {"t", "true", "True"}:
            return "TRUE"
        if value in {"f", "false", "False"}:
            return "FALSE"
        return "'" + value.replace("'", "''") + "'"

    def reset_public_schema(self, dst_url: str) -> None:
        self.info("Resetting shadow public schema…")
        sql = (
            "DROP SCHEMA IF EXISTS public CASCADE; "
            "CREATE SCHEMA public; "
            "GRANT ALL ON SCHEMA public TO public; "
            "GRANT ALL ON SCHEMA public TO CURRENT_USER;"
        )
        self.run_checked(
            ["psql", "--quiet", "--echo-errors", "-v", "ON_ERROR_STOP=1", "-c", sql],
            label="psql reset shadow schema",
            env=libpq_environ(dst_url),
        )

    def stream_dump_to_psql(self, *, src_url: str, dst_url: str, work_dir: Path) -> None:
        """``pg_dump | psql`` with PG17 ``transaction_timeout`` lines stripped."""
        dump_cmd = ["pg_dump", "--format=plain", "--no-owner", "--no-acl", "--no-privileges"]
        psql_cmd = ["psql", "--quiet", "--echo-errors", "-v", "ON_ERROR_STOP=1"]
        src_env = libpq_environ(src_url)
        dst_env = libpq_environ(dst_url)
        self.info("Streaming pg_dump → psql (mirror source → shadow)…")
        dump_err_path = work_dir / "pg_dump.stderr"
        psql_err_path = work_dir / "psql.stderr"
        line_filter = DumpLineFilter()
        with dump_err_path.open("w+b") as dump_err, psql_err_path.open("w+b") as psql_err:
            dump_proc = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=dump_err, env=src_env)
            if dump_proc.stdout is None:
                raise CommandError("pg_dump stdout pipe was not created.")
            psql_proc = subprocess.Popen(
                psql_cmd,
                stdin=subprocess.PIPE,
                stderr=psql_err,
                env=dst_env,
            )
            if psql_proc.stdin is None:
                raise CommandError("psql stdin pipe was not created.")
            children = (dump_proc, psql_proc)
            completed = False
            downstream_broken = False
            try:
                while True:
                    raw = dump_proc.stdout.readline()
                    if not raw:
                        completed = True
                        break
                    if line_filter.keep(raw):
                        try:
                            psql_proc.stdin.write(raw)
                        except BrokenPipeError:
                            downstream_broken = True
                            break
            except BaseException:
                for proc in children:
                    if proc.poll() is None:
                        proc.kill()
                raise
            finally:
                dump_proc.stdout.close()
                try:
                    psql_proc.stdin.close()
                except BrokenPipeError:
                    pass
                if not completed and dump_proc.poll() is None:
                    dump_proc.kill()
                dump_rc = dump_proc.wait()
                psql_rc = psql_proc.wait()
        dump_stderr = dump_err_path.read_text(encoding="utf-8", errors="replace")
        psql_stderr = psql_err_path.read_text(encoding="utf-8", errors="replace")
        if downstream_broken and psql_rc != 0:
            raise CommandError(f"psql restore failed ({psql_rc}): {psql_stderr.strip()}")
        if dump_rc != 0:
            raise CommandError(f"pg_dump failed ({dump_rc}): {dump_stderr.strip()}")
        if psql_rc != 0:
            raise CommandError(f"psql restore failed ({psql_rc}): {psql_stderr.strip()}")

    def migrate_database(self, database_url: str) -> None:
        """Run Django migrations against ``database_url`` via a temporary DB alias."""
        self.info("Running migrate against shadow database…")
        migrate_database_url(
            database_url,
            alias="mirroring_restore_shadow",
            verbosity=min(self.verbosity, 1),
        )

    def record_restore_completion(self, database_url: str) -> None:
        """Stamp ``restored_at`` on the live target after a successful cutover."""
        self.info("Recording MirrorDatabaseState.restored_at on live target…")
        with temporary_database_alias(database_url, alias="mirroring_restore_live") as alias:
            state = MirrorDatabaseState.get_solo(using=alias)
            state.restored_at = timezone.now()
            state.save(using=alias, update_fields=["restored_at"])
        self.info(f"Recorded restore completion at {state.restored_at.isoformat()}.")

    def run_checked(
        self,
        cmd: list[str],
        *,
        label: str,
        env: dict[str, str] | None = None,
        timeout: int | None = SUBPROCESS_TIMEOUT_SECONDS,
    ) -> None:
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"{label} timed out after {timeout}s.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise CommandError(f"{label} failed ({result.returncode}): {detail}")

    def run_capture(
        self,
        cmd: list[str],
        *,
        label: str,
        env: dict[str, str] | None = None,
        timeout: int | None = SUBPROCESS_TIMEOUT_SECONDS,
    ) -> str:
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"{label} timed out after {timeout}s.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise CommandError(f"{label} failed ({result.returncode}): {detail}")
        return result.stdout
