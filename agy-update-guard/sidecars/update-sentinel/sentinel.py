#!/usr/bin/env python3
"""Zero-quota background sentinel for Antigravity updates, model changes, and builtin diffs."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import builtin_snapshot  # noqa: E402

CHANGELOG_URL = "https://antigravity.google/docs/changelog.md"
MODELS_URL = "https://antigravity.google/docs/models.md"
DEFAULT_MODEL = "gemini-4-hydrogen-high"
PRODUCT_LINES = ("Antigravity App", "Antigravity CLI", "Antigravity IDE")

RISK_RULES: dict[str, tuple[str, ...]] = {
    "permission_sandbox": (
        "sandbox",
        "permission",
        "auto mode",
        "auto-approv",
        "outside your project",
        "outside of folder",
        "non-workspace",
        "strict mode",
        "allowlist",
        "denylist",
        "session connection",
        "blocked",
        "access control",
    ),
    "model_safety_or_deprecation": (
        "safety",
        "guardrail",
        "refusal",
        "moderation",
        "content filter",
        "removed on",
        "leaving on",
        "deprecat",
    ),
    "browser_network": (
        "browser allowlist",
        "csp",
        "actuation",
        "origin block",
        "sandboxproxy",
    ),
    "telemetry_upload": (
        "telemetry",
        "diagnostic upload",
    ),
}

HIGH_RISK_KEYWORDS = {
    "strict mode",
    "denylist",
    "blocked",
    "access control",
    "origin block",
    "sandboxproxy",
    "outside your project",
    "outside of folder",
    "non-workspace",
    "sandbox",
    "permission",
}

DIM_LABELS_ZH: dict[str, str] = {
    "permission_sandbox": "权限与沙箱",
    "model_safety_or_deprecation": "模型可用性与下架",
    "browser_network": "浏览器与网络",
    "builtin_prompt_change": "内置配置变动",
    "telemetry_upload": "遥测上传",
}


def parse_version_tuple(ver_str: str) -> tuple[int, ...]:
    """Parse a semantic version string such as 'v2.35.1' into an integer tuple."""
    cleaned = ver_str.strip()
    if cleaned.lower().startswith("v"):
        cleaned = cleaned[1:]
    core = re.split(r"[-+]", cleaned, maxsplit=1)[0]
    parts = re.findall(r"\d+", core)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def fetch_url_text(url: str, timeout: int = 20) -> str:
    """Fetch UTF-8 text from an HTTPS URL with a clear User-Agent."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "agy-update-guard/1.1 (+https://antigravity.google)",
            "Accept": "text/markdown, text/plain, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def parse_changelog(markdown_text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse Antigravity App / CLI / IDE release blocks from changelog.md."""
    result: dict[str, list[dict[str, Any]]] = {name: [] for name in PRODUCT_LINES}
    lines = markdown_text.splitlines()

    sections: dict[str, list[str]] = {}
    current_product: str | None = None
    for line in lines:
        h2_match = re.match(r"^##\s+(.+?)\s*$", line)
        if h2_match:
            heading = h2_match.group(1).strip()
            if heading in PRODUCT_LINES:
                current_product = heading
                sections.setdefault(current_product, [])
            else:
                current_product = None
            continue
        if current_product is not None:
            sections[current_product].append(line)

    ver_header_re = re.compile(
        r"^###\s+(?:\[(v?\d+\.\d+(?:\.\d+)*)\]\([^\)]*\)|(v?\d+\.\d+(?:\.\d+)*))\s*$"
    )
    date_re = re.compile(
        r"^(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}$"
    )

    for product, sec_lines in sections.items():
        blocks: list[dict[str, Any]] = []
        current_block_lines: list[str] = []
        current_ver: str | None = None

        def flush_block() -> None:
            if current_ver is None:
                return
            body_text = "\n".join(current_block_lines).strip()
            norm_ver = current_ver[1:] if current_ver.lower().startswith("v") else current_ver
            date_str = ""
            summary_title = ""
            for b_line in current_block_lines[1:]:
                stripped = b_line.strip()
                if not stripped or stripped.lower() == "latest" or stripped == "---":
                    continue
                if not date_str and date_re.match(stripped):
                    date_str = stripped
                    continue
                if not summary_title and stripped.startswith("### "):
                    summary_title = stripped[4:].strip()
                    break
            blocks.append(
                {
                    "product": product,
                    "version": norm_ver,
                    "tag": f"v{norm_ver}",
                    "version_tuple": parse_version_tuple(norm_ver),
                    "date": date_str,
                    "summary": summary_title,
                    "body": body_text,
                }
            )

        for line in sec_lines:
            vm = ver_header_re.match(line.strip())
            if vm:
                flush_block()
                current_ver = vm.group(1) or vm.group(2)
                current_block_lines = [line]
            elif current_ver is not None:
                current_block_lines.append(line)
        flush_block()

        blocks.sort(key=lambda item: item["version_tuple"], reverse=True)
        result[product] = blocks

    return result


def extract_reasoning_models_section(markdown_text: str) -> dict[str, Any]:
    """Extract and normalize the '## Reasoning model' section from models.md."""
    match = re.search(
        r"(^##\s+Reasoning model\b.*?)(?=\n##\s+|\Z)",
        markdown_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    raw_section = match.group(1) if match else ""
    normalized_lines = [line.rstrip() for line in raw_section.replace("\r\n", "\n").splitlines()]
    normalized_text = "\n".join(normalized_lines).strip()
    section_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()

    deprecation_notices: list[str] = []
    for line in normalized_lines:
        lower = line.lower()
        if any(tok in lower for tok in ("removed on", "leaving on", "deprecat")):
            deprecation_notices.append(line.strip())

    table_rows: list[str] = [
        line.strip()
        for line in normalized_lines
        if line.strip().startswith("|") and not line.strip().startswith("| ---")
    ]

    return {
        "found": bool(match),
        "text": normalized_text,
        "hash": section_hash,
        "deprecation_notices": deprecation_notices,
        "table_rows": table_rows,
    }


def diff_models_section(previous_text: str, current_text: str) -> str:
    """Generate a compact unified diff between two Reasoning model sections."""
    if previous_text == current_text:
        return ""
    lines = list(
        difflib.unified_diff(
            previous_text.splitlines(),
            current_text.splitlines(),
            fromfile="baseline/models_reasoning.md",
            tofile="current/models_reasoning.md",
            lineterm="",
            n=2,
        )
    )
    if len(lines) > 120:
        lines = lines[:120] + [f"... [models diff truncated: {len(lines) - 120} more lines]"]
    return "\n".join(lines)


def _read_pe_version_ctypes(exe_path: Path) -> str:
    """Read Windows PE fixed file version via ctypes without spawning a shell."""
    if os.name != "nt" or not exe_path.is_file():
        return "unknown"
    try:
        version_dll = ctypes.windll.version  # type: ignore[attr-defined]
        path_str = str(exe_path)
        size = version_dll.GetFileVersionInfoSizeW(path_str, None)
        if not size:
            return "unknown"
        buf = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(path_str, None, size, buf):
            return "unknown"

        class _VSFixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.c_uint32),
                ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32),
                ("dwFileVersionLS", ctypes.c_uint32),
                ("dwProductVersionMS", ctypes.c_uint32),
                ("dwProductVersionLS", ctypes.c_uint32),
                ("dwFileFlagsMask", ctypes.c_uint32),
                ("dwFileFlags", ctypes.c_uint32),
                ("dwFileOS", ctypes.c_uint32),
                ("dwFileType", ctypes.c_uint32),
                ("dwFileSubtype", ctypes.c_uint32),
                ("dwFileDateMS", ctypes.c_uint32),
                ("dwFileDateLS", ctypes.c_uint32),
            ]

        ptr = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not version_dll.VerQueryValueW(
            buf, "\\", ctypes.byref(ptr), ctypes.byref(length)
        ) or not ptr.value:
            return "unknown"
        info = _VSFixedFileInfo.from_address(ptr.value)
        major = info.dwFileVersionMS >> 16
        minor = info.dwFileVersionMS & 0xFFFF
        patch = info.dwFileVersionLS >> 16
        build = info.dwFileVersionLS & 0xFFFF
        return f"{major}.{minor}.{patch}" if build == 0 else f"{major}.{minor}.{patch}.{build}"
    except Exception:
        return "unknown"


