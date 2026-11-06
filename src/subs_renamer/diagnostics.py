"""Opt-in packaged smoke check; reports contain statuses, never credentials."""

import argparse
import json
import threading
from pathlib import Path

from .config import api_key
from .gemini import GeminiAnalyzer
from .mkv import MKVTools
from .models import Project
from .subtitles import parse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", type=Path, required=True, help="JSON report destination")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--live", action="store_true", help="Use Gemini on synthetic dialogue")
    parser.add_argument("--model", default="gemini-3.8-flash")
    args = parser.parse_args()
    report = {}
    try:
        report["embedded_key_available"] = bool(api_key())
        tools = MKVTools()
        report["mkvmerge"] = tools.run("mkvmerge", ["--version"]).strip()
        report["mkvextract"] = tools.run("mkvextract", ["--version"]).strip()
        # Exercise extraction from a real two-track Matroska container, with source paths outside the bundle.
        folder = args.self_check.parent / "smoke-mkv"
        folder.mkdir(parents=True, exist_ok=True)
        ru, en = folder / "ru.srt", folder / "en.srt"
        ru.write_text("1\n00:00:01,000 --> 00:00:02,000\nРэн!\n", encoding="utf-8")
        en.write_text("1\n00:00:01,000 --> 00:00:02,000\nRen!\n", encoding="utf-8")
        container = folder / "synthetic.mkv"
        tools.run(
            "mkvmerge", ["-o", str(container), "--language", "0:rus", str(ru), "--language", "0:eng", str(en)]
        )
        info = tools.probe(container)
        document = tools.extract(container, info, tools.automatic_track(info))
        report["mkv_extract_ok"] = "Рэн!" in document.content and len(tools.tracks(info)) == 2
        if args.live:
            document = parse(
                "synthetic.srt",
                content=(
                    "1\n00:00:01,000 --> 00:00:03,000\nРэн, почему тебя называют Лисом?\n\n"
                    "2\n00:00:03,000 --> 00:00:05,000\nМеня прозвали Лисом за хитрость. Я и есть Рэн.\n\n"
                    "3\n00:00:05,000 --> 00:00:07,000\nЛис, то есть Рэн, пойдём с нами!\n"
                ),
            )
            analyzer = GeminiAnalyzer(args.model, proxy=args.proxy)
            try:
                groups = analyzer.analyze(Project(documents=[document]), threading.Event())
                report["live_alias_grouping_ok"] = any(
                    any("рэн" in a.text.casefold() for a in group.aliases)
                    and any("лис" in a.text.casefold() for a in group.aliases)
                    for group in groups
                )
            finally:
                analyzer.close()
        report["ok"] = report["mkv_extract_ok"] and report.get("live_alias_grouping_ok", True)
    except Exception as error:
        # Key lookup and Gemini requests expose only our own sanitized errors.
        report["ok"] = False
        report["error"] = str(error)
    args.self_check.parent.mkdir(parents=True, exist_ok=True)
    args.self_check.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
