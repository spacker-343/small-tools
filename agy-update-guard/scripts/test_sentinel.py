#!/usr/bin/env python3
"""Unit and mock-integration tests for agy-update-guard v1.1."""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
SENTINEL_DIR = PLUGIN_ROOT / "sidecars" / "update-sentinel"

for p in (SCRIPTS_DIR, SENTINEL_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import builtin_snapshot  # noqa: E402
import mcp_server  # noqa: E402
import sentinel  # noqa: E402
import session_hook  # noqa: E402


SAMPLE_CHANGELOG = """# Changelog

## Antigravity App

### [v2.35.1](/releases?tab=hub&version=2.35.1 "View release 2.35.1")

Latest

November 24, 2026

### Resume conversations and diagnostic uploads

**Improvements:**
* Added automatic resumption on startup.

---

### [v2.35.0](/releases?tab=hub&version=2.35.0 "View release 2.35.0")

November 23, 2026

### Auto permission mode and directory scopes

**Improvements:**
* Auto permission mode now approves file reads outside your project automatically.

## Antigravity IDE integration

* Added open IDE button.

## Antigravity CLI

### [v1.5.16](/releases?tab=cli&version=1.5.16 "View release 1.5.16")

Latest

November 24, 2026

### Live step checklist

* Added `-r` flag.

## Antigravity IDE

### [v1.15.6](/releases?tab=ide&version=1.15.6 "View release 1.15.6")

November 10, 2026

### Terminal Sandboxing

* Added terminal sandbox mode.
"""

SAMPLE_CHANGELOG_V2 = """# Changelog

## Antigravity App

### [v2.36.0](/releases?tab=hub&version=2.36.0 "View release 2.36.0")

Latest

November 28, 2026

### Workspace access control update

**Improvements:**
* Updated sandbox mode and auto permission rules outside your project.

---

### [v2.35.1](/releases?tab=hub&version=2.35.1 "View release 2.35.1")

November 24, 2026

### Resume conversations and diagnostic uploads

**Improvements:**
* Added automatic resumption on startup.

## Antigravity CLI

### [v1.5.16](/releases?tab=cli&version=1.5.16 "View release 1.5.16")

Latest

November 24, 2026

### Live step checklist

* Added `-r` flag.

## Antigravity IDE

### [v1.15.6](/releases?tab=ide&version=1.15.6 "View release 1.15.6")

November 10, 2026

### Terminal Sandboxing

* Added terminal sandbox mode.
"""

SAMPLE_MODELS_V1 = """# Models

## Select a model

Intro text.

## Reasoning model

| Model | Free | Pro |
| --- | --- | --- |
| Gemini 4 Neon | ❌ | ✅ |
| Gemini 4 Argon* | ❌ | ✅ |

* Will be removed on November 30, 2026.

## Orchestrator

Details here.
"""

SAMPLE_MODELS_V2 = """# Models

## Select a model

Intro text.

## Reasoning model

| Model | Free | Pro |
| --- | --- | --- |
| Gemini 4 Neon | ❌ | ✅ |
| Gemini 4 Argon* | ❌ | ❌ |

* Will be removed on November 28, 2026.
* Gemini 4 Argon is leaving on November 28, 2026.

## Orchestrator

Details here.
"""

FORBIDDEN_SENSITIVE_TERMS = (
    "\u5916\u5ba1",
    "\u9006\u5411",
    "\u7ed5\u8fc7",
    "\u89c4\u907f",
    "\u6697\u6539",
    "\u5c01\u5835",
    "\u5bf9\u6297",
    "\u8d8a\u72f1",
    "\u7834\u89e3",
    "\u9632\u5c01",
    "\u5c01\u53f7",
    "\u5077\u6539",
)

FORBIDDEN_WINDOWS_NOTIFY_TERMS = (
    "ToastNotificationManager",
    "NotifyIcon",
    "STATIC_NOTIFY_PS_SCRIPT",
    "send_windows_notification",
    "--test-notify",
    "--notify-all",
)


class _MockHTTPHandler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, str] = {}
    conv_state: dict[str, dict] = {}
    calls: list[tuple[str, dict]] = []

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path in self.routes:
            body = self.routes[self.path].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        payload = json.loads(raw)
        self.calls.append((self.path, payload))

        if self.path == "/product.v1.ConversationService/CreateConversation":
            conv_id = self.conv_state["id"]
            self.conv_state["title"] = payload.get("title", "")
            resp = {"conversation": {"id": conv_id, "annotations": {"title": payload.get("title", "")}}}
        elif self.path == "/product.v1.ConversationService/GetConversation":
            resp = {
                "conversation": {
                    "id": self.conv_state["id"],
                    "state": {"runState": "RUN_STATE_IDLE"},
                }
            }
        elif self.path == "/product.v1.ConversationService/UpdateConversation":
            self.conv_state["updated"] = payload.get("annotations", {})
            resp = {"conversation": {"id": self.conv_state["id"], "annotations": self.conv_state["updated"]}}
        else:
            self.send_response(404)
            self.end_headers()
            return

        data = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestUpdateGuardV11(unittest.TestCase):
    def test_01_parse_changelog_multiproduct_and_multiversion(self) -> None:
        parsed = sentinel.parse_changelog(SAMPLE_CHANGELOG)
        self.assertEqual([v["version"] for v in parsed["Antigravity App"]], ["2.35.1", "2.35.0"])
        self.assertEqual([v["version"] for v in parsed["Antigravity CLI"]], ["1.5.16"])
        self.assertEqual([v["version"] for v in parsed["Antigravity IDE"]], ["1.15.6"])
        self.assertEqual(parsed["Antigravity App"][0]["date"], "November 24, 2026")
        self.assertIn("diagnostic uploads", parsed["Antigravity App"][0]["summary"])

    def test_02_semantic_version_comparison(self) -> None:
        self.assertGreater(
            sentinel.parse_version_tuple("v2.35.1"),
            sentinel.parse_version_tuple("2.35.0"),
        )
        self.assertGreater(
            sentinel.parse_version_tuple("2.35.0"),
            sentinel.parse_version_tuple("v2.9.4"),
        )
        self.assertEqual(sentinel.parse_version_tuple("v1.5.16"), (1, 5, 16))

    def test_03_models_section_and_deprecation_diff(self) -> None:
        sec1 = sentinel.extract_reasoning_models_section(SAMPLE_MODELS_V1)
        sec2 = sentinel.extract_reasoning_models_section(SAMPLE_MODELS_V2)
        self.assertTrue(sec1["found"])
        self.assertNotEqual(sec1["hash"], sec2["hash"])
        self.assertTrue(any("leaving on" in n.lower() for n in sec2["deprecation_notices"]))
        diff_txt = sentinel.diff_models_section(sec1["text"], sec2["text"])
        self.assertIn("-| Gemini 4 Argon* | ❌ | ✅ |", diff_txt)
        self.assertIn("+| Gemini 4 Argon* | ❌ | ❌ |", diff_txt)

    def test_04_builtin_snapshot_diff_and_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "builtin"
            (root / "skills" / "a").mkdir(parents=True)
            (root / "skills" / "a" / "SKILL.md").write_text("line1\nline2\n", encoding="utf-8")
            (root / "rules.json").write_text('{"k": 1}\n', encoding="utf-8")
            (root / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
            (root / "huge.md").write_text("x" * (513 * 1024), encoding="utf-8")

            base = builtin_snapshot.collect_snapshot(root)
            self.assertEqual(base["file_count"], 2)

            (root / "rules.json").unlink()
            (root / "new_skill.md").write_text("hello\n", encoding="utf-8")
            many_lines = "\n".join(f"changed_line_{i}" for i in range(150))
            (root / "skills" / "a" / "SKILL.md").write_text(many_lines + "\n", encoding="utf-8")

            curr = builtin_snapshot.collect_snapshot(root)
            diff = builtin_snapshot.diff_snapshots(
                base, curr, max_lines_per_file=80, max_total_chars=6000
            )
            self.assertTrue(diff["has_changes"])
            self.assertEqual(diff["added"], ["new_skill.md"])
            self.assertEqual(diff["removed"], ["rules.json"])
            self.assertEqual(diff["modified"], ["skills/a/SKILL.md"])
            self.assertIn("truncated", diff["diffs"]["skills/a/SKILL.md"])

            diff_small_cap = builtin_snapshot.diff_snapshots(
                base, curr, max_lines_per_file=200, max_total_chars=200
            )
            self.assertTrue(diff_small_cap["truncated"])
            self.assertIn("total diff truncated at 200 chars", diff_small_cap["diff_summary"])

    def test_05_check_zero_account_touch_and_acknowledge(self) -> None:
        _MockHTTPHandler.routes = {
            "/changelog.md": SAMPLE_CHANGELOG,
            "/models.md": SAMPLE_MODELS_V1,
        }
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockHTTPHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                builtin_dir = Path(tmp) / "builtin"
                builtin_dir.mkdir()
                (builtin_dir / "test.md").write_text("stable\n", encoding="utf-8")

                args = argparse.Namespace(
                    check=True,
                    init_baseline=False,
                    acknowledge=False,
                    audit_latest=False,
                    dry_run=False,
                    manual_create_conversation=False,
                    json=False,
                    data_dir=str(data_dir),
                    builtin_dir=str(builtin_dir),
                    model=None,
                    wait_seconds=1,
                    changelog_url=f"http://127.0.0.1:{port}/changelog.md",
                    models_url=f"http://127.0.0.1:{port}/models.md",
                )
                with mock.patch.object(sentinel, "launch_audit_conversation") as mocked_launch:
                    # Run 1: initial baseline
                    code1, res1 = sentinel.run_sentinel(args)
                    self.assertEqual(code1, 0)
                    self.assertEqual(res1["status"], "initialized")
                    self.assertFalse(res1["conversation_triggered"])
                    mocked_launch.assert_not_called()

                    # Run 2: no changes
                    code2, res2 = sentinel.run_sentinel(args)
                    self.assertEqual(code2, 0)
                    self.assertEqual(res2["status"], "no_changes")
                    self.assertFalse(res2["conversation_triggered"])
                    self.assertFalse((data_dir / "pending_notice.json").exists())
                    mocked_launch.assert_not_called()

                    # Run 3: upstream publishes v2.36.0 -> must NOT launch conversation; must write pending_notice.json
                    _MockHTTPHandler.routes["/changelog.md"] = SAMPLE_CHANGELOG_V2
                    code3, res3 = sentinel.run_sentinel(args)
                    self.assertEqual(code3, 0)
                    self.assertEqual(res3["status"], "pending_notice")
                    self.assertFalse(res3["conversation_triggered"])
                    self.assertTrue(res3["notice_written"])
                    mocked_launch.assert_not_called()

                    notice = sentinel.load_pending_notice(data_dir)
                    self.assertIsNotNone(notice)
                    assert notice is not None
                    self.assertFalse(notice["acknowledged"])
                    self.assertIn("2.36.0", notice["version_label"])
                    self.assertEqual(notice["verdict"], "HOLD")
                    self.assertIn("Audit-Run-ID:", notice["review_prompt"])

                    # State baseline must remain at 2.35.1 until acknowledged
                    state_before_ack = sentinel.load_state(data_dir / "state.json")
                    assert state_before_ack is not None
                    self.assertEqual(state_before_ack["last_seen_versions"]["Antigravity App"], "2.35.1")

                    # Acknowledge via --acknowledge
                    ack_args = argparse.Namespace(
                        check=False,
                        init_baseline=False,
                        acknowledge=True,
                        audit_latest=False,
                        dry_run=False,
                        manual_create_conversation=False,
                        json=False,
                        data_dir=str(data_dir),
                        builtin_dir=str(builtin_dir),
                        model=None,
                        wait_seconds=1,
                        changelog_url=args.changelog_url,
                        models_url=args.models_url,
                    )
                    code4, res4 = sentinel.run_sentinel(ack_args)
                    self.assertEqual(code4, 0)
                    self.assertTrue(res4["acknowledged"])

                    notice_after = sentinel.load_pending_notice(data_dir)
                    assert notice_after is not None
                    self.assertTrue(notice_after["acknowledged"])

                    state_after_ack = sentinel.load_state(data_dir / "state.json")
                    assert state_after_ack is not None
                    self.assertEqual(state_after_ack["last_seen_versions"]["Antigravity App"], "2.36.0")
        finally:
            server.shutdown()
            server.server_close()

    def test_06_prescreen_permission_sandbox(self) -> None:
        parsed = sentinel.parse_changelog(SAMPLE_CHANGELOG)
        new_versions = {"Antigravity App": [parsed["Antigravity App"][1]]}
        res = sentinel.prescreen_risks(
            new_versions_by_product=new_versions,
            models_changed=False,
            models_diff_text="",
            models_notices=[],
            builtin_diff={"has_changes": False, "added": [], "removed": [], "modified": [], "diff_summary": ""},
        )
        self.assertIn("permission_sandbox", res["dimensions"])
        self.assertEqual(res["verdict"], "HOLD")

    def test_07_parse_audit_result_valid_and_fallback(self) -> None:
        valid_reply = """分析正文...
[AUDIT_RESULT]
VERDICT: CAUTION
HEADLINE: 权限范围微调建议评估后升级
RISK_DIMENSIONS: permission_sandbox, telemetry_upload
[/AUDIT_RESULT]
"""
        ok = sentinel.parse_audit_result(valid_reply, "HOLD", "默认结论", ["permission_sandbox"])
        self.assertTrue(ok["parsed_ok"])
        self.assertEqual(ok["verdict"], "CAUTION")
        self.assertEqual(ok["headline"], "权限范围微调建议评估后升级")
        self.assertEqual(ok["risk_dimensions"], ["permission_sandbox", "telemetry_upload"])

        broken_reply = "超时或未输出结构化块"
        fb = sentinel.parse_audit_result(broken_reply, "HOLD", "预筛兜底结论", ["builtin_prompt_change"])
        self.assertFalse(fb["parsed_ok"])
        self.assertEqual(fb["verdict"], "HOLD")
        self.assertEqual(fb["headline"], "预筛兜底结论")
        self.assertEqual(fb["risk_dimensions"], ["builtin_prompt_change"])

    def test_08_invalid_upstream_aborts_without_polluting_state(self) -> None:
        _MockHTTPHandler.routes = {
            "/changelog.md": "<html><body>Rate limited</body></html>",
            "/models.md": "<html><body>Maintenance</body></html>",
        }
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockHTTPHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                builtin_dir = Path(tmp) / "builtin"
                builtin_dir.mkdir()
                state_path = data_dir / "state.json"
                original_state = {
                    "last_seen_versions": {"Antigravity App": "2.35.1"},
                    "models_section_hash": "abc123",
                    "models_section_text": "## Reasoning model\nstable",
                    "local_exe_version": "2.35.1",
                    "audit_history": [],
                }
                sentinel.save_state(state_path, original_state)

                args = argparse.Namespace(
                    check=True,
                    init_baseline=False,
                    acknowledge=False,
                    audit_latest=False,
                    dry_run=False,
                    manual_create_conversation=False,
                    json=False,
                    data_dir=str(data_dir),
                    builtin_dir=str(builtin_dir),
                    model=None,
                    wait_seconds=1,
                    changelog_url=f"http://127.0.0.1:{port}/changelog.md",
                    models_url=f"http://127.0.0.1:{port}/models.md",
                )
                with self.assertRaises(RuntimeError):
                    sentinel.run_sentinel(args)
                self.assertEqual(sentinel.load_state(state_path), original_state)
        finally:
            server.shutdown()
            server.server_close()

    def test_09_session_hook_empty_and_unread_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(parents=True)

            # Recent state and no unread notice -> {} and no background spawn
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            sentinel.save_state(
                data_dir / "state.json",
                {"last_checked_at": now_iso, "last_seen_versions": {"Antigravity App": "2.35.1"}},
            )
            spawn_calls: list[tuple] = []
            no_pending = {
                "has_pending": False,
                "updater_dir": "",
                "files": [],
                "file_versions": {},
                "pending_version": "",
                "has_newer_pending": False,
            }
            with mock.patch.object(
                session_hook.subprocess,
                "Popen",
                side_effect=lambda *a, **kw: spawn_calls.append((a, kw)),
            ):
                empty_out = session_hook.build_hook_output(
                    data_dir,
                    spawn_check=True,
                    local_exe={"path": "", "version": "2.35.1"},
                    pending_updater=no_pending,
                )
                self.assertEqual(empty_out, {})
                self.assertEqual(len(spawn_calls), 0)

            # Stale state (> 4 hours) -> spawns detached check without blocking
            stale_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).isoformat()
            sentinel.save_state(
                data_dir / "state.json",
                {"last_checked_at": stale_iso, "last_seen_versions": {"Antigravity App": "2.35.1"}},
            )
            # Unread notice present -> valid injectSteps
            (data_dir / "pending_notice.json").write_text(
                json.dumps(
                    {
                        "notice_id": "n-1",
                        "acknowledged": False,
                        "version_label": "App v2.36.0",
                        "verdict": "HOLD",
                        "headline": "命中权限与沙箱变更预检",
                        "dimension_labels": ["权限与沙箱"],
                        "pending_updater": {"has_pending": True, "files": ["pending/Antigravity.exe"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                session_hook.subprocess,
                "Popen",
                side_effect=lambda *a, **kw: spawn_calls.append((a, kw)),
            ):
                hook_out = session_hook.build_hook_output(
                    data_dir,
                    spawn_check=True,
                    local_exe={"path": "", "version": "2.35.1"},
                    pending_updater=no_pending,
                )

            self.assertEqual(len(spawn_calls), 1)
            self.assertTrue((data_dir / "check.lock").exists())
            steps = hook_out.get("injectSteps")
            self.assertIsInstance(steps, list)
            self.assertEqual(len(steps), 1)
            sys_msg = steps[0]["systemMessage"]
            self.assertEqual(sys_msg["systemMessage"], "")
            md = sys_msg["metadata"]["user_markdown"]
            self.assertIn("App v2.36.0", md)
            self.assertIn("customization://agy-update-guard", md)
            self.assertIn("🚀 新建对话审查本次更新", md)

            # Acknowledged notice -> {}
            (data_dir / "pending_notice.json").write_text(
                json.dumps({"notice_id": "n-1", "acknowledged": True}),
                encoding="utf-8",
            )
            ack_out = session_hook.build_hook_output(
                data_dir,
                spawn_check=False,
                local_exe={"path": "", "version": "2.35.1"},
                pending_updater=no_pending,
            )
            self.assertEqual(ack_out, {})

    def test_10_mcp_server_e2e_protocol(self) -> None:
        _MockHTTPHandler.routes = {
            "/changelog.md": SAMPLE_CHANGELOG_V2,
            "/models.md": SAMPLE_MODELS_V1,
        }
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockHTTPHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                builtin_dir = Path(tmp) / "builtin"
                builtin_dir.mkdir()
                (builtin_dir / "SKILL.md").write_text("baseline\n", encoding="utf-8")

                # Seed baseline v2.35.1 + unread notice v2.36.0
                sentinel.save_state(
                    data_dir / "state.json",
                    {
                        "last_seen_versions": {"Antigravity App": "2.35.1"},
                        "models_section_hash": "oldhash",
                        "models_section_text": "## Reasoning model\nold",
                        "last_checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                )
                (data_dir / "pending_notice.json").write_text(
                    json.dumps(
                        {
                            "notice_id": "notice-mcp-1",
                            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "acknowledged": False,
                            "version_label": "App v2.36.0",
                            "verdict": "HOLD",
                            "headline": "命中权限与沙箱变更预检",
                            "dimensions": ["permission_sandbox"],
                            "dimension_labels": ["权限与沙箱"],
                            "latest_versions": {"Antigravity App": "2.36.0"},
                            "new_versions": {"Antigravity App": [{"version": "2.36.0", "tag": "v2.36.0", "summary": "Workspace access control update"}]},
                            "models_info": {"found": True, "hash": "newhash", "changed": False, "deprecation_notices": [], "table_rows_count": 2},
                            "builtin_diff": {"has_changes": False, "added": [], "removed": [], "modified": [], "current_file_count": 1},
                            "review_prompt": "Audit-Run-ID: notice-mcp-1\n请审查更新。",
                            "pending_baseline": {
                                "last_seen_versions": {"Antigravity App": "2.36.0"},
                                "models_section_hash": "newhash",
                                "models_section_text": "## Reasoning model\nnew",
                                "local_exe_version": "2.35.1",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                # Part A: Subprocess NDJSON E2E check
                requests = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ui://update-guard/app.html"}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "open_update_guard", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "show_update_card", "arguments": {"refresh": True}}},
                    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "app_acknowledge_notice", "arguments": {}}},
                ]
                stdin_bytes = b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8") for r in requests)
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(SCRIPTS_DIR / "mcp_server.py"),
                        "--data-dir",
                        str(data_dir),
                        "--builtin-dir",
                        str(builtin_dir),
                        "--changelog-url",
                        f"http://127.0.0.1:{port}/changelog.md",
                        "--models-url",
                        f"http://127.0.0.1:{port}/models.md",
                    ],
                    input=stdin_bytes,
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr.decode("utf-8", errors="replace"))
                lines = [ln for ln in proc.stdout.decode("utf-8").splitlines() if ln.strip()]
                responses = {obj["id"]: obj for obj in (json.loads(ln) for ln in lines)}

                # 1. initialize
                init_res = responses[1]["result"]
                self.assertIn("tools", init_res["capabilities"])
                self.assertIn("resources", init_res["capabilities"])
                self.assertIn("io.modelcontextprotocol/ui", init_res["capabilities"]["extensions"])

                # 2 & 3. resources
                res_list = responses[2]["result"]["resources"]
                self.assertEqual(res_list[0]["uri"], "ui://update-guard/app.html")
                self.assertEqual(res_list[0]["mimeType"], "text/html;profile=mcp-app")
                res_read = responses[3]["result"]["contents"][0]
                self.assertEqual(res_read["mimeType"], "text/html;profile=mcp-app")
                self.assertGreater(len(res_read["text"]), 1000)

                # 4. tools/list
                tools = {t["name"]: t for t in responses[4]["result"]["tools"]}
                self.assertIn("open_update_guard", tools)
                self.assertIn("show_update_card", tools)
                self.assertIn("app_refresh_updates", tools)
                self.assertIn("app_acknowledge_notice", tools)
                entrypoints = tools["open_update_guard"]["_meta"]["com.google.antigravity/ui"]["entrypoints"]
                self.assertEqual(entrypoints, [{"type": "aux_pane", "title": "Update Guard"}])
                self.assertEqual(tools["app_acknowledge_notice"]["_meta"]["ui"]["visibility"], ["app"])

                # 5 & 6. tools/call open_update_guard & show_update_card
                open_sc = responses[5]["result"]["structuredContent"]
                self.assertTrue(open_sc["has_unread_notice"])
                self.assertIn("Audit-Run-ID:", open_sc["review_prompt"])
                show_sc = responses[6]["result"]["structuredContent"]
                self.assertEqual(show_sc["latest_versions"]["Antigravity App"], "2.36.0")

                # 7. app_acknowledge_notice marks acknowledged & updates state.json
                ack_sc = responses[7]["result"]["structuredContent"]
                self.assertFalse(ack_sc["has_unread_notice"])
                self.assertTrue(ack_sc["acknowledged"])
                saved_notice = sentinel.load_pending_notice(data_dir)
                assert saved_notice is not None
                self.assertTrue(saved_notice["acknowledged"])
                saved_state = sentinel.load_state(data_dir / "state.json")
                assert saved_state is not None
                self.assertEqual(saved_state["last_seen_versions"]["Antigravity App"], "2.36.0")

                # Part B: Content-Length framing in-memory stream test
                ping_req = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}).encode("utf-8")
                framed_in = io.BytesIO(f"Content-Length: {len(ping_req)}\r\n\r\n".encode("ascii") + ping_req)
                framed_out = io.BytesIO()
                srv = mcp_server.UpdateGuardMcpServer(data_dir=data_dir, builtin_dir=builtin_dir)
                mcp_server.serve_stdio(srv, stdin_buf=framed_in, stdout_buf=framed_out)
                raw_out = framed_out.getvalue().decode("utf-8")
                self.assertTrue(raw_out.startswith("Content-Length: "))
                self.assertIn('"id": 99', raw_out)
        finally:
            server.shutdown()
            server.server_close()

    def test_11_update_guard_app_html_integrity(self) -> None:
        html_path = PLUGIN_ROOT / "assets" / "update_guard_app.html"
        self.assertTrue(html_path.is_file(), f"Missing {html_path}")
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("com.google.antigravity/message", html)
        self.assertIn("new", html)
        self.assertIn("app_acknowledge_notice", html)
        self.assertIn("app_refresh_updates", html)
        self.assertNotRegex(html, r'<script[^>]+src=["\']https?://')
        self.assertNotRegex(html, r'<link[^>]+href=["\']https?://')

    def test_12_no_windows_notify_remnants_and_zero_sensitive_words(self) -> None:
        self.assertFalse((PLUGIN_ROOT / "node_modules").exists(), "node_modules must not remain in plugin dir")
        scan_files: list[Path] = []
        for path in PLUGIN_ROOT.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if path.resolve() == Path(__file__).resolve():
                continue
            scan_files.append(path)

        self.assertGreaterEqual(len(scan_files), 10)
        for file_path in scan_files:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            for term in FORBIDDEN_WINDOWS_NOTIFY_TERMS:
                self.assertNotIn(term, content, f"Windows notify remnant '{term}' found in {file_path}")
            for term in FORBIDDEN_SENSITIVE_TERMS:
                self.assertNotIn(term, content, f"Sensitive term found in {file_path}")

        root_readme = PLUGIN_ROOT.parent / "README.md"
        if root_readme.is_file():
            for line in root_readme.read_text(encoding="utf-8").splitlines():
                if "agy-update-guard" in line:
                    for term in FORBIDDEN_SENSITIVE_TERMS:
                        self.assertNotIn(term, line, f"Sensitive term found in root README line: {line}")

    def test_13_manual_conversation_service_http_mock_flow(self) -> None:
        conv_id = "11111111-2222-3333-4444-555555555555"
        _MockHTTPHandler.conv_state = {"id": conv_id, "title": "", "updated": {}}
        _MockHTTPHandler.calls = []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockHTTPHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                brain_dir = Path(tmp) / "brain"
                logs_dir = brain_dir / conv_id / ".system_generated" / "logs"
                logs_dir.mkdir(parents=True)
                run_id = "run-uuid-9999"
                transcript_lines = [
                    {"source": "USER", "type": "USER_MESSAGE", "content": f"Audit-Run-ID: {run_id}"},
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "content": (
                            "审查完成。\n[AUDIT_RESULT]\nVERDICT: HOLD\n"
                            "HEADLINE: 新增目录沙箱限制建议暂缓升级\n"
                            "RISK_DIMENSIONS: permission_sandbox\n[/AUDIT_RESULT]\n"
                        ),
                    },
                ]
                (logs_dir / "transcript_full.jsonl").write_text(
                    "\n".join(json.dumps(x, ensure_ascii=False) for x in transcript_lines) + "\n",
                    encoding="utf-8",
                )

                env_patch = {
                    "ANTIGRAVITY_LS_ADDRESS": f"127.0.0.1:{port}",
                    "ANTIGRAVITY_CSRF_TOKEN": "test-csrf-token",
                    "ANTIGRAVITY_PROJECT_ID": "proj-abc",
                }
                with mock.patch.dict(os.environ, env_patch, clear=False):
                    launch = sentinel.launch_audit_conversation(
                        title="初始标题",
                        model="gemini-4-hydrogen-high",
                        prompt_text=f"Audit-Run-ID: {run_id}",
                    )
                    self.assertEqual(launch["channel"], "conversation_service")
                    self.assertEqual(launch["conversation_id"], conv_id)

                    final = sentinel.wait_and_finalize_audit(
                        launch_info=launch,
                        run_id=run_id,
                        version_label="App v2.35.1",
                        prescreen={
                            "verdict": "CAUTION",
                            "fallback_headline": "兜底",
                            "dimensions": ["permission_sandbox"],
                        },
                        wait_seconds=5,
                        poll_interval=0.05,
                        brain_dir=brain_dir,
                    )

                self.assertTrue(final["parsed_ok"])
                self.assertEqual(final["verdict"], "HOLD")
                self.assertTrue(final["pinned"])
                self.assertTrue(final["starred"])
                self.assertTrue(final["updated_remote"])
                self.assertIn("🛑 [建议暂缓升级] App v2.35.1", final["title"])

                called_paths = [p for p, _ in _MockHTTPHandler.calls]
                self.assertEqual(
                    called_paths,
                    [
                        "/product.v1.ConversationService/CreateConversation",
                        "/product.v1.ConversationService/GetConversation",
                        "/product.v1.ConversationService/UpdateConversation",
                    ],
                )
        finally:
            server.shutdown()
            server.server_close()

    def test_14_agentapi_bat_resolves_native_exe_and_preserves_multiline_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "language_server.exe"
            fake_exe.write_text("stub", encoding="utf-8")
            fake_bat = Path(tmp) / "agentapi.bat"
            fake_bat.write_text(f'@echo off\n"{fake_exe}" agentapi %*\n', encoding="utf-8")

            captured: dict[str, object] = {}

            def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                return argparse.Namespace(
                    returncode=0,
                    stdout="11111111-2222-3333-4444-555555555555",
                    stderr="",
                )

            multiline_prompt = "Line1 %TEMP% & calc.exe\nLine2 | echo ^caret > out.txt"
            with (
                mock.patch.dict(os.environ, {"ANTIGRAVITY_LS_ADDRESS": "", "ANTIGRAVITY_CSRF_TOKEN": ""}),
                mock.patch.object(sentinel.shutil, "which", return_value=str(fake_bat)),
                mock.patch.object(sentinel.subprocess, "run", side_effect=fake_run),
            ):
                launch = sentinel.launch_audit_conversation(
                    title="Test Title",
                    model="gemini-4-hydrogen-high",
                    prompt_text=multiline_prompt,
                )

            self.assertEqual(launch["channel"], "agentapi")
            self.assertEqual(captured["kwargs"]["shell"], False)
            cmd = captured["cmd"]
            self.assertEqual(cmd[:2], [str(fake_exe), "agentapi"])
            self.assertEqual(cmd[-1], multiline_prompt)

    def test_15_acknowledge_and_cached_prompt_use_live_local_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            builtin_dir = Path(tmp) / "builtin"
            builtin_dir.mkdir()
            (builtin_dir / "SKILL.md").write_text("baseline\n", encoding="utf-8")

            stale_prompt = (
                "Audit-Run-ID: old-run-1\n\n"
                "## 本机客户端环境\n"
                "- 本机 Antigravity.exe 版本: `2.35.1` (`C:\\Old\\Antigravity.exe`)\n"
                "- 本地更新缓存 (`antigravity-updater`): 未检测到待安装 `.exe` 文件。\n\n"
                "## 本地预检命中情况\n"
                "- 预检级别: `SAFE`\n"
            )
            sentinel.save_state(
                data_dir / "state.json",
                {
                    "last_seen_versions": {"Antigravity App": "2.35.1"},
                    "local_exe_version": "2.35.1",
                    "last_checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
            )
            (data_dir / "pending_notice.json").write_text(
                json.dumps(
                    {
                        "notice_id": "old-run-1",
                        "created_at": "2026-11-26T04:15:00+00:00",
                        "acknowledged": False,
                        "version_label": "App v2.35.2",
                        "verdict": "SAFE",
                        "headline": "常规修复",
                        "local_exe": {"path": "C:\\Old\\Antigravity.exe", "version": "2.35.1"},
                        "latest_versions": {"Antigravity App": "2.35.2"},
                        "review_prompt": stale_prompt,
                        "pending_baseline": {
                            "last_seen_versions": {"Antigravity App": "2.35.2"},
                            "local_exe_version": "2.35.1",
                            "pending_updater_version": "2.35.2",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            live_exe = {"path": "C:\\Live\\Antigravity.exe", "version": "2.35.2"}
            live_pending = {
                "has_pending": True,
                "updater_dir": "C:\\Live\\antigravity-updater",
                "files": ["installer.exe", "pending/Antigravity-x64.exe"],
                "file_versions": {
                    "installer.exe": "2.35.2",
                    "pending/Antigravity-x64.exe": "2.36.0",
                },
                "pending_version": "2.36.0",
                "has_newer_pending": True,
            }
            with (
                mock.patch.object(sentinel, "get_local_antigravity_version", return_value=live_exe),
                mock.patch.object(sentinel, "check_pending_updater_files", return_value=live_pending),
            ):
                sc = sentinel.collect_guard_structured_content(
                    data_dir=data_dir,
                    refresh=False,
                    builtin_dir=builtin_dir,
                )
                self.assertEqual(sc["local_exe"]["version"], "2.35.2")
                self.assertIn("`2.35.2`", sc["review_prompt"])
                self.assertNotIn("`2.35.1`", sc["review_prompt"])
                self.assertIn("pending/Antigravity-x64.exe (v2.36.0)", sc["review_prompt"])

                sentinel.acknowledge_pending_notice(data_dir=data_dir, builtin_dir=builtin_dir)
                saved_state = sentinel.load_state(data_dir / "state.json")
                assert saved_state is not None
                self.assertEqual(saved_state["local_exe_version"], "2.35.2")

    def test_16_session_hook_alerts_and_bypasses_cooldown_on_newer_pending_installer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            sentinel.save_state(
                data_dir / "state.json",
                {
                    "last_checked_at": now_iso,
                    "last_seen_versions": {"Antigravity App": "2.35.2"},
                    "local_exe_version": "2.35.2",
                    "last_seen_pending_version": "2.35.2",
                },
            )
            (data_dir / "pending_notice.json").write_text(
                json.dumps(
                    {
                        "notice_id": "old-2352",
                        "acknowledged": True,
                        "latest_versions": {"Antigravity App": "2.35.2"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            live_exe = {"path": "C:\\Live\\Antigravity.exe", "version": "2.35.2"}
            newer_pending = {
                "has_pending": True,
                "updater_dir": "C:\\Live\\antigravity-updater",
                "files": ["installer.exe", "pending/Antigravity-x64.exe"],
                "file_versions": {
                    "installer.exe": "2.35.2",
                    "pending/Antigravity-x64.exe": "2.36.0",
                },
                "pending_version": "2.36.0",
                "has_newer_pending": True,
            }

            spawn_calls: list[tuple] = []
            with mock.patch.object(
                session_hook.subprocess,
                "Popen",
                side_effect=lambda *a, **kw: spawn_calls.append((a, kw)),
            ):
                out = session_hook.build_hook_output(
                    data_dir,
                    spawn_check=True,
                    local_exe=live_exe,
                    pending_updater=newer_pending,
                )

            # Even within 4h cooldown, newer pending installer forces background check and alerts immediately
            self.assertEqual(len(spawn_calls), 1)
            md = out["injectSteps"][0]["systemMessage"]["metadata"]["user_markdown"]
            self.assertIn("App v2.36.0", md)
            self.assertIn("v2.35.2", md)
            self.assertIn("pending/Antigravity-x64.exe (v2.36.0)", md)

            # Once acknowledged in state.json for 2.36.0, no duplicate pending alert
            sentinel.save_state(
                data_dir / "state.json",
                {
                    "last_checked_at": now_iso,
                    "last_seen_versions": {"Antigravity App": "2.35.2"},
                    "local_exe_version": "2.35.2",
                    "last_seen_pending_version": "2.36.0",
                },
            )
            ack_out = session_hook.build_hook_output(
                data_dir,
                spawn_check=False,
                local_exe=live_exe,
                pending_updater=newer_pending,
            )
            self.assertEqual(ack_out, {})

    def test_17_sentinel_check_triggers_notice_on_newer_pending_installer_without_changelog_bump(self) -> None:
        _MockHTTPHandler.routes = {
            "/changelog.md": SAMPLE_CHANGELOG_V2,
            "/models.md": SAMPLE_MODELS_V1,
        }
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockHTTPHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                builtin_dir = Path(tmp) / "builtin"
                builtin_dir.mkdir()
                (builtin_dir / "SKILL.md").write_text("baseline\n", encoding="utf-8")

                no_pending = {
                    "has_pending": False,
                    "updater_dir": "",
                    "files": [],
                    "file_versions": {},
                    "pending_version": "",
                    "has_newer_pending": False,
                }
                live_exe = {"path": "C:\\Live\\Antigravity.exe", "version": "2.36.0"}

                with (
                    mock.patch.object(sentinel, "get_local_antigravity_version", return_value=live_exe),
                    mock.patch.object(sentinel, "check_pending_updater_files", return_value=no_pending),
                ):
                    code, _ = sentinel.run_sentinel(
                        argparse.Namespace(
                            check=False,
                            init_baseline=True,
                            acknowledge=False,
                            audit_latest=False,
                            dry_run=False,
                            manual_create_conversation=False,
                            data_dir=str(data_dir),
                            builtin_dir=str(builtin_dir),
                            changelog_url=f"http://127.0.0.1:{port}/changelog.md",
                            models_url=f"http://127.0.0.1:{port}/models.md",
                            model=None,
                            wait_seconds=5,
                        )
                    )
                    self.assertEqual(code, 0)

                newer_pending = {
                    "has_pending": True,
                    "updater_dir": "C:\\Live\\antigravity-updater",
                    "files": ["pending/Antigravity-x64.exe"],
                    "file_versions": {"pending/Antigravity-x64.exe": "2.37.0"},
                    "pending_version": "2.37.0",
                    "has_newer_pending": True,
                }
                with (
                    mock.patch.object(sentinel, "get_local_antigravity_version", return_value=live_exe),
                    mock.patch.object(sentinel, "check_pending_updater_files", return_value=newer_pending),
                ):
                    code, res = sentinel.run_sentinel(
                        argparse.Namespace(
                            check=True,
                            init_baseline=False,
                            acknowledge=False,
                            audit_latest=False,
                            dry_run=False,
                            manual_create_conversation=False,
                            data_dir=str(data_dir),
                            builtin_dir=str(builtin_dir),
                            changelog_url=f"http://127.0.0.1:{port}/changelog.md",
                            models_url=f"http://127.0.0.1:{port}/models.md",
                            model=None,
                            wait_seconds=5,
                        )
                    )
                self.assertEqual(code, 0)
                self.assertEqual(res["status"], "pending_notice")
                self.assertTrue(res["notice_written"])
                self.assertIn("2.37.0", res["version_label"])
                self.assertEqual(res["verdict"], "CAUTION")

                # If state already acknowledged App 2.37.0 (even with empty last_seen_pending_version),
                # sentinel --check should stay consistent with session_hook and not re-alert.
                sentinel.save_state(
                    data_dir / "state.json",
                    {
                        "last_seen_versions": {
                            "Antigravity App": "2.37.0",
                            "Antigravity CLI": "1.5.16",
                            "Antigravity IDE": "2.8.0",
                        },
                        "models_section_hash": sentinel.extract_reasoning_models_section(SAMPLE_MODELS_V1)["hash"],
                        "models_section_text": sentinel.extract_reasoning_models_section(SAMPLE_MODELS_V1)["text"],
                        "local_exe_version": "2.36.0",
                        "last_seen_pending_version": "",
                        "last_checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                )
                with (
                    mock.patch.object(sentinel, "get_local_antigravity_version", return_value=live_exe),
                    mock.patch.object(sentinel, "check_pending_updater_files", return_value=newer_pending),
                ):
                    code2, res2 = sentinel.run_sentinel(
                        argparse.Namespace(
                            check=True,
                            init_baseline=False,
                            acknowledge=False,
                            audit_latest=False,
                            dry_run=False,
                            manual_create_conversation=False,
                            data_dir=str(data_dir),
                            builtin_dir=str(builtin_dir),
                            changelog_url=f"http://127.0.0.1:{port}/changelog.md",
                            models_url=f"http://127.0.0.1:{port}/models.md",
                            model=None,
                            wait_seconds=5,
                        )
                    )
                self.assertEqual(code2, 0)
                self.assertEqual(res2["status"], "no_changes")
                self.assertFalse(res2["notice_written"])
        finally:
            server.shutdown()
            server.server_close()

    def test_18_check_pending_updater_files_and_mixed_product_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upd_dir = Path(tmp) / "antigravity-updater"
            pending_dir = upd_dir / "pending"
            pending_dir.mkdir(parents=True)
            root_exe = upd_dir / "installer.exe"
            old_pending = pending_dir / "old.exe"
            new_pending = pending_dir / "Antigravity-x64.exe"
            root_exe.write_bytes(b"stub")
            old_pending.write_bytes(b"stub")
            new_pending.write_bytes(b"stub")

            ver_map = {
                root_exe.resolve(): "2.35.2",
                old_pending.resolve(): "2.35.3",
                new_pending.resolve(): "2.36.0",
            }

            with mock.patch.object(
                sentinel,
                "_read_pe_version_ctypes",
                side_effect=lambda p: ver_map.get(Path(p).resolve(), "unknown"),
            ):
                res_newer = sentinel.check_pending_updater_files(
                    updater_dir=upd_dir, local_version="2.35.2"
                )
                self.assertTrue(res_newer["has_pending"])
                self.assertEqual(res_newer["pending_version"], "2.36.0")
                self.assertTrue(res_newer["has_newer_pending"])
                self.assertEqual(
                    res_newer["file_versions"]["pending/Antigravity-x64.exe"], "2.36.0"
                )
                self.assertEqual(res_newer["file_versions"]["installer.exe"], "2.35.2")

                res_same = sentinel.check_pending_updater_files(
                    updater_dir=upd_dir, local_version="2.36.0"
                )
                self.assertEqual(res_same["pending_version"], "2.36.0")
                self.assertFalse(res_same["has_newer_pending"])

        # Mixed case: CLI has a new version while App only has a local pending installer
        label = sentinel._build_version_label(
            new_versions={"Antigravity CLI": [{"tag": "v1.5.17", "version": "1.5.17"}]},
            parsed_changelog={},
            models_changed=False,
            builtin_changed=False,
            pending_installer_version="2.36.0",
        )
        self.assertIn("App v2.36.0 (本地待安装)", label)
        self.assertIn("CLI v1.5.17", label)


if __name__ == "__main__":
    unittest.main()
