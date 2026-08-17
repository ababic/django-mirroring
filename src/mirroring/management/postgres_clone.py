"""Shared helpers for Postgres dump/restore management commands."""

from __future__ import annotations

import os
import re

from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlparse, urlunparse


# Postgres identifiers: unquoted names are folded to lowercase; keep safe charset only.
SAFE_POSTGRES_DB_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# ``run_checked(cmd, *, label, env)`` — matches management command helpers.
RunChecked = Callable[..., None]
# ``run_capture(cmd, *, label, env) -> str`` — stdout from a successful command.
RunCapture = Callable[..., str]
SHADOW_DB_SUFFIX = "_tmp"
PRESWAP_DB_SUFFIX = "_preswap"
BACKOUT_DB_SUFFIX = "_backout"


def database_identity(url: str) -> tuple[str, str, str]:
    """Return ``(host, port, dbname)`` for equality checks (credentials ignored)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = str(parsed.port or 5432)
    dbname = (parsed.path or "/").lstrip("/").split("?", 1)[0]
    return host, port, dbname


def database_name(url: str) -> str:
    """Return the database name from a Postgres URL."""
    return database_identity(url)[2]


def redact_database_url(url: str) -> str:
    """Return a credential-free display form of a Postgres URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "?"
    port = parsed.port or 5432
    dbname = (parsed.path or "/").lstrip("/") or "?"
    return f"postgres://***:***@{host}:{port}/{dbname}"


def replace_database_name(url: str, dbname: str) -> str:
    """Return ``url`` with the path (database name) replaced."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{dbname}"))


def maintenance_database_url(url: str, *, maintenance_db: str = "postgres") -> str:
    """Return a URL on the same server pointing at the maintenance database."""
    return replace_database_name(url, maintenance_db)


def libpq_environ(url: str, *, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess env with libpq connection vars so passwords stay out of argv.

    Honours ``sslmode`` from the URL query string when present; does not infer SSL
    from the hostname.
    """
    parsed = urlparse(url)
    env = dict(base_env if base_env is not None else os.environ)
    env["PGHOST"] = parsed.hostname or ""
    env["PGPORT"] = str(parsed.port or 5432)
    env["PGUSER"] = unquote(parsed.username or "")
    if parsed.password is not None:
        env["PGPASSWORD"] = unquote(parsed.password)
    env["PGDATABASE"] = (parsed.path or "/").lstrip("/").split("?", 1)[0]
    query = {key.lower(): values[0] for key, values in parse_qs(parsed.query).items() if values}
    if "sslmode" in query:
        env["PGSSLMODE"] = query["sslmode"]
    return env


class DumpLineFilter:
    """Forward a ``pg_dump`` plain stream, dropping unsupported session settings.

    PG17 emits ``SET transaction_timeout`` that older servers reject. Those lines
    must only be stripped **outside** ``COPY … FROM stdin`` payloads — a text
    COPY row can literally be ``SET transaction_timeout = 0;``.
    """

    def __init__(self) -> None:
        self.in_copy = False

    def keep(self, raw: bytes) -> bool:
        stripped = raw.lstrip()
        upper = stripped.upper()
        if self.in_copy:
            if stripped.rstrip(b"\r\n") == b"\\.":
                self.in_copy = False
            return True
        if upper.startswith(b"COPY ") and upper.rstrip().endswith(b"FROM STDIN;"):
            self.in_copy = True
            return True
        if upper.startswith(b"SET") and b"TRANSACTION_TIMEOUT" in upper:
            return False
        return True


def keep_dump_line(raw: bytes) -> bool:
    """Stateless SET-filter for unit tests; prefer :class:`DumpLineFilter` on streams."""
    return DumpLineFilter().keep(raw)


def host_matches_suffix(host: str, suffix: str) -> bool:
    """True when ``host`` equals ``suffix`` or is a subdomain of it (dot boundary)."""
    host = host.lower().rstrip(".")
    suffix = suffix.lower().rstrip(".")
    return host == suffix or host.endswith(f".{suffix}")


