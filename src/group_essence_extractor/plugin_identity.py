from __future__ import annotations

from pathlib import Path


PLUGIN_NAME = "astrbot_plugin_groupessence"
LEGACY_PLUGIN_NAMES = ("astrbot_plugin_group_essence",)
PLUGIN_DATABASE_FILENAME = "group_essence.db"


def resolve_plugin_data_dir(astrbot_data_path: str | Path) -> Path:
    """Use the canonical data directory while preserving existing installations."""
    plugin_data_root = Path(astrbot_data_path) / "plugin_data"
    canonical = plugin_data_root / PLUGIN_NAME
    if canonical.exists():
        return canonical

    for legacy_name in LEGACY_PLUGIN_NAMES:
        legacy = plugin_data_root / legacy_name
        if legacy.exists():
            return legacy
    return canonical
