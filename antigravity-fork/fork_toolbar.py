"""Build and apply an opt-in native Fork toolbar patch for Antigravity desktop."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile


EOCD = b"PK\x05\x06"
DESKTOP_CHECK = 'typeof window.dialog?.showOpenDialog==="function"'
TARGET_FLAGS = (
    "conversationForkingEnabled",
    "conversationForkingHistoricalStep",
)
SUBAGENT_FORK_FLAG = b"enable-fork-with-subagents"
SUBAGENT_FORK_FUNC_SUFFIX = "allowForkWithSubagents"
RETURN_TRUE_STUB = b"\xb8\x01\x00\x00\x00\xc3"  # mov eax, 1; ret
PCLNTAB_MAGICS = (b"\xf1\xff\xff\xff\x00\x00", b"\xf0\xff\xff\xff\x00\x00")
PROLOGUE_PREFIXES = (
    b"\x49\x3b\x66\x10",  # cmp rsp, [r14+0x10]
    b"\x4c\x8d",          # lea r12, [rsp-...]
    b"\x55",              # push rbp
    b"\x48\x83\xec",      # sub rsp, imm8
)


def read_installed_package(install: Path) -> dict:
    """Read package.json metadata from Antigravity's app.asar."""
    asar = install / "resources" / "app.asar"
    with asar.open("rb") as app:
        _, header_size, _, json_size = struct.unpack("<IIII", app.read(16))
        entry = json.loads(app.read(json_size))["files"]["package.json"]
        app.seek(8 + header_size + int(entry["offset"]))
        return json.loads(app.read(entry["size"]))


def detect_version(install: Path, expected_version: str | None = None) -> str:
    """Return the installed Antigravity version and optionally verify it."""
    package = read_installed_package(install)
    if package.get("name") != "antigravity":
        raise ValueError(f"Unexpected app name in app.asar: {package.get('name')!r}")
    version = package.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Could not determine Antigravity version from app.asar")
    if expected_version and version != expected_version:
        raise ValueError(f"Requires Antigravity {expected_version}; found {version}")
    return version


def is_flag_patched(source: str, flag: str) -> bool:
    """Return True if a single Fork flag already contains our desktop check."""
    return bool(re.search(rf"{re.escape(flag)}\s*:\s*{re.escape(DESKTOP_CHECK)}", source))


def patch_renderer(source: str) -> tuple[str, bool]:
    """Enable native Fork and historical-step Fork flags in main.js.

    Returns (patched_source, changed).
    """
    changed = False
    for flag in TARGET_FLAGS:
        if is_flag_patched(source, flag):
            continue

        pattern = re.compile(rf"({re.escape(flag)}\s*:\s*)(?:!1|false)(?=[,\}}\s])")
        matches = list(pattern.finditer(source))
        if len(matches) == 1:
            source = pattern.sub(rf"\g<1>{DESKTOP_CHECK}", source, count=1)
            changed = True
            continue
        if len(matches) > 1:
            raise ValueError(f"Ambiguous renderer layout: {len(matches)} matches for {flag}")

        if re.search(rf"{re.escape(flag)}\s*:\s*(?:!0|true)(?=[,\}}\s])", source):
            raise ValueError(
                f"Flag {flag} appears already enabled natively or layout changed; "
                "please check whether Antigravity now enables Fork out of the box."
            )
        raise ValueError(f"Unexpected renderer layout: could not locate {flag}")

    return source, changed


def is_renderer_patched(source: str) -> bool:
    """Return True if all target Fork flags already contain our desktop check."""
    return all(is_flag_patched(source, flag) for flag in TARGET_FLAGS)


def locate_renderer(data: bytes) -> tuple[int, int, zipfile.ZipFile]:
    """Locate the embedded renderer ZIP archive inside language_server.exe."""
    offset = len(data)
    while (offset := data.rfind(EOCD, 0, offset)) >= 0:
        if offset + 22 > len(data):
            continue
        size, start, comment = struct.unpack_from("<IIH", data, offset + 12)
        begin, end = offset - size - start, offset + 22 + comment
        if not (0 <= begin < offset and end <= len(data)):
            continue
        if data[begin:begin + 4] != b"PK\x03\x04":
            continue
        try:
            archive = zipfile.ZipFile(io.BytesIO(data[begin:end]))
        except zipfile.BadZipFile:
            continue
        if {"main.js", "index.html"} <= set(archive.namelist()):
            return begin, end, archive
        archive.close()
    raise ValueError("Antigravity renderer archive not found")


