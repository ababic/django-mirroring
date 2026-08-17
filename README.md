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
| `MEDIA_SYNC_EXCLUDE_MODELS` | Skip whole models: `app_label.model` (e.g. `images.rendition`) |
| `MEDIA_SYNC_EXCLUDE_FIELDS` | Skip fields: `app_label.model.field` (e.g. `reskinned_inventory.picture.preview`) |
| `MEDIA_SYNC_DUMMY_MODELS` | Skip CopyObject **and** plant dummy objects at the same keys |
| `MEDIA_SYNC_DUMMY_FIELDS` | Same for individual fields (`app_label.model.field`) |
| `MEDIA_SYNC_DUMMY_PROVIDER` | Optional dotted callable `(MediaObjectRef) -> MediaDummySpec \| None` |
| `MEDIA_SYNC_DUMMY_FROM_SOURCE_HASH` | Seed image/PDF dummies from source ETag (default `True`; alias: `MEDIA_SYNC_DUMMY_IMAGES_FROM_SOURCE_HASH`) |
| `AWS_STORAGE_BUCKET_NAME` | Destination bucket (current env) |

```bash
python manage.py sync_referenced_media --dry-run
MEDIA_SYNC_ALLOW=1 python manage.py sync_referenced_media --confirm
```

Default behaviour skips keys already present on the destination (`--skip-existing`).
Missing source keys are counted and skipped (common when DB rows outlive deleted
objects).

### Opting out of fields / models / extras

```python
# settings.py
MEDIA_SYNC_EXCLUDE_MODELS = [
    "data_reporting.exporteddata",  # skip entirely (404s OK / table usually empty)
]
MEDIA_SYNC_EXCLUDE_FIELDS = [
    "reskinned_inventory.picture.thumbnail",
    "reskinned_inventory.picture.preview",
]
# Omit a collector from MEDIA_SYNC_EXTRA_COLLECTORS to skip that path bag entirely.
```

### Dummy replacements for PII (non-nullable / UI-linked paths)

When a FileField must stay populated (non-null, or operators open the file in admin)
but the real object is PII, list it under ``MEDIA_SYNC_DUMMY_*``. Those keys are
**not** copied from production; instead a tiny placeholder is `PutObject`'d at the
same destination key (PDF/CSV/XML/PNG defaults by suffix). Optional host hook:

```python
MEDIA_SYNC_DUMMY_MODELS = [
    "listing.shipment",  # dispatch/return labels + courier XML
]
MEDIA_SYNC_DUMMY_FIELDS = []
# Optional override; return None to fall back to suffix defaults:
# MEDIA_SYNC_DUMMY_PROVIDER = "myapp.media_sync.dummy_for_ref"
# Image/PDF dummies: seed visuals from the source object's ETag
# (content fingerprint) so placeholders differ without copying real bytes.
# MEDIA_SYNC_DUMMY_FROM_SOURCE_HASH = True  # default
```

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
