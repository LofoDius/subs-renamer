from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Occurrence, Project, ReplacementRule, stable_id, uid
from .subtitles import visible


def pattern(text: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(text) + r"(?!\w)", re.IGNORECASE)


def occurrences(project: Project, cancel=None) -> list[Occurrence]:
    aliases = [a for g in project.groups for a in g.aliases if a.text.strip()]
    compiled = [(a, pattern(a.text)) for a in aliases]
    found = []
    for doc in project.documents:
        for cue in doc.cues:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Анализ отменён")
            text, positions, reason = visible(doc, cue)
            for alias, regex in compiled:
                for match in regex.finditer(text):
                    mapping = positions[match.start() : match.end()]
                    contiguous = (
                        bool(mapping)
                        and mapping[0] >= 0
                        and all(b == a + 1 for a, b in zip(mapping, mapping[1:]))
                    )
                    start = next((p for p in mapping if p >= 0), cue.start)
                    end = next((p + 1 for p in reversed(mapping) if p >= 0), start)
                    found.append(
                        Occurrence(
                            id=stable_id(doc.id, cue.id, start, end, alias.text.casefold()),
                            document_id=doc.id,
                            cue_id=cue.id,
                            alias_id=alias.id,
                            start=start,
                            end=end,
                            text=match.group(),
                            safe=contiguous and not reason,
                            reason=reason
                            or ("Имя пересекает теги или перенос строки" if not contiguous else ""),
                        )
                    )
    return found


def frequency(project: Project, found: list[Occurrence]) -> dict[str, int]:
    result = {}
    for group in project.groups:
        aliases = {a.id for a in group.aliases}
        candidates = sorted(
            (o for o in found if o.alias_id in aliases and o.id not in project.excluded),
            key=lambda o: (o.document_id, o.cue_id, o.start, -o.end),
        )
        count, previous_key, end = 0, None, -1
        for occurrence in candidates:
            key = (occurrence.document_id, occurrence.cue_id)
            if key != previous_key or occurrence.start >= end:
                count += 1
                end = occurrence.end
            else:
                end = max(end, occurrence.end)
            previous_key = key
        result[group.id] = count
    return result


def rules(project: Project) -> list[ReplacementRule]:
    result, seen = [], {}
    for group in project.groups:
        for alias in group.aliases:
            if not alias.text.strip() or not alias.replacement.strip():
                continue
            if any(char in alias.replacement for char in "\r\n{}<>\\"):
                raise ValueError(
                    f"Замена для «{alias.text}» должна содержать только текст, без переносов и тегов."
                )
            key = alias.text.casefold()
            if key in seen and seen[key] != alias.replacement:
                raise ValueError(
                    f"Конфликт замен для «{alias.text}». Задайте одинаковую замену или удалите дубль."
                )
            if key not in seen:
                seen[key] = alias.replacement
                result.append(ReplacementRule(alias_id=alias.id, source=alias.text, target=alias.replacement))
    return result


def recase(source: str, target: str) -> str:
    if source.isupper():
        return target.upper()
    if source.islower():
        return target.lower()
    if source[:1].isupper() and source[1:].islower():
        return target[:1].upper() + target[1:]
    return target


@dataclass
class Change:
    occurrence: Occurrence
    replacement: str


def preview(project: Project) -> tuple[list[Change], list[Occurrence]]:
    configured = rules(project)
    # Prefer longest matches, regardless of whether a particular occurrence was excluded.
    by_alias = {r.alias_id: r for r in configured}
    by_text = {r.source.casefold(): r for r in configured}
    for group in project.groups:
        for alias in group.aliases:
            if alias.text.casefold() in by_text:
                by_alias[alias.id] = by_text[alias.text.casefold()]
    found = [o for o in occurrences(project) if o.alias_id in by_alias]
    found.sort(key=lambda o: (o.document_id, o.cue_id, -(o.end - o.start), o.start))
    selected, skipped, occupied = [], [], {}
    for occurrence in found:
        key = (occurrence.document_id, occurrence.cue_id)
        spans = occupied.setdefault(key, [])
        if any(occurrence.start < end and occurrence.end > start for start, end in spans):
            continue
        spans.append((occurrence.start, occurrence.end))
        if occurrence.id in project.excluded:
            continue
        if not occurrence.safe:
            skipped.append(occurrence)
            continue
        target = recase(occurrence.text, by_alias[occurrence.alias_id].target)
        if target != occurrence.text:
            selected.append(Change(occurrence, target))
    return selected, skipped


def rendered(project: Project, changes: list[Change]) -> dict[str, str]:
    output = {}
    for doc in project.documents:
        content = doc.content
        edits = sorted(
            (c for c in changes if c.occurrence.document_id == doc.id),
            key=lambda c: c.occurrence.start,
            reverse=True,
        )
        for change in edits:
            o = change.occurrence
            content = content[: o.start] + change.replacement + content[o.end :]
        output[doc.id] = content
    return output


def output_paths(project: Project, directory: Path) -> dict[str, Path]:
    result, seen = {}, set()
    for doc in project.documents:
        source = Path(doc.source_path)
        try:
            relative = source.resolve().relative_to(Path(project.base_root).resolve())
        except ValueError:
            relative = Path(source.name)
        track = f".track{doc.track_id}" if doc.track_id is not None else ""
        destination = directory / relative.parent / f"{relative.stem}{track}.renamed.{doc.format}"
        key = os.path.normcase(str(destination.resolve()))
        if key in seen:
            raise ValueError("Совпадающие имена результатов. Выберите другой набор файлов.")
        seen.add(key)
        if destination.exists():
            raise FileExistsError(f"Результат уже существует: {destination}")
        if destination.resolve() == source.resolve():
            raise ValueError("Нельзя перезаписать исходный файл")
        result[doc.id] = destination
    return result


