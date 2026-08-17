# django-mirroring

Django add-on for production database mirror refresh and staging restore. Uses
[Dumpling](https://github.com/ababic/dumpling) for in-stream anonymisation and
Postgres shadow-database cutover so consumers never see a half-loaded mirror.

## Installation

From GitHub:

```bash
pip install git+https://github.com/ababic/django-mirroring.git
```

From PyPI (when published):

```bash
pip install django-mirroring
```

Add `"mirroring"` to `INSTALLED_APPS` and run migrations:

```bash
python manage.py migrate mirroring
```

## Pinned dependencies

Every dependency is pinned to a compatible release (`~=`) so a mirror run cannot
silently pick up an incompatible Dumpling policy format or AWS SDK behaviour.

| Dependency | Pin | Used by |
|------------|-----|---------|
| Python | `>=3.12` | all |
| `Django` | `~=6.0.0` | all |
| `dj-database-url` | `~=2.2.0` | temporary database aliases |
| `python-dateutil` | `~=2.9.0` | retain-window cutoffs |
| `dumpling-cli` | `~=0.9.0` | `refresh_database_mirror` anonymisation |
| `boto3` | `~=1.42.0` | `sync_referenced_media` |

`dumpling-cli` ships the `dumpling` executable, so installing this package also
pins the CLI. The commands verify it at start-up and refuse to run against a
different minor series.

**Postgres client tools** (`pg_dump`, `psql`) are system packages, not pip
dependencies, so they are pinned as a **minimum major** — `pg_dump` refuses to
dump a server newer than itself, while newer clients read older servers fine:

| Setting / env | Default | Purpose |
|---------------|---------|---------|
| `MIRRORING_POSTGRES_CLIENT_MAJOR` | `15` | Minimum `pg_dump` / `psql` major; set to the highest server major you mirror from |

Commands fail fast with a clear error when a tool is missing or too old, rather
than part-way through a dump.

## Dumpling policy (project-owned)

Anonymisation rules live in a **project-owned** Dumpling TOML file, not inside this
package. Point the refresh command at it with `MIRROR_DUMPLING_CONFIG` (env) or
`MIRROR_DUMPLING_CONFIG` (Django setting). The effective config may also embed
generated `row_filters` and staff username keep rules at refresh time.

## Settings overview

Configure via Django settings and/or environment variables (host projects typically
wire env → settings in one place).

| Setting / env | Purpose |
|---------------|---------|
| `MIRROR_SOURCE_DATABASE_URL` | `pg_dump` source (prefer a full-access follower/replica) |
| `MIRROR_DATABASE_URL` | Published mirror database (destination for refresh; source for restore) |
| `MIRROR_DUMPLING_CONFIG` | Path to project Dumpling TOML (required for refresh) |
| `MIRROR_EXCLUDED_SCHEMA` | Schemas omitted from dump (default: none — set in host settings) |
| `MIRROR_EXCLUDED_TABLES` | Tables omitted entirely |
| `MIRROR_EXCLUDED_TABLE_DATA` | Tables whose data is omitted (schema kept) — build with `build_mirror_excluded_table_data()` |
| `MIRROR_ROW_RETAIN` | Per-table datetime retain specs for Dumpling `row_filters` |
| `MIRROR_RETAIN_MONTHS` | Months of row history to keep (0 disables) |
| `MIRROR_RESTORE_TARGET_DATABASE_URL` | Staging DB replaced by `restore_from_mirror` |
| `MIRROR_RESTORE_ALLOW` | Must be `1` to run restore/revert |
| `MIRROR_RESTORE_STAFF_EMAIL_DOMAINS` | Comma-separated staff email domains (username keep + restore rematerialisation) |
| `MIRROR_RESTORE_USER_MATCH_FIELD` | User field to match on restore (default: `username`) |
| `MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES` | Allow-list for restore target hosts (fail closed when empty) |
| `MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES` | Block-list for restore target hosts |
| `MIRROR_AUTH_USER_DB_TABLE` | Optional qualified user table (default: `get_user_model()._meta.db_table`) |
| `MIRRORING_AUTO_REGISTER_ADMIN` | Register admin model (default: `True`) |
| `MIRRORING_ADMIN_SITE` | Optional dotted path to a custom `AdminSite` (e.g. `"core.admin.site"`) |
| `MIRRORING_POSTGRES_CLIENT_MAJOR` | Minimum `pg_dump` / `psql` major (default: `15`) |

`DUMPLING_GLOBAL_SALT` must be set in the environment for Dumpling lint/run.

## Endpoint guidance (operators)

Refresh only refuses when source and destination resolve to the **same**
host/port/database. Credential choice is otherwise an operator responsibility:

- Prefer pointing `MIRROR_SOURCE_DATABASE_URL` at a **full-access** follower or
  offline replica so `pg_dump` can read every table Dumpling anonymises, and so
  refresh load does not compete with live writes. Dumping the primary is allowed
  but not recommended under load.
- Prefer pointing `MIRROR_DATABASE_URL` at a **separate** mirror database from the
  live app primary. The destination role needs `CREATEDB` for shadow load + rename
  cutover.
- A restricted / allow-listed role that cannot `SELECT` PII tables will produce an
  incomplete or failing dump — use full-access credentials for the source.
- Put `sslmode` on connection URLs when the server requires TLS (no hostname-based
  SSL inference).
- Omit provider schemas (e.g. Heroku's `heroku_ext` / `_heroku`) via
  `MIRROR_EXCLUDED_SCHEMA` in the host project when needed.

Restore/revert use configurable host allow/block lists
(`MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES` /
`MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES`) rather than hardcoded production
URL env vars.

## Management commands

| Command | Role |
|---------|------|
| `refresh_database_mirror` | Nightly production job: dump follower → Dumpling → shadow DB → rename cutover |
| `restore_from_mirror` | Replace staging from the mirror via shadow load + rename cutover |
| `revert_mirror_restore` | Swap `{target}_preswap` back after a restore |
| `sync_referenced_media` | After restore: copy DB-referenced S3 keys from a source bucket into `AWS_STORAGE_BUCKET_NAME` |

## Selective media sync (separate buckets)

When staging must **not** share the production media bucket, run
`sync_referenced_media` after `restore_from_mirror`. It collects keys from every
`FileField` / `ImageField` (honouring private storage `location` prefixes) plus
optional host collectors, then `CopyObject`s only those keys.

| Setting / env | Purpose |
|---------------|---------|
| `MEDIA_SYNC_SOURCE_BUCKET` | Production (or mirror-source) media bucket to read from |
| `MEDIA_SYNC_SOURCE_REGION` | Optional source region (defaults to `AWS_DEFAULT_REGION`) |
| `MEDIA_SYNC_ALLOW` | Must be `1` for a live copy (`--dry-run` does not need it) |
| `MEDIA_SYNC_EXTRA_COLLECTORS` | List of dotted callables yielding extra relative keys (JSON path bags, CharFields, …) |
| `MIRRORING_ANONYMISE_MEDIA_FIELDS` | Models/fields to anonymise: `app.model` or `app.model.field` (skip CopyObject; plant placeholders) |
| `MIRRORING_ANONYMISE_MEDIA_PROVIDER` | Optional dotted callable `(MediaObjectRef) -> MediaAnonymiseSpec \| None` |
| `AWS_STORAGE_BUCKET_NAME` | Destination bucket (current env) |

```bash
python manage.py sync_referenced_media --dry-run
MEDIA_SYNC_ALLOW=1 python manage.py sync_referenced_media --confirm
```

Default behaviour skips keys already present on the destination (`--skip-existing`).
Missing source keys are counted and skipped (common when DB rows outlive deleted
objects).

### Anonymising PII media

List models or fields that must not be copied as-is. Those keys are **not**
copied from production; instead a placeholder is `PutObject`'d at the same
destination key. Image/PDF placeholders are seeded from the source object's
ETag (content fingerprint) so they stay visually distinct without copying real
bytes.

```python
# settings.py
MIRRORING_ANONYMISE_MEDIA_FIELDS = [
    "listing.shipment",                 # dispatch/return labels + courier XML
    "ebay.ebaycoupondownload",          # coupon transaction CSVs
    "data_reporting.exporteddata",      # admin exports
    # or field-level: "reskinned_inventory.picture.preview",
]
# Optional override; return None to fall back to suffix defaults:
# MIRRORING_ANONYMISE_MEDIA_PROVIDER = "myapp.media_sync.anonymise_for_ref"
```

Omit a collector from `MEDIA_SYNC_EXTRA_COLLECTORS` to skip that path bag entirely.

## Admin

`MirrorDatabaseState` is a read-only singleton watermark (generation + restore time).
By default it registers on `django.contrib.admin.site`. Set `MIRRORING_ADMIN_SITE`
to your project's admin site (for example `"core.admin.site"`) or call
`mirroring.admin.register_admin(site)` yourself with `MIRRORING_AUTO_REGISTER_ADMIN = False`.

## Development

```bash
just create-venv
source .venv/bin/activate
just install-requirements
```

Run the package tests from a host Django project that installs this package, e.g.:

```bash
pytest --pyargs mirroring.tests
```
