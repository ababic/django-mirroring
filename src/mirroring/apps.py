from django.apps import AppConfig


class MirroringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mirroring"
    verbose_name = "Database mirroring"

    def ready(self) -> None:
        from django.conf import settings
        from django.utils.module_loading import import_string

        from mirroring.admin import register_admin

        if not getattr(settings, "MIRRORING_AUTO_REGISTER_ADMIN", True):
            return
        site_path = getattr(settings, "MIRRORING_ADMIN_SITE", None)
        admin_site = import_string(site_path) if site_path else None
        register_admin(admin_site)
