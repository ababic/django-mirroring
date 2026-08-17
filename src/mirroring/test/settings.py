"""
Django settings for django-mirroring's own test suite.

Minimal Django project (no Wagtail) — commands and models under test do not
require Wagtail.
"""

from __future__ import annotations

import os

import dj_database_url


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.path.dirname(PROJECT_DIR)

SECRET_KEY = "not-a-secure-key"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "testserver"]

INSTALLED_APPS = [
    "mirroring",
    "mirroring.test",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mirroring.test.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

PASSWORD_HASHERS = ("django.contrib.auth.hashers.MD5PasswordHasher",)

DATABASES = {
    "default": dj_database_url.config(default="sqlite:///test_django_mirroring.db"),
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "test-static")
MEDIA_ROOT = os.path.join(BASE_DIR, "test-media")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MIRROR_DUMPLING_CONFIG = ""
MIRROR_EXCLUDED_SCHEMA = []
MIRROR_EXCLUDED_TABLES = []
MIRROR_EXCLUDED_TABLE_DATA = []
MIRROR_ROW_RETAIN = []
MIRROR_RETAIN_MONTHS = 0
MIRROR_RESTORE_ALLOWED_TARGET_HOST_SUFFIXES = []
MIRROR_RESTORE_BLOCKED_TARGET_HOST_SUFFIXES = []
MIRROR_RESTORE_STAFF_EMAIL_DOMAINS = []
MIRROR_RESTORE_USER_MATCH_FIELD = "username"
MIRRORING_AUTO_REGISTER_ADMIN = True
MIRRORING_ANONYMISE_MEDIA_FIELDS = []
MIRRORING_ANONYMISE_MEDIA_PROVIDER = ""
MEDIA_SYNC_EXTRA_COLLECTORS = []
AWS_STORAGE_BUCKET_NAME = ""
AWS_DEFAULT_REGION = "eu-west-2"
AWS_DEFAULT_ACL = "public-read"
