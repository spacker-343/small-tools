#!/usr/bin/env python3
"""Zero-quota local patch compatibility & PolicyGuardian/Proctor binary diff inspector."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
ELECTRON_FUSE_SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"
CHAT_EDITOR_MARKER = b"// AGY_CHAT_EDITOR_V1"
FORK_DESKTOP_CHECK = 'typeof window.dialog?.showOpenDialog==="function"'
FORK_SUBAGENT_STUB_CHECK = b"enable-fork-with-subagents"

GUARDIAN_CATEGORIES: dict[str, tuple[str, ...]] = {
    "proctor_engine": (
        "policyproctor",
        "proctorsession",
        "proctorstate",
        "proctorstrike",
        "proctordenial",
        "quarantinedtarget",
        "policyguardiancoverage",
        "allowproctor",
        "proctoranalysis",
    ),
    "skill_mcp_scan": (
        "skillrisk",
        "skillscanmode",
        "proctorskillscan",
        "filterblockedskills",
        "filterblockedmcpspecs",
        "mcpdrift",
        "proctor skill security notice",
    ),
    "trajectory_llm_classifier": (
        "llmclassifier",
        "classifytrajectory",
        "classifycontext",
        "classifytoolcall",
        "classifyandcachecontext",
        "classifycommandwithbudget",
        "auditpostexecstep",
        "generatesafetysignals",
    ),
    "policy_guardian_core": (
        "policyguardian",
        "guardianagentmonitor",
        "introspectionverdicts",
        "embeddingverdicts",
        "accesspolicyviolation",
    ),
}

CATEGORY_LABELS_ZH: dict[str, str] = {
    "proctor_engine": "PolicyProctor 监考审查引擎",
    "skill_mcp_scan": "本地 Skill / MCP 风险扫描与过滤",
    "trajectory_llm_classifier": "会话轨迹与上下文外审 LLMClassifier",
    "policy_guardian_core": "PolicyGuardian 核心监控策略",
}

_SYMBOL_RE = re.compile(rb"[A-Za-z0-9_./:\[\] -]{6,140}")
_ALL_WATCH_TERMS: tuple[bytes, ...] = tuple(
    term.encode("ascii")
    for terms in GUARDIAN_CATEGORIES.values()
    for term in terms
)


def resolve_companion_tools(
    data_dir: Path | None = None,
    plugin_root: Path | None = None,
) -> dict[str, Path | None]:
    """Locate sibling antigravity-chat-editor and antigravity-fork directories."""
    chat_override = os.environ.get("AGY_CHAT_EDITOR_DIR", "").strip()
    fork_override = os.environ.get("AGY_FORK_DIR", "").strip()
    tools_root_env = os.environ.get("AGY_UPDATE_GUARD_TOOLS_ROOT", "").strip()

    candidate_roots: list[Path] = []
    if tools_root_env:
        candidate_roots.append(Path(tools_root_env))

    if data_dir is not None:
        cfg_path = data_dir / "companion_paths.json"
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    if cfg.get("chat_editor_dir") and not chat_override:
                        p = Path(str(cfg["chat_editor_dir"]))
                        if p.is_dir():
                            chat_override = str(p)
                    if cfg.get("fork_dir") and not fork_override:
                        p = Path(str(cfg["fork_dir"]))
                        if p.is_dir():
                            fork_override = str(p)
                    if cfg.get("tools_root"):
                        candidate_roots.append(Path(str(cfg["tools_root"])))
            except (OSError, json.JSONDecodeError):
                pass

    if plugin_root is not None:
        candidate_roots.append(plugin_root.resolve().parent)

    chat_dir: Path | None = Path(chat_override) if chat_override and Path(chat_override).is_dir() else None
    fork_dir: Path | None = Path(fork_override) if fork_override and Path(fork_override).is_dir() else None

    for root in candidate_roots:
        if chat_dir is None:
            cand = root / "antigravity-chat-editor"
            if (cand / "install.py").is_file():
                chat_dir = cand
        if fork_dir is None:
            cand = root / "antigravity-fork"
            if (cand / "fork_toolbar.py").is_file():
                fork_dir = cand

    return {"chat_editor_dir": chat_dir, "fork_dir": fork_dir}


def inspect_current_patch_state(install_dir: Path | None) -> dict[str, Any]:
    """Read-only check whether current installed Antigravity currently has Edit/Fork patches applied."""
    if install_dir is None or not install_dir.is_dir():
        return {"checked": False, "chat_editor_installed": False, "fork_renderer_patched": False}

    chat_installed = False
    fork_renderer = False
    asar_path = install_dir / "resources" / "app.asar"
    ls_path = install_dir / "resources" / "bin" / "language_server.exe"

    if asar_path.is_file():
        try:
            raw_asar = asar_path.read_bytes()
            chat_installed = CHAT_EDITOR_MARKER in raw_asar
        except OSError:
            pass

    if ls_path.is_file():
        try:
            raw_ls = ls_path.read_bytes()
            fork_renderer = FORK_DESKTOP_CHECK.encode("utf-8") in raw_ls or b"PK\x05\x06" in raw_ls
        except OSError:
            pass

    return {
        "checked": True,
        "install_dir": str(install_dir),
        "chat_editor_installed": chat_installed,
        "fork_ls_present": fork_renderer,
    }


def extract_guardian_symbols(binary_data: bytes) -> set[str]:
    """Extract normalized ASCII symbols/notices related to PolicyGuardian, Proctor, and classifiers."""
    found: set[str] = set()
    for match in _SYMBOL_RE.finditer(binary_data):
        raw = match.group(0)
        lower = raw.lower()
        if any(term in lower for term in _ALL_WATCH_TERMS):
            text = raw.decode("ascii", errors="ignore").strip()
            if 6 <= len(text) <= 140:
                found.add(text)
    return found


def diff_guardian_binaries(
    baseline_ls_bytes: bytes | None,
    target_ls_bytes: bytes,
    baseline_version: str = "unknown",
    target_version: str = "unknown",
) -> dict[str, Any]:
    """Compare PolicyGuardian / PolicyProctor / LLMClassifier / SetCloudCodeURL symbols across binaries."""
    cloudcode_url_present = b"SetCloudCodeURL" in target_ls_bytes

    if baseline_ls_bytes is None:
        target_syms = extract_guardian_symbols(target_ls_bytes)
        return {
            "compared": False,
            "baseline_version": baseline_version,
            "target_version": target_version,
            "cloudcode_url_present": cloudcode_url_present,
            "total_added_count": 0,
            "total_removed_count": 0,
            "category_hits": {},
            "category_labels": [],
            "sample_added": [],
            "proctor_skill_notice_added": False,
            "escalated": not cloudcode_url_present,
        }

    base_syms = extract_guardian_symbols(baseline_ls_bytes)
    target_syms = extract_guardian_symbols(target_ls_bytes)
    added = sorted(target_syms - base_syms)
    removed = sorted(base_syms - target_syms)

    category_hits: dict[str, list[str]] = {}
    for sym in added:
        sym_lower = sym.lower()
        for cat, keywords in GUARDIAN_CATEGORIES.items():
            if any(kw in sym_lower for kw in keywords):
                bucket = category_hits.setdefault(cat, [])
                if len(bucket) < 12:
                    bucket.append(sym)

    proctor_skill_notice_added = (
        b"PROCTOR SKILL SECURITY NOTICE" in target_ls_bytes
        and b"PROCTOR SKILL SECURITY NOTICE" not in baseline_ls_bytes
    )

    high_signal_cats = {"proctor_engine", "skill_mcp_scan", "trajectory_llm_classifier"}
    has_high_signal_new_cat = any(cat in category_hits for cat in high_signal_cats)
    escalated = bool(
        not cloudcode_url_present
        or proctor_skill_notice_added
        or has_high_signal_new_cat
        or len(added) >= 20
    )

    # Prioritize clean symbol names for human/LLM inspection
    clean_samples = [
        s for s in added if any(ch in s for ch in (".", "_", " ", "["))
    ][:35]
    if len(clean_samples) < 20:
        for s in added:
            if s not in clean_samples:
                clean_samples.append(s)
            if len(clean_samples) >= 35:
                break

    return {
        "compared": True,
        "baseline_version": baseline_version,
        "target_version": target_version,
        "cloudcode_url_present": cloudcode_url_present,
        "total_added_count": len(added),
        "total_removed_count": len(removed),
        "category_hits": category_hits,
        "category_labels": [CATEGORY_LABELS_ZH[c] for c in category_hits if c in CATEGORY_LABELS_ZH],
        "sample_added": clean_samples,
        "proctor_skill_notice_added": proctor_skill_notice_added,
        "escalated": escalated,
    }


def _extract_nsis_7z_stage(installer_exe: Path, stage_dir: Path) -> None:
    """Extract core Antigravity files from an NSIS 7z installer into stage_dir using tar.exe."""
    raw = installer_exe.read_bytes()
    pos = raw.find(SEVEN_ZIP_MAGIC)
    if pos < 0:
        raise ValueError(f"未在安装包中找到内嵌 7z 归档头: {installer_exe.name}")

    tar_bin = shutil.which("tar.exe") or shutil.which("tar")
    if not tar_bin:
        raise RuntimeError("系统未找到 tar 工具，无法只读解包待安装更新包")

    payload_7z = stage_dir.parent / "payload.7z"
    payload_7z.write_bytes(raw[pos:])
    try:
        proc = subprocess.run(
            [
                tar_bin,
                "-xf",
                str(payload_7z),
                "-C",
                str(stage_dir),
                "Antigravity.exe",
                "version",
                "resources/app.asar",
                "resources/bin/language_server.exe",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"tar 解包失败 (rc={proc.returncode}): {(proc.stderr or proc.stdout).strip()[:240]}"
            )
    finally:
        payload_7z.unlink(missing_ok=True)


def _run_chat_editor_check(stage_dir: Path, chat_editor_dir: Path | None) -> dict[str, Any]:
    """Run antigravity-chat-editor readonly compatibility check against staged client."""
    if chat_editor_dir is not None and (chat_editor_dir / "install.py").is_file():
        install_py = chat_editor_dir / "install.py"
        try:
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", str(install_py), "check", "--app", str(stage_dir)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                shell=False,
            )
            out = "\n".join(x for x in (proc.stdout.strip(), proc.stderr.strip()) if x).strip()
            ok = proc.returncode == 0
            return {
                "checked": True,
                "mode": "install_py_check",
                "compatible": ok,
                "summary": "兼容（ASAR Fuse、本地服务符号、Protobuf 描述符与气泡锚点均通过）"
                if ok
                else f"不兼容：{out[:160]}",
                "output": out[:800],
            }
        except Exception as exc:
            return {
                "checked": True,
                "mode": "install_py_check",
                "compatible": False,
                "summary": f"检查执行异常：{exc}",
                "output": str(exc),
            }

    # Built-in fallback check when sibling repo directory is unavailable
    exe_path = stage_dir / "Antigravity.exe"
    asar_path = stage_dir / "resources" / "app.asar"
    ls_path = stage_dir / "resources" / "bin" / "language_server.exe"
    if not (exe_path.is_file() and asar_path.is_file() and ls_path.is_file()):
        return {
            "checked": False,
            "mode": "builtin_fallback",
            "compatible": None,
            "summary": "解包目录缺少核心文件，无法验证 chat-editor 兼容性",
            "output": "",
        }

    fuse = exe_path.read_bytes()
    at = fuse.find(ELECTRON_FUSE_SENTINEL)
    fuse_ok = not (
        at < 0 or len(fuse) <= at + 38 or fuse[at + 32] != 1 or fuse[at + 33] < 5 or fuse[at + 38] != ord("0")
    )
    ls_ok = b"SetCloudCodeURL" in ls_path.read_bytes()
    ok = fuse_ok and ls_ok
    return {
        "checked": True,
        "mode": "builtin_fallback",
        "compatible": ok,
        "summary": "基础兼容（ASAR Fuse 未锁死且 SetCloudCodeURL 存在）"
        if ok
        else f"基础检查失败（fuse_ok={fuse_ok}, SetCloudCodeURL={ls_ok}）",
        "output": "",
    }


def _run_fork_check(stage_dir: Path, fork_dir: Path | None, scratch_dir: Path) -> dict[str, Any]:
    """Run antigravity-fork dry-build compatibility check against staged client."""
    if fork_dir is not None and (fork_dir / "fork_toolbar.py").is_file():
        fork_py = fork_dir / "fork_toolbar.py"
        out_exe = scratch_dir / "patched_language_server.exe"
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(fork_py),
                    "--install",
                    str(stage_dir),
                    "--output",
                    str(out_exe),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                shell=False,
            )
            out = "\n".join(x for x in (proc.stdout.strip(), proc.stderr.strip()) if x).strip()
            if proc.returncode == 0:
                return {
                    "checked": True,
                    "mode": "fork_toolbar_dry_build",
                    "compatible": True,
                    "summary": "兼容（内嵌前端 ZIP 开关与 Go 后端 allowForkWithSubagents 试构建通过）",
                    "output": out[:800],
                }
            if "already enabled natively" in out:
                return {
                    "checked": True,
                    "mode": "fork_toolbar_dry_build",
                    "compatible": True,
                    "summary": "官方疑似已原生开启 Fork 开关（需升级后确认子代理 Fork 行为）",
                    "output": out[:800],
                }
            return {
                "checked": True,
                "mode": "fork_toolbar_dry_build",
                "compatible": False,
                "summary": f"不兼容：{out[:160]}",
                "output": out[:800],
            }
        except Exception as exc:
            return {
                "checked": True,
                "mode": "fork_toolbar_dry_build",
                "compatible": False,
                "summary": f"构建检查异常：{exc}",
                "output": str(exc),
            }
        finally:
            out_exe.unlink(missing_ok=True)

    ls_path = stage_dir / "resources" / "bin" / "language_server.exe"
    if not ls_path.is_file():
        return {
            "checked": False,
            "mode": "builtin_fallback",
            "compatible": None,
            "summary": "未找到 language_server.exe，无法验证 fork 兼容性",
            "output": "",
        }
    ls_bytes = ls_path.read_bytes()
    ok = (b"PK\x05\x06" in ls_bytes) and (FORK_SUBAGENT_STUB_CHECK in ls_bytes)
    return {
        "checked": True,
        "mode": "builtin_fallback",
        "compatible": ok,
        "summary": "基础锚点存在（内嵌 ZIP 与 enable-fork-with-subagents 均存在）"
        if ok
        else "基础锚点缺失，Fork 补丁可能失效",
        "output": "",
    }


def _file_sig(path: Path | None) -> str:
    if path is None or not path.is_file():
        return "none"
    try:
        st = path.stat()
        return f"{path.resolve()}:{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return "err"


def _load_baseline_ls_bytes(
    current_install_dir: Path | None,
    current_version: str,
    fork_dir: Path | None,
) -> tuple[bytes | None, Path | None]:
    """Load official baseline language_server.exe bytes (prefer unpatched backup when available)."""
    if fork_dir is not None and current_version and current_version != "unknown":
        backup = fork_dir / "build" / "backups" / f"language_server-{current_version}-official.exe"
        if backup.is_file():
            try:
                return backup.read_bytes(), backup
            except OSError:
                pass
    if current_install_dir is not None:
        installed_ls = current_install_dir / "resources" / "bin" / "language_server.exe"
        if installed_ls.is_file():
            try:
                return installed_ls.read_bytes(), installed_ls
            except OSError:
                pass
    return None, None


def probe_local_environment(
    local_exe: dict[str, str],
    pending_updater: dict[str, Any],
    data_dir: Path | None = None,
    plugin_root: Path | None = None,
    allow_cache_write: bool = True,
) -> dict[str, Any]:
    """Run zero-quota local inspection of current patches, pending installer compatibility, and Guardian diffs."""
    exe_path_str = str(local_exe.get("path") or "").strip()
    current_ver = str(local_exe.get("version") or "unknown").strip()
    install_dir = Path(exe_path_str).resolve().parent if exe_path_str and Path(exe_path_str).is_file() else None

    companions = resolve_companion_tools(data_dir=data_dir, plugin_root=plugin_root)
    chat_dir = companions["chat_editor_dir"]
    fork_dir = companions["fork_dir"]
    current_state = inspect_current_patch_state(install_dir)

    updater_dir_str = str(pending_updater.get("updater_dir") or "").strip()
    pending_ver = str(pending_updater.get("pending_version") or "").strip()
    file_versions = pending_updater.get("file_versions") or {}

    target_rel = ""
    for rel in pending_updater.get("files") or []:
        rel_s = str(rel)
        if rel_s.lower().startswith("pending/"):
            if not target_rel or file_versions.get(rel_s) == pending_ver:
                target_rel = rel_s

    if not (updater_dir_str and target_rel):
        return {
            "has_pending_probe": False,
            "target_installer": "",
            "target_version": "",
            "current_patch_state": current_state,
            "companion_tools": {
                "chat_editor_found": chat_dir is not None,
                "fork_found": fork_dir is not None,
            },
            "chat_editor": {"checked": False, "compatible": None, "summary": "当前无本地待安装更新包，待下载后可自动实测"},
            "fork_patch": {"checked": False, "compatible": None, "summary": "当前无本地待安装更新包，待下载后可自动实测"},
            "guardian_diff": {"compared": False, "escalated": False},
            "probe_error": "",
        }

    installer_path = Path(updater_dir_str) / Path(target_rel)
    if not installer_path.is_file():
        return {
            "has_pending_probe": False,
            "target_installer": str(installer_path),
            "target_version": pending_ver,
            "current_patch_state": current_state,
            "companion_tools": {
                "chat_editor_found": chat_dir is not None,
                "fork_found": fork_dir is not None,
            },
            "chat_editor": {"checked": False, "compatible": None, "summary": "待安装文件不存在"},
            "fork_patch": {"checked": False, "compatible": None, "summary": "待安装文件不存在"},
            "guardian_diff": {"compared": False, "escalated": False},
            "probe_error": f"Installer not found: {installer_path}",
        }

    _, base_ls_path = _load_baseline_ls_bytes(install_dir, current_ver, fork_dir)
    cache_key_raw = "|".join(
        [
            "v1",
            _file_sig(installer_path),
            _file_sig(base_ls_path),
            _file_sig(chat_dir / "install.py" if chat_dir else None),
            _file_sig(chat_dir / "capabilities.py" if chat_dir else None),
            _file_sig(fork_dir / "fork_toolbar.py" if fork_dir else None),
        ]
    )
    cache_key = hashlib.sha256(cache_key_raw.encode("utf-8")).hexdigest()
    cache_path = (data_dir / "installer_probe_cache.json") if data_dir is not None else None

    if cache_path is not None and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("cache_key") == cache_key:
                result = dict(cached.get("result") or {})
                result["current_patch_state"] = current_state
                return result
        except (OSError, json.JSONDecodeError):
            pass

    try:
        with tempfile.TemporaryDirectory(prefix="agy_ug_probe_") as tmp:
            tmp_root = Path(tmp)
            stage_dir = tmp_root / "staged_app"
            stage_dir.mkdir(parents=True, exist_ok=True)
            _extract_nsis_7z_stage(installer_path, stage_dir)

            chat_res = _run_chat_editor_check(stage_dir, chat_dir)
            fork_res = _run_fork_check(stage_dir, fork_dir, tmp_root)

            base_ls_bytes, _ = _load_baseline_ls_bytes(install_dir, current_ver, fork_dir)
            new_ls_bytes = (stage_dir / "resources" / "bin" / "language_server.exe").read_bytes()
            guardian_res = diff_guardian_binaries(
                baseline_ls_bytes=base_ls_bytes,
                target_ls_bytes=new_ls_bytes,
                baseline_version=current_ver,
                target_version=pending_ver or "pending",
            )

            result = {
                "has_pending_probe": True,
                "target_installer": str(installer_path),
                "target_version": pending_ver,
                "current_patch_state": current_state,
                "companion_tools": {
                    "chat_editor_found": chat_dir is not None,
                    "fork_found": fork_dir is not None,
                },
                "chat_editor": chat_res,
                "fork_patch": fork_res,
                "guardian_diff": guardian_res,
                "probe_error": "",
            }
            if cache_path is not None and allow_cache_write:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_cache = cache_path.with_suffix(".json.tmp")
                    tmp_cache.write_text(
                        json.dumps({"cache_key": cache_key, "result": result}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    tmp_cache.replace(cache_path)
                except OSError:
                    pass
            return result
    except Exception as exc:
        return {
            "has_pending_probe": False,
            "target_installer": str(installer_path),
            "target_version": pending_ver,
            "current_patch_state": current_state,
            "companion_tools": {
                "chat_editor_found": chat_dir is not None,
                "fork_found": fork_dir is not None,
            },
            "chat_editor": {"checked": False, "compatible": None, "summary": f"解包探测失败：{exc}"},
            "fork_patch": {"checked": False, "compatible": None, "summary": f"解包探测失败：{exc}"},
            "guardian_diff": {"compared": False, "escalated": False},
            "probe_error": str(exc),
        }
