"""Pinned dependency and external-tool versions for django-mirroring.

Python packages are declared with compatible-release pins in ``pyproject.toml``.
CLI tools that cannot be installed via pip are pinned here and checked at
command start (``dumpling``, ``pg_dump``, ``psql``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any

from django.conf import settings
from django.core.management.base import CommandError

# Compatible-release pins (keep in sync with pyproject.toml).
PINNED_DJANGO = "6.0"
PINNED_DJ_DATABASE_URL = "2.2.0"
PINNED_PYTHON_DATEUTIL = "2.9.0"
PINNED_DUMPLING_CLI = "0.9.0"
PINNED_BOTO3 = "1.42.0"

# Postgres client tools are system packages, so they are pinned as a minimum major
# rather than an exact version: ``pg_dump`` refuses to dump a server newer than
# itself, while newer clients read older servers fine. Set this to the highest
# server major you mirror from (Heroku production runs PostgreSQL 15.x).
PINNED_POSTGRES_CLIENT_MAJOR = 15

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_POSTGRES_VERSION_RE = re.compile(r"\(PostgreSQL\)\s+(\d+)", re.IGNORECASE)


def _parse_semver(text: str) -> tuple[int, int, int]:
    match = _VERSION_RE.search(text.strip())
    if not match:
        raise ValueError(f"Could not parse version from {text!r}")
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    return major, minor, patch


def _run_version(executable: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            [executable, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"Required executable not found on PATH: {executable}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise CommandError(f"Failed to read version for {executable}: {detail or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"Timed out reading version for {executable}") from exc
    return (completed.stdout or completed.stderr or "").strip()


def require_executable(name: str) -> str:
    """Return the resolved path for ``name``, or raise if missing."""
    path = shutil.which(name)
    if path is None:
        raise CommandError(f"Required executable not found on PATH: {name}")
    return path


def minimum_postgres_client_major() -> int:
    """Return the pinned minimum Postgres client major, honouring host override."""
    raw = getattr(settings, "MIRRORING_POSTGRES_CLIENT_MAJOR", None)
    if raw is None or raw == "":
        raw = os.environ.get("MIRRORING_POSTGRES_CLIENT_MAJOR")
    if raw is None or raw == "":
        return PINNED_POSTGRES_CLIENT_MAJOR
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise CommandError(f"Invalid MIRRORING_POSTGRES_CLIENT_MAJOR: {raw!r}") from exc


def parse_postgres_client_major(version_text: str) -> int:
    match = _POSTGRES_VERSION_RE.search(version_text)
    if not match:
        raise ValueError(f"Could not parse PostgreSQL client major from {version_text!r}")
    return int(match.group(1))


def assert_compatible_release(actual: str, pinned: str, *, label: str) -> None:
    """Require ``actual`` to be in the same major.minor series as ``pinned`` (PEP 440 ~=)."""
    actual_v = _parse_semver(actual)
    pinned_v = _parse_semver(pinned)
    if actual_v[:2] != pinned_v[:2]:
        raise CommandError(
            f"{label} version {actual!r} is incompatible with pinned {pinned} "
            f"(expected {pinned_v[0]}.{pinned_v[1]}.x)."
        )
    if actual_v < pinned_v:
        raise CommandError(
            f"{label} version {actual!r} is older than pinned minimum {pinned}."
        )


def require_dumpling(executable: str | None = None) -> str:
    """Ensure Dumpling is on PATH and matches ``PINNED_DUMPLING_CLI`` (~=)."""
    dumpling_bin = executable or os.environ.get("DUMPLING_BIN") or "dumpling"
    require_executable(dumpling_bin)
    version_text = _run_version(dumpling_bin, "--version")
    # Examples: "dumpling 0.9.0", "0.9.0"
    actual = version_text.split()[-1]
    assert_compatible_release(actual, PINNED_DUMPLING_CLI, label="dumpling")
    return dumpling_bin


def require_postgres_clients(*names: str) -> dict[str, Any]:
    """Ensure the named client tools exist and meet the pinned minimum major."""
    minimum_major = minimum_postgres_client_major()
    details: dict[str, Any] = {"minimum_major": minimum_major, "tools": {}}
    for name in names or ("pg_dump", "psql"):
        require_executable(name)
        version_text = _run_version(name, "--version")
        try:
            major = parse_postgres_client_major(version_text)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if major < minimum_major:
            raise CommandError(
                f"{name} reports PostgreSQL client major {major}, but django-mirroring "
                f"requires at least major {minimum_major} "
                f"(set MIRRORING_POSTGRES_CLIENT_MAJOR to match your mirror source server)."
            )
        details["tools"][name] = {"major": major, "version_text": version_text}
    return details
