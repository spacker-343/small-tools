#!/usr/bin/env python3
"""Portable cross-machine client snapshot creator, exporter, and rollback manager."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import local_inspector
import update_blocker

CHAT_EDITOR_MARKER = "// AGY_CHAT_EDITOR_V1"
PORTABLE_CHAT_EDITOR_SNIPPET = (
    "\n// AGY_CHAT_EDITOR_V1\n"
    "try { require(require('path').join(process.env.LOCALAPPDATA || '', 'AntigravityChatEditor', 'main.cjs'))"
    ".start(require('./languageServer')); } "
    "catch (error) { console.error('AI reply editor could not load', error); }\n"
)

CHAT_EDITOR_RUNTIME_FILES = (
    "main.cjs",
    "renderer.js",
    "renderer.source.js",
    "renderer.licenses.txt",
    "history.cjs",
    "identity.cjs",
    "proxy.cjs",
    "summary.cjs",
    "markdown.js",
    "install.py",
    "capabilities.py",
    "wire.py",
    "verify.py",
    "build.cjs",
    "package.json",
    "README.md",
    "安装.cmd",
    "卸载.cmd",
    "启动Antigravity.cmd",
    "核验请求.cmd",
    "检查兼容性.cmd",
)

RESTORE_CMD_TEMPLATE = """@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_snapshot.ps1" %*
if errorlevel 1 (
    echo.
    echo [失败] 部署或回滚未完成，请查看上方提示信息。
    pause
    exit /b 1
)
echo.
pause
"""

RESTORE_PS1_TEMPLATE = r"""[CmdletBinding()]
param(
    [switch]$UnlockUpdates,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'
$BundleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path $BundleRoot 'snapshot_manifest.json'
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "未找到快照清单文件: $ManifestPath"
}
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Ver = $Manifest.version

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " Antigravity v$Ver 便携快照一键部署 / 回滚工具" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan

foreach ($ProcName in @('Antigravity', 'language_server')) {
    $Running = Get-Process -Name $ProcName -ErrorAction SilentlyContinue
    if ($Running) {
        throw "检测到正在运行的 $ProcName.exe 进程，请先完全退出 Antigravity 后再运行本脚本！"
    }
}

$TargetApp = Join-Path $env:LOCALAPPDATA 'Programs\antigravity'
$SourceApp = Join-Path $BundleRoot 'app'
if (-not (Test-Path -LiteralPath (Join-Path $SourceApp 'Antigravity.exe'))) {
    throw "快照包内缺少 app\Antigravity.exe，压缩包可能未完整解压。"
}