def get_local_antigravity_version() -> dict[str, str]:
    """Detect local Antigravity.exe path and version."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local_app_data:
        candidates.extend(
            [
                Path(local_app_data) / "Programs" / "antigravity" / "Antigravity.exe",
                Path(local_app_data) / "antigravity" / "Antigravity.exe",
            ]
        )
    for exe in candidates:
        if exe.is_file():
            return {"path": str(exe), "version": _read_pe_version_ctypes(exe)}
    return {"path": "", "version": "unknown"}


def check_pending_updater_files(
    updater_dir: Path | None = None,
    local_version: str | None = None,
) -> dict[str, Any]:
    """Check %LOCALAPPDATA%\\antigravity-updater recursively for pending .exe installers."""
    if updater_dir is None:
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if not local_app_data:
            return {
                "has_pending": False,
                "updater_dir": "",
                "files": [],
                "file_versions": {},
                "pending_version": "",
                "has_newer_pending": False,
            }
        updater_dir = Path(local_app_data) / "antigravity-updater"

    if not updater_dir.exists() or not updater_dir.is_dir():
        return {
            "has_pending": False,
            "updater_dir": str(updater_dir),
            "files": [],
            "file_versions": {},
            "pending_version": "",
            "has_newer_pending": False,
        }

    exe_paths = sorted(
        (p for p in updater_dir.rglob("*.exe") if p.is_file()),
        key=lambda p: p.relative_to(updater_dir).as_posix(),
    )
    exe_files: list[str] = []
    file_versions: dict[str, str] = {}
    pending_candidates: list[tuple[tuple[int, ...], str]] = []

    for exe_path in exe_paths:
        rel = exe_path.relative_to(updater_dir).as_posix()
        exe_files.append(rel)
        ver = _read_pe_version_ctypes(exe_path)
        if ver and ver != "unknown":
            file_versions[rel] = ver
            if rel.lower().startswith("pending/"):
                pending_candidates.append((parse_version_tuple(ver), ver))

    pending_version = max(pending_candidates, key=lambda x: x[0])[1] if pending_candidates else ""
    has_newer_pending = bool(
        pending_version
        and local_version
        and local_version != "unknown"
        and parse_version_tuple(pending_version) > parse_version_tuple(local_version)
    )
    return {
        "has_pending": bool(exe_files),
        "updater_dir": str(updater_dir),
        "files": exe_files,
        "file_versions": file_versions,
        "pending_version": pending_version,
        "has_newer_pending": has_newer_pending,
    }


def _is_pending_installer_unseen(
    pending_updater: dict[str, Any],
    state: dict[str, Any] | None,
) -> bool:
    """Return True if a newer local pending installer has not yet been acknowledged in state."""
    if not pending_updater.get("has_newer_pending"):
        return False
    pending_ver = str(pending_updater.get("pending_version") or "").strip()
    if not pending_ver:
        return False
    pending_tuple = parse_version_tuple(pending_ver)
    state_obj = state or {}
    last_seen_pending = str(state_obj.get("last_seen_pending_version") or "").strip()
    if last_seen_pending and parse_version_tuple(last_seen_pending) >= pending_tuple:
        return False
    last_seen_app = str(
        (state_obj.get("last_seen_versions") or {}).get("Antigravity App") or ""
    ).strip()
    if last_seen_app and parse_version_tuple(last_seen_app) >= pending_tuple:
        return False
    return True


def _format_pending_files_display(pending_updater: dict[str, Any]) -> str:
    """Format updater .exe file list with PE versions when available."""
    file_versions = pending_updater.get("file_versions") or {}
    formatted: list[str] = []
    for rel in pending_updater.get("files") or []:
        rel_str = str(rel)
        ver = str(file_versions.get(rel_str) or "").strip()
        formatted.append(f"{rel_str} (v{ver})" if ver else rel_str)
    return ", ".join(formatted)


def _build_local_env_lines(
    local_exe: dict[str, str],
    pending_updater: dict[str, Any],
) -> list[str]:
    """Build the '## 本机客户端环境' body lines for audit prompts."""
    lines = [
        f"- 本机 Antigravity.exe 版本: `{local_exe.get('version', 'unknown')}` (`{local_exe.get('path') or '未检测到路径'}`)",
    ]
    if pending_updater.get("has_pending"):
        files_str = _format_pending_files_display(pending_updater)
        pending_ver = str(pending_updater.get("pending_version") or "").strip()
        ver_note = f"（待安装版本：`v{pending_ver}`）" if pending_ver else ""
        lines.append(
            f"- ⚠️ **本地更新缓存提示**：检测到 `{pending_updater.get('updater_dir')}` 存在已下载待安装文件 (`{files_str}`){ver_note}。若评估建议暂缓升级，**请勿点击“重启更新”或直接重启客户端**。"
        )
    else:
        lines.append("- 本地更新缓存 (`antigravity-updater`): 未检测到待安装 `.exe` 文件。")
    return lines


def _refresh_prompt_local_env(
    prompt_text: str,
    local_exe: dict[str, str],
    pending_updater: dict[str, Any],
) -> str:
    """Refresh the '## 本机客户端环境' section inside a cached review prompt."""
    if not prompt_text or "## 本机客户端环境" not in prompt_text:
        return prompt_text
    replacement = "## 本机客户端环境\n" + "\n".join(
        _build_local_env_lines(local_exe, pending_updater)
    )
    return re.sub(
        r"## 本机客户端环境\r?\n.*?(?=\r?\n\r?\n## |\Z)",
        lambda _m: replacement,
        prompt_text,
        count=1,
        flags=re.DOTALL,
    )


def resolve_data_dir(cli_data_dir: str | Path | None = None) -> Path:
    """Resolve unified persistent data dir shared by sentinel, session hook, and MCP server."""
    if cli_data_dir:
        return Path(cli_data_dir)
    return Path.home() / ".gemini" / "antigravity" / "plugin_data" / "agy-update-guard"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a JSON dictionary to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state(state_path: Path) -> dict[str, Any] | None:
    """Load persisted state.json if present."""
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """Atomically write state.json."""
    _write_json_atomic(state_path, state)


def load_pending_notice(data_dir: Path) -> dict[str, Any] | None:
    """Load pending_notice.json if present and valid."""
    notice_path = data_dir / "pending_notice.json"
    if not notice_path.is_file():
        return None
    try:
        data = json.loads(notice_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def prescreen_risks(
    new_versions_by_product: dict[str, list[dict[str, Any]]],
    models_changed: bool,
    models_diff_text: str,
    models_notices: list[str],
    builtin_diff: dict[str, Any],
) -> dict[str, Any]:
    """Run deterministic case-insensitive compatibility pre-screening across 5 dimensions."""
    corpus_parts: list[str] = []
    for entries in new_versions_by_product.values():
        for entry in entries:
            corpus_parts.append(str(entry.get("body", "")))
    if models_changed or models_diff_text:
        corpus_parts.append(models_diff_text)
        corpus_parts.extend(models_notices)
    if builtin_diff.get("has_changes"):
        corpus_parts.append(str(builtin_diff.get("diff_summary", "")))

    combined_lower = "\n".join(corpus_parts).lower()
    matched_dimensions: list[str] = []
    matched_keywords: dict[str, list[str]] = {}

    for dim, keywords in RISK_RULES.items():
        hits = [kw for kw in keywords if kw in combined_lower]
        if hits:
            matched_dimensions.append(dim)
            matched_keywords[dim] = hits

    if builtin_diff.get("has_changes"):
        if "builtin_prompt_change" not in matched_dimensions:
            matched_dimensions.append("builtin_prompt_change")
        matched_keywords["builtin_prompt_change"] = [
            f"added={len(builtin_diff.get('added', []))}",
            f"removed={len(builtin_diff.get('removed', []))}",
            f"modified={len(builtin_diff.get('modified', []))}",
        ]

    has_high_risk_kw = any(
        kw in HIGH_RISK_KEYWORDS
        for kws in matched_keywords.values()
        for kw in kws
    )
    if (
        "builtin_prompt_change" in matched_dimensions
        or "browser_network" in matched_dimensions
        or ("permission_sandbox" in matched_dimensions and has_high_risk_kw)
        or len(matched_dimensions) >= 2
    ):
        verdict = "HOLD"
    elif matched_dimensions:
        verdict = "CAUTION"
    else:
        verdict = "SAFE"

    if not matched_dimensions:
        fallback_headline = "未命中权限策略或内置配置重大变更"
    else:
        zh_names = [DIM_LABELS_ZH.get(d, d) for d in matched_dimensions]
        if len(zh_names) > 2:
            fallback_headline = f"命中{'、'.join(zh_names[:2])}等{len(zh_names)}项变更预检"
        else:
            fallback_headline = f"命中{'、'.join(zh_names)}变更预检"
    fallback_headline = fallback_headline[:36]

    return {
        "verdict": verdict,
        "dimensions": matched_dimensions,
        "dimension_labels": [DIM_LABELS_ZH.get(d, d) for d in matched_dimensions],
        "matched_keywords": matched_keywords,
        "fallback_headline": fallback_headline,
    }


def _collect_new_versions(
    parsed_changelog: dict[str, list[dict[str, Any]]],
    last_seen_versions: dict[str, str],
    audit_latest: bool,
) -> dict[str, list[dict[str, Any]]]:
    """Determine which versions per product line are newer than last_seen_versions."""
    new_versions: dict[str, list[dict[str, Any]]] = {}
    for product in PRODUCT_LINES:
        entries = parsed_changelog.get(product, [])
        if not entries:
            continue
        if audit_latest:
            new_versions[product] = entries[:2]
            continue
        seen_ver = last_seen_versions.get(product, "")
        if not seen_ver:
            new_versions[product] = entries[:1]
            continue
        seen_tuple = parse_version_tuple(seen_ver)
        newer = [item for item in entries if item["version_tuple"] > seen_tuple]
        if newer:
            new_versions[product] = newer
    return new_versions


def _compact_new_versions(new_versions: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, str]]]:
    """Serialize compact version summaries for JSON storage and UI rendering."""
    return {
        product: [
            {
                "version": str(item.get("version", "")),
                "tag": str(item.get("tag", "")),
                "date": str(item.get("date", "")),
                "summary": str(item.get("summary", "")),
            }
            for item in entries
        ]
        for product, entries in new_versions.items()
    }


def _build_version_label(
    new_versions: dict[str, list[dict[str, Any]]],
    parsed_changelog: dict[str, list[dict[str, Any]]],
    models_changed: bool,
    builtin_changed: bool,
    pending_installer_version: str = "",
) -> str:
    """Create a concise version label for UI headers and reports."""
    parts: list[str] = []
    for product in PRODUCT_LINES:
        items = new_versions.get(product) or []
        if items:
            short = product.replace("Antigravity ", "")
            parts.append(f"{short} {items[0]['tag']}")
    if not (new_versions.get("Antigravity App") or []) and pending_installer_version:
        parts.insert(0, f"App v{pending_installer_version} (本地待安装)")
    if not parts:
        app_entries = parsed_changelog.get("Antigravity App") or []
        if app_entries:
            parts.append(f"App {app_entries[0]['tag']}")
    if models_changed:
        parts.append("Models")
    if builtin_changed:
        parts.append("Builtin")
    return " / ".join(parts) if parts else "Check"


def build_audit_prompt(
    run_id: str,
    local_exe: dict[str, str],
    pending_updater: dict[str, Any],
    new_versions: dict[str, list[dict[str, Any]]],
    models_info: dict[str, Any],
    models_changed: bool,
    models_diff_text: str,
    builtin_diff: dict[str, Any],
    prescreen: dict[str, Any],
) -> str:
    """Build a self-contained engineering compatibility review prompt requiring zero tool calls."""
    lines: list[str] = [
        f"Audit-Run-ID: {run_id}",
        "",
        "你是 Antigravity 版本更新与工作区权限兼容性评估助手。**请仅基于下方提供的采集材料直接分析输出，不要调用任何联网、终端、读文件或子代理工具。**",
        "",
        "## 本机客户端环境",
        *_build_local_env_lines(local_exe, pending_updater),
    ]

    lines.extend(
        [
            "",
            "## 本地预检命中情况",
            f"- 预检级别: `{prescreen['verdict']}`",
            f"- 命中维度: `{', '.join(prescreen['dimensions']) if prescreen['dimensions'] else 'NONE'}`",
            f"- 命中关键词: `{json.dumps(prescreen['matched_keywords'], ensure_ascii=False)}`",
            "",
            "## 官方 Changelog 版本条目",
        ]
    )

    if new_versions:
        for product, entries in new_versions.items():
            lines.append(f"### {product}")
            for entry in entries:
                lines.append(entry.get("body", "").strip())
                lines.append("")
    else:
        lines.append("本次无新增版本条目。")

    lines.extend(["", "## 官方 Models 页面状态 (`## Reasoning model`)"])
    lines.append(f"- 是否发生哈希变化: `{models_changed}`")
    if models_info.get("deprecation_notices"):
        lines.append("- 模型生命周期与下架公告:")
        for notice in models_info["deprecation_notices"]:
            lines.append(f"  - {notice}")
    if models_diff_text:
        lines.extend(["```diff", models_diff_text, "```"])
    elif models_info.get("table_rows"):
        lines.append("- 当前模型可用性表摘要:")
        lines.extend(models_info["table_rows"][:20])

    lines.extend(["", "## 本机 `builtin` 内置技能与默认配置快照差异"])
    lines.append(f"- 是否有变化: `{builtin_diff.get('has_changes', False)}`")
    lines.append(f"- 新增: `{builtin_diff.get('added', [])}`")
    lines.append(f"- 删除: `{builtin_diff.get('removed', [])}`")
    lines.append(f"- 修改: `{builtin_diff.get('modified', [])}`")
    if builtin_diff.get("diff_summary"):
        lines.extend(["```diff", builtin_diff["diff_summary"], "```"])

    lines.extend(
        [
            "",
            "## 评估输出要求",
            "请严格围绕以下 4 项输出结构化中文兼容性评估：",
            "1. **版本更新与修复摘要**：概括新增特性、交互调整与问题修复。",
            "2. **工作区权限、终端沙箱与配置兼容性评估**：检查 `Auto` 审批策略、跨工作区文件读写范围、终端沙箱行为、浏览器访问策略、诊断上传及模型可用性/下架计划，说明对日常自动化脚本与多目录开发工作流的兼容性影响。",
            "3. **生效范围区分**：客观区分【客户端本地版本变更（随客户端安装包生效）】与【云端模型及服务端配置变更（与本地客户端版本无关）】。",
            "4. **升级建议结论**：明确输出 `HOLD`（建议暂缓升级，待确认工作流兼容性）、`CAUTION`（建议关注变更项后升级）或 `SAFE`（常规功能与修复升级），并在回复末尾保留以下 `[AUDIT_RESULT]` 结构块以便阅读与归档（禁止省略或修改标签名）：",
            "```text",
            "[AUDIT_RESULT]",
            "VERDICT: HOLD | CAUTION | SAFE",
            "HEADLINE: <不超过36字的中文一句话结论>",
            "RISK_DIMENSIONS: <逗号分隔维度名或 NONE>",
            "[/AUDIT_RESULT]",
            "```",
        ]
    )
    return "\n".join(lines)


def parse_audit_result(
    text: str,
    fallback_verdict: str,
    fallback_headline: str,
    fallback_dimensions: list[str],
) -> dict[str, Any]:
    """Parse [AUDIT_RESULT]...[/AUDIT_RESULT] block with safe fallback."""
    block_match = re.search(
        r"\[AUDIT_RESULT\]\s*(.*?)\s*\[/AUDIT_RESULT\]",
        text or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not block_match:
        return {
            "parsed_ok": False,
            "verdict": fallback_verdict,
            "headline": fallback_headline[:36],
            "risk_dimensions": list(fallback_dimensions),
        }

    block = block_match.group(1)
    verdict = ""
    headline = ""
    dimensions: list[str] = []

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, val = line.split(":", 1)
        k = key.strip().upper()
        v = val.strip()
        if k == "VERDICT":
            cand = v.upper()
            if cand in {"HOLD", "CAUTION", "SAFE"}:
                verdict = cand
        elif k == "HEADLINE":
            headline = v[:36]
        elif k == "RISK_DIMENSIONS":
            if v.upper() == "NONE" or not v:
                dimensions = []
            else:
                dimensions = [part.strip() for part in v.split(",") if part.strip()]

    if not verdict:
        return {
            "parsed_ok": False,
            "verdict": fallback_verdict,
            "headline": (headline or fallback_headline)[:36],
            "risk_dimensions": dimensions or list(fallback_dimensions),
        }

    return {
        "parsed_ok": True,
        "verdict": verdict,
        "headline": (headline or fallback_headline)[:36],
        "risk_dimensions": dimensions,
    }


def map_title_and_annotations(
    verdict: str,
    version_label: str,
    headline: str,
) -> dict[str, Any]:
    """Map verdict to conversation title, pinned, and starred flags."""
    if verdict == "HOLD":
        return {
            "title": f"🛑 [建议暂缓升级] {version_label} — {headline}",
            "pinned": True,
            "starred": True,
        }
    if verdict == "CAUTION":
        return {
            "title": f"⚠️ [关注变更后升级] {version_label} — {headline}",
            "pinned": True,
            "starred": False,
        }
    return {
        "title": f"✅ [常规升级] {version_label} — {headline}",
        "pinned": False,
        "starred": False,
    }


def _call_conversation_service(
    ls_address: str,
    csrf_token: str,
    method: str,
    payload: dict[str, Any],
    timeout: int = 15,
) -> dict[str, Any]:
    """Call Antigravity ConversationService over Connect HTTP+JSON."""
    url = f"http://{ls_address}/product.v1.ConversationService/{method}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-antigravity-csrf-token": csrf_token,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else {}


def _resolve_native_exe_from_bat(bat_path: Path) -> str | None:
    """Extract language_server.exe (or underlying .exe) path from an agentapi .bat/.cmd wrapper."""
    try:
        content = bat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for match in re.finditer(r'"([^"]+?\.exe)"|([A-Za-z]:\\[^\s"]+?\.exe)', content, re.I):
        candidate = match.group(1) or match.group(2)
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def _probe_local_language_server_exe() -> str | None:
    """Fallback probe for Antigravity language_server.exe in standard install dirs."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None
    for rel in (
        Path("Programs") / "antigravity" / "resources" / "bin" / "language_server.exe",
        Path("antigravity") / "resources" / "bin" / "language_server.exe",
    ):
        cand = Path(local_app_data) / rel
        if cand.is_file():
            return str(cand)
    return None


