from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def temporary_media_dir(settings, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture(autouse=True)
def default_dumpling_config(settings, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Provide a minimal Dumpling config so refresh tests need not invent one."""
    configured = getattr(settings, "MIRROR_DUMPLING_CONFIG", "") or ""
    if configured and Path(configured).is_file():
        return
    config_dir = tmp_path_factory.mktemp("dumpling")
    config = config_dir / "dumplingconf.toml"
    config.write_text(
        'salt = "${DUMPLING_GLOBAL_SALT}"\n[rules."public.t"]\nemail = { strategy = "email" }\n',
        encoding="utf-8",
    )
    settings.MIRROR_DUMPLING_CONFIG = str(config)
