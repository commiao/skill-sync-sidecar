from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CONFIG_ENV = "SKILL_SYNC_CONFIG"
DEFAULT_KEYCHAIN_SERVICE = "skill-sync-sidecar-webdav"


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


# ---- sidecar 自有配置（T-0260 第 1 步：凭据不再依赖 cc-switch）----
#
# 配置文件只放地址和用户名，密码只能来自 Keychain / 环境变量 / 权限 600 的文件，
# 不接受明文 password 字段：配置文件会被备份、同步、贴进工单，密码不该跟着走。
#
# {
#   "webdav": {
#     "base_url": "https://nas.example/dav",
#     "username": "me",
#     "password_source": {"keychain": {"service": "skill-sync-sidecar-webdav", "account": "me"}}
#                     |  {"env": "SKILL_SYNC_WEBDAV_PASSWORD"}
#                     |  {"file": "~/.config/skill-sync-sidecar/webdav-password"}
#   }
# }


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "skill-sync-sidecar" / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "skill-sync-sidecar" / "config.json"


def keychain_read_password(service: str, account: str) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConfigError("macOS `security` command not found; Keychain password source is macOS-only") from exc
    if result.returncode != 0:
        raise ConfigError(f"Keychain item not found: service={service} account={account}")
    return result.stdout.rstrip("\n")


def keychain_write_password(service: str, account: str, password: str) -> None:
    # -U 更新已存在的条目；密码走 argv 会短暂出现在本机进程列表里，`security` 没有从 stdin 读的写法
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", password],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ConfigError(f"failed to write Keychain item service={service} account={account}: {result.stderr.strip()}")


def _resolve_password(source: object, keychain_reader: Callable[[str, str], str]) -> Optional[str]:
    if source is None:
        return None
    if not isinstance(source, dict) or len(source) != 1:
        raise ConfigError("webdav.password_source must have exactly one of: keychain, env, file")
    kind, spec = next(iter(source.items()))
    if kind == "keychain":
        if not isinstance(spec, dict) or not spec.get("account"):
            raise ConfigError("webdav.password_source.keychain needs an account")
        return keychain_reader(str(spec.get("service") or DEFAULT_KEYCHAIN_SERVICE), str(spec["account"]))
    if kind == "env":
        value = os.environ.get(str(spec))
        if value is None:
            raise ConfigError(f"environment variable {spec} for the WebDAV password is not set")
        return value
    if kind == "file":
        path = Path(str(spec)).expanduser()
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError as exc:
            raise ConfigError(f"WebDAV password file not found: {path}") from exc
        if mode & 0o077:
            raise ConfigError(f"WebDAV password file {path} is readable by others (mode {mode:o}); chmod 600 it")
        return path.read_text(encoding="utf-8").rstrip("\n")
    raise ConfigError(f"unknown webdav.password_source kind: {kind}")


def load_sidecar_webdav_settings(
    path: Optional[Path] = None,
    keychain_reader: Callable[[str, str], str] = keychain_read_password,
) -> WebDavSettings:
    config_path = path or default_config_path()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"sidecar config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"sidecar config is not valid JSON: {config_path}") from exc
    webdav = data.get("webdav") if isinstance(data, dict) else None
    if not isinstance(webdav, dict):
        raise ConfigError(f"sidecar config has no webdav section: {config_path}")
    if "password" in webdav:
        raise ConfigError(
            f"{config_path}: plaintext webdav.password is not allowed; use password_source (keychain, env or file)"
        )
    base_url = webdav.get("base_url")
    if not base_url:
        raise ConfigError(f"sidecar config webdav.base_url is not configured: {config_path}")
    return WebDavSettings(
        base_url=base_url,
        username=webdav.get("username"),
        password=_resolve_password(webdav.get("password_source"), keychain_reader),
        remote_root=webdav.get("remote_root"),
        enabled=webdav.get("enabled"),
        auto_sync=webdav.get("auto_sync"),
    )


def load_webdav_settings(
    config_path: Optional[Path] = None,
    cc_switch_path: Optional[Path] = None,
    keychain_reader: Callable[[str, str], str] = keychain_read_password,
    warn: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
) -> WebDavSettings:
    """sidecar 自有配置优先；还没有配置的机器回退到 cc-switch（过渡期兼容，会提示）。

    配置文件存在但写错了要直接报错，不能静默回退：那样改错的配置永远不会被发现，
    而机器还在用 cc-switch 的凭据跑，看上去一切正常。
    """
    path = config_path or default_config_path()
    if path.exists():
        return load_sidecar_webdav_settings(path, keychain_reader)
    settings = load_cc_switch_webdav_settings(cc_switch_path)
    warn(
        f"skill-sync-sidecar: {path} not found, falling back to ~/.cc-switch/settings.json "
        "(run `config-init --from-cc-switch` to migrate)"
    )
    return settings


def init_config_from_cc_switch(
    config_path: Optional[Path] = None,
    cc_switch_path: Optional[Path] = None,
    keychain_service: str = DEFAULT_KEYCHAIN_SERVICE,
    keychain_writer: Callable[[str, str, str], None] = keychain_write_password,
    dry_run: bool = True,
) -> dict:
    """把 cc-switch 的 WebDAV 地址/用户名迁进 sidecar 配置，密码存进 Keychain。"""
    path = config_path or default_config_path()
    if path.exists():
        raise ConfigError(f"sidecar config already exists, not overwriting: {path}")
    source = load_cc_switch_webdav_settings(cc_switch_path)
    if not source.username:
        raise ConfigError("cc-switch WebDAV username is empty; cannot key the Keychain item")
    webdav: dict = {"base_url": source.base_url, "username": source.username}
    if source.password:
        webdav["password_source"] = {"keychain": {"service": keychain_service, "account": source.username}}
    plan = {
        "config_path": str(path),
        "config": {"webdav": webdav},
        "keychain": {"service": keychain_service, "account": source.username} if source.password else None,
        "dry_run": dry_run,
    }
    if dry_run:
        return plan
    if source.password:
        keychain_writer(keychain_service, source.username, source.password)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"webdav": webdav}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return plan