def _resolve_agentapi_command() -> list[str] | None:
    """Resolve agentapi into a direct executable command list without invoking cmd.exe."""
    candidates: list[Path] = []
    found = shutil.which("agentapi")
    if found:
        candidates.append(Path(found))
    win_bat = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi.bat"
    if win_bat.is_file() and win_bat not in candidates:
        candidates.append(win_bat)

    for cand in candidates:
        suffix = cand.suffix.lower()
        if suffix not in (".bat", ".cmd"):
            return [str(cand)]
        exe_path = _resolve_native_exe_from_bat(cand) or _probe_local_language_server_exe()
        if exe_path:
            return [exe_path, "agentapi"]

    fallback_exe = _probe_local_language_server_exe()
    if fallback_exe:
        return [fallback_exe, "agentapi"]
    return None


def launch_audit_conversation(
    title: str,
    model: str,
    prompt_text: str,
) -> dict[str, Any]:
    """Optional manual debug helper to launch a native AGY conversation."""
    ls_address = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    csrf_token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    project_id = os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()

    if ls_address and csrf_token:
        payload: dict[str, Any] = {
            "title": title,
            "model": model,
            "tags": ["agy-update-guard"],
            "prompt": [{"text": prompt_text}],
        }
        if project_id and project_id != "outside-of-project":
            payload["projectId"] = project_id
        try:
            resp = _call_conversation_service(
                ls_address, csrf_token, "CreateConversation", payload
            )
            conv = resp.get("conversation") or {}
            conv_id = str(conv.get("id") or "")
            if conv_id:
                return {
                    "channel": "conversation_service",
                    "conversation_id": conv_id,
                    "ls_address": ls_address,
                    "csrf_token": csrf_token,
                }
        except Exception as exc:
            print(
                f"[update-sentinel] ConversationService CreateConversation failed, falling back to agentapi: {exc}",
                file=sys.stderr,
            )

    agentapi_cmd = _resolve_agentapi_command()
    if not agentapi_cmd:
        raise RuntimeError(
            "Neither ConversationService env vars nor a native agentapi executable (language_server.exe) is available."
        )

    cmd = [
        *agentapi_cmd,
        "new-conversation",
        f"--model={model}",
        f"--title={title}",
        "--",
        prompt_text,
    ]
    proc = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"agentapi new-conversation failed ({proc.returncode}): {proc.stderr or proc.stdout}"
        )

    conv_id = ""
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", proc.stdout, re.I)
    if m:
        conv_id = m.group(1)
    return {
        "channel": "agentapi",
        "conversation_id": conv_id,
        "ls_address": ls_address,
        "csrf_token": csrf_token,
        "stdout": proc.stdout.strip(),
    }