def run_psql_statements(
    *,
    server_url: str,
    run_checked: RunChecked,
    label: str,
    statements: list[str],
    maintenance_db: str = "postgres",
) -> None:
    """Run one or more SQL strings as separate ``psql -c`` arguments.

    ``DROP DATABASE`` / ``CREATE DATABASE`` cannot share a simple-query
    transaction with other statements, so each entry is its own ``-c``.
    """
    if not statements:
        raise ValueError("run_psql_statements requires at least one statement.")
    cmd = ["psql", "--quiet", "--echo-errors", "-v", "ON_ERROR_STOP=1"]
    for sql in statements:
        cmd.extend(["-c", sql])
    run_checked(
        cmd,
        label=label,
        env=libpq_environ(maintenance_database_url(server_url, maintenance_db=maintenance_db)),
    )


def terminate_backends_sql(dbname: str) -> str:
    return (
        "SELECT pg_terminate_backend(pid) "
        "FROM pg_stat_activity "
        f"WHERE datname = '{dbname}' AND pid <> pg_backend_pid();"
    )


def rollback_parked_database(
    server_url: str,
    *,
    parked_db: str,
    target_db: str,
    replacement_db: str,
    run_checked: RunChecked,
) -> None:
    """Restore a parked live DB after replacement promotion fails."""
    run_psql_statements(
        server_url=server_url,
        run_checked=run_checked,
        label="psql rollback parked live database",
        statements=[
            f"ALTER DATABASE {parked_db} ALLOW_CONNECTIONS false;",
            terminate_backends_sql(parked_db),
            (f"ALTER DATABASE {parked_db} RENAME TO {target_db}; ALTER DATABASE {target_db} ALLOW_CONNECTIONS true;"),
            f"ALTER DATABASE {replacement_db} ALLOW_CONNECTIONS true;",
        ],
    )


def shadow_database_name(target_dbname: str) -> str:
    """Return ``{target}_tmp`` (sanitized) for the same-cluster shadow database."""
    return _derived_database_name(target_dbname, SHADOW_DB_SUFFIX)


def preswap_database_name(target_dbname: str) -> str:
    """Return ``{target}_preswap`` — where the live DB is renamed at staging cutover."""
    return _derived_database_name(target_dbname, PRESWAP_DB_SUFFIX)


def backout_database_name(target_dbname: str) -> str:
    """Return ``{target}_backout`` — ephemeral park for the live DB during a revert."""
    return _derived_database_name(target_dbname, BACKOUT_DB_SUFFIX)


