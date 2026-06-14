from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebDavSettings:
    base_url: str
    username: Optional[str]
    password: Optional[str]
    remote_root: Optional[str]
    enabled: Optional[bool]
    auto_sync: Optional[bool]


def load_cc_switch_webdav_settings(path: Optional[Path] = None) -> WebDavSettings:
    settings_path = path or Path.home() / ".cc-switch" / "settings.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"cc-switch settings not found: {settings_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"cc-switch settings is not valid JSON: {settings_path}") from exc

    webdav = data.get("webdavSync") or data.get("webdav_sync") or {}
    base_url = webdav.get("baseUrl") or webdav.get("base_url")
    if not base_url:
        raise ConfigError("cc-switch WebDAV baseUrl is not configured")

    return WebDavSettings(
        base_url=base_url,
        username=webdav.get("username"),
        password=webdav.get("password"),
        remote_root=webdav.get("remoteRoot") or webdav.get("remote_root"),
        enabled=webdav.get("enabled"),
        auto_sync=webdav.get("autoSync") if "autoSync" in webdav else webdav.get("auto_sync"),
    )
