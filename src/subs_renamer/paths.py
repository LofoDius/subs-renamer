import os
import sys
from pathlib import Path


def data_dir() -> Path:
    root = Path(
        os.environ.get("SUBS_RENAMER_DATA", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SubsRenamer")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