def inspect_binary_patch_state(binary_path: Path) -> tuple[bool, bool]:
    """Return (renderer_patched, subagent_fork_patched) for a language_server.exe binary."""
    data = binary_path.read_bytes()
    _, _, archive = locate_renderer(data)
    with archive:
        main_js = archive.read("main.js").decode("utf-8")
    return is_renderer_patched(main_js), is_subagent_fork_patched(data)


def inspect_binary_patched(binary_path: Path) -> bool:
    """Check whether a language_server.exe binary already has all Fork patches."""
    renderer_patched, subagent_patched = inspect_binary_patch_state(binary_path)
    return renderer_patched and subagent_patched


def parse_pe64_sections(data: bytes) -> tuple[int, list[tuple[str, int, int, int, int]]]:
    """Parse PE32+ image base and section headers from a Windows x64 executable."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("Not a valid PE executable")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != b"PE\x00\x00":
        raise ValueError("Invalid PE header signature")
    machine, num_sections, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, pe_off + 4)
    if machine != 0x8664:
        raise ValueError(f"Unsupported PE architecture: 0x{machine:04x} (expected x64)")
    opt_off = pe_off + 24
    if opt_off + opt_size > len(data) or opt_size < 32:
        raise ValueError("Truncated PE optional header")
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic != 0x020B:
        raise ValueError(f"Unsupported PE optional header magic: 0x{magic:04x} (expected PE32+)")
    image_base = struct.unpack_from("<Q", data, opt_off + 24)[0]

    sec_off = opt_off + opt_size
    if sec_off + num_sections * 40 > len(data):
        raise ValueError("Truncated PE section table")
    sections: list[tuple[str, int, int, int, int]] = []
    for i in range(num_sections):
        off = sec_off + i * 40
        name = data[off:off + 8].rstrip(b"\x00").decode("latin1")
        vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((name, vaddr, vsize, rptr, rsize))
    return image_base, sections


def raw_to_va(raw: int, image_base: int, sections: list[tuple[str, int, int, int, int]]) -> int | None:
    """Convert a raw file offset to a virtual address if it lies inside a PE section."""
    for _, vaddr, _, rptr, rsize in sections:
        if rptr <= raw < rptr + rsize:
            return image_base + vaddr + (raw - rptr)
    return None


def find_rip_lea_refs(data: bytes, text_section: tuple[str, int, int, int, int], image_base: int, target_vas: set[int]) -> list[int]:
    """Return raw offsets of RIP-relative LEA instructions in .text targeting target_vas."""
    _, tvaddr, _, trptr, trsize = text_section
    text_va = image_base + tvaddr
    text_bytes = data[trptr:trptr + trsize]
    pattern = re.compile(rb"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]")
    hits: list[int] = []
    for match in pattern.finditer(text_bytes):
        idx = match.start()
        if idx + 7 > len(text_bytes):
            continue
        disp = struct.unpack_from("<i", text_bytes, idx + 3)[0]
        if text_va + idx + 7 + disp in target_vas:
            hits.append(trptr + idx)
    return hits


def lookup_gopclntab_function(data: bytes, text_va: int, target_va: int) -> tuple[int, str]:
    """Resolve the Go function entry VA and name containing target_va from gopclntab."""
    for magic in PCLNTAB_MAGICS:
        pos = 0
        while (pos := data.find(magic, pos)) >= 0:
            if pos + 72 > len(data):
                break
            min_lc, ptr_sz = data[pos + 6], data[pos + 7]
            if ptr_sz != 8 or min_lc not in (1, 2, 4):
                pos += 1
                continue
            nfunc = struct.unpack_from("<Q", data, pos + 8)[0]
            if not (1 <= nfunc <= 5_000_000):
                pos += 1
                continue
            text_start, funcname_off, _, _, _, pcln_off = struct.unpack_from("<QQQQQQ", data, pos + 24)
            ftab_off = pos + pcln_off
            if ftab_off + nfunc * 8 > len(data) or pos + funcname_off >= len(data):
                pos += 1
                continue

            base_va = text_start if text_start else text_va
            rel = target_va - base_va
            if rel < 0:
                pos += 1
                continue
            lo, hi = 0, nfunc - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                entry_off, _ = struct.unpack_from("<II", data, ftab_off + mid * 8)
                if entry_off <= rel:
                    lo = mid + 1
                else:
                    hi = mid - 1
            if hi >= 0:
                entry_off, func_off = struct.unpack_from("<II", data, ftab_off + hi * 8)
                if ftab_off + func_off + 8 <= len(data):
                    name_off = struct.unpack_from("<i", data, ftab_off + func_off + 4)[0]
                    fn_start = pos + funcname_off + name_off
                    if 0 <= fn_start < len(data):
                        fn_end = data.find(b"\x00", fn_start)
                        if fn_end != -1:
                            return base_va + entry_off, data[fn_start:fn_end].decode("utf-8", "replace")
            pos += 1
    raise ValueError("Could not resolve allowForkWithSubagents in Go pclntab")


def locate_subagent_fork_entry(data: bytes) -> int:
    """Locate the raw file offset of CascadeManager.allowForkWithSubagents."""
    image_base, sections = parse_pe64_sections(data)
    text_sections = [s for s in sections if s[0] == ".text"]
    if len(text_sections) != 1:
        raise ValueError(f"Expected 1 .text section in PE binary; found {len(text_sections)}")
    text_section = text_sections[0]
    _, tvaddr, _, trptr, trsize = text_section
    text_va = image_base + tvaddr

    target_vas: set[int] = set()
    pos = 0
    while (pos := data.find(SUBAGENT_FORK_FLAG, pos)) >= 0:
        va = raw_to_va(pos, image_base, sections)
        if va is not None:
            target_vas.add(va)
        pos += 1
    if not target_vas:
        raise ValueError("Could not locate enable-fork-with-subagents flag string in binary")

    lea_raws = find_rip_lea_refs(data, text_section, image_base, target_vas)
    if len(lea_raws) != 1:
        raise ValueError(
            f"Expected 1 reference to enable-fork-with-subagents; found {len(lea_raws)}"
        )
    lea_raw = lea_raws[0]
    lea_va = text_va + (lea_raw - trptr)

    func_va, func_name = lookup_gopclntab_function(data, text_va, lea_va)
    if not func_name.endswith(f".{SUBAGENT_FORK_FUNC_SUFFIX}"):
        raise ValueError(f"Unexpected function for enable-fork-with-subagents: {func_name}")

    func_raw = trptr + (func_va - text_va)
    if not (trptr <= func_raw < trptr + trsize) or not (len(RETURN_TRUE_STUB) <= lea_raw - func_raw <= 0x200):
        raise ValueError(f"Unexpected layout around {func_name}")
    return func_raw


def is_subagent_fork_patched(data: bytes) -> bool:
    """Return True if allowForkWithSubagents already returns true unconditionally."""
    func_raw = locate_subagent_fork_entry(data)
    return data[func_raw:func_raw + len(RETURN_TRUE_STUB)] == RETURN_TRUE_STUB


def patch_subagent_fork(data: bytes) -> tuple[bytes, bool]:
    """Patch CascadeManager.allowForkWithSubagents to return true unconditionally.

    Returns (patched_bytes, changed).
    """
    func_raw = locate_subagent_fork_entry(data)
    current = data[func_raw:func_raw + len(RETURN_TRUE_STUB)]
    if current == RETURN_TRUE_STUB:
        return data, False
    if not any(current.startswith(prefix) for prefix in PROLOGUE_PREFIXES):
        raise ValueError(f"Unexpected prologue bytes in allowForkWithSubagents: {current.hex()}")
    patched = bytearray(data)
    patched[func_raw:func_raw + len(RETURN_TRUE_STUB)] = RETURN_TRUE_STUB
    return bytes(patched), True


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write bytes to target atomically via a temporary file in the same directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_copy_file(src: Path, dst: Path) -> None:
    """Copy src to dst atomically via a temporary file in dst's directory."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        shutil.copyfile(src, tmp_path)
        os.replace(tmp_path, dst)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def build_from_binary(source_exe: Path, output: Path) -> tuple[bool, int]:
    """Patch source_exe and write the resulting binary to output.

    Returns (changed, size_bytes).
    """
    data = source_exe.read_bytes()
    begin, end, original = locate_renderer(data)
    buffer = io.BytesIO()
    renderer_changed = False
    with original, zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as patched:
        for name in original.namelist():
            content = original.read(name)
            if name == "main.js":
                patched_js, renderer_changed = patch_renderer(content.decode("utf-8"))
                content = patched_js.encode("utf-8")
            patched.writestr(name, content)

    payload = buffer.getvalue()
    remaining = end - begin - len(payload)
    if not 0 <= remaining <= 65535:
        raise ValueError(f"Patched renderer cannot fit exactly ({remaining:+} bytes available)")
    if payload[-22:-18] != EOCD:
        raise ValueError("Could not locate the rebuilt ZIP end record")

    # Go locates this embedded ZIP using a fixed-size slice. Keep its exact size.
    payload = payload[:-2] + struct.pack("<H", remaining) + b" " * remaining
    result_bytes = data[:begin] + payload + data[end:]
    result_bytes, subagent_changed = patch_subagent_fork(result_bytes)
    if len(result_bytes) != len(data):
        raise ValueError("Internal error: patched binary size mismatch")

    atomic_write_bytes(output, result_bytes)
    return (renderer_changed or subagent_changed), len(result_bytes)


