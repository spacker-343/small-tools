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


def inspect_binary_patched(binary_path: Path) -> bool:
    """Check whether a language_server.exe binary already has the Fork patch."""
    data = binary_path.read_bytes()
    _, _, archive = locate_renderer(data)
    with archive:
        main_js = archive.read("main.js").decode("utf-8")
    return is_renderer_patched(main_js)


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
    changed = False
    with original, zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as patched:
        for name in original.namelist():
            content = original.read(name)
            if name == "main.js":
                patched_js, changed = patch_renderer(content.decode("utf-8"))
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
    if len(result_bytes) != len(data):
        raise ValueError("Internal error: patched binary size mismatch")

    atomic_write_bytes(output, result_bytes)
    return changed, len(result_bytes)


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
    already_patched = inspect_binary_patched(installed_exe)

    if already_patched:
        if existing_backup is None:
            build_from_binary(installed_exe, output)
            return version, None, True
        source_exe = existing_backup
    else:
        backup_target = backup_dir / f"language_server-{version}-official.exe"
        if existing_backup is None:
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
                print(f"Official backup: {backup.resolve()}")
                print(f"Installed binary: {(args.install / 'resources' / 'bin' / 'language_server.exe').resolve()}")
        else:
            version = build(args.install, args.output, args.expected_version)
            print(f"Built patch for Antigravity {version}: {args.output.resolve()}")
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
