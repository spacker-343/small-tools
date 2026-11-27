#!/usr/bin/env python3
"""Pure-stdlib MCP stdio JSON-RPC 2.0 server with MCP App UI support for Update Guard."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, BinaryIO

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
SENTINEL_DIR = PLUGIN_ROOT / "sidecars" / "update-sentinel"
APP_HTML_PATH = PLUGIN_ROOT / "assets" / "update_guard_app.html"
UI_RESOURCE_URI = "ui://update-guard/app.html"
UI_MIME_TYPE = "text/html;profile=mcp-app"

for _p in (SCRIPTS_DIR, SENTINEL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import sentinel  # noqa: E402
import update_blocker  # noqa: E402


def _resolve_data_dir(cli_data_dir: str | None = None) -> Path:
    if cli_data_dir:
        return Path(cli_data_dir)
    env_dir = os.environ.get("AGY_UPDATE_GUARD_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return sentinel.resolve_data_dir()


def _load_app_html() -> str:
    override = os.environ.get("AGY_UPDATE_GUARD_APP_HTML", "").strip()
    target = Path(override) if override else APP_HTML_PATH
    if not target.is_file():
        raise FileNotFoundError(f"MCP App HTML not found: {target}")
    return target.read_text(encoding="utf-8")


def _format_text_summary(payload: dict[str, Any]) -> str:
    status = "有待审查更新" if payload.get("has_unread_notice") else "已确认基线"
    verdict = payload.get("verdict", "SAFE")
    label = payload.get("version_label", "Update Guard")
    headline = payload.get("headline", "")
    local_ver = (payload.get("local_exe") or {}).get("version", "unknown")
    latest = payload.get("latest_versions") or {}
    pending = payload.get("pending_updater") or {}
    lines = [
        f"Update Guard 状态：{status} | 预检等级：{verdict} | 目标：{label}",
        f"摘要：{headline}",
        f"本机版本：{local_ver} | 线上最新：{json.dumps(latest, ensure_ascii=False)}",
    ]
    if pending.get("has_pending"):
        lines.append(f"待安装更新包：{', '.join(pending.get('files') or [])}")
    if payload.get("error"):
        lines.append(f"刷新提示：{payload['error']}")
    return "\n".join(lines)


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "open_update_guard",
            "title": "Update Guard",
            "description": (
                "Open the Update Guard side pane showing current update notice, local client "
                "version, builtin snapshot diff, and one-click new-conversation review buttons."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "description": "Optional. When true, fetch latest online changelog and models.md.",
                    }
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {"resourceUri": UI_RESOURCE_URI},
                "com.google.antigravity/ui": {
                    "entrypoints": [
                        {"type": "aux_pane", "title": "Update Guard"},
                    ]
                },
            },
        },
        {
            "name": "show_update_card",
            "title": "Update Guard Status Card",
            "description": (
                "Render an inline interactive Update Guard status card in the conversation "
                "with one-click new-conversation review and zero-quota refresh buttons."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "description": "Optional. Defaults to true to fetch latest online preflight data.",
                    }
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {"resourceUri": UI_RESOURCE_URI},
            },
        },
        {
            "name": "app_refresh_updates",
            "title": "Refresh Update Guard",
            "description": (
                "App-only tool to fetch latest changelog.md, models.md, and local builtin snapshot "
                "with zero LLM quota."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "include_latest": {
                        "type": "boolean",
                        "description": "Optional. Defaults to true so latest release info is always available.",
                    }
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {"visibility": ["app"]},
            },
        },
        {
            "name": "app_acknowledge_notice",
            "title": "Acknowledge Update Notice",
            "description": (
                "App-only tool to mark pending_notice.json as acknowledged and advance local baseline."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {"visibility": ["app"]},
            },
        },
        {
            "name": "app_set_update_lock",
            "title": "Toggle Auto-Update Lock",
            "description": (
                "App-only tool to purge cached installers and lock or unlock Antigravity auto-updates."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "locked": {
                        "type": "boolean",
                        "description": "True to purge pending installers and block updates; false to restore.",
                    }
                },
                "required": ["locked"],
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {"visibility": ["app"]},
            },
        },
    ]


class UpdateGuardMcpServer:
    """Stateful request dispatcher for Update Guard MCP JSON-RPC 2.0 server."""

    def __init__(
        self,
        data_dir: Path | None = None,
        builtin_dir: Path | None = None,
        changelog_url: str | None = None,
        models_url: str | None = None,
    ) -> None:
        self.data_dir = data_dir or _resolve_data_dir()
        self.builtin_dir = builtin_dir
        self.changelog_url = (
            changelog_url
            or os.environ.get("AGY_UPDATE_GUARD_CHANGELOG_URL", "").strip()
            or sentinel.CHANGELOG_URL
        )
        self.models_url = (
            models_url
            or os.environ.get("AGY_UPDATE_GUARD_MODELS_URL", "").strip()
            or sentinel.MODELS_URL
        )

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        # JSON-RPC notifications have no id and require no response.
        if req_id is None:
            return None

        try:
            if method == "initialize":
                proto = params.get("protocolVersion") or "2024-11-05"
                result = {
                    "protocolVersion": proto,
                    "capabilities": {
                        "tools": {},
                        "resources": {},
                        "extensions": {
                            "io.modelcontextprotocol/ui": {
                                "mimeTypes": [UI_MIME_TYPE],
                            }
                        },
                    },
                    "serverInfo": {
                        "name": "agy-update-guard",
                        "version": "1.1.0",
                    },
                }
                return {"jsonrpc": "2.0", "id": req_id, "result": result}

            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            if method == "resources/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "resources": [
                            {
                                "uri": UI_RESOURCE_URI,
                                "name": "Update Guard UI",
                                "description": (
                                    "Interactive status and one-click review card for Update Guard."
                                ),
                                "mimeType": UI_MIME_TYPE,
                                "_meta": {"ui": {"prefersBorder": True}},
                            }
                        ]
                    },
                }

            if method == "resources/read":
                uri = params.get("uri")
                if uri != UI_RESOURCE_URI:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": f"Unknown resource URI: {uri}"},
                    }
                html = _load_app_html()
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "contents": [
                            {
                                "uri": UI_RESOURCE_URI,
                                "mimeType": UI_MIME_TYPE,
                                "text": html,
                                "_meta": {"ui": {"prefersBorder": True}},
                            }
                        ]
                    },
                }

            if method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": _tool_definitions()},
                }

            if method == "tools/call":
                tool_name = params.get("name")
                args = params.get("arguments") or {}
                result = self._call_tool(tool_name, args)
                return {"jsonrpc": "2.0", "id": req_id, "result": result}

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            }

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "open_update_guard":
            refresh = bool(args.get("refresh", False))
            payload = sentinel.collect_guard_structured_content(
                data_dir=self.data_dir,
                refresh=refresh,
                include_latest=True,
                changelog_url=self.changelog_url,
                models_url=self.models_url,
                builtin_dir=self.builtin_dir,
            )
            return {
                "content": [{"type": "text", "text": _format_text_summary(payload)}],
                "structuredContent": payload,
            }

        if name == "show_update_card":
            refresh = bool(args.get("refresh", True))
            payload = sentinel.collect_guard_structured_content(
                data_dir=self.data_dir,
                refresh=refresh,
                include_latest=True,
                changelog_url=self.changelog_url,
                models_url=self.models_url,
                builtin_dir=self.builtin_dir,
            )
            return {
                "content": [{"type": "text", "text": _format_text_summary(payload)}],
                "structuredContent": payload,
            }

        if name == "app_refresh_updates":
            include_latest = bool(args.get("include_latest", True))
            payload = sentinel.collect_guard_structured_content(
                data_dir=self.data_dir,
                refresh=True,
                include_latest=include_latest,
                changelog_url=self.changelog_url,
                models_url=self.models_url,
                builtin_dir=self.builtin_dir,
            )
            return {
                "content": [{"type": "text", "text": _format_text_summary(payload)}],
                "structuredContent": payload,
            }

        if name == "app_acknowledge_notice":
            ack = sentinel.acknowledge_pending_notice(
                self.data_dir, builtin_dir=self.builtin_dir
            )
            payload = sentinel.collect_guard_structured_content(
                data_dir=self.data_dir,
                refresh=False,
                include_latest=True,
                changelog_url=self.changelog_url,
                models_url=self.models_url,
                builtin_dir=self.builtin_dir,
            )
            payload["ack_result"] = ack
            return {
                "content": [{"type": "text", "text": f"已确认当前版本提醒。\n{_format_text_summary(payload)}"}],
                "structuredContent": payload,
            }

        if name == "app_set_update_lock":
            want_locked = bool(args.get("locked", True))
            lock_res = (
                update_blocker.lock_updates(self.data_dir)
                if want_locked
                else update_blocker.unlock_updates(self.data_dir)
            )
            payload = sentinel.collect_guard_structured_content(
                data_dir=self.data_dir,
                refresh=False,
                include_latest=True,
                changelog_url=self.changelog_url,
                models_url=self.models_url,
                builtin_dir=self.builtin_dir,
            )
            payload["update_lock"] = lock_res
            action_txt = "已锁定自动更新并清理本地安装包" if want_locked else "已解除自动更新锁定"
            return {
                "content": [{"type": "text", "text": f"{action_txt}。\n{_format_text_summary(payload)}"}],
                "structuredContent": payload,
            }

        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
        }


def _read_framed_message(stream: BinaryIO) -> tuple[dict[str, Any] | None, str]:
    """Read one JSON-RPC message in either NDJSON or Content-Length framing."""
    while True:
        raw_line = stream.readline()
        if not raw_line:
            return None, "ndjson"
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            return json.loads(stripped.decode("utf-8")), "ndjson"

        # Parse Content-Length header block
        headers: dict[str, str] = {}
        line = stripped.decode("ascii", errors="replace")
        while True:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
            next_line = stream.readline()
            if not next_line or next_line in (b"\r\n", b"\n"):
                break
            line = next_line.strip().decode("ascii", errors="replace")

        length_str = headers.get("content-length")
        if not length_str:
            continue
        length = int(length_str)
        body = stream.read(length)
        if not body:
            return None, "content-length"
        return json.loads(body.decode("utf-8")), "content-length"


def _write_framed_message(stream: BinaryIO, payload: dict[str, Any], framing: str) -> None:
    """Write one JSON-RPC response using the matched transport framing."""
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if framing == "content-length":
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        stream.write(header + encoded)
    else:
        stream.write(encoded + b"\n")
    stream.flush()


def serve_stdio(
    server: UpdateGuardMcpServer,
    stdin_buf: BinaryIO | None = None,
    stdout_buf: BinaryIO | None = None,
) -> int:
    """Serve JSON-RPC 2.0 requests over binary stdio until EOF."""
    in_stream = stdin_buf or sys.stdin.buffer
    out_stream = stdout_buf or sys.stdout.buffer

    while True:
        try:
            msg, framing = _read_framed_message(in_stream)
        except Exception as exc:
            err = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            _write_framed_message(out_stream, err, "ndjson")
            continue

        if msg is None:
            break

        if isinstance(msg, list):
            responses = []
            for item in msg:
                resp = server.handle_message(item)
                if resp is not None:
                    responses.append(resp)
            if responses:
                _write_framed_message(out_stream, responses, framing)  # type: ignore[arg-type]
        elif isinstance(msg, dict):
            resp = server.handle_message(msg)
            if resp is not None:
                _write_framed_message(out_stream, resp, framing)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update Guard MCP stdio server.")
    parser.add_argument("--data-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--builtin-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--changelog-url", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--models-url", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    server = UpdateGuardMcpServer(
        data_dir=_resolve_data_dir(args.data_dir),
        builtin_dir=Path(args.builtin_dir) if args.builtin_dir else None,
        changelog_url=args.changelog_url,
        models_url=args.models_url,
    )
    return serve_stdio(server)


if __name__ == "__main__":
    raise SystemExit(main())
