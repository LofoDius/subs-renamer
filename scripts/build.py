"""Build a self-contained EXE with a private build-time Gemini key from .env."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from subs_renamer.config import EMBEDDED_CONFIG, development_key

ROOT = Path(__file__).resolve().parents[1]


def build_arguments(notice_dir: Path, private_config: Path, tools_archive: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onefile",
        "--name",
        "SubsRenamer",
        "--paths",
        str(ROOT / "src"),
        "--add-data",
        f"{notice_dir};licenses",
        "--add-data",
        f"{private_config};.",
        "--add-data",
        f"{tools_archive};.",
        "--add-data",
        f"{ROOT / 'README.md'};.",
        "--add-data",
        f"{ROOT / 'THIRD_PARTY_NOTICES.md'};.",
        "--collect-data",
        "certifi",
        "--copy-metadata",
        "google-genai",
        "--hidden-import",
        "httpcore._sync.socks_proxy",
        "--hidden-import",
        "httpcore._async.socks_proxy",
        "--hidden-import",
        "socksio",
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
        "--exclude-module",
        "PySide6.QtPdf",
        str(ROOT / "scripts" / "entry.py"),
    ]


def pack_tools(destination: Path):
    folder = ROOT / "vendor" / "mkvtoolnix"
    # Only the two required, self-contained executables, plus documentation, licenses and matching sources.
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        files = [folder / "mkvmerge.exe", folder / "mkvextract.exe", folder / "SOURCE-AND-VERSION.txt"]
        files += [p for name in ("doc", "source") for p in (folder / name).rglob("*") if p.is_file()]
        files += list(folder.glob("*.dll"))
        for path in files:
            archive.write(path, path.relative_to(folder))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist", help="Output directory for the EXE")
    args = parser.parse_args()
    destination = args.dist_dir.resolve()
    # Validate before downloads or starting an expensive build. Never pass a key in command-line arguments.
    key = development_key(ROOT / ".env")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "prepare_vendor.py")], check=True)
    notice_dir = ROOT / "build" / "licenses"
    notice_dir.mkdir(parents=True, exist_ok=True)
    for dist in importlib.metadata.distributions():
        for file in dist.files or []:
            if ".dist-info/" in str(file).replace("\\", "/") and any(
                word in str(file).upper() for word in ("LICENSE", "COPYING", "NOTICE")
            ):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    target = notice_dir / dist.metadata["Name"] / Path(str(file)).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copyfile(python_license, notice_dir / "PYTHON-LICENSE.txt")
    # Do not let unrelated DLLs on the host PATH (e.g. Poppler ICU) contaminate Qt's dependencies.
    build_env = os.environ.copy()
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    build_env["PATH"] = os.pathsep.join(
        map(str, [Path(sys.executable).parent, Path(sys.base_prefix), windows / "System32", windows])
    )
    build_env["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".cache" / "pyinstaller")
    tools_archive = ROOT / "build" / "mkvtoolnix.zip"
    pack_tools(tools_archive)
    private_config = ROOT / "build" / "private" / EMBEDDED_CONFIG
    private_config.parent.mkdir(parents=True, exist_ok=True)
    try:
        private_config.write_text(json.dumps({"gemini_api_key": key}), encoding="utf-8")
        subprocess.run(
            build_arguments(notice_dir, private_config, tools_archive) + ["--distpath", str(destination)],
            cwd=ROOT,
            env=build_env,
            check=True,
        )
    finally:
        private_config.unlink(missing_ok=True)
    print(f"Single-file application (contains your private key): {destination / 'SubsRenamer.exe'}")


if __name__ == "__main__":
    main()
