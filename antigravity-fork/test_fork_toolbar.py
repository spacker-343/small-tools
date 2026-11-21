"""Unit tests for fork_toolbar.py."""
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zipfile

import fork_toolbar


def make_asar(package_obj: dict) -> bytes:
    """Create a minimal valid Electron ASAR archive containing package.json."""
    pkg_bytes = json.dumps(package_obj).encode("utf-8")
    header_dict = {
        "files": {
            "package.json": {
                "size": len(pkg_bytes),
                "offset": "0",
            }
        }
    }
    json_bytes = json.dumps(header_dict).encode("utf-8")
    # Pad json_bytes to 4-byte boundary as ASAR does, and compute header_size.
    pad = (4 - (len(json_bytes) % 4)) % 4
    padded_json = json_bytes + b"\x00" * pad
    header_size = 8 + len(padded_json)
    return (
        struct.pack("<IIII", 4, header_size, header_size - 4, len(json_bytes))
        + padded_json
        + pkg_bytes
    )


def make_fake_exe(main_js: str, extra_padding: int = 128) -> bytes:
    """Create a synthetic Go-like binary with an embedded renderer ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("index.html", "<html></html>")
        zf.writestr("main.js", main_js.encode("utf-8"))
    zip_bytes = buf.getvalue()
    # Add extra comment padding so compressed patched output fits easily.
    zip_bytes = zip_bytes[:-2] + struct.pack("<H", extra_padding) + b" " * extra_padding
    return b"MZ_FAKE_PREFIX_" + zip_bytes + b"_FAKE_SUFFIX"


class ForkToolbarTests(unittest.TestCase):
    SAMPLE_JS = (
        'const defaults={conversationView:{feature:{'
        'textSelectionEnabled:!0,'
        'conversationForkingEnabled:!1,'
        'conversationForkingNewWorktree:!1,'
        'conversationForkingHistoricalStep:!1,'
        'hideDiffViewsInConversation:!1}}};'
        'const gate=[{runtimeGating:{customFlag:"enable-fork"},feature:{conversationForkingEnabled:!0}}];'
    )

    def test_patch_renderer_standard_and_idempotent(self):
        patched, changed = fork_toolbar.patch_renderer(self.SAMPLE_JS)
        self.assertTrue(changed)
        self.assertIn(f"conversationForkingEnabled:{fork_toolbar.DESKTOP_CHECK},", patched)
        self.assertIn(f"conversationForkingHistoricalStep:{fork_toolbar.DESKTOP_CHECK},", patched)
        # runtimeGating !0 must remain untouched
        self.assertIn("feature:{conversationForkingEnabled:!0}", patched)

        patched_again, changed_again = fork_toolbar.patch_renderer(patched)
        self.assertFalse(changed_again)
        self.assertEqual(patched, patched_again)

    def test_patch_renderer_non_adjacent_and_false_literal(self):
        js = (
            'const cfg={conversationForkingEnabled : false,otherFlag:!0,'
            'conversationForkingNewWorktree:!1,conversationForkingHistoricalStep : false};'
        )
        patched, changed = fork_toolbar.patch_renderer(js)
        self.assertTrue(changed)
        self.assertIn(f"conversationForkingEnabled : {fork_toolbar.DESKTOP_CHECK},otherFlag:!0", patched)
        self.assertIn(f"conversationForkingHistoricalStep : {fork_toolbar.DESKTOP_CHECK}}}", patched)
        patched_again, changed_again = fork_toolbar.patch_renderer(patched)
        self.assertFalse(changed_again)
        self.assertEqual(patched, patched_again)

    def test_patch_renderer_detects_already_native_or_ambiguous(self):
        native_js = (
            'const cfg={conversationForkingEnabled:!0,conversationForkingHistoricalStep:!0};'
        )
        with self.assertRaises(ValueError) as ctx:
            fork_toolbar.patch_renderer(native_js)
        self.assertIn("already enabled natively", str(ctx.exception))

        ambiguous_js = (
            'const a={conversationForkingEnabled:!1};const b={conversationForkingEnabled:!1,'
            'conversationForkingHistoricalStep:!1};'
        )
        with self.assertRaises(ValueError) as ctx2:
            fork_toolbar.patch_renderer(ambiguous_js)
        self.assertIn("Ambiguous", str(ctx2.exception))

    def test_apply_and_restore_cross_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "antigravity"
            (install / "resources" / "bin").mkdir(parents=True)
            (install / "resources" / "app.asar").write_bytes(
                make_asar({"name": "antigravity", "version": "2.34.0"})
            )
            official_exe = install / "resources" / "bin" / "language_server.exe"
            official_bytes = make_fake_exe(self.SAMPLE_JS)
            official_exe.write_bytes(official_bytes)

            output = root / "work" / "build" / "language_server-native-fork-history.exe"
            backup_dir = root / "work" / "build" / "backups"

            with mock.patch.object(fork_toolbar, "ensure_antigravity_stopped"):
                version, backup, already = fork_toolbar.apply_patch(install, output, backup_dir)
                self.assertEqual(version, "2.34.0")
                self.assertFalse(already)
                self.assertTrue(backup.is_file())
                self.assertEqual(backup.read_bytes(), official_bytes)
                self.assertEqual(len(official_exe.read_bytes()), len(official_bytes))
                self.assertTrue(fork_toolbar.inspect_binary_patched(official_exe))

                # Re-running apply must NOT overwrite official backup with patched binary
                version2, backup2, _ = fork_toolbar.apply_patch(install, output, backup_dir)
                self.assertEqual(version2, "2.34.0")
                self.assertEqual(backup2.read_bytes(), official_bytes)

                # Restore must put back exact official bytes
                r_version, _ = fork_toolbar.restore_official(install, backup_dir, output.parent)
                self.assertEqual(r_version, "2.34.0")
                self.assertEqual(official_exe.read_bytes(), official_bytes)
                self.assertFalse(fork_toolbar.inspect_binary_patched(official_exe))

    def test_legacy_backup_and_already_patched_without_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "antigravity"
            (install / "resources" / "bin").mkdir(parents=True)
            (install / "resources" / "app.asar").write_bytes(
                make_asar({"name": "antigravity", "version": "2.33.0"})
            )
            installed_exe = install / "resources" / "bin" / "language_server.exe"
            patched_js, _ = fork_toolbar.patch_renderer(self.SAMPLE_JS)
            installed_exe.write_bytes(make_fake_exe(patched_js))

            output = root / "work" / "build" / "language_server-native-fork-history.exe"
            backup_dir = root / "work" / "build" / "backups"

            with mock.patch.object(fork_toolbar, "ensure_antigravity_stopped"):
                # Already patched and no backup -> backup_path must be None
                version, backup, already = fork_toolbar.apply_patch(install, output, backup_dir)
                self.assertEqual(version, "2.33.0")
                self.assertIsNone(backup)
                self.assertTrue(already)

                # Legacy backup in output.parent should be discovered for restore
                legacy_bytes = make_fake_exe(self.SAMPLE_JS)
                legacy_backup = output.parent / "language_server-2.33.0-official-backup.exe"
                legacy_backup.write_bytes(legacy_bytes)
                r_version, r_backup = fork_toolbar.restore_official(install, backup_dir, output.parent)
                self.assertEqual(r_version, "2.33.0")
                self.assertEqual(r_backup, legacy_backup)
                self.assertEqual(installed_exe.read_bytes(), legacy_bytes)

    def test_ensure_antigravity_stopped_raises_when_running(self):
        fake_proc = mock.Mock(returncode=0, stdout="Antigravity.exe              12345 Console")
        with mock.patch.object(fork_toolbar.os, "name", "nt"), mock.patch.object(
            fork_toolbar.subprocess, "run", return_value=fake_proc
        ):
            with self.assertRaises(ValueError) as ctx:
                fork_toolbar.ensure_antigravity_stopped()
            self.assertIn("Antigravity.exe", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
