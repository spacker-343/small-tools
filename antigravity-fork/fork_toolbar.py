"""Build an opt-in toolbar variant of the Antigravity 2.33.0 language server."""
import argparse
import io
import json
import os
from pathlib import Path
import struct
import sys
import zipfile


VERSION = "2.33.0"
EOCD = b"PK\x05\x06"


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError(f"Unexpected renderer layout near {before[:80]!r}")
    return source.replace(before, after, 1)


def patch_renderer(source):
    # Keep the native checks for a valid historical step and supported workspace.
    desktop = 'typeof window.dialog?.showOpenDialog==="function"'
    enabled = f"conversationForkingEnabled:{desktop},conversationForkingNewWorktree:"
    # The initial release may already be installed.
    if enabled not in source:
        source = replace_once(
            source,
            "conversationForkingEnabled:!1,conversationForkingNewWorktree:",
            enabled,
        )
    return replace_once(
        source,
        "conversationForkingHistoricalStep:!1,",
        f"conversationForkingHistoricalStep:{desktop},",
    )


def locate_renderer(data):
    # Search EOCD markers in the original binary without scanning it in Python byte by byte.
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


def build(install, output):
    asar = install / "resources" / "app.asar"
    with asar.open("rb") as app:
        _, header_size, _, json_size = struct.unpack("<IIII", app.read(16))
        entry = json.loads(app.read(json_size))["files"]["package.json"]
        app.seek(8 + header_size + int(entry["offset"]))
        package = json.loads(app.read(entry["size"]))
    if package.get("name") != "antigravity" or package.get("version") != VERSION:
        raise ValueError(f"Requires Antigravity {VERSION}; found {package.get('version')}")

    source = install / "resources" / "bin" / "language_server.exe"
    if output.resolve().is_relative_to(install.resolve()):
        raise ValueError("Output must be outside the Antigravity installation")
    data = source.read_bytes()
    begin, end, original = locate_renderer(data)
    buffer = io.BytesIO()
    with original, zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as patched:
        for name in original.namelist():
            content = original.read(name)
            if name == "main.js":
                content = patch_renderer(content.decode("utf-8")).encode("utf-8")
            patched.writestr(name, content)
    payload = buffer.getvalue()
    remaining = end - begin - len(payload)
    if not 0 <= remaining <= 65535:
        raise ValueError(f"Patched renderer cannot fit exactly ({remaining:+} bytes available)")
    if payload[-22:-18] != EOCD:
        raise ValueError("Could not locate the rebuilt ZIP end record")
    # Go locates this embedded ZIP using a fixed-size slice. Keep its exact size.
    payload = payload[:-2] + struct.pack("<H", remaining) + b" " * remaining
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as result:
        result.write(memoryview(data)[:begin])
        result.write(payload)
        result.write(memoryview(data)[end:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "antigravity"
    parser.add_argument("--install", type=Path, default=default)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "build" / "language_server-native-fork-history.exe")
    args = parser.parse_args()
    try:
        build(args.install, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        sys.exit(str(error))
    print(args.output.resolve())


if __name__ == "__main__":
    main()
