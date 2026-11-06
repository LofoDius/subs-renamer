"""Download official MKVToolNix, verify SHA-256, retain licenses and matching source."""

from __future__ import annotations

import hashlib
import shutil
import urllib.request
import zipfile
from pathlib import Path

VERSION = "103.0"
ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = f"mkvtoolnix-64-bit-{VERSION}.zip"
URL = f"https://mkvtoolnix.download/windows/releases/{VERSION}/{ARCHIVE}"
SOURCE_URL = f"https://mkvtoolnix.download/sources/mkvtoolnix-{VERSION}.tar.xz"


def download(url: str, destination: Path):
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".partial")
        with urllib.request.urlopen(url, timeout=120) as source, temporary.open("wb") as output:
            shutil.copyfileobj(source, output)
        temporary.replace(destination)


def main():
    cache = ROOT / ".cache" / "downloads"
    archive = cache / ARCHIVE
    download(URL, archive)
    checksum = cache / (ARCHIVE + ".sha256")
    download(URL + ".sha256", checksum)
    expected = checksum.read_text().split()[0].lower()
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(
            "Контрольная сумма MKVToolNix не совпадает; удалите повреждённый архив из .cache/downloads"
        )
    folder = ROOT / "vendor" / "mkvtoolnix"
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "SOURCE-AND-VERSION.txt").exists():
        with zipfile.ZipFile(archive) as package:
            for name in package.namelist():
                target = (folder / name).resolve()
                if not target.is_relative_to(folder.resolve()):
                    raise RuntimeError("Небезопасный путь в архиве")
            package.extractall(folder)
        nested = list(folder.rglob("mkvmerge.exe"))
        if nested and nested[0].parent != folder:
            parent = nested[0].parent
            for child in parent.iterdir():
                shutil.move(str(child), str(folder / child.name))
    source = folder / "source" / f"mkvtoolnix-{VERSION}.tar.xz"
    download(SOURCE_URL, source)
    (folder / "SOURCE-AND-VERSION.txt").write_text(
        f"MKVToolNix {VERSION}\nBinary: {URL}\nSHA256: {actual}\n"
        f"Corresponding source: {SOURCE_URL}\nA copy is included in source/.\n"
        "Unmodified official Windows distribution. License: GNU GPL v2 or later; see COPYING.\n",
        encoding="utf-8",
    )
    if not (folder / "mkvmerge.exe").exists() or not (folder / "mkvextract.exe").exists():
        raise RuntimeError("В архиве не найдены нужные исполняемые файлы")
    print(f"MKVToolNix {VERSION}: ready, SHA256 verified")


if __name__ == "__main__":
    main()
