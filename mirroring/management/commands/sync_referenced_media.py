"""Copy DB-referenced media objects from a source S3 bucket into the local bucket.

Use after ``restore_from_mirror`` when staging (or another env) has a **separate**
media bucket from production. Only keys referenced by FileField / ImageField
columns (plus optional ``MEDIA_SYNC_EXTRA_COLLECTORS``) are copied — not a full
bucket sync.

Endpoints::

    source = MEDIA_SYNC_SOURCE_BUCKET (+ optional MEDIA_SYNC_SOURCE_REGION)
    destination = AWS_STORAGE_BUCKET_NAME (+ AWS_DEFAULT_REGION)

Safety::

    MEDIA_SYNC_ALLOW=1 is required for a live copy (``--dry-run`` skips this).
    Prefer ``--skip-existing`` (default) so re-runs are cheap.

Examples::

    python manage.py sync_referenced_media --dry-run
    MEDIA_SYNC_ALLOW=1 python manage.py sync_referenced_media --confirm
    MEDIA_SYNC_ALLOW=1 python manage.py sync_referenced_media --confirm --limit 100
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.management.base import CommandError

from mirroring.base import BaseMirroringCommand
from mirroring.media import collect_referenced_media_refs, sync_media_refs_between_buckets

if TYPE_CHECKING:
    from argparse import ArgumentParser

SOURCE_BUCKET_ENV = "MEDIA_SYNC_SOURCE_BUCKET"
SOURCE_REGION_ENV = "MEDIA_SYNC_SOURCE_REGION"
ALLOW_ENV = "MEDIA_SYNC_ALLOW"


class Command(BaseMirroringCommand):
    help = __doc__

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required for a live copy (with MEDIA_SYNC_ALLOW=1).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Collect keys and count would-copy / missing / existing; write nothing.",
        )
        parser.add_argument(
            "--skip-existing",
            action="store_true",
            default=True,
            help="Skip keys already present on the destination (default).",
        )
        parser.add_argument(
            "--no-skip-existing",
            action="store_false",
            dest="skip_existing",
            help="Overwrite / re-copy keys even when the destination already has them.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after N referenced keys (0 = no limit). Useful for smoke tests.",
        )
        parser.add_argument(
            "--source-bucket",
            default="",
            help=f"Override {SOURCE_BUCKET_ENV}.",
        )
        parser.add_argument(
            "--source-region",
            default="",
            help=f"Override {SOURCE_REGION_ENV} (defaults to AWS_DEFAULT_REGION).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        if not dry_run:
            if not options["confirm"]:
                raise CommandError("Refusing to copy media without --confirm (or pass --dry-run).")
            if os.environ.get(ALLOW_ENV) != "1":
                raise CommandError(f"Refusing to copy media without {ALLOW_ENV}=1.")

        source_bucket = (options["source_bucket"] or os.environ.get(SOURCE_BUCKET_ENV) or "").strip()
        if not source_bucket:
            raise CommandError(f"Set {SOURCE_BUCKET_ENV} or pass --source-bucket.")

        dest_bucket = (getattr(settings, "AWS_STORAGE_BUCKET_NAME", None) or "").strip()
        if not dest_bucket:
            raise CommandError("AWS_STORAGE_BUCKET_NAME is not set (destination bucket).")

        if source_bucket == dest_bucket:
            raise CommandError("Source and destination buckets must differ.")

        source_region = (
            options["source_region"]
            or os.environ.get(SOURCE_REGION_ENV)
            or getattr(settings, "AWS_DEFAULT_REGION", None)
            or getattr(settings, "AWS_S3_REGION_NAME", None)
            or ""
        ).strip() or None
        dest_region = (
            getattr(settings, "AWS_DEFAULT_REGION", None) or getattr(settings, "AWS_S3_REGION_NAME", None) or ""
        ).strip() or None

        self.render_h1("Sync referenced media")
        self.info(f"Source bucket: {source_bucket}" + (f" ({source_region})" if source_region else ""))
        self.info(f"Destination bucket: {dest_bucket}" + (f" ({dest_region})" if dest_region else ""))
        self.info(f"Skip existing: {options['skip_existing']}")
        if dry_run:
            self.warning("Dry run — no objects will be written.")

        self.info("Collecting referenced media keys from the database…")
        exclude_models = getattr(settings, "MEDIA_SYNC_EXCLUDE_MODELS", None) or []
        exclude_fields = getattr(settings, "MEDIA_SYNC_EXCLUDE_FIELDS", None) or []
        if exclude_models:
            self.info(f"Exclude models: {', '.join(exclude_models)}")
        if exclude_fields:
            self.info(f"Exclude fields: {', '.join(exclude_fields)}")
        refs = collect_referenced_media_refs()
        self.info(f"Found {len(refs):,} distinct referenced key(s).")

        limit = options["limit"] or None
        stats = sync_media_refs_between_buckets(
            refs,
            source_bucket=source_bucket,
            dest_bucket=dest_bucket,
            source_region=source_region,
            dest_region=dest_region,
            skip_existing=bool(options["skip_existing"]),
            dry_run=dry_run,
            limit=limit,
            default_acl=getattr(settings, "AWS_DEFAULT_ACL", "public-read") or "public-read",
        )

        self.render_h2("Summary")
        self.info(f"Referenced (considered): {stats.referenced:,}")
        verb = "Would copy" if dry_run else "Copied"
        self.info(f"{verb}: {stats.copied:,}")
        self.info(f"Skipped (already on destination): {stats.skipped_existing:,}")
        if stats.missing_source:
            self.warning(f"Missing on source: {stats.missing_source:,}")
        if stats.errors:
            self.error(f"Errors: {stats.errors:,}")
        elif dry_run:
            self.success("Dry run complete.")
        else:
            self.success("Referenced media sync complete.")
