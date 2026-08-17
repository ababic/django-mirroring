"""ORM models for database mirroring metadata."""

from __future__ import annotations

from typing import Self

from django.db import models


class MirrorDatabaseState(models.Model):
    """Singleton row describing the latest mirror generation and staging restore.

    Written on ``{db}_tmp`` during ``refresh_database_mirror`` (generation fields;
    ``restored_at`` cleared) so rename cutover publishes it with the mirror.
    ``restore_from_mirror`` stamps ``restored_at`` only after a successful cutover.
    """

    SINGLETON_PK = 1

    generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When refresh_database_mirror last finished loading this database.",
    )
    source_host = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Hostname of the dump source (no credentials).",
    )
    source_database = models.CharField(
        max_length=63,
        blank=True,
        default="",
        help_text="Database name of the dump source.",
    )
    retain_cutoff = models.DateField(
        null=True,
        blank=True,
        help_text="Row-retain cutoff date applied during generation, if any.",
    )
    dumpling_version = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Dumpling CLI version from the generation report, when available.",
    )
    dumpling_config_sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="SHA-256 of the effective Dumpling config from the generation report.",
    )
    dumpling_report = models.JSONField(
        null=True,
        blank=True,
        help_text="Dumpling --report JSON provenance sidecar from the last generation.",
    )
    restored_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When restore_from_mirror last completed cutover onto this database.",
    )

    class Meta:
        verbose_name = "Mirror database state"
        verbose_name_plural = "Mirror database state"

    def __str__(self) -> str:
        generated = self.generated_at.isoformat() if self.generated_at else "never"
        restored = self.restored_at.isoformat() if self.restored_at else "never"
        return f"Mirror state (generated {generated}, restored {restored})"

    def save(self, *args: object, **kwargs: object) -> None:
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls, *, using: str | None = None) -> Self:
        """Return the singleton row, creating an empty one if needed."""
        manager = cls.objects.using(using) if using is not None else cls.objects
        obj, _created = manager.get_or_create(pk=cls.SINGLETON_PK)
        return obj
