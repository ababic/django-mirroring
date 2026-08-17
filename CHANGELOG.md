# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.3] - 2026-08-17

### Removed

- `MIRROR_AUTH_USER_DB_TABLE` — user table for Dumpling staff rules and post-restore
  password fixes always comes from `get_user_model()._meta.db_table`.

## [0.2.2] - 2026-08-17

### Removed

- `MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES` / `MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES`
  and `host_matches_suffix` — restore/revert target whatever
  `MIRROR_RESTORE_TARGET_DATABASE_URL` points at (still gated by
  `MIRROR_RESTORE_ALLOW=1`, `--confirm`, and source≠target).

## [0.2.1] - 2026-08-17

### Removed

- `MIRROR_RESTORE_USER_MATCH_FIELD` — staff credential rematerialise always uses `UserModel.USERNAME_FIELD`.

## [0.2.0] - 2026-08-17

### Changed

- Restructured the repository to follow [cookiecutter-wagtail-package](https://github.com/wagtail/cookiecutter-wagtail-package) conventions (Django-only): `src/` layout, top-level `tests/`, nested `mirroring.test` settings, `uv` build backend, Ruff, Just recipes, and GitHub Actions CI/publish workflows.
- Dependencies remain compatible-release pinned (`Django~=6.0`, `dumpling-cli~=0.9`, `boto3~=1.42`, …). External tools (`dumpling`, `pg_dump`, `psql`) are still verified at command start-up.

## [0.1.9] - 2026-08-17

### Added

- Pinned `dumpling-cli` and `boto3` as real package dependencies.
- `mirroring.versions` start-up checks for Dumpling and Postgres client majors.

## [0.1.8] - 2026-08-17

### Changed

- Removed legacy `MEDIA_SYNC_EXCLUDE_*` / `MEDIA_SYNC_DUMMY_*` aliases.
- Renamed dummy placeholder APIs to anonymise terminology (`MediaAnonymiseSpec`, `plant_anonymised_media_refs`, …).

## [0.1.0] - 2026-08-16

### Added

- Initial extraction of the Reskinned Inventory mirroring app into `django-mirroring`.
- Management commands: `refresh_database_mirror`, `restore_from_mirror`, `revert_mirror_restore`, `sync_referenced_media`.
