from __future__ import annotations

from django.apps import AppConfig


class MirroringTestAppConfig(AppConfig):
    name = "mirroring.test"
    label = "mirroring_test"
    verbose_name = "django-mirroring test app"
