#!/usr/bin/env python3
"""Reversible local auto-update blocker & pending installer cleaner for Antigravity."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

LOCKED_MANIFEST_URL = "http://127.0.0.1:0/update-locked-by-update-guard/"
LOCKED_MANIFEST_CONTENT = (
    "provider: generic\n"
    f"url: {LOCKED_MANIFEST_URL}\n"
    "updaterCacheDirName: antigravity-updater\n"
)
PENDING_BLOCKER_TEXT = "Locked by agy-update-guard to prevent background auto-download.\n"


def _default_app_dir() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None
    for cand in (
        Path(local_app_data) / "Programs" / "antigravity",
        Path(local_app_data) / "antigravity",
    ):
        if cand.is_dir():
            return cand
    return Path(local_app_data) / "Programs" / "antigravity"


def _default_updater_dir() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    return (Path(local_app_data) / "antigravity-updater") if local_app_data else None


def _default_appdata_dir() -> Path | None:
    appdata = os.environ.get("APPDATA", "")
    return (Path(appdata) / "Antigravity") if appdata else None


def _clear_readonly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _remove_tree_force(target: Path) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if target.is_dir() and not target.is_symlink():
        for child in target.rglob("*"):
            _clear_readonly(child)
        _clear_readonly(target)
        shutil.rmtree(target, ignore_errors=True)
    else:
        _clear_readonly(target)
        try:
            target.unlink()
        except OSError:
            pass


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def get_update_lock_status(
    data_dir: Path | None = None,
    app_dir: Path | None = None,
    updater_dir: Path | None = None,
    appdata_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect current update-lock protections across flag, storage, manifest, and cache dir."""
    resolved_app = app_dir if app_dir is not None else _default_app_dir()
    resolved_upd = updater_dir if updater_dir is not None else _default_updater_dir()
    resolved_roaming = appdata_dir if appdata_dir is not None else _default_appdata_dir()

    lock_flag = False
    if data_dir is not None:
        lock_file = data_dir / "update_lock.json"
        if lock_file.is_file():
            try:
                obj = json.loads(lock_file.read_text(encoding="utf-8"))
                lock_flag = bool(isinstance(obj, dict) and obj.get("locked") is True)
            except (OSError, json.JSONDecodeError):
                pass

    auto_check_disabled = False
    if resolved_roaming is not None:
        storage_path = resolved_roaming / "app_storage.json"
        if storage_path.is_file():
            try:
                st_obj = json.loads(storage_path.read_text(encoding="utf-8"))
                auto_check_disabled = isinstance(st_obj, dict) and st_obj.get("autoCheckForUpdates") == "false"
            except (OSError, json.JSONDecodeError):
                pass

    manifest_blocked = False
    if resolved_app is not None:
        yml_path = resolved_app / "resources" / "app-update.yml"
        if yml_path.is_file():
            try:
                manifest_blocked = LOCKED_MANIFEST_URL in yml_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

    pending_slot_blocked = False
    remaining_exes: list[str] = []
    if resolved_upd is not None and resolved_upd.exists():
        pending_slot = resolved_upd / "pending"
        pending_slot_blocked = pending_slot.is_file()
        if resolved_upd.is_dir():
            remaining_exes = sorted(
                p.relative_to(resolved_upd).as_posix()
                for p in resolved_upd.rglob("*.exe")
                if p.is_file()
            )

    locked = bool(lock_flag or (manifest_blocked and pending_slot_blocked))
    return {
        "locked": locked,
        "lock_flag": lock_flag,
        "auto_check_disabled": auto_check_disabled,
        "manifest_blocked": manifest_blocked,
        "pending_slot_blocked": pending_slot_blocked,
        "remaining_exe_files": remaining_exes,
    }


