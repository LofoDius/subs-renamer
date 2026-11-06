from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .models import stable_id
from .paths import data_dir, resource_dir
from .subtitles import parse

TEXT_CODECS = {"S_TEXT/ASS": "ass", "S_TEXT/SSA": "ass", "S_TEXT/UTF8": "srt", "S_TEXT/ASCII": "srt"}


class MKVTools:
    def __init__(self, directory: str = ""):
        self.directory = directory

    def executable(self, name: str) -> str:
        if self.directory:
            folder = Path(self.directory)
        elif getattr(sys, "frozen", False):
            folder = unpack_tools(resource_dir() / "mkvtoolnix.zip")
        else:
            folder = resource_dir() / "vendor" / "mkvtoolnix"
        path = folder / (name + ".exe")
        if path.is_file():
            return str(path)
        raise FileNotFoundError(
            "Встроенный MKVToolNix не найден. Пересоберите приложение; "
            "для запуска из исходников выполните scripts/prepare_vendor.py."
        )

    def run(self, name: str, arguments: list[str], cancel=None) -> str:
        # File pipes avoid deadlocks on verbose subprocess output and allow cancellation.
        import tempfile

        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                [self.executable(name), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            while True:
                try:
                    process.wait(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        process.terminate()
                        process.wait(timeout=10)
                        raise InterruptedError("Операция отменена")
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read().decode("utf-8-sig", errors="replace")
            error = stderr.read().decode("utf-8-sig", errors="replace")
            if process.returncode not in (0, 1):
                raise RuntimeError(f"MKVToolNix: {(error or output)[-1600:]}")
            return output

    def probe(self, path: str | Path, cancel=None) -> dict:
        return json.loads(self.run("mkvmerge", ["-J", str(path)], cancel))

    @staticmethod
    def tracks(info: dict) -> list[dict]:
        return [t for t in info.get("tracks", []) if t.get("type") == "subtitles"]

    @staticmethod
    def text_format(track: dict) -> str | None:
        return TEXT_CODECS.get(track.get("properties", {}).get("codec_id", ""))

    @classmethod
    def automatic_track(cls, info: dict) -> int | None:
        russian = [
            t
            for t in cls.tracks(info)
            if cls.text_format(t)
            and (
                t.get("properties", {}).get("language") in {"rus", "ru"}
                or t.get("properties", {}).get("language_ietf", "").lower().split("-")[0] == "ru"
            )
        ]
        return russian[0]["id"] if len(russian) == 1 else None

    def extract(self, path: str | Path, info: dict, track_id: int, cancel=None):
        path = Path(path)
        track = next(t for t in self.tracks(info) if t["id"] == track_id)
        format_ = self.text_format(track)
        if not format_:
            raise ValueError("Графические субтитры PGS/VobSub не поддерживаются: требуется OCR")
        stat = path.stat()
        folder = data_dir() / "extracted" / stable_id(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"track-{track_id}.{format_}"
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".partial")
            try:
                self.run("mkvextract", [str(path), "tracks", f"{track_id}:{temporary}"], cancel)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        doc = parse(target, format_=format_)
        doc.source_path = str(path.resolve())
        doc.track_id = track_id
        doc.track_label = track.get("properties", {}).get("track_name", "") or track.get("codec", "")
        if format_ == "ass":
            fonts = []
            for item in info.get("attachments", []):
                filename = Path(item.get("file_name", "")).name
                if Path(filename).suffix.lower() not in {".ttf", ".otf", ".ttc", ".woff", ".woff2"}:
                    continue
                # Attachment filenames are untrusted container data.
                target_font = folder / f"attachment-{item['id']}{Path(filename).suffix.lower()}"
                if not target_font.exists():
                    temporary_font = target_font.with_suffix(target_font.suffix + ".partial")
                    try:
                        self.run(
                            "mkvextract", [str(path), "attachments", f"{item['id']}:{temporary_font}"], cancel
                        )
                        temporary_font.replace(target_font)
                    finally:
                        temporary_font.unlink(missing_ok=True)
                fonts.append(str(target_font))
            doc.font_paths = fonts
        return doc


def unpack_tools(archive: Path) -> Path:
    """Unpack the embedded, versioned tools once; never depend on an installed copy."""
    if not archive.is_file():
        raise FileNotFoundError("В EXE отсутствует MKVToolNix. Пересоберите приложение.")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    root = data_dir() / "tools"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"mkvtoolnix-{digest[:24]}"
    if (
        (destination / ".complete").is_file()
        and (destination / "mkvmerge.exe").is_file()
        and (destination / "mkvextract.exe").is_file()
    ):
        return destination
    # Build in an isolated directory so cancellation, crashes and parallel launches cannot publish half a toolset.
    with tempfile.TemporaryDirectory(prefix="unpack-", dir=root) as staging:
        folder = Path(staging) / "tools"
        folder.mkdir()
        with zipfile.ZipFile(archive) as package:
            for entry in package.namelist():
                if not (folder / entry).resolve().is_relative_to(folder.resolve()):
                    raise ValueError("Повреждённый архив встроенного MKVToolNix")
            package.extractall(folder)
        if not all((folder / name).is_file() for name in ("mkvmerge.exe", "mkvextract.exe")):
            raise ValueError("Неполный архив встроенного MKVToolNix")
        (folder / ".complete").write_text(digest, encoding="ascii")
        try:
            folder.rename(destination)
        except FileExistsError:
            if not (destination / ".complete").is_file():
                raise RuntimeError(
                    "Кэш MKVToolNix повреждён. Удалите папку tools из кэша приложения."
                ) from None
    return destination
