#!/usr/bin/env python3
"""Fast non-blocking SessionStart hook for Update Guard in-app notifications."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SENTINEL_SCRIPT = PLUGIN_ROOT / "sidecars" / "update-sentinel" / "sentinel.py"
CHECK_INTERVAL_SECONDS = 4 * 3600
LOCK_TTL_SECONDS = 5 * 60


def resolve_data_dir(cli_data_dir: str | None = None) -> Path:
    """Resolve unified data directory shared with sentinel and MCP server."""
    if cli_data_dir:
        return Path(cli_data_dir)
    env_dir = os.environ.get("AGY_UPDATE_GUARD_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".gemini" / "antigravity" / "plugin_data" / "agy-update-guard"


def _read_json_safe(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _parse_iso_timestamp(value: str | None) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _is_check_due(data_dir: Path, now_ts: float | None = None) -> bool:
    now = now_ts if now_ts is not None else time.time()
    state = _read_json_safe(data_dir / "state.json")
    if state is None:
        return True
    last_ts = _parse_iso_timestamp(state.get("last_checked_at") or state.get("updated_at"))
    if last_ts is None:
        return True
    return (now - last_ts) >= CHECK_INTERVAL_SECONDS


def _is_lock_active(lock_path: Path, now_ts: float | None = None) -> bool:
    if not lock_path.exists():
        return False
    now = now_ts if now_ts is not None else time.time()
    try:
        mtime = lock_path.stat().st_mtime
        return (now - mtime) < LOCK_TTL_SECONDS
    except OSError:
        return False


def maybe_spawn_background_check(
    data_dir: Path,
    now_ts: float | None = None,
    popen_fn: Any = None,
) -> bool:
    """Spawn detached `sentinel.py --check` if >4h since last check and no active lock."""
    launcher = popen_fn or subprocess.Popen
    if not SENTINEL_SCRIPT.is_file():
        return False
    if not _is_check_due(data_dir, now_ts=now_ts):
        return False

    lock_path = data_dir / "check.lock"
    if _is_lock_active(lock_path, now_ts=now_ts):
        return False

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        return False

    cmd = [
        sys.executable or "python",
        str(SENTINEL_SCRIPT),
        "--check",
        "--data-dir",
        str(data_dir),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = 0
        for attr in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, attr, 0))
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    try:
        launcher(cmd, **kwargs)
        return True
    except OSError:
        return False


def build_notice_markdown(notice: dict[str, Any]) -> str:
    """Format an unread pending_notice.json payload as a concise GitHub Alert Markdown."""
    verdict = str(notice.get("verdict") or "CAUTION").upper()
    alert_type = "WARNING" if verdict in ("HOLD", "CAUTION") else "NOTE"
    version_label = str(notice.get("version_label") or "Antigravity 更新")
    headline = str(notice.get("headline") or "检测到新的版本或配置变动")
    dim_labels = notice.get("dimension_labels") or notice.get("dimensions") or []
    dims_text = "、".join(str(x) for x in dim_labels) if dim_labels else "常规更新"

    pending_updater = notice.get("pending_updater") or {}
    pending_line = ""
    if pending_updater.get("has_pending"):
        files = ", ".join(str(f) for f in (pending_updater.get("files") or []))
        pending_line = (
            f"\n> - **待安装更新包**：检测到本地 `antigravity-updater` 缓存 (`{files}`)，建议先完成兼容性审查再决定是否重启更新。"
        )

    return (
        f"> [!{alert_type}]\n"
        f"> **Update Guard 检测到待审查更新：{version_label}**（预检：`{verdict}` · 维度：{dims_text}）\n"
        f"> - **摘要**：{headline}"
        f"{pending_line}\n"
        f"> - [🛡️ 打开 Update Guard 插件页（点击预设按钮直接新建审查对话）](customization://agy-update-guard)\n"
        f"> - 也可在右上角 **Overview → Plugins → `Update Guard`** 侧边栏卡片中点击 **「🚀 新建对话审查本次更新」** 按钮一键送入新对话审查，或点击「已知悉」关闭本条提醒。"
    )


def build_hook_output(
    data_dir: Path,
    spawn_check: bool = True,
) -> dict[str, Any]:
    """Execute SessionStart logic and return the JSON payload for Antigravity."""
    if spawn_check:
        maybe_spawn_background_check(data_dir)

    notice = _read_json_safe(data_dir / "pending_notice.json")
    if not notice or notice.get("acknowledged") is True:
        return {}

    user_markdown = build_notice_markdown(notice)
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": "",
                    "metadata": {
                        "user_markdown": user_markdown,
                    },
                }
            }
        ]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update Guard SessionStart hook.")
    parser.add_argument("--data-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--no-spawn", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # Drain stdin tolerating empty or malformed JSON.
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                json.loads(raw)
    except Exception:
        pass

    data_dir = resolve_data_dir(args.data_dir)
    output = build_hook_output(data_dir, spawn_check=not args.no_spawn)
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