def _derived_database_name(target_dbname: str, suffix: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", target_dbname.lower()).strip("_") or "db"
    max_base = 63 - len(suffix)
    base = base[:max_base].rstrip("_") or "db"
    name = f"{base}{suffix}"
    if not SAFE_POSTGRES_DB_NAME.match(name):
        raise ValueError(f"Refusing unsafe database name: {name!r}")
    if name == target_dbname.lower():
        raise ValueError(f"Derived database name collides with target: {name!r}")
    return name


def retired_database_name(target_dbname: str) -> str:
    """Build a timestamped name for the database being replaced at mirror cutover."""
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    base = re.sub(r"[^a-z0-9_]", "_", target_dbname.lower())[:40].strip("_") or "db"
    name = f"{base}_old_{stamp}"
    if SAFE_POSTGRES_DB_NAME.match(name):
        return name
    return f"db_old_{stamp}"


def postgres_database_exists(
    server_url: str,
    dbname: str,
    *,
    run_capture: RunCapture,
) -> bool:
    """Return True when ``dbname`` exists on the server for ``server_url``."""
    if not SAFE_POSTGRES_DB_NAME.match(dbname):
        raise ValueError(f"Refusing unsafe database name: {dbname!r}")
    maintenance_url = maintenance_database_url(server_url)
    sql = f"SELECT 1 FROM pg_database WHERE datname = '{dbname}'"
    stdout = run_capture(
        ["psql", "--quiet", "--tuples-only", "--no-align", "-v", "ON_ERROR_STOP=1", "-c", sql],
        label="psql check database exists",
        env=libpq_environ(maintenance_url),
    )
    return stdout.strip() == "1"


def drop_database_if_exists(
    server_url: str,
    dbname: str,
    *,
    run_checked: RunChecked,
) -> None:
    """Terminate backends and ``DROP DATABASE IF EXISTS`` ``dbname``."""
    if not SAFE_POSTGRES_DB_NAME.match(dbname):
        raise ValueError(f"Refusing unsafe database name: {dbname!r}")
    # Separate -c: DROP cannot share a simple-query transaction with SELECT.
    run_psql_statements(
        server_url=server_url,
        run_checked=run_checked,
        label=f"psql drop database {dbname}",
        statements=[
            terminate_backends_sql(dbname),
            f"DROP DATABASE IF EXISTS {dbname};",
        ],
    )


def recreate_shadow_database(target_url: str, *, run_checked: RunChecked) -> str:
    """Drop ``{target}_tmp`` if it exists, recreate it empty, return its URL.

    Uses the target connection credentials against the maintenance database
    (``postgres``). Callers must have ``CREATEDB`` (or superuser) rights.
    """
    target_db = database_name(target_url)
    if not SAFE_POSTGRES_DB_NAME.match(target_db):
        raise ValueError(f"Refusing unsafe target database name: {target_db!r}")
    shadow_name = shadow_database_name(target_db)
    # Separate -c arguments: DROP/CREATE cannot run inside a multi-statement transaction.
    run_psql_statements(
        server_url=target_url,
        run_checked=run_checked,
        label="psql recreate shadow database",
        statements=[
            terminate_backends_sql(shadow_name),
            f"DROP DATABASE IF EXISTS {shadow_name};",
            f"CREATE DATABASE {shadow_name} WITH TEMPLATE template0;",
        ],
    )
    return replace_database_name(target_url, shadow_name)


def cutover_by_rename(
    target_url: str,
    shadow_url: str,
    *,
    run_checked: RunChecked,
    retired_name: str | None = None,
    delete_existing_retired: bool = False,
    run_capture: RunCapture | None = None,
) -> str:
    """Terminate backends, rename live → ``retired_name``, shadow → live name.

    Returns the retired database name. Requires shadow and target on the same host/port.

    Park the live database first so its name is free, then rename the shadow onto
    that name (two separate ``psql`` calls).

    When ``retired_name`` already exists:
    * ``delete_existing_retired=True`` drops it first (opt-in),
    * otherwise raises — callers should pass ``--delete-preswap`` (or equivalent).
    """
    target_host, target_port, target_db = database_identity(target_url)
    shadow_host, shadow_port, shadow_db = database_identity(shadow_url)
    if (target_host, target_port) != (shadow_host, shadow_port):
        raise ValueError("Rename cutover requires shadow and target on the same host/port.")
    if not SAFE_POSTGRES_DB_NAME.match(target_db) or not SAFE_POSTGRES_DB_NAME.match(shadow_db):
        raise ValueError("Refusing rename cutover with unsafe database names.")
    retired = retired_name or retired_database_name(target_db)
    if not SAFE_POSTGRES_DB_NAME.match(retired):
        raise ValueError(f"Refusing unsafe retired database name: {retired!r}")
    if retired in {target_db, shadow_db}:
        raise ValueError(f"Retired database name collides with target or shadow: {retired!r}")

    if run_capture is not None and postgres_database_exists(target_url, retired, run_capture=run_capture):
        if not delete_existing_retired:
            raise ValueError(
                f"Database {retired!r} already exists from a previous swap. "
                "Pass --delete-preswap to drop it before cutover, or drop it manually."
            )
        drop_database_if_exists(target_url, retired, run_checked=run_checked)

    # Park live first so ``target_db`` is free for the shadow rename.
    # Block new connections before terminate so dynos cannot reconnect mid-rename.
    run_psql_statements(
        server_url=target_url,
        run_checked=run_checked,
        label="psql park live database",
        statements=[
            f"ALTER DATABASE {target_db} ALLOW_CONNECTIONS false;",
            terminate_backends_sql(target_db),
            (f"ALTER DATABASE {target_db} RENAME TO {retired}; ALTER DATABASE {retired} ALLOW_CONNECTIONS true;"),
        ],
    )
    try:
        run_psql_statements(
            server_url=target_url,
            run_checked=run_checked,
            label="psql promote shadow to live",
            statements=[
                f"ALTER DATABASE {shadow_db} ALLOW_CONNECTIONS false;",
                terminate_backends_sql(shadow_db),
                (
                    f"ALTER DATABASE {shadow_db} RENAME TO {target_db}; "
                    f"ALTER DATABASE {target_db} ALLOW_CONNECTIONS true;"
                ),
            ],
        )
    except BaseException as exc:
        try:
            rollback_parked_database(
                target_url,
                parked_db=retired,
                target_db=target_db,
                replacement_db=shadow_db,
                run_checked=run_checked,
            )
        except BaseException as rollback_exc:
            exc.add_note(f"Automatic cutover rollback also failed: {rollback_exc}")
        raise
    return retired


def revert_preswap_cutover(
    target_url: str,
    *,
    run_checked: RunChecked,
    run_capture: RunCapture,
) -> None:
    """Undo a staging rename cutover using ``{target}_preswap``.

    Requires ``{target}_preswap`` to exist. Sequence (park first so the live
    name is free before promoting ``_preswap``):

    1. Drop leftover ``{target}_backout`` if present (ephemeral parking name).
    2. Rename live ``{target}`` → ``{target}_backout`` (frees the live name).
    3. Rename ``{target}_preswap`` → ``{target}``.
    4. Drop ``{target}_backout``.
    """
    target_db = database_name(target_url)
    if not SAFE_POSTGRES_DB_NAME.match(target_db):
        raise ValueError(f"Refusing unsafe target database name: {target_db!r}")
    preswap_name = preswap_database_name(target_db)
    backout_name = backout_database_name(target_db)

    if not postgres_database_exists(target_url, target_db, run_capture=run_capture):
        raise ValueError(f"Live database {target_db!r} does not exist; cannot revert.")
    if not postgres_database_exists(target_url, preswap_name, run_capture=run_capture):
        raise ValueError(
            f"No {preswap_name!r} database found — nothing to revert "
            "(restore_from_mirror cutover leaves the previous live DB as _preswap)."
        )

    if postgres_database_exists(target_url, backout_name, run_capture=run_capture):
        drop_database_if_exists(target_url, backout_name, run_checked=run_checked)

    # Park the current live DB first so ``target_db`` is free for the preswap rename.
    run_psql_statements(
        server_url=target_url,
        run_checked=run_checked,
        label="psql park live database as _backout",
        statements=[
            f"ALTER DATABASE {target_db} ALLOW_CONNECTIONS false;",
            terminate_backends_sql(target_db),
            (
                f"ALTER DATABASE {target_db} RENAME TO {backout_name}; "
                f"ALTER DATABASE {backout_name} ALLOW_CONNECTIONS true;"
            ),
        ],
    )
    try:
        run_psql_statements(
            server_url=target_url,
            run_checked=run_checked,
            label="psql promote _preswap to live",
            statements=[
                f"ALTER DATABASE {preswap_name} ALLOW_CONNECTIONS false;",
                terminate_backends_sql(preswap_name),
                (
                    f"ALTER DATABASE {preswap_name} RENAME TO {target_db}; "
                    f"ALTER DATABASE {target_db} ALLOW_CONNECTIONS true;"
                ),
            ],
        )
    except BaseException as exc:
        try:
            rollback_parked_database(
                target_url,
                parked_db=backout_name,
                target_db=target_db,
                replacement_db=preswap_name,
                run_checked=run_checked,
            )
        except BaseException as rollback_exc:
            exc.add_note(f"Automatic revert rollback also failed: {rollback_exc}")
        raise
    drop_database_if_exists(target_url, backout_name, run_checked=run_checked)