def lock_updates(
    data_dir: Path,
    app_dir: Path | None = None,
    updater_dir: Path | None = None,
    appdata_dir: Path | None = None,
) -> dict[str, Any]:
    """Delete local pending installers, block auto-downloads, and mark UI update button locked."""
    resolved_app = app_dir if app_dir is not None else _default_app_dir()
    resolved_upd = updater_dir if updater_dir is not None else _default_updater_dir()
    resolved_roaming = appdata_dir if appdata_dir is not None else _default_appdata_dir()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    deleted_files: list[str] = []

    # 1. Purge all cached .exe installers & pending/ folder, then place a read-only file at `pending`
    if resolved_upd is not None:
        resolved_upd.mkdir(parents=True, exist_ok=True)
        for exe in list(resolved_upd.rglob("*.exe")):
            if exe.is_file():
                rel = exe.relative_to(resolved_upd).as_posix()
                _remove_tree_force(exe)
                if not exe.exists():
                    deleted_files.append(rel)
        pending_slot = resolved_upd / "pending"
        if pending_slot.exists():
            if pending_slot.is_dir():
                for child in list(pending_slot.rglob("*")):
                    if child.is_file():
                        rel = child.relative_to(resolved_upd).as_posix()
                        if rel not in deleted_files:
                            deleted_files.append(rel)
            _remove_tree_force(pending_slot)
        pending_slot.write_text(PENDING_BLOCKER_TEXT, encoding="utf-8")
        try:
            os.chmod(pending_slot, stat.S_IREAD)
        except OSError:
            pass

    # 2. Set autoCheckForUpdates = "false" in %APPDATA%\Antigravity\app_storage.json
    prev_auto_check: str | None = None
    if resolved_roaming is not None:
        storage_path = resolved_roaming / "app_storage.json"
        st_data: dict[str, Any] = {}
        if storage_path.is_file():
            try:
                loaded = json.loads(storage_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    st_data = loaded
            except (OSError, json.JSONDecodeError):
                st_data = {}
        if "autoCheckForUpdates" in st_data and st_data["autoCheckForUpdates"] != "false":
            prev_auto_check = str(st_data["autoCheckForUpdates"])
        st_data["autoCheckForUpdates"] = "false"
        try:
            _write_json_atomic(storage_path, st_data)
        except OSError:
            pass

    # 3. Backup resources/app-update.yml and point its URL to an inert loopback endpoint
    if resolved_app is not None:
        yml_path = resolved_app / "resources" / "app-update.yml"
        if yml_path.is_file():
            try:
                current_yml = yml_path.read_text(encoding="utf-8", errors="replace")
                if LOCKED_MANIFEST_URL not in current_yml:
                    data_dir.mkdir(parents=True, exist_ok=True)
                    (data_dir / "app-update.yml.bak").write_text(current_yml, encoding="utf-8")
                yml_path.write_text(LOCKED_MANIFEST_CONTENT, encoding="utf-8")
            except OSError:
                pass

    # 4. Persist update_lock.json shared with MCP UI and antigravity-chat-editor renderer
    existing_lock: dict[str, Any] = {}
    lock_file = data_dir / "update_lock.json"
    if lock_file.is_file():
        try:
            loaded_lock = json.loads(lock_file.read_text(encoding="utf-8"))
            if isinstance(loaded_lock, dict):
                existing_lock = loaded_lock
        except (OSError, json.JSONDecodeError):
            pass

    saved_prev = prev_auto_check or existing_lock.get("previous_auto_check") or "true"
    _write_json_atomic(
        lock_file,
        {
            "locked": True,
            "updated_at": now_iso,
            "previous_auto_check": saved_prev,
            "last_deleted_files": deleted_files,
        },
    )

    status = get_update_lock_status(
        data_dir=data_dir,
        app_dir=resolved_app,
        updater_dir=resolved_upd,
        appdata_dir=resolved_roaming,
    )
    status["deleted_files"] = deleted_files
    return status


def unlock_updates(
    data_dir: Path,
    app_dir: Path | None = None,
    updater_dir: Path | None = None,
    appdata_dir: Path | None = None,
) -> dict[str, Any]:
    """Restore Antigravity's auto-update manifest, storage setting, and pending directory."""
    resolved_app = app_dir if app_dir is not None else _default_app_dir()
    resolved_upd = updater_dir if updater_dir is not None else _default_updater_dir()
    resolved_roaming = appdata_dir if appdata_dir is not None else _default_appdata_dir()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    lock_file = data_dir / "update_lock.json"
    prev_auto_check = "true"
    if lock_file.is_file():
        try:
            obj = json.loads(lock_file.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and obj.get("previous_auto_check"):
                prev_auto_check = str(obj["previous_auto_check"])
        except (OSError, json.JSONDecodeError):
            pass

    # 1. Remove read-only pending blocker file and recreate empty pending/ directory
    if resolved_upd is not None:
        pending_slot = resolved_upd / "pending"
        if pending_slot.is_file() or pending_slot.is_symlink():
            _remove_tree_force(pending_slot)
        try:
            pending_slot.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # 2. Restore resources/app-update.yml from backup
    if resolved_app is not None:
        yml_path = resolved_app / "resources" / "app-update.yml"
        bak_path = data_dir / "app-update.yml.bak"
        if bak_path.is_file() and yml_path.parent.is_dir():
            try:
                yml_path.write_text(bak_path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass

    # 3. Restore autoCheckForUpdates in app_storage.json
    if resolved_roaming is not None:
        storage_path = resolved_roaming / "app_storage.json"
        if storage_path.is_file():
            try:
                loaded = json.loads(storage_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    loaded["autoCheckForUpdates"] = prev_auto_check
                    _write_json_atomic(storage_path, loaded)
            except (OSError, json.JSONDecodeError):
                pass

    _write_json_atomic(
        lock_file,
        {
            "locked": False,
            "updated_at": now_iso,
            "previous_auto_check": prev_auto_check,
        },
    )

    return get_update_lock_status(
        data_dir=data_dir,
        app_dir=resolved_app,
        updater_dir=resolved_upd,
        appdata_dir=resolved_roaming,
    )
