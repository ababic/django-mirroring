"""Collect DB-referenced media keys and copy them between S3 buckets.

Intended as a post-``restore_from_mirror`` companion: the restored database already
points at storage keys, but staging uses a separate bucket from production. Sync
only keys that appear in FileField / ImageField columns (plus optional host
collectors), not the whole prod bucket.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.files.storage import Storage
from django.db.models.fields.files import FieldFile, FileField

logger = logging.getLogger(__name__)

ExtraCollector = Callable[[], Iterable[str]]


@dataclass(frozen=True, slots=True)
class MediaObjectRef:
    """One object to sync. ``key`` is the wire key inside the bucket (incl. storage location)."""

    key: str
    private: bool = False


@dataclass(slots=True)
class MediaSyncStats:
    referenced: int = 0
    copied: int = 0
    skipped_existing: int = 0
    missing_source: int = 0
    errors: int = 0


def storage_object_key(storage: Storage, name: str) -> str:
    """Return the bucket object key for a FileField ``name`` on ``storage``."""
    name = (name or "").strip()
    if not name:
        return ""
    clean = storage._clean_name(name) if hasattr(storage, "_clean_name") else name
    if hasattr(storage, "_normalize_name"):
        return storage._normalize_name(clean)
    location = (getattr(storage, "location", None) or "").strip("/")
    if location:
        return f"{location}/{clean.lstrip('/')}"
    return clean.lstrip("/")


def storage_is_private(storage: Storage) -> bool:
    return getattr(storage, "default_acl", None) == "private"


def iter_filefield_media_refs(*, exclude_models: set[str] | None = None) -> Iterator[MediaObjectRef]:
    """Yield distinct media refs from every concrete FileField / ImageField."""
    excluded = exclude_models or set()
    seen: set[str] = set()
    for model in apps.get_models():
        label = model._meta.label_lower
        if label in excluded:
            continue
        file_fields = [field for field in model._meta.concrete_fields if isinstance(field, FileField)]
        if not file_fields:
            continue
        field_names = [field.name for field in file_fields]
        queryset = model.objects.all().values_list(*field_names).iterator(chunk_size=2000)
        storage_by_name = {field.name: field.storage for field in file_fields}
        private_by_name = {field.name: storage_is_private(field.storage) for field in file_fields}
        for row in queryset:
            for field_name, value in zip(field_names, row, strict=True):
                name = getattr(value, "name", value) if isinstance(value, FieldFile) else value
                if not name:
                    continue
                key = storage_object_key(storage_by_name[field_name], str(name))
                if not key or key in seen:
                    continue
                seen.add(key)
                yield MediaObjectRef(key=key, private=private_by_name[field_name])


def load_extra_collectors(dotted_paths: Iterable[str] | None = None) -> list[ExtraCollector]:
    """Import callables listed in ``MEDIA_SYNC_EXTRA_COLLECTORS`` (or ``dotted_paths``)."""
    paths = list(dotted_paths) if dotted_paths is not None else list(getattr(settings, "MEDIA_SYNC_EXTRA_COLLECTORS", []) or [])
    collectors: list[ExtraCollector] = []
    for path in paths:
        module_path, _, attr = path.rpartition(".")
        if not module_path or not attr:
            raise ValueError(f"Invalid MEDIA_SYNC_EXTRA_COLLECTORS entry: {path!r}")
        module = import_module(module_path)
        collector = getattr(module, attr)
        if not callable(collector):
            raise TypeError(f"{path} is not callable")
        collectors.append(collector)
    return collectors


def iter_extra_media_refs(collectors: Iterable[ExtraCollector] | None = None) -> Iterator[MediaObjectRef]:
    """Yield keys from host-project collectors (plain relative keys on default storage)."""
    from django.core.files.storage import default_storage

    seen: set[str] = set()
    private = storage_is_private(default_storage)
    for collector in collectors if collectors is not None else load_extra_collectors():
        for raw in collector():
            name = (raw or "").strip()
            if not name:
                continue
            key = storage_object_key(default_storage, name)
            if not key or key in seen:
                continue
            seen.add(key)
            yield MediaObjectRef(key=key, private=private)


def collect_referenced_media_refs(
    *,
    include_filefields: bool = True,
    extra_collectors: Iterable[ExtraCollector] | None = None,
    exclude_models: set[str] | None = None,
) -> list[MediaObjectRef]:
    """Return deduped media refs from FileFields and optional host collectors."""
    seen: set[str] = set()
    refs: list[MediaObjectRef] = []
    streams: list[Iterator[MediaObjectRef]] = []
    if include_filefields:
        streams.append(iter_filefield_media_refs(exclude_models=exclude_models))
    streams.append(iter_extra_media_refs(extra_collectors))
    for stream in streams:
        for ref in stream:
            if ref.key in seen:
                continue
            seen.add(ref.key)
            refs.append(ref)
    refs.sort(key=lambda item: item.key)
    return refs


def sync_media_refs_between_buckets(
    refs: Iterable[MediaObjectRef],
    *,
    source_bucket: str,
    dest_bucket: str,
    source_region: str | None = None,
    dest_region: str | None = None,
    source_client: Any | None = None,
    dest_client: Any | None = None,
    skip_existing: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
    default_acl: str = "public-read",
) -> MediaSyncStats:
    """Copy referenced keys from ``source_bucket`` to ``dest_bucket`` via S3 CopyObject.

    Missing source keys are counted and skipped. Errors on individual keys are
    logged and counted without aborting the run.
    """
    import boto3
    from botocore.exceptions import ClientError

    src = source_client or boto3.client("s3", region_name=source_region)
    dst = dest_client or boto3.client("s3", region_name=dest_region)
    stats = MediaSyncStats()

    for ref in refs:
        if limit is not None and stats.referenced >= limit:
            break
        stats.referenced += 1
        key = ref.key
        try:
            if skip_existing:
                try:
                    dst.head_object(Bucket=dest_bucket, Key=key)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                        raise
                else:
                    stats.skipped_existing += 1
                    continue

            try:
                src.head_object(Bucket=source_bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in {"404", "NoSuchKey", "NotFound"}:
                    stats.missing_source += 1
                    continue
                raise

            if dry_run:
                stats.copied += 1
                continue

            acl = "private" if ref.private else default_acl
            copy_kwargs: dict[str, Any] = {
                "Bucket": dest_bucket,
                "Key": key,
                "CopySource": {"Bucket": source_bucket, "Key": key},
            }
            if acl:
                copy_kwargs["ACL"] = acl
            # Cross-region copy needs the destination client; MetadataDirective COPY is default.
            dst.copy_object(**copy_kwargs)
            stats.copied += 1
        except Exception:
            stats.errors += 1
            logger.exception("Failed syncing media key %s", key)

    return stats