Write-Host "[1/4] 正在部署 Antigravity v$Ver 客户端文件到 $TargetApp ..."
$Stamp = Get-Date -Format 'yyyyMMddHHmmss'
$StagingNew = Join-Path $env:LOCALAPPDATA "Programs\antigravity.new-$Stamp"
$StagingPrev = Join-Path $env:LOCALAPPDATA "Programs\antigravity.prev-$Stamp"
if (Test-Path -LiteralPath $StagingNew) {
    Remove-Item -LiteralPath $StagingNew -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Path $StagingNew -Force | Out-Null
try {
    Copy-Item -Path (Join-Path $SourceApp '*') -Destination $StagingNew -Recurse -Force
    if (-not (Test-Path -LiteralPath (Join-Path $StagingNew 'Antigravity.exe'))) {
        throw "复制到临时目录后缺少 Antigravity.exe"
    }
    if (Test-Path -LiteralPath $TargetApp) {
        Move-Item -LiteralPath $TargetApp -Destination $StagingPrev -Force
    }
    Move-Item -LiteralPath $StagingNew -Destination $TargetApp -Force
    if (Test-Path -LiteralPath $StagingPrev) {
        Remove-Item -LiteralPath $StagingPrev -Recurse -Force -ErrorAction SilentlyContinue
    }
} catch {
    if (Test-Path -LiteralPath $StagingNew) {
        Remove-Item -LiteralPath $StagingNew -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ((Test-Path -LiteralPath $StagingPrev) -and -not (Test-Path -LiteralPath $TargetApp)) {
        Move-Item -LiteralPath $StagingPrev -Destination $TargetApp -Force
    }
    throw
}

$SourceEditor = Join-Path $BundleRoot 'AntigravityChatEditor'
if (Test-Path -LiteralPath $SourceEditor) {
    $TargetEditor = Join-Path $env:LOCALAPPDATA 'AntigravityChatEditor'
    Write-Host "[2/4] 正在同步 AI 回复即时编辑器运行时到 $TargetEditor （保留本机已有编辑记录）..."
    New-Item -ItemType Directory -Path $TargetEditor -Force | Out-Null
    Get-ChildItem -LiteralPath $SourceEditor -Force | ForEach-Object {
        $DestItem = Join-Path $TargetEditor $_.Name
        Copy-Item -LiteralPath $_.FullName -Destination $DestItem -Recurse -Force
    }
} else {
    Write-Host "[2/4] 快照未附带 AntigravityChatEditor，跳过。"
}

$SourceGuard = Join-Path $BundleRoot 'agy-update-guard'
if (Test-Path -LiteralPath $SourceGuard) {
    $TargetGuard = Join-Path $HOME '.gemini\config\plugins\agy-update-guard'
    Write-Host "[3/4] 正在同步 Update Guard 插件到 $TargetGuard ..."
    New-Item -ItemType Directory -Path $TargetGuard -Force | Out-Null
    Copy-Item -Path (Join-Path $SourceGuard '*') -Destination $TargetGuard -Recurse -Force
} else {
    Write-Host "[3/4] 快照未附带 Update Guard 插件，跳过。"
}

$GuardDataDir = Join-Path $HOME '.gemini\antigravity\plugin_data\agy-update-guard'
New-Item -ItemType Directory -Path $GuardDataDir -Force | Out-Null

if (-not $UnlockUpdates) {
    Write-Host "[4/4] 正在开启防自动更新保护（清理更新缓存、关闭自动检查并锁定顶部更新按钮）..."
    $UpdaterDir = Join-Path $env:LOCALAPPDATA 'antigravity-updater'
    New-Item -ItemType Directory -Path $UpdaterDir -Force | Out-Null
    Get-ChildItem -LiteralPath $UpdaterDir -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
        try { $_.Attributes = 'Normal' } catch {}
    }
    Get-ChildItem -LiteralPath $UpdaterDir -Filter '*.exe' -Recurse -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    $PendingSlot = Join-Path $UpdaterDir 'pending'
    if (Test-Path -LiteralPath $PendingSlot) {
        Remove-Item -LiteralPath $PendingSlot -Recurse -Force -ErrorAction SilentlyContinue
    }
    [System.IO.File]::WriteAllText($PendingSlot, "Locked by agy-update-guard`n", [System.Text.UTF8Encoding]::new($false))
    Set-ItemProperty -LiteralPath $PendingSlot -Name IsReadOnly -Value $true -ErrorAction SilentlyContinue

    $RoamingDir = Join-Path $env:APPDATA 'Antigravity'
    New-Item -ItemType Directory -Path $RoamingDir -Force | Out-Null
    $StoragePath = Join-Path $RoamingDir 'app_storage.json'
    $StorageObj = @{}
    if (Test-Path -LiteralPath $StoragePath) {
        try {
            $Parsed = Get-Content -LiteralPath $StoragePath -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($Prop in $Parsed.PSObject.Properties) { $StorageObj[$Prop.Name] = $Prop.Value }
        } catch {}
    }
    $StorageObj['autoCheckForUpdates'] = 'false'
    [System.IO.File]::WriteAllText($StoragePath, ($StorageObj | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))

    $OfficialYml = Join-Path $TargetApp 'resources\app-update.yml.official'
    if (Test-Path -LiteralPath $OfficialYml) {
        Copy-Item -LiteralPath $OfficialYml -Destination (Join-Path $GuardDataDir 'app-update.yml.bak') -Force
    }
    $LockJson = @{
        locked = $true
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
        previous_auto_check = 'true'
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText((Join-Path $GuardDataDir 'update_lock.json'), $LockJson, [System.Text.UTF8Encoding]::new($false))
} else {
    Write-Host "[4/4] 已按参数跳过防自动更新锁定。"
}

if (-not $NoShortcut) {
    try {
        $StartMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
        $ShortcutPath = Join-Path $StartMenuDir 'Antigravity.lnk'
        $WshShell = New-Object -ComObject WScript.Shell
        $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = Join-Path $TargetApp 'Antigravity.exe'
        $Shortcut.WorkingDirectory = $TargetApp
        $Shortcut.Save()
    } catch {}
}

Write-Host ""
Write-Host "✅ 部署完成！当前客户端版本：Antigravity v$Ver（Fork 补丁、AI 回复编辑器与禁更保护均已就绪）。" -ForegroundColor Green
Write-Host "现在可直接从开始菜单或运行 $TargetApp\Antigravity.exe 启动。" -ForegroundColor Green
"""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unpack_asar(data: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Unpack an Electron ASAR archive into (header_dict, inline_entries_map)."""
    if len(data) < 16:
        raise ValueError("ASAR 文件头不完整")
    tag, header_size, pickle_size, json_size = struct.unpack("<4I", data[:16])
    if tag != 4 or header_size != pickle_size + 4 or json_size > pickle_size - 4:
        raise ValueError("不支持的 ASAR 头部结构")
    header = json.loads(data[16 : 16 + json_size])
    entries: dict[str, bytes] = {}

    def walk(node: dict[str, Any], prefix: str = "") -> None:
        for name, entry in node.get("files", {}).items():
            key = prefix + name
            if "files" in entry:
                walk(entry, key + "/")
            elif "offset" in entry:
                start = 8 + header_size + int(entry["offset"])
                size = int(entry["size"])
                val = data[start : start + size]
                if len(val) != size:
                    raise ValueError(f"ASAR 条目长度不匹配: {key}")
                entries[key] = val

    walk(header)
    return header, entries


def repack_asar(
    header: dict[str, Any],
    entries: dict[str, bytes],
    changed_keys: set[str],
) -> bytes:
    """Repack an Electron ASAR archive updating offsets and SHA256 block integrity."""
    new_header = copy.deepcopy(header)
    output = bytearray()

    def walk(node: dict[str, Any], prefix: str = "") -> None:
        for name, entry in node.get("files", {}).items():
            key = prefix + name
            if "files" in entry:
                walk(entry, key + "/")
            elif "offset" in entry:
                content = entries[key]
                entry["offset"] = str(len(output))
                entry["size"] = len(content)
                if key in changed_keys:
                    block = int((entry.get("integrity") or {}).get("blockSize") or 4194304)
                    entry["integrity"] = {
                        "algorithm": "SHA256",
                        "hash": _sha256_bytes(content),
                        "blockSize": block,
                        "blocks": [
                            _sha256_bytes(content[i : i + block])
                            for i in range(0, len(content), block)
                        ],
                    }
                output.extend(content)

    walk(new_header)
    raw = json.dumps(new_header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = struct.pack("<I", len(raw)) + raw
    payload += b"\x00" * (-len(payload) % 4)
    pickle_data = struct.pack("<I", len(payload)) + payload
    return bytes(struct.pack("<II", 4, len(pickle_data)) + pickle_data + output)


def make_asar_portable(asar_bytes: bytes) -> tuple[bytes, str, bool]:
    """Rewrite user-specific chat-editor absolute path in app.asar to %LOCALAPPDATA% dynamic lookup."""
    header, entries = unpack_asar(asar_bytes)
    pkg = json.loads(entries["package.json"].decode("utf-8"))
    app_version = str(pkg.get("version") or "unknown")

    main_js = entries["dist/main.js"].decode("utf-8")
    if CHAT_EDITOR_MARKER not in main_js:
        return asar_bytes, app_version, False

    idx = main_js.index(CHAT_EDITOR_MARKER)
    portable_main = main_js[:idx].rstrip() + PORTABLE_CHAT_EDITOR_SNIPPET
    entries["dist/main.js"] = portable_main.encode("utf-8")
    repacked = repack_asar(header, entries, {"dist/main.js"})

    # Read-back verification
    _, verify_entries = unpack_asar(repacked)
    if verify_entries["dist/main.js"] != entries["dist/main.js"]:
        raise ValueError("便携化 app.asar 回读校验失败")
    return repacked, app_version, True


def _default_app_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    return Path(local_app_data) / "Programs" / "antigravity"


def _resolve_chat_editor_source(
    explicit_dir: Path | None = None,
    companion_dir: Path | None = None,
) -> Path | None:
    if explicit_dir and (explicit_dir / "main.cjs").is_file():
        return explicit_dir
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        runtime_dir = Path(local_app_data) / "AntigravityChatEditor"
        if (runtime_dir / "main.cjs").is_file() and (runtime_dir / "renderer.js").is_file():
            return runtime_dir
    if companion_dir and (companion_dir / "main.cjs").is_file():
        return companion_dir
    return None


def get_snapshots_dir(data_dir: Path) -> Path:
    return data_dir / "client_snapshots"


def list_snapshots(data_dir: Path) -> list[dict[str, Any]]:
    """List saved portable snapshot .zip archives in data_dir/client_snapshots."""
    snap_dir = get_snapshots_dir(data_dir)
    if not snap_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for zp in sorted(snap_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not zp.is_file():
            continue
        st = zp.stat()
        meta: dict[str, Any] = {}
        try:
            with zipfile.ZipFile(zp, "r") as zf:
                if "snapshot_manifest.json" in zf.namelist():
                    meta = json.loads(zf.read("snapshot_manifest.json").decode("utf-8"))
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
            meta = {}
        items.append(
            {
                "file_name": zp.name,
                "path": str(zp.resolve()),
                "version": str(meta.get("version") or ""),
                "created_at": str(meta.get("created_at") or dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()),
                "size_mb": round(st.st_size / (1024 * 1024), 1),
                "chat_editor_bundled": bool(meta.get("chat_editor_bundled", False)),
                "fork_patched": bool(meta.get("fork_patched", False)),
            }
        )
    return items


def _detect_fork_patched_in_ls(ls_bytes: bytes) -> bool:
    """Detect Fork renderer patch inside the deflate-compressed embedded ZIP of language_server.exe."""
    marker = b'typeof window.dialog?.showOpenDialog==="function"'
    if marker in ls_bytes:
        return True
    offset = len(ls_bytes)
    while (offset := ls_bytes.rfind(b"PK\x05\x06", 0, offset)) >= 0:
        if offset + 22 > len(ls_bytes):
            continue
        size, start, comment = struct.unpack_from("<IIH", ls_bytes, offset + 12)
        begin, end = offset - size - start, offset + 22 + comment
        if not (0 <= begin < offset and end <= len(ls_bytes)):
            continue
        if ls_bytes[begin : begin + 4] != b"PK\x03\x04":
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(ls_bytes[begin:end])) as zf:
                if "main.js" in zf.namelist() and marker in zf.read("main.js"):
                    return True
        except Exception:
            continue
    return False


def create_portable_snapshot(
    data_dir: Path,
    export_path: Path | str | None = None,
    app_dir: Path | None = None,
    chat_editor_dir: Path | None = None,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    """Create a self-contained cross-machine portable .zip snapshot of the current client."""
    resolved_app = (app_dir if app_dir is not None else _default_app_dir()).resolve()
    exe_path = resolved_app / "Antigravity.exe"
    asar_path = resolved_app / "resources" / "app.asar"
    ls_path = resolved_app / "resources" / "bin" / "language_server.exe"
    if not (exe_path.is_file() and asar_path.is_file() and ls_path.is_file()):
        raise FileNotFoundError(f"客户端安装目录不完整: {resolved_app}")

    portable_asar, version, asar_rewritten = make_asar_portable(asar_path.read_bytes())
    fork_patched = _detect_fork_patched_in_ls(ls_path.read_bytes())

    companions = local_inspector.resolve_companion_tools(
        data_dir=data_dir, plugin_root=plugin_root
    )
    ce_src = _resolve_chat_editor_source(
        chat_editor_dir, companion_dir=companions.get("chat_editor_dir")
    )
    created_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    snap_dir = get_snapshots_dir(data_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    library_zip = (snap_dir / f"Antigravity-v{version}-portable-{stamp}.zip").resolve()

    export_zip: Path | None = None
    if export_path is not None:
        exp = Path(export_path)
        if exp.suffix.lower() == ".zip":
            export_zip = exp.resolve()
        else:
            exp.mkdir(parents=True, exist_ok=True)
            export_zip = (exp / f"Antigravity-v{version}-portable.zip").resolve()

    tmp_zip = library_zip.with_suffix(".zip.tmp")

    # Preserve official app-update.yml if a backup exists
    official_yml_text = ""
    bak_yml = data_dir / "app-update.yml.bak"
    cur_yml = resolved_app / "resources" / "app-update.yml"
    if bak_yml.is_file():
        official_yml_text = bak_yml.read_text(encoding="utf-8", errors="replace")
    elif cur_yml.is_file():
        raw_yml = cur_yml.read_text(encoding="utf-8", errors="replace")
        if update_blocker.LOCKED_MANIFEST_URL not in raw_yml:
            official_yml_text = raw_yml

    app_file_count = 0
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 1. Pack client app/ tree (with portable app.asar and locked app-update.yml)
        for item in sorted(resolved_app.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(resolved_app).as_posix()
            arcname = f"app/{rel}"
            if rel == "resources/app.asar":
                zf.writestr(arcname, portable_asar)
            elif rel == "resources/app-update.yml":
                zf.writestr(arcname, update_blocker.LOCKED_MANIFEST_CONTENT.encode("utf-8"))
                if official_yml_text:
                    zf.writestr("app/resources/app-update.yml.official", official_yml_text.encode("utf-8"))
            else:
                zf.write(item, arcname)
            app_file_count += 1

        # 2. Pack clean AntigravityChatEditor runtime (excluding personal .local/ edits)
        ce_bundled = False
        if ce_src is not None:
            for fname in CHAT_EDITOR_RUNTIME_FILES:
                fpath = ce_src / fname
                if fpath.is_file():
                    zf.write(fpath, f"AntigravityChatEditor/{fname}")
                    ce_bundled = True

        # 3. Pack clean agy-update-guard plugin (excluding __pycache__)
        ug_bundled = False
        if plugin_root is not None and (plugin_root / "plugin.json").is_file():
            for pfile in sorted(plugin_root.rglob("*")):
                if not pfile.is_file():
                    continue
                if "__pycache__" in pfile.parts or pfile.suffix in (".pyc", ".pyo"):
                    continue
                rel_p = pfile.relative_to(plugin_root).as_posix()
                zf.write(pfile, f"agy-update-guard/{rel_p}")
                ug_bundled = True

        # 4. Pack 1-click cross-machine deploy scripts & manifest
        manifest = {
            "schema": "agy-portable-snapshot/v1",
            "version": version,
            "created_at": created_iso,
            "app_file_count": app_file_count,
            "asar_portable_rewritten": asar_rewritten,
            "fork_patched": fork_patched,
            "chat_editor_bundled": ce_bundled,
            "update_guard_bundled": ug_bundled,
            "default_update_locked": True,
        }
        zf.writestr(
            "snapshot_manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        zf.writestr("restore_snapshot.ps1", RESTORE_PS1_TEMPLATE.encode("utf-8-sig"))
        zf.writestr("部署到此电脑.cmd", RESTORE_CMD_TEMPLATE.replace("\n", "\r\n").encode("utf-8"))

    tmp_zip.replace(library_zip)
    final_path = library_zip
    if export_zip is not None and export_zip != library_zip:
        export_zip.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(library_zip, export_zip)
        final_path = export_zip

    size_mb = round(final_path.stat().st_size / (1024 * 1024), 1)
    return {
        "created": True,
        "version": version,
        "snapshot_path": str(final_path),
        "library_snapshot_path": str(library_zip),
        "size_mb": size_mb,
        "app_file_count": app_file_count,
        "asar_portable_rewritten": asar_rewritten,
        "fork_patched": fork_patched,
        "chat_editor_bundled": ce_bundled,
        "update_guard_bundled": ug_bundled,
    }


def _is_antigravity_running() -> bool:
    if os.name != "nt":
        return False
    for image in ("Antigravity.exe", "language_server.exe"):
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and image.lower() in proc.stdout.lower():
            return True
    return False


def restore_from_snapshot_zip(
    snapshot_zip: Path | str,
    data_dir: Path,
    app_dir: Path | None = None,
    chat_editor_target_dir: Path | None = None,
    lock_after_restore: bool = True,
    check_running: bool = True,
) -> dict[str, Any]:
    """Restore local client & companion runtime from a portable snapshot .zip (requires app closed)."""
    zp = Path(snapshot_zip).resolve()
    if not zp.is_file():
        raise FileNotFoundError(f"未找到快照文件: {zp}")

    if check_running and _is_antigravity_running():
        raise RuntimeError(
            "检测到 Antigravity.exe 或 language_server.exe 正在运行，请先完全退出客户端后再执行回滚！"
        )

    resolved_app = (app_dir if app_dir is not None else _default_app_dir()).resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    resolved_ce = (
        chat_editor_target_dir.resolve()
        if chat_editor_target_dir is not None
        else (Path(local_app_data) / "AntigravityChatEditor" if local_app_data else None)
    )

    with zipfile.ZipFile(zp, "r") as zf:
        if "snapshot_manifest.json" not in zf.namelist():
            raise ValueError("无效的快照压缩包（缺少 snapshot_manifest.json）")
        manifest = json.loads(zf.read("snapshot_manifest.json").decode("utf-8"))
        app_entries = [n for n in zf.namelist() if n.startswith("app/") and not n.endswith("/")]
        if not any(n == "app/Antigravity.exe" for n in app_entries):
            raise ValueError("快照压缩包缺少 app/Antigravity.exe")

        staging_new = resolved_app.parent / f"{resolved_app.name}.restore-new"
        staging_prev = resolved_app.parent / f"{resolved_app.name}.restore-prev"
        if staging_new.exists():
            shutil.rmtree(staging_new, ignore_errors=True)
        if staging_prev.exists():
            shutil.rmtree(staging_prev, ignore_errors=True)

        staging_new.mkdir(parents=True, exist_ok=True)
        for name in app_entries:
            rel = name[len("app/") :]
            dest = (staging_new / rel).resolve()
            if not dest.is_relative_to(staging_new.resolve()):
                raise ValueError(f"非法归档路径: {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src_s, dest.open("wb") as dst_s:
                shutil.copyfileobj(src_s, dst_s)

        if resolved_app.exists():
            resolved_app.rename(staging_prev)
        try:
            staging_new.rename(resolved_app)
            if staging_prev.exists():
                shutil.rmtree(staging_prev, ignore_errors=True)
        except Exception:
            if staging_prev.exists() and not resolved_app.exists():
                staging_prev.rename(resolved_app)
            raise

        if resolved_ce is not None:
            ce_entries = [
                n for n in zf.namelist() if n.startswith("AntigravityChatEditor/") and not n.endswith("/")
            ]
            if ce_entries:
                resolved_ce.mkdir(parents=True, exist_ok=True)
                for name in ce_entries:
                    rel = name[len("AntigravityChatEditor/") :]
                    dest = (resolved_ce / rel).resolve()
                    if not dest.is_relative_to(resolved_ce.resolve()):
                        raise ValueError(f"非法归档路径: {name}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src_s, dest.open("wb") as dst_s:
                        shutil.copyfileobj(src_s, dst_s)

    official_yml = resolved_app / "resources" / "app-update.yml.official"
    if official_yml.is_file():
        data_dir.mkdir(parents=True, exist_ok=True)
        bak_yml = data_dir / "app-update.yml.bak"
        if not bak_yml.is_file():
            shutil.copy2(official_yml, bak_yml)
        if not lock_after_restore:
            shutil.copy2(official_yml, resolved_app / "resources" / "app-update.yml")

    if lock_after_restore:
        update_blocker.lock_updates(data_dir=data_dir, app_dir=resolved_app)

    return {
        "restored": True,
        "version": str(manifest.get("version") or "unknown"),
        "snapshot_path": str(zp),
        "target_app_dir": str(resolved_app),
        "locked_updates": lock_after_restore,
    }
