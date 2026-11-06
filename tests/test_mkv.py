import zipfile
from pathlib import Path

import pytest

from subs_renamer import mkv as mkv_module
from subs_renamer.mkv import MKVTools, unpack_tools
from subs_renamer.subtitles import parse


def track(id_, language="rus", codec="S_TEXT/UTF8"):
    return {"id": id_, "type": "subtitles", "properties": {"language": language, "codec_id": codec}}


def test_selection_is_per_file_and_requires_unambiguous_russian():
    assert MKVTools.automatic_track({"tracks": [track(3)]}) == 3
    assert MKVTools.automatic_track({"tracks": [track(7)]}) == 7
    assert MKVTools.automatic_track({"tracks": [track(3), track(7)]}) is None
    assert MKVTools.automatic_track({"tracks": [track(3, "eng")]}) is None
    assert MKVTools.automatic_track({"tracks": [track(3, codec="S_HDMV/PGS")]}) is None


def test_embedded_archive_is_self_sufficient_and_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBS_RENAMER_DATA", str(tmp_path / "cache"))
    archive = tmp_path / "mkvtoolnix.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("mkvmerge.exe", b"synthetic exe")
        package.writestr("mkvextract.exe", b"synthetic exe")
        package.writestr("doc/COPYING.txt", "synthetic license")
    destination = unpack_tools(archive)
    marker_time = (destination / ".complete").stat().st_mtime_ns
    assert unpack_tools(archive) == destination
    assert (destination / ".complete").stat().st_mtime_ns == marker_time
    monkeypatch.setattr(mkv_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mkv_module, "resource_dir", lambda: tmp_path)
    assert Path(MKVTools().executable("mkvmerge")) == destination / "mkvmerge.exe"


def test_unsafe_embedded_archive_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBS_RENAMER_DATA", str(tmp_path / "cache"))
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../escape.exe", b"invalid")
    with pytest.raises(ValueError, match="Повреждённый"):
        unpack_tools(archive)


def test_real_multitrack_mkv(tmp_path, monkeypatch):
    tools = MKVTools(str(Path(__file__).resolve().parents[1] / "vendor" / "mkvtoolnix"))
    try:
        tools.executable("mkvmerge")
    except FileNotFoundError:
        pytest.skip("Run scripts/prepare_vendor.py for MKV integration tests")
    monkeypatch.setenv("SUBS_RENAMER_DATA", str(tmp_path / "data"))
    first, second = tmp_path / "ru.srt", tmp_path / "en.srt"
    first.write_text("1\n00:00:01,000 --> 00:00:02,000\nРэн!\n", encoding="utf-8")
    second.write_text("1\n00:00:01,000 --> 00:00:02,000\nRen!\n", encoding="utf-8")
    mkv = tmp_path / "test.mkv"
    tools.run(
        "mkvmerge", ["-o", str(mkv), "--language", "0:rus", str(first), "--language", "0:eng", str(second)]
    )
    info = tools.probe(mkv)
    assert len(tools.tracks(info)) == 2
    selected = tools.automatic_track(info)
    extracted = tools.extract(mkv, info, selected)
    assert "Рэн!" in extracted.content
    assert extracted.cues[0].timing == parse(first).cues[0].timing

    from subs_renamer.engine import replace_originals
    from subs_renamer.models import Alias, CharacterGroup, Project

    project = Project(documents=[extracted], groups=[
        CharacterGroup(label="Ren", aliases=[Alias(text="\u0420\u044d\u043d", replacement="Renamed")])
    ])
    assert replace_originals(project) == [str(mkv.resolve())]
    updated = tools.probe(mkv)
    assert len(tools.tracks(updated)) == 2
    russian = tools.extract(mkv, updated, tools.automatic_track(updated))
    assert "Renamed!" in russian.content
    english_id = next(t["id"] for t in tools.tracks(updated) if t["properties"]["language"] == "eng")
    assert "Ren!" in tools.extract(mkv, updated, english_id).content
