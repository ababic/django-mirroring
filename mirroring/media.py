"""Collect DB-referenced media keys and copy them between S3 buckets.

Intended as a post-``restore_from_mirror`` companion: the restored database already
points at storage keys, but staging uses a separate bucket from production. Sync
only keys that appear in FileField / ImageField columns (plus optional host
collectors), not the whole prod bucket.

PII-bearing fields can be anonymised via ``MIRRORING_ANONYMISE_MEDIA_FIELDS``:
those keys are not copied from production; instead harmless placeholders are
planted at the same destination keys (image/PDF placeholders seeded from the
source ETag when available).
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
AnonymiseProvider = Callable[["MediaObjectRef"], "MediaAnonymiseSpec | None"]

_MINIMAL_CSV = b"redacted\n"
_MINIMAL_XML = b'<?xml version="1.0" encoding="UTF-8"?><redacted/>\n'
_MINIMAL_TEXT = b"redacted\n"
_MINIMAL_BIN = b"redacted\n"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_CONTENT_SEED_SUFFIXES = _IMAGE_SUFFIXES | {".pdf"}
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
class MediaAnonymiseSpec:
    """Bytes + headers for a placeholder object planted on the destination bucket."""

    body: bytes
    content_type: str
    private: bool | None = None


@dataclass(slots=True)
class MediaSyncStats:
    referenced: int = 0
    copied: int = 0
    anonymised: int = 0
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


def parse_anonymise_media_labels(values: Iterable[str] | None) -> tuple[set[str], set[str]]:
    """Split ``app.model`` / ``app.model.field`` labels into (models, fields)."""
    models: set[str] = set()
    fields: set[str] = set()
    for raw in values or []:
        label = str(raw).strip().lower()
        if not label:
            continue
        parts = label.split(".")
        if len(parts) == 2:
            models.add(label)
        elif len(parts) >= 3:
            fields.add(".".join(parts[:3]))
        else:
            raise ValueError(
                f"Invalid MIRRORING_ANONYMISE_MEDIA_FIELDS entry {raw!r}; "
                "expected app_label.model or app_label.model.field"
            )
    return models, fields


def anonymise_media_labels(
    values: Iterable[str] | None = None,
) -> tuple[set[str], set[str]]:
    """Return (model_labels, field_labels) from ``MIRRORING_ANONYMISE_MEDIA_FIELDS``."""
    if values is not None:
        return parse_anonymise_media_labels(values)
    return parse_anonymise_media_labels(getattr(settings, "MIRRORING_ANONYMISE_MEDIA_FIELDS", None))


def _iter_concrete_filefield_models() -> Iterator[tuple[Any, str, list[Any]]]:
    for model in apps.get_models():
        label = model._meta.label_lower
        file_fields = [field for field in model._meta.concrete_fields if isinstance(field, FileField)]
        if file_fields:
            yield model, label, file_fields


def iter_filefield_media_refs(
    *,
    anonymise_models: set[str] | None = None,
    anonymise_fields: set[str] | None = None,
    only_anonymise: bool = False,
) -> Iterator[MediaObjectRef]:
    """Yield distinct media refs from concrete FileField / ImageField columns.

    When ``only_anonymise`` is false (default), yields refs for sync — skipping
    models/fields listed in ``MIRRORING_ANONYMISE_MEDIA_FIELDS``.

    When ``only_anonymise`` is true, yields only anonymised targets (for
    placeholder planting).
    """
    if anonymise_models is None and anonymise_fields is None:
        anonymise_models, anonymise_fields = anonymise_media_labels()
    else:
        anonymise_models = anonymise_models or set()
        anonymise_fields = anonymise_fields or set()

    if only_anonymise:
        skip_models: set[str] = set()
        skip_fields: set[str] = set()
    else:
        skip_models = anonymise_models
        skip_fields = anonymise_fields

    seen: set[str] = set()
    for model, label, all_file_fields in _iter_concrete_filefield_models():
        if label in skip_models:
            continue
        if only_anonymise and label not in anonymise_models and not any(
            f"{label}.{field.name.lower()}" in anonymise_fields for field in all_file_fields
        ):
            continue

        selected_fields = []
        for field in all_file_fields:
            field_label = f"{label}.{field.name.lower()}"
            if only_anonymise:
                if label in anonymise_models or field_label in anonymise_fields:
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


def load_anonymise_provider(dotted_path: str | None = None) -> AnonymiseProvider | None:
    """Import optional ``MIRRORING_ANONYMISE_MEDIA_PROVIDER`` callable."""
    path = dotted_path if dotted_path is not None else getattr(settings, "MIRRORING_ANONYMISE_MEDIA_PROVIDER", None)
    if not path:
        return None
    module_path, _, attr = str(path).rpartition(".")
    if not module_path or not attr:
        raise ValueError(f"Invalid MIRRORING_ANONYMISE_MEDIA_PROVIDER: {path!r}")
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
    anonymise_models: set[str] | None = None,
    anonymise_fields: set[str] | None = None,
) -> list[MediaObjectRef]:
    """Return deduped media refs to CopyObject from the source bucket."""
    seen: set[str] = set()
    refs: list[MediaObjectRef] = []
    streams: list[Iterator[MediaObjectRef]] = []
    if include_filefields:
        streams.append(
            iter_filefield_media_refs(
                anonymise_models=anonymise_models,
                anonymise_fields=anonymise_fields,
                only_anonymise=False,
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


def collect_anonymised_media_refs(
    *,
    anonymise_models: set[str] | None = None,
    anonymise_fields: set[str] | None = None,
) -> list[MediaObjectRef]:
    """Return deduped refs that should receive anonymised placeholder objects."""
    refs = list(
        iter_filefield_media_refs(
            anonymise_models=anonymise_models,
            anonymise_fields=anonymise_fields,
            only_anonymise=True,
        )
    )
    refs.sort(key=lambda item: item.key)
    return refs


def is_image_media_key(key: str) -> bool:
    return PurePosixPath(key).suffix.lower() in _IMAGE_SUFFIXES


def uses_content_seed_placeholder(key: str) -> bool:
    """True when placeholders should vary by source content hash (images + PDFs)."""
    return PurePosixPath(key).suffix.lower() in _CONTENT_SEED_SUFFIXES


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


def placeholder_pdf(seed: bytes) -> bytes:
    """Deterministic one-page PDF with hash-coloured banner (no ReportLab dependency)."""
    digest = hashlib.sha256(seed).digest()
    r, g, b = (c / 255.0 for c in digest[:3])
    label = digest[:8].hex().upper()
    # Letter page; coloured header bar + label so admins can tell placeholders apart.
    stream = (
        "q\n"
        f"{r:.4f} {g:.4f} {b:.4f} rg\n"
        "0 692 612 100 re f\n"
        "1 1 1 rg\n"
        "BT /F1 22 Tf 72 742 Td (REDACTED PLACEHOLDER) Tj ET\n"
        f"BT /F1 12 Tf 72 718 Td ({label}) Tj ET\n"
        "Q\n"
    ).encode("latin-1")

    objects = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        (
            b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj\n"
        ),
        (
            b"4 0 obj<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>stream\n"
            + stream
            + b"\nendstream\nendobj\n"
        ),
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(
        f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return bytes(out)


def content_seed_from_s3_head(head: dict[str, Any], *, fallback_key: str) -> bytes:
    """Prefer S3 ETag (content fingerprint) as seed; fall back to the object key."""
    etag = str(head.get("ETag") or "").strip().strip('"')
    if etag:
        return f"etag:{etag}".encode()
    return f"key:{fallback_key}".encode()


def default_anonymise_for_key(
    key: str,
    *,
    private: bool = False,
    content_seed: bytes | None = None,
) -> MediaAnonymiseSpec:
    """Built-in placeholder body chosen from the object key's suffix.

    Image keys get a visual identicon; PDFs get a coloured one-page placeholder.
    Pass ``content_seed`` (e.g. from the source object's ETag) so placeholders
    vary with original content rather than only the path.
    """
    suffix = PurePosixPath(key).suffix.lower()
    seed = content_seed if content_seed is not None else f"key:{key}".encode()
    if suffix == ".pdf":
        return MediaAnonymiseSpec(body=placeholder_pdf(seed), content_type="application/pdf", private=private)
    if suffix == ".csv":
        return MediaAnonymiseSpec(body=_MINIMAL_CSV, content_type="text/csv", private=private)
    if suffix in {".xml", ".xsl", ".xslt"}:
        return MediaAnonymiseSpec(body=_MINIMAL_XML, content_type="application/xml", private=private)
    if suffix in {".txt", ".log", ".json"}:
        content_type = "application/json" if suffix == ".json" else "text/plain"
        return MediaAnonymiseSpec(body=_MINIMAL_TEXT, content_type=content_type, private=private)
    if is_image_media_key(key):
        return MediaAnonymiseSpec(body=identicon_png(seed), content_type="image/png", private=private)
    return MediaAnonymiseSpec(body=_MINIMAL_BIN, content_type="application/octet-stream", private=private)


def resolve_anonymise_spec(
    ref: MediaObjectRef,
    provider: AnonymiseProvider | None = None,
    *,
    content_seed: bytes | None = None,
) -> MediaAnonymiseSpec:
    """Resolve placeholder bytes via optional host provider, else suffix defaults."""
    if provider is not None:
        custom = provider(ref)
        if custom is not None:
            return custom
    return default_anonymise_for_key(ref.key, private=ref.private, content_seed=content_seed)


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


def plant_anonymised_media_refs(
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
    provider: AnonymiseProvider | None = None,
) -> MediaSyncStats:
    """Put placeholder objects at ``refs`` keys on ``dest_bucket``.

    Pass ``provider`` (e.g. ``load_anonymise_provider()``) to honour host overrides;
    ``None`` uses suffix defaults.

    For image and PDF keys, when ``source_bucket`` is set, the source object's ETag
    seeds a visual placeholder so files differ by original content without copying
    real bytes. If the source object is missing, the key path is used as the seed.
    """
    import boto3
    from botocore.exceptions import ClientError

    dst = dest_client or boto3.client("s3", region_name=dest_region)
    src = source_client or (boto3.client("s3", region_name=source_region) if source_bucket else None)
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
            if uses_content_seed_placeholder(key):
                content_seed = f"key:{key}".encode()
                if src is not None and source_bucket:
                    try:
                        head = src.head_object(Bucket=source_bucket, Key=key)
                        content_seed = content_seed_from_s3_head(head, fallback_key=key)
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                            raise

            if dry_run:
                stats.anonymised += 1
                continue

            spec = resolve_anonymise_spec(ref, provider=provider, content_seed=content_seed)
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
            stats.anonymised += 1
        except Exception:
            stats.errors += 1
            logger.exception("Failed planting anonymised media key %s", key)

    return stats
