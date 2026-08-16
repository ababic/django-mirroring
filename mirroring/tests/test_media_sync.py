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

    with patch("mirroring.management.commands.sync_referenced_media.sync_media_refs_between_buckets") as sync_mock:
        sync_mock.return_value = MediaSyncStats(referenced=2, copied=2)
        out = StringIO()
        call_command("sync_referenced_media", "--dry-run", stdout=out)

    assert sync_mock.called
    assert sync_mock.call_args.kwargs["dry_run"] is True
    assert sync_mock.call_args.kwargs["source_bucket"] == "prod-bucket"
    assert sync_mock.call_args.kwargs["dest_bucket"] == "staging-bucket"
    assert "Dry run complete" in out.getvalue()
