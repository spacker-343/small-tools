from pathlib import Path
from urllib.parse import quote
import os
import sys


def target_for(path: str) -> str:
    file = Path(path).resolve(strict=True)
    if file.is_file() and file.suffix.lower() == ".md":
        return "obsidian://open?path=" + quote(str(file), safe="")
    return str(file)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Expected one file path")
    os.startfile(target_for(sys.argv[1]))


if __name__ == "__main__":
    main()
