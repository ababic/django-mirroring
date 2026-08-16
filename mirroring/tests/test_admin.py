"""Admin permission tests for MirrorDatabaseStateAdmin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory

import pytest

from mirroring.admin import MirrorDatabaseStateAdmin
from mirroring.models import MirrorDatabaseState

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


@pytest.mark.django_db
@pytest.mark.unit
def test_mirror_database_state_admin_denies_staff_without_model_perms(
    django_user_model: type[AbstractBaseUser],
) -> None:
    staff = django_user_model.objects.create_user(username="mirror-staff", password="x", is_staff=True)
    admin = MirrorDatabaseStateAdmin(MirrorDatabaseState, AdminSite())
    request = RequestFactory().get("/admin/mirroring/mirrordatabasestate/1/change/")
    request.user = staff

    assert admin.has_add_permission(request) is False
    assert admin.has_delete_permission(request) is False
    assert admin.has_change_permission(request) is False
    assert admin.has_view_permission(request) is False


@pytest.mark.django_db
@pytest.mark.unit
def test_mirror_database_state_admin_allows_get_with_view_perm(
    django_user_model: type[AbstractBaseUser],
) -> None:
    staff = django_user_model.objects.create_user(username="mirror-viewer", password="x", is_staff=True)
    content_type = ContentType.objects.get_for_model(MirrorDatabaseState)
    staff.user_permissions.add(Permission.objects.get(content_type=content_type, codename="view_mirrordatabasestate"))
    admin = MirrorDatabaseStateAdmin(MirrorDatabaseState, AdminSite())

    get_request = RequestFactory().get("/admin/mirroring/mirrordatabasestate/1/change/")
    get_request.user = staff
    assert admin.has_change_permission(get_request) is True

    post_request = RequestFactory().post("/admin/mirroring/mirrordatabasestate/1/change/")
    post_request.user = staff
    assert admin.has_change_permission(post_request) is False
