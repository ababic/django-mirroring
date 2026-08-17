"""Tests for selective referenced-media sync helpers."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest
from botocore.exceptions import ClientError

from mirroring.media import (
    MediaObjectRef,
    MediaSyncStats,
    collect_referenced_media_refs,
    storage_object_key,
    sync_media_refs_between_buckets,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch
    from pytest_django.fixtures import SettingsWrapper


class _StorageWithLocation:
    location = "labels"

    def _clean_name(self, name: str) -> str:
        return name

    def _normalize_name(self, name: str) -> str:
        return f"{self.location}/{name}"


@pytest.mark.unit
def test_storage_object_key_uses_normalize_name() -> None:
    assert storage_object_key(_StorageWithLocation(), "dispatch/a.pdf") == "labels/dispatch/a.pdf"


def _sample_extra_keys():
    yield "brand-images/one.jpg"
    yield "product-spin/two.jpg"
    yield "brand-images/one.jpg"


@pytest.mark.unit
def test_collect_extra_media_refs_dedupes(settings: SettingsWrapper) -> None:
    settings.MEDIA_SYNC_EXTRA_COLLECTORS = ["mirroring.tests.test_media_sync._sample_extra_keys"]
    refs = collect_referenced_media_refs(include_filefields=False)
    assert [ref.key for ref in refs] == ["brand-images/one.jpg", "product-spin/two.jpg"]


@pytest.mark.unit
def test_normalized_label_set_and_anonymise_settings(settings: SettingsWrapper) -> None:
    from mirroring.media import _normalized_label_set, anonymise_media_labels, iter_filefield_media_refs

    assert _normalized_label_set([" Listing.Shipment ", "", "DATA_REPORTING.ExportedData"]) == {
        "listing.shipment",
        "data_reporting.exporteddata",
    }
    settings.MIRRORING_ANONYMISE_MEDIA_FIELDS = [
        "listing.shipment",
        "ebay.ebaycoupondownload.raw_file",
    ]
    models, fields = anonymise_media_labels()
    assert models == {"listing.shipment"}
    assert fields == {"ebay.ebaycoupondownload.raw_file"}
    # Smoke: settings parse and do not raise when scanning installed models.
    list(iter_filefield_media_refs())


@pytest.mark.unit
def test_default_dummy_for_key_by_suffix() -> None:
    from mirroring.media import default_dummy_for_key

    pdf = default_dummy_for_key("labels/dispatch/a.pdf", private=True)
    assert pdf.content_type == "application/pdf"
    assert pdf.body.startswith(b"%PDF")
    assert pdf.private is True
    csv = default_dummy_for_key("exports/a.csv")
    assert csv.content_type == "text/csv"


@pytest.mark.unit
def test_identicon_png_is_deterministic_and_varies_by_seed() -> None:
    from mirroring.media import identicon_png

    a = identicon_png(b"etag:abc")
    b = identicon_png(b"etag:abc")
    c = identicon_png(b"etag:xyz")
    assert a.startswith(b"\x89PNG")
    assert a == b
    assert a != c
    assert len(a) > 100


@pytest.mark.unit
def test_image_dummy_uses_content_seed() -> None:
    from mirroring.media import default_dummy_for_key

    by_key = default_dummy_for_key("photos/a.jpg")
    by_etag = default_dummy_for_key("photos/a.jpg", content_seed=b"etag:111")
    by_etag_same = default_dummy_for_key("photos/other.jpg", content_seed=b"etag:111")
    assert by_key.content_type == "image/png"
    assert by_key.body != by_etag.body
    assert by_etag.body == by_etag_same.body


@pytest.mark.unit
def test_placeholder_pdf_is_deterministic_and_varies_by_seed() -> None:
    from mirroring.media import placeholder_pdf

    a = placeholder_pdf(b"etag:abc")
    b = placeholder_pdf(b"etag:abc")
    c = placeholder_pdf(b"etag:xyz")
    assert a.startswith(b"%PDF")
    assert a == b
    assert a != c
    assert b"REDACTED PLACEHOLDER" in a


@pytest.mark.unit
def test_pdf_dummy_uses_content_seed() -> None:
    from mirroring.media import default_dummy_for_key

    by_key = default_dummy_for_key("labels/dispatch/a.pdf")
    by_etag = default_dummy_for_key("labels/dispatch/a.pdf", content_seed=b"etag:111")
    by_etag_same = default_dummy_for_key("labels/return/b.pdf", content_seed=b"etag:111")
    assert by_key.content_type == "application/pdf"
    assert by_key.body != by_etag.body
    assert by_etag.body == by_etag_same.body


@pytest.mark.unit
def test_plant_dummy_media_refs_put_object() -> None:
    from mirroring.media import plant_dummy_media_refs

    dst = MagicMock()

    def dst_head(*, Bucket: str, Key: str):
        if Key == "already.pdf":
            return {}
        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    dst.head_object.side_effect = dst_head
    refs = [
        MediaObjectRef("already.pdf", private=True, model_label="listing.shipment", field_name="dispatch_label_file"),
        MediaObjectRef("fresh.pdf", private=True, model_label="listing.shipment", field_name="dispatch_label_file"),
    ]
    stats = plant_dummy_media_refs(
        refs,
        dest_bucket="staging",
        dest_client=dst,
        skip_existing=True,
        dry_run=False,
        images_from_source_hash=False,
    )
    assert stats.referenced == 2
    assert stats.skipped_existing == 1
    assert stats.dummied == 1
    dst.put_object.assert_called_once()
    assert dst.put_object.call_args.kwargs["Key"] == "fresh.pdf"
    assert dst.put_object.call_args.kwargs["ACL"] == "private"
    assert dst.put_object.call_args.kwargs["Body"].startswith(b"%PDF")
    assert b"REDACTED PLACEHOLDER" in dst.put_object.call_args.kwargs["Body"]


@pytest.mark.unit
def test_plant_dummy_pdf_uses_source_etag_seed() -> None:
    from mirroring.media import placeholder_pdf, plant_dummy_media_refs

    src = MagicMock()
    dst = MagicMock()
    dst.head_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
    src.head_object.return_value = {"ETag": '"cafef00d"'}

    stats = plant_dummy_media_refs(
        [MediaObjectRef("labels/dispatch/x.pdf", private=True)],
        dest_bucket="staging",
        dest_client=dst,
        source_bucket="prod",
        source_client=src,
        skip_existing=True,
        dry_run=False,
        from_source_hash=True,
    )
    assert stats.dummied == 1
    body = dst.put_object.call_args.kwargs["Body"]
    assert body == placeholder_pdf(b"etag:cafef00d")
    assert dst.put_object.call_args.kwargs["ContentType"] == "application/pdf"


@pytest.mark.unit
def test_plant_dummy_image_uses_source_etag_seed() -> None:
    from mirroring.media import identicon_png, plant_dummy_media_refs

    src = MagicMock()
    dst = MagicMock()
    dst.head_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
    src.head_object.return_value = {"ETag": '"deadbeef"'}

    stats = plant_dummy_media_refs(
        [MediaObjectRef("brand-images/x.jpg", private=False)],
        dest_bucket="staging",
        dest_client=dst,
        source_bucket="prod",
        source_client=src,
        skip_existing=True,
        dry_run=False,
        images_from_source_hash=True,
    )
    assert stats.dummied == 1
    body = dst.put_object.call_args.kwargs["Body"]
    assert body == identicon_png(b"etag:deadbeef")
    assert dst.put_object.call_args.kwargs["ContentType"] == "image/png"


@pytest.mark.unit
def test_sync_media_refs_skip_existing_and_missing() -> None:
    src = MagicMock()
    dst = MagicMock()

    def dst_head(*, Bucket: str, Key: str):
        if Key == "already-there.jpg":
            return {}
        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    def src_head(*, Bucket: str, Key: str):
        if Key == "missing.jpg":
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {}

    dst.head_object.side_effect = dst_head
    src.head_object.side_effect = src_head

    refs = [
        MediaObjectRef("already-there.jpg"),
        MediaObjectRef("missing.jpg"),
        MediaObjectRef("fresh.jpg"),
    ]
    stats = sync_media_refs_between_buckets(
        refs,
        source_bucket="prod",
        dest_bucket="staging",
        source_client=src,
        dest_client=dst,
        skip_existing=True,
        dry_run=False,
    )
    assert stats.referenced == 3
    assert stats.skipped_existing == 1
    assert stats.missing_source == 1
    assert stats.copied == 1
    dst.copy_object.assert_called_once()
    assert dst.copy_object.call_args.kwargs["Key"] == "fresh.jpg"
    assert dst.copy_object.call_args.kwargs["CopySource"] == {"Bucket": "prod", "Key": "fresh.jpg"}


@pytest.mark.unit
def test_sync_referenced_media_requires_confirm(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_SYNC_ALLOW", "1")
    monkeypatch.setenv("MEDIA_SYNC_SOURCE_BUCKET", "prod-bucket")
    with pytest.raises(CommandError, match="--confirm"):
        call_command("sync_referenced_media", stdout=StringIO())


@pytest.mark.unit
def test_sync_referenced_media_requires_allow(monkeypatch: MonkeyPatch, settings: SettingsWrapper) -> None:
    settings.AWS_STORAGE_BUCKET_NAME = "staging-bucket"
    monkeypatch.delenv("MEDIA_SYNC_ALLOW", raising=False)
    monkeypatch.setenv("MEDIA_SYNC_SOURCE_BUCKET", "prod-bucket")
    with pytest.raises(CommandError, match="MEDIA_SYNC_ALLOW=1"):
        call_command("sync_referenced_media", "--confirm", stdout=StringIO())


@pytest.mark.django_db
@pytest.mark.unit
def test_sync_referenced_media_dry_run(monkeypatch: MonkeyPatch, settings: SettingsWrapper) -> None:
    settings.AWS_STORAGE_BUCKET_NAME = "staging-bucket"
    settings.AWS_DEFAULT_REGION = "eu-west-2"
    settings.MEDIA_SYNC_EXTRA_COLLECTORS = ["mirroring.tests.test_media_sync._sample_extra_keys"]
    monkeypatch.setenv("MEDIA_SYNC_SOURCE_BUCKET", "prod-bucket")
    monkeypatch.setenv("MEDIA_SYNC_SOURCE_REGION", "eu-central-1")

    with (
        patch("mirroring.management.commands.sync_referenced_media.sync_media_refs_between_buckets") as sync_mock,
        patch("mirroring.management.commands.sync_referenced_media.plant_dummy_media_refs") as dummy_mock,
        patch("mirroring.management.commands.sync_referenced_media.collect_anonymised_media_refs") as collect_dummy,
    ):
        sync_mock.return_value = MediaSyncStats(referenced=2, copied=2)
        dummy_mock.return_value = MediaSyncStats(referenced=0, dummied=0)
        collect_dummy.return_value = []
        out = StringIO()
        call_command("sync_referenced_media", "--dry-run", stdout=out)

    assert sync_mock.called
    assert sync_mock.call_args.kwargs["dry_run"] is True
    assert sync_mock.call_args.kwargs["source_bucket"] == "prod-bucket"
    assert sync_mock.call_args.kwargs["dest_bucket"] == "staging-bucket"
    assert dummy_mock.called
    assert "Dry run complete" in out.getvalue()