def _find_transcript_file(
    brain_dir: Path,
    conversation_id: str,
    run_id: str,
) -> Path | None:
    """Locate transcript_full.jsonl or transcript.jsonl by conversation_id or Audit-Run-ID."""
    if conversation_id:
        logs_dir = brain_dir / conversation_id / ".system_generated" / "logs"
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            candidate = logs_dir / name
            if candidate.is_file():
                return candidate

    if not brain_dir.exists():
        return None

    marker = f"Audit-Run-ID: {run_id}"
    candidates = sorted(
        brain_dir.glob("*/.system_generated/logs/transcript*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[:30]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if marker in text:
            full_sibling = path.with_name("transcript_full.jsonl")
            if full_sibling.is_file():
                return full_sibling
            return path
    return None


def _extract_model_reply_from_transcript(transcript_path: Path) -> str:
    """Extract the latest MODEL response text from a transcript JSONL file."""
    last_planner = ""
    last_audit_hit = ""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("source") != "MODEL":
            continue
        content = obj.get("content") or obj.get("response") or obj.get("text") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        if "[AUDIT_RESULT]" in content:
            last_audit_hit = content
        if obj.get("type") in ("PLANNER_RESPONSE", "RESPONSE", "ASSISTANT_MESSAGE"):
            last_planner = content
    return last_audit_hit or last_planner


def wait_and_finalize_audit(
    launch_info: dict[str, Any],
    run_id: str,
    version_label: str,
    prescreen: dict[str, Any],
    wait_seconds: int = 90,
    poll_interval: float = 2.0,
    brain_dir: Path | None = None,
) -> dict[str, Any]:
    """Poll conversation state & transcript, parse [AUDIT_RESULT], and update annotations."""
    resolved_brain = brain_dir or (Path.home() / ".gemini" / "antigravity" / "brain")
    conv_id = launch_info.get("conversation_id", "")
    ls_address = launch_info.get("ls_address", "")
    csrf_token = launch_info.get("csrf_token", "")
    can_use_http = bool(conv_id and ls_address and csrf_token)

    deadline = time.monotonic() + max(0, wait_seconds)
    model_reply = ""
    parsed: dict[str, Any] | None = None
    timed_out = False

    while True:
        is_idle = False
        if can_use_http:
            try:
                conv_resp = _call_conversation_service(
                    ls_address, csrf_token, "GetConversation", {"conversationId": conv_id}
                )
                conv_obj = conv_resp.get("conversation") or conv_resp
                run_state = (conv_obj.get("state") or {}).get("runState", "")
                if run_state == "RUN_STATE_IDLE":
                    is_idle = True
            except Exception:
                is_idle = False

        t_path = _find_transcript_file(resolved_brain, conv_id, run_id)
        if t_path is not None:
            model_reply = _extract_model_reply_from_transcript(t_path)
            if model_reply:
                candidate = parse_audit_result(
                    model_reply,
                    prescreen["verdict"],
                    prescreen["fallback_headline"],
                    prescreen["dimensions"],
                )
                if candidate["parsed_ok"]:
                    parsed = candidate
                    if not can_use_http or is_idle:
                        break
                elif is_idle:
                    parsed = candidate
                    break

        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(poll_interval)

    if parsed is None:
        parsed = parse_audit_result(
            model_reply,
            prescreen["verdict"],
            prescreen["fallback_headline"],
            prescreen["dimensions"],
        )

    annotations = map_title_and_annotations(
        parsed["verdict"], version_label, parsed["headline"]
    )

    updated_remote = False
    if can_use_http:
        try:
            _call_conversation_service(
                ls_address,
                csrf_token,
                "UpdateConversation",
                {
                    "conversationId": conv_id,
                    "annotations": {
                        "title": annotations["title"],
                        "pinned": annotations["pinned"],
                        "starred": annotations["starred"],
                    },
                },
            )
            updated_remote = True
        except Exception as exc:
            print(
                f"[update-sentinel] UpdateConversation failed: {exc}",
                file=sys.stderr,
            )

    return {
        "verdict": parsed["verdict"],
        "headline": parsed["headline"],
        "risk_dimensions": parsed["risk_dimensions"],
        "parsed_ok": parsed["parsed_ok"],
        "timed_out": timed_out,
        "title": annotations["title"],
        "pinned": annotations["pinned"],
        "starred": annotations["starred"],
        "updated_remote": updated_remote,
        "model_reply": model_reply,
    }


def _write_report_files(
    data_dir: Path,
    run_id: str,
    report_payload: dict[str, Any],
    prompt_text: str,
    model_reply: str = "",
) -> Path:
    """Archive preflight/audit report into <data_dir>/reports/."""
    reports_dir = data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md_path = reports_dir / f"{stamp}_{run_id[:8]}.md"
    lines = [
        f"# Update Guard Report ({stamp})",
        "",
        f"- **Run ID**: `{run_id}`",
        f"- **Verdict**: `{report_payload.get('final_verdict', report_payload.get('prescreen', {}).get('verdict', 'UNKNOWN'))}`",
        f"- **Local EXE**: `{report_payload.get('local_exe', {}).get('version', 'unknown')}`",
        f"- **Pending Updater**: `{report_payload.get('pending_updater', {})}`",
        "",
        "## Prompt / Collected Evidence",
        "",
        prompt_text,
    ]
    if model_reply:
        lines.extend(["", "## Model Reply", "", model_reply])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def _latest_versions_map(parsed_changelog: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """Return latest version string for each product line."""
    out: dict[str, str] = {}
    for product in PRODUCT_LINES:
        entries = parsed_changelog.get(product) or []
        if entries:
            out[product] = entries[0]["version"]
    return out


def acknowledge_pending_notice(
    data_dir: Path | str | None = None,
    builtin_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Mark pending_notice.json as acknowledged and advance state.json & builtin baseline."""
    resolved_data = resolve_data_dir(data_dir)
    notice_path = resolved_data / "pending_notice.json"
    state_path = resolved_data / "state.json"
    baseline_path = resolved_data / "snapshots" / "builtin_baseline.json"
    pending_snap_path = resolved_data / "snapshots" / "builtin_pending.json"

    notice = load_pending_notice(resolved_data)
    state = load_state(state_path) or {}
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    if notice is None:
        return {
            "mode": "acknowledge",
            "acknowledged": True,
            "already_acknowledged": True,
            "notice_id": "",
            "state_updated": False,
            "data_dir": str(resolved_data),
        }

    was_acknowledged = bool(notice.get("acknowledged", False))
    notice["acknowledged"] = True
    if not notice.get("acknowledged_at"):
        notice["acknowledged_at"] = now_iso

    pending_baseline = notice.get("pending_baseline") or {}
    existing_versions = state.get("last_seen_versions") or {}
    candidate_versions = (
        pending_baseline.get("last_seen_versions")
        or notice.get("latest_versions")
        or {}
    )
    merged_versions = {**existing_versions, **candidate_versions}

    models_info = notice.get("models_info") or {}
    new_models_hash = (
        pending_baseline.get("models_section_hash")
        or models_info.get("hash")
        or state.get("models_section_hash", "")
    )
    new_models_text = (
        pending_baseline.get("models_section_text")
        or state.get("models_section_text", "")
    )
    local_exe = notice.get("local_exe") or {}
    live_exe = get_local_antigravity_version()
    live_ver = str(live_exe.get("version") or "").strip()
    new_local_ver = (
        (live_ver if live_ver and live_ver != "unknown" else "")
        or pending_baseline.get("local_exe_version")
        or local_exe.get("version")
        or state.get("local_exe_version", "unknown")
    )
    if live_ver and live_ver != "unknown":
        notice["local_exe"] = live_exe

    pending_updater_ver = (
        str(pending_baseline.get("pending_updater_version") or "").strip()
        or str((notice.get("pending_updater") or {}).get("pending_version") or "").strip()
        or str(state.get("last_seen_pending_version") or "").strip()
    )

    new_state = {
        **state,
        "last_seen_versions": merged_versions,
        "models_section_hash": new_models_hash,
        "models_section_text": new_models_text,
        "local_exe_version": new_local_ver,
        "last_seen_pending_version": pending_updater_ver,
        "last_checked_at": state.get("last_checked_at") or now_iso,
        "updated_at": now_iso,
        "audit_history": state.get("audit_history", []),
    }
    save_state(state_path, new_state)

    if pending_snap_path.is_file():
        snap = builtin_snapshot.load_snapshot(pending_snap_path)
        if snap is not None:
            builtin_snapshot.save_snapshot(snap, baseline_path)
        try:
            pending_snap_path.unlink()
        except OSError:
            pass
    elif not baseline_path.is_file():
        resolved_builtin = (
            Path(builtin_dir)
            if builtin_dir is not None
            else builtin_snapshot.get_default_builtin_dir()
        )
        builtin_snapshot.save_snapshot(
            builtin_snapshot.collect_snapshot(resolved_builtin), baseline_path
        )

    _write_json_atomic(notice_path, notice)
    return {
        "mode": "acknowledge",
        "acknowledged": True,
        "already_acknowledged": was_acknowledged,
        "notice_id": str(notice.get("notice_id", "")),
        "state_updated": True,
        "data_dir": str(resolved_data),
    }


def _write_pending_notice_bundle(
    data_dir: Path,
    run_id: str,
    now_iso: str,
    version_label: str,
    prescreen: dict[str, Any],
    local_exe: dict[str, str],
    pending_updater: dict[str, Any],
    parsed_changelog: dict[str, list[dict[str, Any]]],
    new_versions: dict[str, list[dict[str, Any]]],
    models_info: dict[str, Any],
    models_changed: bool,
    builtin_diff: dict[str, Any],
    current_builtin: dict[str, Any],
    existing_versions: dict[str, str],
    prompt_text: str,
) -> dict[str, Any]:
    """Persist pending_notice.json and snapshots/builtin_pending.json without advancing baseline."""
    latest_versions = _latest_versions_map(parsed_changelog)
    compact_new = _compact_new_versions(new_versions)

    notice = {
        "notice_id": run_id,
        "created_at": now_iso,
        "acknowledged": False,
        "acknowledged_at": "",
        "version_label": version_label,
        "verdict": prescreen["verdict"],
        "headline": prescreen["fallback_headline"],
        "dimensions": prescreen["dimensions"],
        "dimension_labels": prescreen.get(
            "dimension_labels",
            [DIM_LABELS_ZH.get(d, d) for d in prescreen["dimensions"]],
        ),
        "matched_keywords": prescreen["matched_keywords"],
        "local_exe": local_exe,
        "pending_updater": pending_updater,
        "latest_versions": latest_versions,
        "new_versions": compact_new,
        "models_info": {
            "found": models_info["found"],
            "hash": models_info["hash"],
            "changed": models_changed,
            "deprecation_notices": models_info["deprecation_notices"],
            "table_rows_count": len(models_info["table_rows"]),
        },
        "builtin_diff": {
            "has_changes": builtin_diff["has_changes"],
            "added": builtin_diff["added"],
            "removed": builtin_diff["removed"],
            "modified": builtin_diff["modified"],
            "current_file_count": builtin_diff["current_file_count"],
        },
        "review_prompt": prompt_text,
        "pending_baseline": {
            "last_seen_versions": {**existing_versions, **latest_versions},
            "models_section_hash": models_info["hash"],
            "models_section_text": models_info["text"],
            "local_exe_version": local_exe["version"],
            "pending_updater_version": str(pending_updater.get("pending_version") or ""),
        },
    }
    _write_json_atomic(data_dir / "pending_notice.json", notice)
    builtin_snapshot.save_snapshot(
        current_builtin, data_dir / "snapshots" / "builtin_pending.json"
    )
    return notice


def _clear_check_lock(data_dir: Path) -> None:
    lock_path = data_dir / "check.lock"
    if lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass


def collect_guard_structured_content(
    data_dir: Path | str | None = None,
    refresh: bool = False,
    include_latest: bool = True,
    changelog_url: str = CHANGELOG_URL,
    models_url: str = MODELS_URL,
    builtin_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build structuredContent payload for MCP App UI from pending_notice, state, or live refresh."""
    resolved_data = resolve_data_dir(data_dir)
    state_path = resolved_data / "state.json"
    baseline_path = resolved_data / "snapshots" / "builtin_baseline.json"
    resolved_builtin = (
        Path(builtin_dir)
        if builtin_dir is not None
        else builtin_snapshot.get_default_builtin_dir()
    )

    state = load_state(state_path)
    notice = load_pending_notice(resolved_data)
    local_exe = get_local_antigravity_version()
    pending_updater = check_pending_updater_files(local_version=local_exe["version"])

    should_refresh = refresh or (state is None and notice is None)
    refresh_error = ""

    if should_refresh:
        try:
            changelog_md = fetch_url_text(changelog_url)
            models_md = fetch_url_text(models_url)
            parsed_changelog = parse_changelog(changelog_md)
            models_info = extract_reasoning_models_section(models_md)
            if not any(parsed_changelog.values()):
                raise RuntimeError("Upstream changelog.md parsing yielded 0 release entries.")
            if not models_info.get("found"):
                raise RuntimeError("Upstream models.md parsing did not find '## Reasoning model'.")

            current_builtin = builtin_snapshot.collect_snapshot(resolved_builtin)
            baseline_snap = builtin_snapshot.load_snapshot(baseline_path)
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            latest_versions = _latest_versions_map(parsed_changelog)

            if state is None:
                state = {
                    "last_seen_versions": latest_versions,
                    "models_section_hash": models_info["hash"],
                    "models_section_text": models_info["text"],
                    "local_exe_version": local_exe["version"],
                    "last_checked_at": now_iso,
                    "updated_at": now_iso,
                    "audit_history": [],
                }
                save_state(state_path, state)
                builtin_snapshot.save_snapshot(current_builtin, baseline_path)
                baseline_snap = current_builtin
            else:
                state["last_checked_at"] = now_iso
                state["local_exe_version"] = local_exe["version"]
                save_state(state_path, state)

            existing_versions = state.get("last_seen_versions", {})
            prev_models_hash = state.get("models_section_hash", "")
            prev_models_text = state.get("models_section_text", "")

            delta_versions = _collect_new_versions(
                parsed_changelog, existing_versions, audit_latest=False
            )
            models_changed = bool(prev_models_hash and prev_models_hash != models_info["hash"])
            models_diff_text = (
                diff_models_section(prev_models_text, models_info["text"])
                if models_changed
                else ""
            )
            if baseline_snap is not None:
                builtin_diff = builtin_snapshot.diff_snapshots(baseline_snap, current_builtin)
            else:
                builtin_diff = {
                    "has_changes": False,
                    "added": [],
                    "removed": [],
                    "modified": [],
                    "diffs": {},
                    "diff_summary": "",
                    "truncated": False,
                    "baseline_file_count": current_builtin["file_count"],
                    "current_file_count": current_builtin["file_count"],
                }

            pending_ver = str(pending_updater.get("pending_version") or "")
            pending_installer_changed = _is_pending_installer_unseen(pending_updater, state)
            has_any_change = bool(
                delta_versions
                or models_changed
                or builtin_diff.get("has_changes")
                or pending_installer_changed
            )
            display_versions = (
                delta_versions
                if (has_any_change or not include_latest)
                else _collect_new_versions(parsed_changelog, existing_versions, audit_latest=True)
            )
            prescreen = prescreen_risks(
                display_versions,
                models_changed,
                models_diff_text,
                models_info.get("deprecation_notices", []),
                builtin_diff,
            )
            if (
                pending_installer_changed
                and not (delta_versions.get("Antigravity App") or [])
                and prescreen["verdict"] == "SAFE"
            ):
                prescreen["verdict"] = "CAUTION"
                prescreen["fallback_headline"] = f"本地已下载待安装更新包 v{pending_ver}"[:36]
            run_id = str(uuid.uuid4())
            version_label = _build_version_label(
                display_versions,
                parsed_changelog,
                models_changed,
                bool(builtin_diff.get("has_changes")),
                pending_installer_version=pending_ver if pending_installer_changed else "",
            )
            prompt_text = build_audit_prompt(
                run_id=run_id,
                local_exe=local_exe,
                pending_updater=pending_updater,
                new_versions=display_versions,
                models_info=models_info,
                models_changed=models_changed,
                models_diff_text=models_diff_text,
                builtin_diff=builtin_diff,
                prescreen=prescreen,
            )

            if has_any_change:
                notice = _write_pending_notice_bundle(
                    data_dir=resolved_data,
                    run_id=run_id,
                    now_iso=now_iso,
                    version_label=version_label,
                    prescreen=prescreen,
                    local_exe=local_exe,
                    pending_updater=pending_updater,
                    parsed_changelog=parsed_changelog,
                    new_versions=display_versions,
                    models_info=models_info,
                    models_changed=models_changed,
                    builtin_diff=builtin_diff,
                    current_builtin=current_builtin,
                    existing_versions=existing_versions,
                    prompt_text=prompt_text,
                )
                return {
                    "has_unread_notice": True,
                    "acknowledged": False,
                    "notice_id": notice["notice_id"],
                    "created_at": notice["created_at"],
                    "acknowledged_at": "",
                    "last_checked_at": now_iso,
                    "version_label": version_label,
                    "verdict": prescreen["verdict"],
                    "headline": prescreen["fallback_headline"],
                    "dimensions": prescreen["dimensions"],
                    "dimension_labels": prescreen["dimension_labels"],
                    "local_exe": local_exe,
                    "pending_updater": pending_updater,
                    "latest_versions": latest_versions,
                    "new_versions": notice["new_versions"],
                    "models_info": notice["models_info"],
                    "builtin_diff": notice["builtin_diff"],
                    "review_prompt": prompt_text,
                    "source": "live_refresh",
                    "error": "",
                }

            unread_existing = bool(notice and not notice.get("acknowledged", False))
            return {
                "has_unread_notice": unread_existing,
                "acknowledged": not unread_existing,
                "notice_id": str((notice or {}).get("notice_id", run_id)),
                "created_at": str((notice or {}).get("created_at", now_iso)),
                "acknowledged_at": str((notice or {}).get("acknowledged_at", "")),
                "last_checked_at": now_iso,
                "version_label": version_label,
                "verdict": prescreen["verdict"],
                "headline": prescreen["fallback_headline"],
                "dimensions": prescreen["dimensions"],
                "dimension_labels": prescreen["dimension_labels"],
                "local_exe": local_exe,
                "pending_updater": pending_updater,
                "latest_versions": latest_versions,
                "new_versions": _compact_new_versions(display_versions),
                "models_info": {
                    "found": models_info["found"],
                    "hash": models_info["hash"],
                    "changed": models_changed,
                    "deprecation_notices": models_info["deprecation_notices"],
                    "table_rows_count": len(models_info["table_rows"]),
                },
                "builtin_diff": {
                    "has_changes": builtin_diff["has_changes"],
                    "added": builtin_diff["added"],
                    "removed": builtin_diff["removed"],
                    "modified": builtin_diff["modified"],
                    "current_file_count": builtin_diff["current_file_count"],
                },
                "review_prompt": prompt_text,
                "source": "live_refresh",
                "error": "",
            }
        except Exception as exc:
            refresh_error = str(exc)

    if notice is not None:
        acknowledged = bool(notice.get("acknowledged", False))
        dims = list(notice.get("dimensions") or [])
        effective_local_exe = (
            local_exe
            if local_exe.get("version") != "unknown"
            else (notice.get("local_exe") or local_exe)
        )
        refreshed_prompt = _refresh_prompt_local_env(
            str(notice.get("review_prompt", "")),
            effective_local_exe,
            pending_updater,
        )
        return {
            "has_unread_notice": not acknowledged,
            "acknowledged": acknowledged,
            "notice_id": str(notice.get("notice_id", "")),
            "created_at": str(notice.get("created_at", "")),
            "acknowledged_at": str(notice.get("acknowledged_at", "")),
            "last_checked_at": str((state or {}).get("last_checked_at") or (state or {}).get("updated_at") or ""),
            "version_label": str(notice.get("version_label", "Check")),
            "verdict": str(notice.get("verdict", "SAFE")),
            "headline": str(notice.get("headline", "")),
            "dimensions": dims,
            "dimension_labels": list(
                notice.get("dimension_labels") or [DIM_LABELS_ZH.get(d, d) for d in dims]
            ),
            "local_exe": effective_local_exe,
            "pending_updater": pending_updater,
            "latest_versions": dict(notice.get("latest_versions") or (state or {}).get("last_seen_versions") or {}),
            "new_versions": dict(notice.get("new_versions") or {}),
            "models_info": dict(notice.get("models_info") or {}),
            "builtin_diff": dict(notice.get("builtin_diff") or {}),
            "review_prompt": refreshed_prompt,
            "source": "pending_notice",
            "error": refresh_error,
        }

    # Cached state fallback (no pending_notice.json yet)
    cached_versions = dict((state or {}).get("last_seen_versions") or {})
    cached_models_text = str((state or {}).get("models_section_text") or "")
    cached_models_info = (
        extract_reasoning_models_section(cached_models_text)
        if cached_models_text
        else {"found": False, "hash": str((state or {}).get("models_section_hash", "")), "deprecation_notices": [], "table_rows": []}
    )
    current_builtin = builtin_snapshot.collect_snapshot(resolved_builtin)
    baseline_snap = builtin_snapshot.load_snapshot(baseline_path)
    builtin_diff = (
        builtin_snapshot.diff_snapshots(baseline_snap, current_builtin)
        if baseline_snap is not None
        else {
            "has_changes": False,
            "added": [],
            "removed": [],
            "modified": [],
            "diffs": {},
            "diff_summary": "",
            "truncated": False,
            "baseline_file_count": current_builtin["file_count"],
            "current_file_count": current_builtin["file_count"],
        }
    )
    prescreen = prescreen_risks(
        {},
        False,
        "",
        cached_models_info.get("deprecation_notices", []),
        builtin_diff,
    )
    run_id = str(uuid.uuid4())
    app_ver = cached_versions.get("Antigravity App", "")
    version_label = f"App v{app_ver}" if app_ver else "Baseline"
    if builtin_diff.get("has_changes"):
        version_label = f"{version_label} / Builtin"
    fallback_prompt = build_audit_prompt(
        run_id=run_id,
        local_exe=local_exe,
        pending_updater=pending_updater,
        new_versions={},
        models_info=cached_models_info,
        models_changed=False,
        models_diff_text="",
        builtin_diff=builtin_diff,
        prescreen=prescreen,
    )
    return {
        "has_unread_notice": False,
        "acknowledged": True,
        "notice_id": "",
        "created_at": "",
        "acknowledged_at": "",
        "last_checked_at": str((state or {}).get("last_checked_at") or (state or {}).get("updated_at") or ""),
        "version_label": version_label,
        "verdict": prescreen["verdict"],
        "headline": "当前处于已确认基线状态" if not builtin_diff.get("has_changes") else prescreen["fallback_headline"],
        "dimensions": prescreen["dimensions"],
        "dimension_labels": prescreen["dimension_labels"],
        "local_exe": local_exe,
        "pending_updater": pending_updater,
        "latest_versions": cached_versions,
        "new_versions": {},
        "models_info": {
            "found": cached_models_info["found"],
            "hash": cached_models_info["hash"],
            "changed": False,
            "deprecation_notices": cached_models_info["deprecation_notices"],
            "table_rows_count": len(cached_models_info["table_rows"]),
        },
        "builtin_diff": {
            "has_changes": builtin_diff["has_changes"],
            "added": builtin_diff["added"],
            "removed": builtin_diff["removed"],
            "modified": builtin_diff["modified"],
            "current_file_count": builtin_diff["current_file_count"],
        },
        "review_prompt": fallback_prompt,
        "source": "cached_state",
        "error": refresh_error,
    }


def run_sentinel(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Execute one sentinel pass and return (exit_code, structured_result)."""
    data_dir = resolve_data_dir(getattr(args, "data_dir", None))
    builtin_dir = (
        Path(args.builtin_dir)
        if getattr(args, "builtin_dir", None)
        else builtin_snapshot.get_default_builtin_dir()
    )

    if getattr(args, "acknowledge", False):
        return 0, acknowledge_pending_notice(data_dir, builtin_dir=builtin_dir)

    state_path = data_dir / "state.json"
    baseline_path = data_dir / "snapshots" / "builtin_baseline.json"
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    try:
        changelog_md = fetch_url_text(args.changelog_url)
        models_md = fetch_url_text(args.models_url)

        parsed_changelog = parse_changelog(changelog_md)
        models_info = extract_reasoning_models_section(models_md)
        if not any(parsed_changelog.values()):
            raise RuntimeError(
                "Upstream changelog.md parsing yielded 0 release entries across all product lines."
            )
        if not models_info.get("found"):
            raise RuntimeError(
                "Upstream models.md parsing did not find '## Reasoning model' section."
            )

        local_exe = get_local_antigravity_version()
        pending_updater = check_pending_updater_files(local_version=local_exe["version"])
        current_builtin = builtin_snapshot.collect_snapshot(builtin_dir)

        state = load_state(state_path)
        baseline_snap = builtin_snapshot.load_snapshot(baseline_path)
        existing_versions = (state or {}).get("last_seen_versions", {})

        if getattr(args, "init_baseline", False) or (
            state is None and not getattr(args, "audit_latest", False)
        ):
            new_state = {
                "last_seen_versions": {**existing_versions, **_latest_versions_map(parsed_changelog)},
                "models_section_hash": models_info["hash"],
                "models_section_text": models_info["text"],
                "local_exe_version": local_exe["version"],
                "last_seen_pending_version": str(pending_updater.get("pending_version") or ""),
                "last_checked_at": now_iso,
                "audit_history": state.get("audit_history", []) if state else [],
                "updated_at": now_iso,
            }
            if not getattr(args, "dry_run", False):
                save_state(state_path, new_state)
                builtin_snapshot.save_snapshot(current_builtin, baseline_path)
            result = {
                "mode": "init_baseline",
                "status": "initialized",
                "conversation_triggered": False,
                "notice_written": False,
                "data_dir": str(data_dir),
                "latest_versions": new_state["last_seen_versions"],
                "models_section_hash": models_info["hash"],
                "local_exe": local_exe,
                "pending_updater": pending_updater,
                "builtin_file_count": current_builtin["file_count"],
            }
            return 0, result

        last_seen = (state or {}).get("last_seen_versions", {})
        prev_models_hash = (state or {}).get("models_section_hash", "")
        prev_models_text = (state or {}).get("models_section_text", "")

        new_versions = _collect_new_versions(
            parsed_changelog, last_seen, getattr(args, "audit_latest", False)
        )
        models_changed = bool(prev_models_hash and prev_models_hash != models_info["hash"])
        models_diff_text = (
            diff_models_section(prev_models_text, models_info["text"])
            if models_changed
            else ""
        )

        if baseline_snap is not None:
            builtin_diff = builtin_snapshot.diff_snapshots(baseline_snap, current_builtin)
        else:
            builtin_diff = {
                "has_changes": False,
                "added": [],
                "removed": [],
                "modified": [],
                "diffs": {},
                "diff_summary": "",
                "truncated": False,
                "baseline_file_count": current_builtin["file_count"],
                "current_file_count": current_builtin["file_count"],
            }

        pending_ver = str(pending_updater.get("pending_version") or "")
        pending_installer_changed = _is_pending_installer_unseen(pending_updater, state)
        has_any_change = bool(
            new_versions
            or models_changed
            or builtin_diff.get("has_changes")
            or pending_installer_changed
        )
        if not has_any_change and not getattr(args, "audit_latest", False):
            if not getattr(args, "dry_run", False) and state is not None:
                updated_state = {
                    **state,
                    "local_exe_version": local_exe["version"],
                    "last_checked_at": now_iso,
                }
                save_state(state_path, updated_state)
            return 0, {
                "mode": "check",
                "status": "no_changes",
                "conversation_triggered": False,
                "notice_written": False,
                "data_dir": str(data_dir),
                "latest_versions": _latest_versions_map(parsed_changelog),
                "local_exe": local_exe,
                "pending_updater": pending_updater,
            }

        prescreen = prescreen_risks(
            new_versions,
            models_changed,
            models_diff_text,
            models_info.get("deprecation_notices", []),
            builtin_diff,
        )
        if (
            pending_installer_changed
            and not (new_versions.get("Antigravity App") or [])
            and prescreen["verdict"] == "SAFE"
        ):
            prescreen["verdict"] = "CAUTION"
            prescreen["fallback_headline"] = f"本地已下载待安装更新包 v{pending_ver}"[:36]
        run_id = str(uuid.uuid4())
        version_label = _build_version_label(
            new_versions,
            parsed_changelog,
            models_changed,
            bool(builtin_diff.get("has_changes")),
            pending_installer_version=pending_ver if pending_installer_changed else "",
        )
        initial_annotations = map_title_and_annotations(
            prescreen["verdict"], version_label, prescreen["fallback_headline"]
        )
        prompt_text = build_audit_prompt(
            run_id=run_id,
            local_exe=local_exe,
            pending_updater=pending_updater,
            new_versions=new_versions,
            models_info=models_info,
            models_changed=models_changed,
            models_diff_text=models_diff_text,
            builtin_diff=builtin_diff,
            prescreen=prescreen,
        )

        if getattr(args, "dry_run", False):
            result = {
                "mode": "dry_run",
                "status": "preflight_ready",
                "run_id": run_id,
                "conversation_triggered": False,
                "notice_written": False,
                "version_label": version_label,
                "initial_title": initial_annotations["title"],
                "latest_versions": _latest_versions_map(parsed_changelog),
                "new_versions": {
                    k: [
                        {"version": item["version"], "date": item["date"], "summary": item["summary"]}
                        for item in v
                    ]
                    for k, v in new_versions.items()
                },
                "local_exe": local_exe,
                "pending_updater": pending_updater,
                "models_info": {
                    "found": models_info["found"],
                    "hash": models_info["hash"],
                    "changed": models_changed,
                    "deprecation_notices": models_info["deprecation_notices"],
                    "table_rows_count": len(models_info["table_rows"]),
                },
                "builtin_diff": {
                    "has_changes": builtin_diff["has_changes"],
                    "added": builtin_diff["added"],
                    "removed": builtin_diff["removed"],
                    "modified": builtin_diff["modified"],
                    "current_file_count": builtin_diff["current_file_count"],
                },
                "prescreen": prescreen,
                "prompt": prompt_text,
                "review_prompt": prompt_text,
            }
            return 0, result

        if not getattr(args, "manual_create_conversation", False):
            notice = _write_pending_notice_bundle(
                data_dir=data_dir,
                run_id=run_id,
                now_iso=now_iso,
                version_label=version_label,
                prescreen=prescreen,
                local_exe=local_exe,
                pending_updater=pending_updater,
                parsed_changelog=parsed_changelog,
                new_versions=new_versions,
                models_info=models_info,
                models_changed=models_changed,
                builtin_diff=builtin_diff,
                current_builtin=current_builtin,
                existing_versions=existing_versions,
                prompt_text=prompt_text,
            )
            base_state = state if state is not None else {
                "last_seen_versions": existing_versions,
                "models_section_hash": prev_models_hash,
                "models_section_text": prev_models_text,
                "audit_history": [],
                "updated_at": now_iso,
            }
            save_state(
                state_path,
                {
                    **base_state,
                    "local_exe_version": local_exe["version"],
                    "last_checked_at": now_iso,
                },
            )
            return 0, {
                "mode": "check",
                "status": "pending_notice",
                "run_id": run_id,
                "notice_id": notice["notice_id"],
                "conversation_triggered": False,
                "notice_written": True,
                "version_label": version_label,
                "verdict": prescreen["verdict"],
                "headline": prescreen["fallback_headline"],
                "dimensions": prescreen["dimensions"],
                "data_dir": str(data_dir),
                "latest_versions": notice["latest_versions"],
                "local_exe": local_exe,
                "pending_updater": pending_updater,
            }

        # Explicit --manual-create-conversation debug path only
        model = (
            getattr(args, "model", None)
            or os.environ.get("AGY_UPDATE_GUARD_MODEL")
            or DEFAULT_MODEL
        )
        launch_info = launch_audit_conversation(
            title=initial_annotations["title"],
            model=model,
            prompt_text=prompt_text,
        )
        final_audit = wait_and_finalize_audit(
            launch_info=launch_info,
            run_id=run_id,
            version_label=version_label,
            prescreen=prescreen,
            wait_seconds=getattr(args, "wait_seconds", 90),
        )

        history = list((state or {}).get("audit_history", []))
        history.append(
            {
                "run_id": run_id,
                "timestamp": now_iso,
                "version_label": version_label,
                "verdict": final_audit["verdict"],
                "headline": final_audit["headline"],
                "risk_dimensions": final_audit["risk_dimensions"],
                "conversation_id": launch_info.get("conversation_id", ""),
            }
        )
        new_state = {
            "last_seen_versions": {**existing_versions, **_latest_versions_map(parsed_changelog)},
            "models_section_hash": models_info["hash"],
            "models_section_text": models_info["text"],
            "local_exe_version": local_exe["version"],
            "last_checked_at": now_iso,
            "audit_history": history[-20:],
            "updated_at": now_iso,
        }
        save_state(state_path, new_state)
        builtin_snapshot.save_snapshot(current_builtin, baseline_path)

        report_payload = {
            "run_id": run_id,
            "final_verdict": final_audit["verdict"],
            "local_exe": local_exe,
            "pending_updater": pending_updater,
            "prescreen": prescreen,
        }
        report_path = _write_report_files(
            data_dir, run_id, report_payload, prompt_text, final_audit.get("model_reply", "")
        )

        return 0, {
            "mode": "audit",
            "status": "completed",
            "run_id": run_id,
            "conversation_triggered": True,
            "channel": launch_info.get("channel"),
            "conversation_id": launch_info.get("conversation_id", ""),
            "final_audit": {k: v for k, v in final_audit.items() if k != "model_reply"},
            "report_path": str(report_path),
        }
    finally:
        if not getattr(args, "dry_run", False):
            _clear_check_lock(data_dir)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update Guard zero-quota sentinel for Antigravity releases."
    )
    parser.add_argument("--check", action="store_true", help="Run periodic check (zero-account-touch default).")
    parser.add_argument(
        "--init-baseline",
        action="store_true",
        help="Capture initial baseline state and builtin snapshot with 0 LLM quota.",
    )
    parser.add_argument(
        "--acknowledge",
        action="store_true",
        help="Mark pending_notice.json as acknowledged and advance baseline state.",
    )
    parser.add_argument(
        "--audit-latest",
        action="store_true",
        help="Include latest changelog versions even if already recorded in state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write state or pending notice; output preflight report only.",
    )
    parser.add_argument(
        "--manual-create-conversation",
        action="store_true",
        help="Debug-only flag to manually launch a conversation via ConversationService/agentapi.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    parser.add_argument("--data-dir", default=None, help="Override persistent data directory.")
    parser.add_argument("--builtin-dir", default=None, help="Override ~/.gemini/antigravity/builtin.")
    parser.add_argument("--model", default=None, help=f"Override audit model for manual debug (default: {DEFAULT_MODEL}).")
    parser.add_argument("--wait-seconds", type=int, default=90, help="Seconds to wait in manual debug mode.")
    parser.add_argument("--changelog-url", default=CHANGELOG_URL, help=argparse.SUPPRESS)
    parser.add_argument("--models-url", default=MODELS_URL, help=argparse.SUPPRESS)
    return parser


def _print_human_report(result: dict[str, Any]) -> None:
    mode = result.get("mode")
    if mode == "dry_run":
        print(f"=== Update Guard Preflight Report (Run {result['run_id']}) ===")
        print(f"Target Label : {result['version_label']}")
        print(f"Initial Title: {result['initial_title']}")
        print(f"Local Exe    : {result['local_exe'].get('version')} ({result['local_exe'].get('path') or 'not found'})")
        pending = result["pending_updater"]
        print(f"Pending Cache: {pending.get('has_pending')} -> {pending.get('files')}")
        print(f"Latest Online: {json.dumps(result['latest_versions'], ensure_ascii=False)}")
        print(f"Models Hash  : {result['models_info']['hash'][:16]}... (changed={result['models_info']['changed']}, rows={result['models_info']['table_rows_count']})")
        if result["models_info"]["deprecation_notices"]:
            print(f"Deprecations : {result['models_info']['deprecation_notices']}")
        bdiff = result["builtin_diff"]
        print(
            f"Builtin Diff : has_changes={bdiff['has_changes']}, files={bdiff['current_file_count']}, "
            f"+{len(bdiff['added'])} -{len(bdiff['removed'])} ~{len(bdiff['modified'])}"
        )
        print(f"Pre-screen   : {result['prescreen']['verdict']} | dims={result['prescreen']['dimensions']}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        code, result = run_sentinel(args)
    except urllib.error.URLError as exc:
        print(f"[update-sentinel] Network error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[update-sentinel] Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human_report(result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
