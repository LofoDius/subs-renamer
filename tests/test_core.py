import threading
from pathlib import Path

import pytest

from subs_renamer.engine import export, frequency, occurrences, preview, rendered
from subs_renamer.models import Alias, CharacterGroup, Project
from subs_renamer.subtitles import parse


def srt(text):
    return f"1\r\n00:00:01,100 --> 00:00:03,200\r\n{text}\r\n"


def ass(text):
    return (
        "[Script Info]\r\nTitle: Рэн\r\n[Events]\r\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
        f"Dialogue: 0,0:00:01.10,0:00:03.20,Default,Рэн,0,0,0,,{text}\r\n"
    )


def project(text, mapping, format_="srt"):
    doc = parse("episode." + format_, content=(srt if format_ == "srt" else ass)(text))
    return Project(
        documents=[doc],
        groups=[
            CharacterGroup(
                label="Герой",
                aliases=[Alias(text=source, replacement=target) for source, target in mapping.items()],
            )
        ],
    )


def transformed(p):
    changes, _ = preview(p)
    return rendered(p, changes)[p.documents[0].id]


def test_simultaneous_case_boundaries_and_honorifics():
    p = project("Рэн РЭН рэн Рэнтаро Рэн-кун Лис.", {"Рэн": "Рен", "Рен": "Другой", "Лис": "Тень"})
    result = transformed(p)
    assert "Рен РЕН рен Рэнтаро Рен-кун Тень." in result
    assert "Другой" not in result
    assert result == transformed(p)
    assert "Рэн РЭН" in p.documents[0].content


def test_longest_match_and_no_fallback_after_exclusion():
    p = project("Рэн Ямада пришёл.", {"Рэн": "Рен", "Рэн Ямада": "Тень"})
    changes, _ = preview(p)
    assert frequency(p, occurrences(p))[p.groups[0].id] == 1
    assert len(changes) == 1 and changes[0].replacement == "Тень"
    p.excluded.add(changes[0].occurrence.id)
    assert transformed(p) == p.documents[0].content


def test_conflicts_require_resolution():
    p = project("Рэн", {"Рэн": "Рен", "РЭН": "Лис"})
    with pytest.raises(ValueError, match="Конфликт"):
        preview(p)


@pytest.mark.parametrize("replacement", ["Рен\n2", r"{\i1}Рен", "<b>Рен", r"Рен\N"])
def test_replacement_cannot_inject_markup_or_structure(replacement):
    with pytest.raises(ValueError, match="только текст"):
        preview(project("Рэн", {"Рэн": replacement}))


def test_ass_preserves_nontext_and_timing():
    p = project(r"{\i1}Рэн{\i0}, привет!", {"Рэн": "Рен"}, "ass")
    original = p.documents[0].content
    result = transformed(p)
    assert result == original.replace(r"{\i1}Рэн{\i0}", r"{\i1}Рен{\i0}")
    assert "Title: Рэн" in result and ",Default,Рэн," in result
    assert parse("result.ass", content=result).cues[0].timing == p.documents[0].cues[0].timing


@pytest.mark.parametrize("text", [r"Р{\i1}эн", r"{\k20}Рэн", r"{\p1}Рэн{\p0}"])
def test_unsafe_ass_is_skipped(text):
    p = project(text, {"Рэн": "Рен"}, "ass")
    changes, skipped = preview(p)
    assert not changes and len(skipped) == 1 and skipped[0].reason


def test_srt_html_tags_and_explicit_inflection():
    p = project("<i>Рэн</i>, я жду Рэна. Рэну привет.", {"Рэн": "Рен", "Рэна": "Рена", "Рэну": "Рену"})
    assert "<i>Рен</i>, я жду Рена. Рену привет." in transformed(p)


def test_homonym_exclusion():
    p = project("Лис — это человек. А в лесу живёт лис.", {"Лис": "Тень"})
    found = occurrences(p)
    p.excluded.add(found[1].id)
    assert "Тень — это человек. А в лесу живёт лис." in transformed(p)


def test_encoding_and_invalid_file(tmp_path):
    path = tmp_path / "legacy.crt"
    path.write_bytes(srt("Рэн").encode("cp1251"))
    assert parse(path).encoding == "cp1251"
    path.write_text("это не субтитры", encoding="utf-8")
    with pytest.raises(ValueError):
        parse(path)


def test_project_roundtrip_and_schema(tmp_path):
    p = project("Рэн", {"Рэн": "Рен"})
    p.excluded.add(occurrences(p)[0].id)
    target = tmp_path / "project.json"
    p.save(target)
    loaded = Project.load(target)
    assert loaded == p
    assert "key" not in target.read_text(encoding="utf-8")
    target.write_text('{"schema_version":2}', encoding="utf-8")
    with pytest.raises(ValueError):
        Project.load(target)


def test_season_25_export_no_overwrite_and_unchanged_sources(tmp_path):
    source = tmp_path / "season"
    source.mkdir()
    p = Project(
        base_root=str(source),
        groups=[CharacterGroup(label="Рэн", aliases=[Alias(text="Рэн", replacement="Рен")])],
    )
    for i in range(25):
        path = source / f"ep{i:02}.srt"
        path.write_text(srt("Рэн пришёл."), encoding="utf-8", newline="")
        p.documents.append(parse(path))
    assert frequency(p, occurrences(p))[p.groups[0].id] == 25
    saved = export(p, tmp_path / "output")
    assert len(saved) == 25
    for path in source.glob("*.srt"):
        assert "Рэн" in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        export(p, tmp_path / "output")
    result = parse(saved[0])
    assert "Рен" in result.content
    assert result.cues[0].timing == p.documents[0].cues[0].timing


def test_cancel_before_export(tmp_path):
    p = project("Рэн", {"Рэн": "Рен"})
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        export(p, tmp_path, cancel)
    assert not list(tmp_path.glob("*.srt"))


def test_export_continues_after_one_write_failure(tmp_path, monkeypatch):
    p = project("Рэн", {"Рэн": "Рен"})
    second = parse("other.srt", content=srt("Рэн"))
    p.documents.append(second)
    original_rename = Path.rename

    def fail_first(path, target):
        if target.name.startswith("episode."):
            raise PermissionError("Synthetic write failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_first)
    with pytest.raises(RuntimeError, match="Сохранено файлов: 1"):
        export(p, tmp_path)
    assert not (tmp_path / "episode.renamed.srt").exists()
    assert (tmp_path / "other.renamed.srt").exists()
    assert not list(tmp_path.glob(".subs-renamer-*.tmp"))


def test_replace_originals_preflights_all_files_and_cancellation(tmp_path):
    import threading

    from subs_renamer.engine import replace_originals

    first, second = tmp_path / "one.srt", tmp_path / "two.srt"
    content = "1\n00:00:01,000 --> 00:00:02,000\nRen!\n"
    for path in (first, second):
        path.write_text(content, encoding="utf-8")
    project = Project(documents=[parse(first), parse(second)], groups=[
        CharacterGroup(label="Ren", aliases=[Alias(text="Ren", replacement="New")])
    ])
    second.write_text(content + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        replace_originals(project)
    assert first.read_text(encoding="utf-8") == content
    second.write_text(content, encoding="utf-8")
    cancel = threading.Event()
    with pytest.raises(InterruptedError):
        replace_originals(project, cancel, lambda *args: cancel.set())
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8") == content
    assert not list(tmp_path.glob(".subs-renamer-*"))
