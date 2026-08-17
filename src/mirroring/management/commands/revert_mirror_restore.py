"""Revert a staging ``restore_from_mirror`` rename cutover from ``{target}_preswap``.

After a successful restore cutover the previous live database remains as
``{target}_preswap``. This command swaps it back:

1. Preflight — ``MIRROR_RESTORE_ALLOW`` (target comes from env URL).
2. Require ``{target}_preswap`` to exist (otherwise nothing to revert).
3. Park live ``{target}`` → ``{target}_backout`` (frees the live name).
4. Rename ``{target}_preswap`` → ``{target}``.
5. Drop ``{target}_backout`` (ephemeral; leftover ``_backout`` from a crashed
   prior revert is dropped first).

Endpoint (no CLI URL overrides)::

    target = MIRROR_RESTORE_TARGET_DATABASE_URL

Examples::

    python backend/manage.py revert_mirror_restore --dry-run
    MIRROR_RESTORE_ALLOW=1 python backend/manage.py revert_mirror_restore --confirm
"""

from __future__ import annotations

import os
import subprocess

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from django.core.management.base import CommandError

from mirroring.base import BaseMirroringCommand
from mirroring.management.postgres_clone import (
    backout_database_name,
    database_name,
    postgres_database_exists,
    preswap_database_name,
    redact_database_url,
    revert_preswap_cutover,
)
from mirroring.versions import require_postgres_clients


if TYPE_CHECKING:
    from argparse import ArgumentParser

TARGET_URL_ENV = "MIRROR_RESTORE_TARGET_DATABASE_URL"
MIRROR_RESTORE_ALLOW_ENV = "MIRROR_RESTORE_ALLOW"
SUBPROCESS_TIMEOUT_SECONDS = 300


class Command(BaseMirroringCommand):
    help = __doc__

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required to rename databases and drop the ephemeral _backout DB.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the planned revert; do not rename or drop databases.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        confirm = bool(options["confirm"])
        if not dry_run and not confirm:
            raise CommandError("Refusing to revert without --confirm (or pass --dry-run).")

        if os.environ.get(MIRROR_RESTORE_ALLOW_ENV) != "1":
            raise CommandError(f"Refusing to run unless {MIRROR_RESTORE_ALLOW_ENV}=1.")

        target_url = self.require_env_url(TARGET_URL_ENV)
        target_db = database_name(target_url)
        try:
            preswap_name = preswap_database_name(target_db)
            backout_name = backout_database_name(target_db)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        require_postgres_clients("psql")

        self.render_h1("Revert staging mirror restore" + (" (dry run)" if dry_run else ""))
        self.info(f"Target ({TARGET_URL_ENV}): {redact_database_url(target_url)}")
        self.info(
            f"Plan: rename {target_db!r} → {backout_name!r}, "
            f"{preswap_name!r} → {target_db!r}, then DROP {backout_name!r}"
        )

        if dry_run:
            # Existence checks are read-only and help operators before --confirm.
            try:
                has_live = postgres_database_exists(target_url, target_db, run_capture=self.run_capture)
                has_preswap = postgres_database_exists(target_url, preswap_name, run_capture=self.run_capture)
                has_backout = postgres_database_exists(target_url, backout_name, run_capture=self.run_capture)
            except Exception as exc:
                raise CommandError(str(exc)) from exc
            self.info(f"Exists {target_db!r}: {has_live}")
            self.info(f"Exists {preswap_name!r}: {has_preswap}")
            self.info(f"Exists {backout_name!r}: {has_backout} (would DROP first if present)")
            if not has_preswap:
                self.warning(f"No {preswap_name!r} — revert would fail without a prior cutover.")
            self.success("Dry run complete — no database writes.")
            return

        try:
            revert_preswap_cutover(
                target_url,
                run_checked=self.run_checked,
                run_capture=self.run_capture,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.success(
            f"Reverted: live DB is again the previous {preswap_name!r} contents "
            f"(now named {target_db!r}); ephemeral {backout_name!r} dropped."
        )

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
