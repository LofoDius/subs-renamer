"""Opt-in live Gemini integration check, using synthetic dialogue only."""

import argparse
import threading

from subs_renamer.gemini import GeminiAnalyzer
from subs_renamer.models import Project
from subs_renamer.subtitles import parse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--proxy", default="", help="SOCKS5 without authentication, e.g. localhost:2080")
    args = parser.parse_args()
    doc = parse(
        "synthetic.srt",
        content=(
            "1\n00:00:01,000 --> 00:00:03,000\nРэн, почему тебя называют Лисом?\n\n"
            "2\n00:00:03,000 --> 00:00:05,000\nМеня прозвали Лисом за хитрость. Я и есть Рэн.\n\n"
            "3\n00:00:05,000 --> 00:00:07,000\nЛис, то есть Рэн, пойдём с нами!\n"
        ),
    )
    client = GeminiAnalyzer(args.model, proxy=args.proxy)
    try:
        groups = client.analyze(Project(documents=[doc]), threading.Event())
        # Output synthetic entity data, never credentials or raw SDK errors.
        print("Model:", args.model)
        for group in groups:
            print(group.label, "=>", ", ".join(a.text for a in group.aliases))
        assert any(
            any("рэн" in a.text.casefold() for a in g.aliases)
            and any("лис" in a.text.casefold() for a in g.aliases)
            for g in groups
        ), "Связь имени и ника не найдена"
        print("Live extraction and alias grouping: OK")
    finally:
        client.close()


if __name__ == "__main__":
    main()
