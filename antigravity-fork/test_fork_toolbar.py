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


def make_fake_exe(
    main_js: str,
    extra_padding: int = 128,
    *,
    include_subagent_flag: bool = True,
    duplicate_subagent_ref: bool = False,
    func_name: str = "google3/third_party/jetski/cortex/cortex.(*CascadeManager).allowForkWithSubagents",
) -> bytes:
    """Create a synthetic PE32+ Go-like binary with pclntab and an embedded renderer ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("index.html", "<html></html>")
        zf.writestr("main.js", main_js.encode("utf-8"))
    zip_bytes = buf.getvalue()
    # Add extra comment padding so compressed patched output fits easily.
    zip_bytes = zip_bytes[:-2] + struct.pack("<H", extra_padding) + b" " * extra_padding

    image_base = 0x140000000
    text_vaddr = 0x1000
    rdata_vaddr = 0x2000
    pe_off = 0x80
    opt_size = 240
    num_sections = 2
    headers_end = pe_off + 24 + opt_size + num_sections * 40
    text_rptr = (headers_end + 0x1FF) & ~0x1FF  # 0x400

    # Reserve exact .rdata layout: flag string at offset 0, pclntab right after.
    flag_bytes = b"enable-fork-with-subagents\x00" if include_subagent_flag else b"other-flag\x00"
    str_va = image_base + rdata_vaddr

    # Build .text: cmp rsp,[r14+0x10]; jbe +0x10; lea rbx,[rip+disp]; mov ecx,0x1a; call rdx; leave; ret
    prologue = b"\x49\x3b\x66\x10\x76\x10\x55\x48\x89\xe5"
    lea_off = len(prologue)
    lea_va = image_base + text_vaddr + lea_off
    disp = str_va - (lea_va + 7)
    text_code = bytearray(
        prologue + b"\x48\x8d\x1d" + struct.pack("<i", disp) + b"\xb9\x1a\x00\x00\x00\xff\xd2\xc9\xc3"
    )
    if duplicate_subagent_ref:
        lea2_va = image_base + text_vaddr + len(text_code)
        disp2 = str_va - (lea2_va + 7)
        text_code.extend(b"\x48\x8d\x1d" + struct.pack("<i", disp2) + b"\xc3")
    text_rsize = (len(text_code) + 0x1FF) & ~0x1FF
    text_padded = bytes(text_code).ljust(text_rsize, b"\xcc")

    # Build Go 1.20+ pclntab in .rdata
    funcname_bytes = func_name.encode("utf-8") + b"\x00"
    header_len = 72
    funcname_off = header_len
    pcln_off = funcname_off + len(funcname_bytes)
    ftab = struct.pack("<II", 0, 8) + struct.pack("<Ii", 0, 0)
    pclntab = (
        b"\xf1\xff\xff\xff\x00\x00\x01\x08"
        + struct.pack("<QQQQQQQQ", 1, 0, image_base + text_vaddr, funcname_off, 0, 0, 0, pcln_off)
        + funcname_bytes
        + ftab
    )
    rdata_raw = flag_bytes + pclntab
    rdata_rsize = (len(rdata_raw) + 0x1FF) & ~0x1FF
    rdata_padded = rdata_raw.ljust(rdata_rsize, b"\x00")
    rdata_rptr = text_rptr + text_rsize

    dos_header = bytearray(pe_off)
    dos_header[0:2] = b"MZ"
    struct.pack_into("<I", dos_header, 0x3C, pe_off)

    coff = b"PE\x00\x00" + struct.pack("<HHIIIHH", 0x8664, num_sections, 0, 0, 0, opt_size, 0x22)
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x020B)
    struct.pack_into("<Q", opt, 24, image_base)

    sec_text = struct.pack(
        "<8sIIIIIIHHI", b".text", len(text_code), text_vaddr, text_rsize, text_rptr, 0, 0, 0, 0, 0x60000020
    )
    sec_rdata = struct.pack(
        "<8sIIIIIIHHI", b".rdata", len(rdata_raw), rdata_vaddr, rdata_rsize, rdata_rptr, 0, 0, 0, 0, 0x40000040
    )

    headers = bytes(dos_header) + coff + bytes(opt) + sec_text + sec_rdata
    headers = headers.ljust(text_rptr, b"\x00")
    return headers + text_padded + rdata_padded + zip_bytes + b"_FAKE_SUFFIX"


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
            partially_patched_bytes = make_fake_exe(patched_js)
            installed_exe.write_bytes(partially_patched_bytes)

            output = root / "work" / "build" / "language_server-native-fork-history.exe"
            backup_dir = root / "work" / "build" / "backups"

            with mock.patch.object(fork_toolbar, "ensure_antigravity_stopped"):
                # Upgrading a partial (main.js-only) binary without backup must apply the Go patch
                # and must NOT save the partially-patched binary as an official backup.
                version, backup, already = fork_toolbar.apply_patch(install, output, backup_dir)
                self.assertEqual(version, "2.33.0")
                self.assertIsNone(backup)
                self.assertFalse(already)
                self.assertTrue(fork_toolbar.inspect_binary_patched(installed_exe))
                self.assertFalse((backup_dir / "language_server-2.33.0-official.exe").exists())

                # Re-running when fully patched and no backup -> already_installed=True, backup=None
                version2, backup2, already2 = fork_toolbar.apply_patch(install, output, backup_dir)
                self.assertEqual(version2, "2.33.0")
                self.assertIsNone(backup2)
                self.assertTrue(already2)

                # Legacy backup in output.parent should be discovered for restore
                legacy_bytes = make_fake_exe(self.SAMPLE_JS)
                legacy_backup = output.parent / "language_server-2.33.0-official-backup.exe"
                legacy_backup.write_bytes(legacy_bytes)
                r_version, r_backup = fork_toolbar.restore_official(install, backup_dir, output.parent)
                self.assertEqual(r_version, "2.33.0")
                self.assertEqual(r_backup, legacy_backup)
                self.assertEqual(installed_exe.read_bytes(), legacy_bytes)

    def test_patch_subagent_fork_idempotent_and_validation(self):
        raw = make_fake_exe(self.SAMPLE_JS)
        self.assertFalse(fork_toolbar.is_subagent_fork_patched(raw))
        patched, changed = fork_toolbar.patch_subagent_fork(raw)
        self.assertTrue(changed)
        self.assertEqual(len(patched), len(raw))
        self.assertTrue(fork_toolbar.is_subagent_fork_patched(patched))

        patched_again, changed_again = fork_toolbar.patch_subagent_fork(patched)
        self.assertFalse(changed_again)
        self.assertEqual(patched, patched_again)

        with self.assertRaises(ValueError):
            fork_toolbar.patch_subagent_fork(make_fake_exe(self.SAMPLE_JS, include_subagent_flag=False))
        with self.assertRaises(ValueError):
            fork_toolbar.patch_subagent_fork(make_fake_exe(self.SAMPLE_JS, duplicate_subagent_ref=True))
        with self.assertRaises(ValueError):
            fork_toolbar.patch_subagent_fork(make_fake_exe(self.SAMPLE_JS, func_name="other.func"))

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