def export(project: Project, directory: str | Path, cancel=None, progress=None) -> list[str]:
    import shutil
    import tempfile

    directory = Path(directory)
    changes, skipped = preview(project)
    paths = output_paths(project, directory)
    output = rendered(project, changes)
    saved, errors, written_changes = [], [], 0
    cancelled = False
    for i, doc in enumerate(project.documents):
        if cancel and cancel.is_set():
            cancelled = True
            break
        destination = paths[doc.id]
        temporary = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=destination.parent,
                prefix=".subs-renamer-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(output[doc.id])
            if os.name == "nt":
                # Windows rename refuses to replace an existing destination, including races after preflight.
                temporary.rename(destination)
            else:
                os.link(temporary, destination)
                temporary.unlink()
            saved.append(str(destination))
            written_changes += sum(c.occurrence.document_id == doc.id for c in changes)
            for font in doc.font_paths:
                source = Path(font)
                if source.is_file():
                    target = destination.parent / "fonts" / f"{source.parent.name}-{source.name}"
                    target.parent.mkdir(exist_ok=True)
                    if not target.exists():
                        shutil.copyfile(source, target)
                else:
                    errors.append(f"Шрифт больше недоступен в кэше: {source.name}")
        except OSError as error:
            errors.append(f"{destination}: {error}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if progress:
            progress(i + 1, len(project.documents), destination.name)
    report = {
        "saved": saved,
        "replacements": written_changes,
        "skipped": [o.model_dump() for o in skipped],
        "errors": errors,
        "cancelled": cancelled,
    }
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / f"subs-renamer-report-{uid()}.json"
    # Reports contain source text and are deliberately excluded from source control.
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    if cancelled:
        raise InterruptedError(f"Сохранение отменено. Сохранено файлов: {len(saved)}. Отчёт: {report_path}")
    if errors:
        raise RuntimeError(
            f"Сохранено файлов: {len(saved)} из {len(project.documents)}. "
            f"Ошибок: {len(errors)}. Отчёт: {report_path}\n" + "\n".join(errors[:5])
        )
    return saved


def replace_originals(project: Project, cancel=None, progress=None) -> list[str]:
    """Prepare all results before atomically replacing each original file."""
    import tempfile
    from contextlib import ExitStack

    from .mkv import MKVTools
    from .subtitles import parse

    changes, _ = preview(project)
    output = rendered(project, changes)
    sources = {}
    for doc in project.documents:
        source = Path(doc.source_path).resolve()
        if source in sources:
            raise ValueError(f"Несколько дорожек одного файла: {source}")
        if not source.is_file():
            raise FileNotFoundError(f"Исходный файл недоступен: {source}")
        sources[source] = doc

    saved = []
    with ExitStack() as stack:
        prepared = []
        for source, doc in sources.items():
            if cancel and cancel.is_set():
                raise InterruptedError("Сохранение отменено. Оригиналы не изменены.")
            stamp = source.stat()
            folder = Path(stack.enter_context(tempfile.TemporaryDirectory(
                prefix=".subs-renamer-", dir=source.parent
            )))
            subtitle = folder / f"subtitles.{doc.format}"
            if doc.track_id is None:
                if parse(source).content != doc.content:
                    raise ValueError(f"Исходный файл изменился после загрузки: {source}")
                subtitle.write_text(output[doc.id], encoding=doc.encoding, newline="")
                result = subtitle
            else:
                tools = MKVTools()
                info = tools.probe(source, cancel)
                tracks = info.get("tracks", [])
                track = next((t for t in tracks if t["id"] == doc.track_id), None)
                if track is None or track.get("type") != "subtitles":
                    raise ValueError(f"Дорожка субтитров больше недоступна: {source}")
                subtitle.write_text(output[doc.id], encoding="utf-8", newline="")
                props = track.get("properties", {})
                result = folder / "result.mkv"
                remaining = [str(t["id"]) for t in tools.tracks(info) if t["id"] != doc.track_id]
                selection = ["--subtitle-tracks", ",".join(remaining)] if remaining else ["--no-subtitles"]
                order = ["1:0" if t["id"] == doc.track_id else f"0:{t['id']}" for t in tracks]
                tools.run("mkvmerge", [
                    "-o", str(result), "--track-order", ",".join(order),
                    *selection, str(source),
                    "--language", f"0:{props.get('language_ietf') or props.get('language', 'und')}",
                    "--track-name", f"0:{props.get('track_name', '')}",
                    "--default-track-flag", f"0:{int(props.get('default_track', False))}",
                    "--forced-display-flag", f"0:{int(props.get('forced_track', False))}",
                    str(subtitle),
                ], cancel)
            prepared.append((source, result, stamp))
            if progress:
                progress(len(prepared), len(sources), f"Подготовка: {source.name}")
        if cancel and cancel.is_set():
            raise InterruptedError("Сохранение отменено. Оригиналы не изменены.")
        for source, _, stamp in prepared:
            current = source.stat()
            if (current.st_size, current.st_mtime_ns) != (stamp.st_size, stamp.st_mtime_ns):
                raise ValueError(f"Исходный файл изменился во время сохранения: {source}")
        for source, result, _ in prepared:
            try:
                result.replace(source)
            except OSError as error:
                raise RuntimeError(
                    f"Заменено файлов: {len(saved)} из {len(prepared)}. {source}: {error}"
                ) from error
            saved.append(str(source))
    return saved
