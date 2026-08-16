from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin

from mirroring.models import MirrorDatabaseState

if TYPE_CHECKING:
    from django.contrib.admin.sites import AdminSite
    from django.http import HttpRequest


class MirrorDatabaseStateAdmin(admin.ModelAdmin):
    """Read-only view of the singleton mirror generation / restore watermark."""

    list_display = (
        "generated_at",
        "source_host",
        "source_database",
        "dumpling_version",
        "restored_at",
    )
    readonly_fields = (
        "generated_at",
        "source_host",
        "source_database",
        "retain_cutoff",
        "dumpling_version",
        "dumpling_config_sha256",
        "dumpling_report",
        "restored_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: MirrorDatabaseState | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: MirrorDatabaseState | None = None) -> bool:
        # Readonly change form: allow GET/HEAD only when the user has view or change.
        if request.method not in {"GET", "HEAD"}:
            return False
        return super().has_view_permission(request, obj) or super().has_change_permission(request, obj)


def register_admin(admin_site: AdminSite | None = None) -> None:
    """Register ``MirrorDatabaseState`` on the given admin site (idempotent)."""
    site = admin_site or admin.site
    if MirrorDatabaseState in site._registry:
        return
    site.register(MirrorDatabaseState, MirrorDatabaseStateAdmin)
