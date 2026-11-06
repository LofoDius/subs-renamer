"""Parse text spans, preserving the original document and timing strings."""

from __future__ import annotations

import re
from pathlib import Path

from .models import Cue, SubtitleDocument, stable_id

TIME = r"\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}"
SRT_TIME = re.compile(rf"^\s*({TIME})\s*-->\s*({TIME})(?:\s+.*)?$")
ASS_TIME = re.compile(r"^\d+:\d{2}:\d{2}\.\d{2}$")


def decode(data: bytes) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        text = data.decode("cp1251")
        if "\x00" in text:
            raise ValueError("Файл не похож на текстовые субтитры")
        return text, "cp1251"


def parse(path: str | Path, *, content: str | None = None, format_: str | None = None) -> SubtitleDocument:
    path = Path(path)
    encoding = "utf-8"
    if content is None:
        content, encoding = decode(path.read_bytes())
    format_ = format_ or ("ass" if path.suffix.lower() in {".ass", ".ssa"} else "srt")
    doc_id = stable_id(str(path.resolve()), content)
    lines = content.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    cues = []
    if format_ == "ass":
        in_events, fields = False, []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("["):
                in_events = stripped.casefold() == "[events]"
            if not in_events:
                continue
            if stripped.lower().startswith("format:"):
                fields = [f.strip().casefold() for f in stripped.split(":", 1)[1].split(",")]
                if not fields or fields[-1] != "text":
                    raise ValueError("В ASS поле Text должно быть последним")
            if line.lstrip().lower().startswith("dialogue:"):
                if not fields or "start" not in fields or "end" not in fields:
                    raise ValueError("В ASS отсутствует корректная строка Format")
                prefix = line.index(":") + 1
                body = line[prefix:].rstrip("\r\n")
                values = body.split(",", len(fields) - 1)
                if len(values) != len(fields):
                    raise ValueError(f"Повреждённая ASS-реплика в строке {i + 1}")
                start_time, end_time = (values[fields.index(f)].strip() for f in ("start", "end"))
                if not ASS_TIME.fullmatch(start_time) or not ASS_TIME.fullmatch(end_time):
                    raise ValueError(f"Некорректное время ASS в строке {i + 1}")
                text_start = offsets[i] + prefix + sum(len(v) + 1 for v in values[:-1])
                cues.append(
                    Cue(
                        id=f"{doc_id}:{len(cues)}",
                        start=text_start,
                        end=offsets[i] + len(line.rstrip("\r\n")),
                        timing=f"{start_time} → {end_time}",
                    )
                )
    else:
        i = 0
        while i < len(lines):
            if not lines[i].strip():
                i += 1
                continue
            if lines[i].strip().isdigit():
                i += 1
            if i >= len(lines) or not SRT_TIME.fullmatch(lines[i].rstrip("\r\n")):
                raise ValueError(f"Некорректный SRT: ожидается время в строке {i + 1}")
            timing = lines[i].rstrip("\r\n")
            first = i + 1
            i = first
            while i < len(lines) and lines[i].strip():
                i += 1
            if first == i:
                raise ValueError("Реплика SRT без текста")
            cues.append(
                Cue(
                    id=f"{doc_id}:{len(cues)}",
                    start=offsets[first],
                    end=offsets[i - 1] + len(lines[i - 1].rstrip("\r\n")),
                    timing=timing,
                )
            )
    if not cues:
        raise ValueError("В файле нет распознаваемых текстовых реплик")
    return SubtitleDocument(
        id=doc_id,
        source_path=str(path.resolve()),
        format=format_,
        content=content,
        encoding=encoding,
        cues=cues,
    )


def visible(doc: SubtitleDocument, cue: Cue) -> tuple[str, list[int], str]:
    raw = doc.content[cue.start : cue.end]
    chars, positions = [], []
    unsafe = ""
    if doc.format == "ass":
        tags = re.findall(r"\{[^}]*\}", raw)
        if any(re.search(r"\\(?:[kK](?:f|o|t)?\d|p\s*[1-9])", tag) for tag in tags):
            unsafe = "Караоке или векторная графика ASS — требуется ручное редактирование"
        token = re.compile(r"\{[^}]*\}|\\[Nnh]")
    else:
        token = re.compile(r"<[^>]*>|\{\\[^}]*\}")
    cursor = 0
    for match in token.finditer(raw):
        for j in range(cursor, match.start()):
            chars.append(raw[j])
            positions.append(cue.start + j)
        if match.group() in {r"\N", r"\n", r"\h"}:
            chars.append(" ")
            positions.append(-1)
        cursor = match.end()
    for j in range(cursor, len(raw)):
        chars.append(raw[j])
        positions.append(cue.start + j)
    return "".join(chars), positions, unsafe