def build(install: Path, output: Path, expected_version: str | None = None) -> str:
    """Build a patched language_server.exe from the installed Antigravity."""
    version = detect_version(install, expected_version)
    if output.resolve().is_relative_to(install.resolve()):
        raise ValueError("Output must be outside the Antigravity installation")
    source = install / "resources" / "bin" / "language_server.exe"
    build_from_binary(source, output)
    return version


def find_official_backup(backup_dir: Path, build_dir: Path, version: str) -> Path | None:
    """Locate an existing official backup for the given version."""
    candidates = (
        backup_dir / f"language_server-{version}-official.exe",
        build_dir / f"language_server-{version}-official-backup.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def ensure_antigravity_stopped() -> None:
    """Abort if Antigravity.exe or language_server.exe is currently running on Windows."""
    if os.name != "nt":
        return
    for image in ("Antigravity.exe", "language_server.exe"):
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and image.lower() in proc.stdout.lower():
            raise ValueError(
                f"Detected running {image}. Please close Antigravity completely before applying or restoring."
            )


def apply_patch(
    install: Path,
    output: Path,
    backup_dir: Path,
    expected_version: str | None = None,
) -> tuple[str, Path | None, bool]:
    """Back up the official binary (if needed), build the patch, and install it.

    Returns (version, backup_path, already_installed).
    """
    ensure_antigravity_stopped()
    version = detect_version(install, expected_version)
    if output.resolve().is_relative_to(install.resolve()):
        raise ValueError("Output must be outside the Antigravity installation")

    installed_exe = install / "resources" / "bin" / "language_server.exe"
    if not installed_exe.is_file():
        raise ValueError(f"Installed binary not found: {installed_exe}")

    existing_backup = find_official_backup(backup_dir, output.parent, version)
    renderer_patched, subagent_patched = inspect_binary_patch_state(installed_exe)
    already_patched = renderer_patched and subagent_patched
    partially_patched = renderer_patched or subagent_patched

    if already_patched:
        if existing_backup is None:
            build_from_binary(installed_exe, output)
            return version, None, True
        source_exe = existing_backup
    elif existing_backup is not None:
        source_exe = existing_backup
    else:
        if not partially_patched:
            backup_target = backup_dir / f"language_server-{version}-official.exe"
            atomic_copy_file(installed_exe, backup_target)
            existing_backup = backup_target
        source_exe = installed_exe

    build_from_binary(source_exe, output)
    atomic_copy_file(output, installed_exe)
    return version, existing_backup, False


def restore_official(
    install: Path,
    backup_dir: Path,
    build_dir: Path,
    expected_version: str | None = None,
) -> tuple[str, Path]:
    """Restore the official language_server.exe from backup for the installed version."""
    ensure_antigravity_stopped()
    version = detect_version(install, expected_version)
    backup = find_official_backup(backup_dir, build_dir, version)
    if backup is None:
        raise ValueError(f"No official backup found for Antigravity {version} in {backup_dir}")
    installed_exe = install / "resources" / "bin" / "language_server.exe"
    atomic_copy_file(backup, installed_exe)
    return version, backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default_install = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "antigravity"
    project_dir = Path(__file__).parent
    default_output = project_dir / "build" / "language_server-native-fork-history.exe"
    default_backups = project_dir / "build" / "backups"

    parser.add_argument("--install", type=Path, default=default_install)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--backup-dir", type=Path, default=default_backups)
    parser.add_argument("--expected-version", default=None, help="Require a specific Antigravity version")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Backup official binary, build patch, and apply it")
    mode.add_argument("--restore", action="store_true", help="Restore the official binary from backup")

    args = parser.parse_args()
    try:
        if args.restore:
            version, backup = restore_official(args.install, args.backup_dir, args.output.parent, args.expected_version)
            print(f"Restored Antigravity {version} official binary from {backup.resolve()}")
        elif args.apply:
            version, backup, already_patched = apply_patch(
                args.install, args.output, args.backup_dir, args.expected_version
            )
            if already_patched:
                print(f"Antigravity {version} is already patched (synced {args.output.resolve()}).")
            else:
                print(f"Patched Antigravity {version} successfully.")
                if backup is not None:
                    print(f"Official backup: {backup.resolve()}")
                print(f"Installed binary: {(args.install / 'resources' / 'bin' / 'language_server.exe').resolve()}")
        else:
            version = build(args.install, args.output, args.expected_version)
            print(f"Built patch for Antigravity {version}: {args.output.resolve()}")
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
