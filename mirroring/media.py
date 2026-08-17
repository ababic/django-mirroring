"""Collect DB-referenced media keys and copy them between S3 buckets.

Intended as a post-``restore_from_mirror`` companion: the restored database already
points at storage keys, but staging uses a separate bucket from production. Sync
only keys that appear in FileField / ImageField columns (plus optional host
collectors), not the whole prod bucket.

PII-bearing fields can be skipped (``MEDIA_SYNC_EXCLUDE_*``) and optionally replaced
with harmless dummy objects at the same keys (``MEDIA_SYNC_DUMMY_*``) so non-nullable
or UI-linked paths still resolve without copying real customer content.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from importlib import import_module
from pathlib import PurePosixPath
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.files.storage import Storage
from django.db.models.fields.files import FieldFile, FileField

logger = logging.getLogger(__name__)

ExtraCollector = Callable[[], Iterable[str]]
DummyProvider = Callable[["MediaObjectRef"], "MediaDummySpec | None"]

_MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
_MINIMAL_CSV = b"redacted\n"
_MINIMAL_XML = b'<?xml version="1.0" encoding="UTF-8"?><redacted/>\n'
_MINIMAL_TEXT = b"redacted\n"
_MINIMAL_BIN = b"redacted\n"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_IDENTICON_SIZE = 128
_IDENTICON_CELLS = 5


@dataclass(frozen=True, slots=True)
class MediaObjectRef:
    """One object to sync or replace. ``key`` is the wire key inside the bucket."""

    key: str
    private: bool = False
    model_label: str = ""
    field_name: str = ""


@dataclass(frozen=True, slots=True)
class MediaDummySpec:
    """Bytes + headers for a placeholder object planted on the destination bucket."""

    body: bytes
    content_type: str
    private: bool | None = None


@dataclass(slots=True)
class MediaSyncStats:
    referenced: int = 0
    copied: int = 0
    dummied: int = 0
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


def _normalized_label_set(values: Iterable[str] | None) -> set[str]:
    return {str(value).strip().lower() for value in (values or []) if str(value).strip()}


def sync_exclude_model_labels(
    exclude_models: set[str] | None = None,
    dummy_models: set[str] | None = None,
) -> set[str]:
    """Models omitted from real CopyObject sync (explicit excludes ∪ dummy targets)."""
    excluded = exclude_models if exclude_models is not None else _normalized_label_set(
        getattr(settings, "MEDIA_SYNC_EXCLUDE_MODELS", None)
    )
    dummies = dummy_models if dummy_models is not None else _normalized_label_set(
        getattr(settings, "MEDIA_SYNC_DUMMY_MODELS", None)
    )
    return excluded | dummies


def sync_exclude_field_labels(
    exclude_fields: set[str] | None = None,
    dummy_fields: set[str] | None = None,
) -> set[str]:
    """Fields omitted from real CopyObject sync (explicit excludes ∪ dummy targets)."""
    excluded = exclude_fields if exclude_fields is not None else _normalized_label_set(
        getattr(settings, "MEDIA_SYNC_EXCLUDE_FIELDS", None)
    )
    dummies = dummy_fields if dummy_fields is not None else _normalized_label_set(
        getattr(settings, "MEDIA_SYNC_DUMMY_FIELDS", None)
    )
    return excluded | dummies


def _iter_concrete_filefield_models() -> Iterator[tuple[Any, str, list[Any]]]:
    for model in apps.get_models():
        label = model._meta.label_lower
        file_fields = [field for field in model._meta.concrete_fields if isinstance(field, FileField)]
        if file_fields:
            yield model, label, file_fields


def iter_filefield_media_refs(
    *,
    exclude_models: set[str] | None = None,
    exclude_fields: set[str] | None = None,
    dummy_models: set[str] | None = None,
    dummy_fields: set[str] | None = None,
    only_dummy: bool = False,
) -> Iterator[MediaObjectRef]:
    """Yield distinct media refs from concrete FileField / ImageField columns.

    When ``only_dummy`` is false (default), yields refs for sync — skipping models /
    fields in ``MEDIA_SYNC_EXCLUDE_*`` and ``MEDIA_SYNC_DUMMY_*``.

    When ``only_dummy`` is true, yields only refs matching ``MEDIA_SYNC_DUMMY_MODELS``
    / ``MEDIA_SYNC_DUMMY_FIELDS`` (for placeholder planting).
    """
    dummy_model_set = dummy_models if dummy_models is not None else _normalized_label_set(
        getattr(settings, "MEDIA_SYNC_DUMMY_MODELS", None)
    )
    dummy_field_set = dummy_fields if dummy_fields is not None else _normalized_label_set(
        getattr(settings, "MEDIA_SYNC_DUMMY_FIELDS", None)
    )
    if only_dummy:
        skip_models = set()
        skip_fields = set()
    else:
        skip_models = sync_exclude_model_labels(exclude_models, dummy_model_set)
        skip_fields = sync_exclude_field_labels(exclude_fields, dummy_field_set)

    seen: set[str] = set()
    for model, label, all_file_fields in _iter_concrete_filefield_models():
        if label in skip_models:
            continue
        if only_dummy and label not in dummy_model_set and not any(
            f"{label}.{field.name.lower()}" in dummy_field_set for field in all_file_fields
        ):
            continue

        selected_fields = []
        for field in all_file_fields:
            field_label = f"{label}.{field.name.lower()}"
            if only_dummy:
                if label in dummy_model_set or field_label in dummy_field_set:
                    selected_fields.append(field)
            elif field_label not in skip_fields:
                selected_fields.append(field)
        if not selected_fields:
            continue

        field_names = [field.name for field in selected_fields]
        queryset = model.objects.all().values_list(*field_names).iterator(chunk_size=2000)
        storage_by_name = {field.name: field.storage for field in selected_fields}
        private_by_name = {field.name: storage_is_private(field.storage) for field in selected_fields}
        for row in queryset:
            for field_name, value in zip(field_names, row, strict=True):
                name = getattr(value, "name", value) if isinstance(value, FieldFile) else value
                if not name:
                    continue
                key = storage_object_key(storage_by_name[field_name], str(name))
                if not key or key in seen:
                    continue
                seen.add(key)
                yield MediaObjectRef(
                    key=key,
                    private=private_by_name[field_name],
                    model_label=label,
                    field_name=field_name,
                )


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


def load_dummy_provider(dotted_path: str | None = None) -> DummyProvider | None:
    """Import optional ``MEDIA_SYNC_DUMMY_PROVIDER`` callable."""
    path = dotted_path if dotted_path is not None else getattr(settings, "MEDIA_SYNC_DUMMY_PROVIDER", None)
    if not path:
        return None
    module_path, _, attr = str(path).rpartition(".")
    if not module_path or not attr:
        raise ValueError(f"Invalid MEDIA_SYNC_DUMMY_PROVIDER: {path!r}")
    module = import_module(module_path)
    provider = getattr(module, attr)
    if not callable(provider):
        raise TypeError(f"{path} is not callable")
    return provider


def iter_extra_media_refs(collectors: Iterable[ExtraCollector] | None = None) -> Iterator[MediaObjectRef]:
    """Yield keys from host-project collectors.

    Collectors return **bucket object keys** (already including any storage
    ``location`` prefix). They are not re-normalized through ``default_storage``,
    so FileSystemStorage test backends cannot rewrite them into local paths.
    """
    seen: set[str] = set()
    for collector in collectors if collectors is not None else load_extra_collectors():
        for raw in collector():
            key = (raw or "").strip().lstrip("/")
            if not key or key in seen:
                continue
            seen.add(key)
            private = key.startswith(("labels/", "catalogue_imports/"))
            yield MediaObjectRef(key=key, private=private)


def collect_referenced_media_refs(
    *,
    include_filefields: bool = True,
    extra_collectors: Iterable[ExtraCollector] | None = None,
    exclude_models: set[str] | None = None,
    exclude_fields: set[str] | None = None,
    dummy_models: set[str] | None = None,
    dummy_fields: set[str] | None = None,
) -> list[MediaObjectRef]:
    """Return deduped media refs to CopyObject from the source bucket."""
    seen: set[str] = set()
    refs: list[MediaObjectRef] = []
    streams: list[Iterator[MediaObjectRef]] = []
    if include_filefields:
        streams.append(
            iter_filefield_media_refs(
                exclude_models=exclude_models,
                exclude_fields=exclude_fields,
                dummy_models=dummy_models,
                dummy_fields=dummy_fields,
                only_dummy=False,
            )
        )
    streams.append(iter_extra_media_refs(extra_collectors))
    for stream in streams:
        for ref in stream:
            if ref.key in seen:
                continue
            seen.add(ref.key)
            refs.append(ref)
    refs.sort(key=lambda item: item.key)
    return refs


def collect_dummy_media_refs(
    *,
    dummy_models: set[str] | None = None,
    dummy_fields: set[str] | None = None,
) -> list[MediaObjectRef]:
    """Return deduped refs that should receive placeholder objects (not prod copies)."""
    refs = list(
        iter_filefield_media_refs(
            dummy_models=dummy_models,
            dummy_fields=dummy_fields,
            only_dummy=True,
        )
    )
    refs.sort(key=lambda item: item.key)
    return refs


def is_image_media_key(key: str) -> bool:
    return PurePosixPath(key).suffix.lower() in _IMAGE_SUFFIXES


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def encode_rgb_png(width: int, height: int, rows: Iterable[bytes]) -> bytes:
    """Encode an 8-bit RGB PNG (no alpha) from raw row bytes (width * 3 each)."""
    raw = b"".join(b"\x00" + row for row in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def identicon_png(
    seed: bytes,
    *,
    size: int = _IDENTICON_SIZE,
    cells: int = _IDENTICON_CELLS,
) -> bytes:
    """Deterministic mirrored-block PNG from ``seed`` (no Pillow dependency)."""
    digest = hashlib.sha256(seed).digest()
    # Saturated foreground from hash; light neutral background.
    fg = (40 + digest[0] % 180, 40 + digest[1] % 180, 40 + digest[2] % 180)
    bg = (245, 245, 248)
    bits = int.from_bytes(digest[3:], "big")
    half = (cells + 1) // 2
    grid: list[list[bool]] = []
    bit_i = 0
    for _row in range(cells):
        left = [bool((bits >> (bit_i + col)) & 1) for col in range(half)]
        bit_i += half
        mirrored = left + (left[-2::-1] if cells % 2 else left[::-1])
        grid.append(mirrored[:cells])

    cell_px = max(1, size // cells)
    pixel = cell_px * cells
    rows: list[bytes] = []
    for y in range(pixel):
        gy = min(cells - 1, y // cell_px)
        row = bytearray(pixel * 3)
        for x in range(pixel):
            gx = min(cells - 1, x // cell_px)
            colour = fg if grid[gy][gx] else bg
            i = x * 3
            row[i : i + 3] = bytes(colour)
        rows.append(bytes(row))
    return encode_rgb_png(pixel, pixel, rows)


def content_seed_from_s3_head(head: dict[str, Any], *, fallback_key: str) -> bytes:
    """Prefer S3 ETag (content fingerprint) as seed; fall back to the object key."""
    etag = str(head.get("ETag") or "").strip().strip('"')
    if etag:
        return f"etag:{etag}".encode()
    return f"key:{fallback_key}".encode()


def default_dummy_for_key(
    key: str,
    *,
    private: bool = False,
    content_seed: bytes | None = None,
) -> MediaDummySpec:
    """Built-in placeholder body chosen from the object key's suffix.

    Image keys get a visual identicon. Pass ``content_seed`` (e.g. from the source
    object's ETag) so placeholders vary with original content rather than only the path.
    """
    suffix = PurePosixPath(key).suffix.lower()
    if suffix == ".pdf":
        return MediaDummySpec(body=_MINIMAL_PDF, content_type="application/pdf", private=private)
    if suffix == ".csv":
        return MediaDummySpec(body=_MINIMAL_CSV, content_type="text/csv", private=private)
    if suffix in {".xml", ".xsl", ".xslt"}:
        return MediaDummySpec(body=_MINIMAL_XML, content_type="application/xml", private=private)
    if suffix in {".txt", ".log", ".json"}:
        content_type = "application/json" if suffix == ".json" else "text/plain"
        return MediaDummySpec(body=_MINIMAL_TEXT, content_type=content_type, private=private)
    if is_image_media_key(key):
        seed = content_seed if content_seed is not None else f"key:{key}".encode()
        return MediaDummySpec(body=identicon_png(seed), content_type="image/png", private=private)
    return MediaDummySpec(body=_MINIMAL_BIN, content_type="application/octet-stream", private=private)


def resolve_dummy_spec(
    ref: MediaObjectRef,
    provider: DummyProvider | None = None,
    *,
    content_seed: bytes | None = None,
) -> MediaDummySpec:
    """Resolve placeholder bytes via optional host provider, else suffix defaults."""
    if provider is not None:
        custom = provider(ref)
        if custom is not None:
            return custom
    return default_dummy_for_key(ref.key, private=ref.private, content_seed=content_seed)


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
            dst.copy_object(**copy_kwargs)
            stats.copied += 1
        except Exception:
            stats.errors += 1
            logger.exception("Failed syncing media key %s", key)

    return stats


def plant_dummy_media_refs(
    refs: Iterable[MediaObjectRef],
    *,
    dest_bucket: str,
    dest_region: str | None = None,
    dest_client: Any | None = None,
    source_bucket: str | None = None,
    source_region: str | None = None,
    source_client: Any | None = None,
    skip_existing: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
    default_acl: str = "public-read",
    provider: DummyProvider | None = None,
    images_from_source_hash: bool | None = None,
) -> MediaSyncStats:
    """Put placeholder objects at ``refs`` keys on ``dest_bucket``.

    Pass ``provider`` (e.g. ``load_dummy_provider()``) to honour host overrides;
    ``None`` uses suffix defaults.

    For image keys, when ``images_from_source_hash`` is true (default) and
    ``source_bucket`` is set, the source object's ETag seeds a visual identicon so
    placeholders differ by original content without copying the real bytes. If the
    source object is missing, the key path is used as the seed instead.
    """
    import boto3
    from botocore.exceptions import ClientError

    if images_from_source_hash is None:
        images_from_source_hash = True
        if settings.configured:
            images_from_source_hash = bool(getattr(settings, "MEDIA_SYNC_DUMMY_IMAGES_FROM_SOURCE_HASH", True))

    dst = dest_client or boto3.client("s3", region_name=dest_region)
    src = None
    if images_from_source_hash and source_bucket:
        src = source_client or boto3.client("s3", region_name=source_region)
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

            content_seed: bytes | None = None
            if is_image_media_key(key):
                content_seed = f"key:{key}".encode()
                if src is not None and source_bucket:
                    try:
                        head = src.head_object(Bucket=source_bucket, Key=key)
                        content_seed = content_seed_from_s3_head(head, fallback_key=key)
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                            raise
                        # Keep key-based seed when the source object is gone.

            if dry_run:
                stats.dummied += 1
                continue

            spec = resolve_dummy_spec(ref, provider=provider, content_seed=content_seed)
            private = ref.private if spec.private is None else bool(spec.private)
            acl = "private" if private else default_acl
            put_kwargs: dict[str, Any] = {
                "Bucket": dest_bucket,
                "Key": key,
                "Body": spec.body,
                "ContentType": spec.content_type,
            }
            if acl:
                put_kwargs["ACL"] = acl
            dst.put_object(**put_kwargs)
            stats.dummied += 1
        except Exception:
            stats.errors += 1
            logger.exception("Failed planting dummy media key %s", key)

    return stats
