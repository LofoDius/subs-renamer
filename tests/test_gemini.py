import json
import threading

import pytest

from subs_renamer.gemini import Extracted, GeminiAnalyzer, Grouped, chunks, validate_groups
from subs_renamer.models import Alias, CharacterGroup, Project
from subs_renamer.subtitles import parse


@pytest.mark.parametrize("invalid", [None, "missing", "duplicate", "unknown", "empty"])
def test_generate_replacements_validates_mapping_and_preserves_project(tmp_path, invalid):
    from subs_renamer.gemini import GeneratedReplacements

    doc = parse("example.srt", content="1\n00:00:01,000 --> 00:00:02,000\nРэн встретил Рэна.\n")
    group = CharacterGroup(label="Рэн", replacement_name="Иван", aliases=[
        Alias(text="Рэн"), Alias(text="Рэна", replacement="Рена"),
    ])
    project = Project(documents=[doc], groups=[group])
    before = project.model_dump()
    calls = []

    def transport(instruction, payload, schema):
        calls.append(payload)
        assert schema is GeneratedReplacements
        assert payload["replacement_name"] == "Иван"
        assert payload["aliases"][1]["contexts"] == ["Рэн встретил Рэна."]
        rows = [{"alias_id": a.id, "replacement": target}
                for a, target in zip(group.aliases, ["Иван", "Ивана"])]
        if invalid == "missing":
            rows.pop()
        elif invalid == "duplicate":
            rows.append(rows[0])
        elif invalid == "unknown":
            rows[0]["alias_id"] = "unknown"
        elif invalid == "empty":
            rows[0]["replacement"] = " "
        return json.dumps({"replacements": rows})

    analyzer = GeminiAnalyzer("fake", cache=tmp_path, transport=transport)
    if invalid:
        with pytest.raises(RuntimeError):
            analyzer.generate_replacements(project, group, threading.Event())
        assert not list(tmp_path.iterdir())
    else:
        assert analyzer.generate_replacements(project, group, threading.Event()) == {
            group.aliases[0].id: "Иван", group.aliases[1].id: "Ивана",
        }
        analyzer.generate_replacements(project, group, threading.Event())
        assert len(calls) == 1
        filename = tmp_path / "project.json"
        project.save(filename)
        assert Project.load(filename).groups[0].replacement_name == "Иван"
    assert project.model_dump() == before


@pytest.mark.parametrize("kind", ["prompt", "candidate"])
def test_blocked_response_skips_without_retry(tmp_path, kind):
    from google.genai import types

    from subs_renamer.gemini import ContentBlockedError, response_text

    response = types.GenerateContentResponse.model_validate(
        {"prompt_feedback": {"block_reason": "PROHIBITED_CONTENT"}}
        if kind == "prompt" else {"candidates": [{"finish_reason": "SAFETY"}]}
    )
    calls = []

    def transport(*args):
        calls.append(1)
        return response_text(response)

    analyzer = GeminiAnalyzer("fake", cache=tmp_path, transport=transport)
    with pytest.raises(ContentBlockedError):
        analyzer.request("test", {}, Extracted, threading.Event())
    assert len(calls) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("all_blocked", [False, True])
def test_blocked_files_continue_analysis(tmp_path, all_blocked):
    from subs_renamer.gemini import ContentBlockedError

    docs = [parse(f"{name}.srt", content=f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n")
            for name, text in [("blocked", "Блок"), ("ok", "Рэн")]]
    project = Project(documents=docs)
    seen = []

    def transport(instruction, payload, schema):
        seen.append(payload)
        if schema == Extracted:
            record = payload["subtitles"][0]
            if all_blocked or record["file"] == "blocked.srt":
                raise ContentBlockedError()
            return json.dumps({"mentions": [{"name": "Рэн", "cue_id": record["cue_id"]}]})
        assert all(r["file"] != "blocked.srt" for r in payload["subtitles"])
        # A grouping refusal must preserve the extracted aliases.
        raise ContentBlockedError()

    analyzer = GeminiAnalyzer("fake", cache=tmp_path, transport=transport)
    result = analyzer.analyze(project, threading.Event())
    assert project.analysis_skipped_document_ids == {d.id for d in (docs if all_blocked else docs[:1])}
    assert [a.text for g in result for a in g.aliases] == ([] if all_blocked else ["Рэн"])
    assert len(seen) == (2 if all_blocked else 3)


def test_partial_blocking_does_not_mark_file_or_resend_blocked_context(tmp_path, monkeypatch):
    from subs_renamer import gemini

    doc = parse("partial.srt", content=(
        "1\n00:00:01,000 --> 00:00:02,000\nБлок\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nРэн\n"
    ))
    monkeypatch.setattr(gemini, "chunks", lambda records, **kwargs: ([r] for r in records))
    project = Project(documents=[doc], analysis_skipped_document_ids={doc.id})

    def transport(instruction, payload, schema):
        record = payload["subtitles"][0]
        if record["text"] == "Блок":
            assert schema == Extracted
            raise gemini.ContentBlockedError()
        if schema == Extracted:
            return json.dumps({"mentions": [{"name": "Рэн", "cue_id": record["cue_id"]}]})
        return '{"groups": []}'

    analyzer = GeminiAnalyzer("fake", cache=tmp_path, transport=transport)
    assert analyzer.analyze(project, threading.Event())
    assert not project.analysis_skipped_document_ids


def test_available_models_filters_sorts_and_uses_proxy(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from subs_renamer import gemini

    client = MagicMock()
    client.__enter__.return_value = client
    client.models.list.return_value = iter([
        SimpleNamespace(name="models/gemini-3.5-flash-lite", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/embedding", supported_actions=["embedContent"]),
        SimpleNamespace(name="models/gemini-3.8-flash", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/nano-banana-pro-preview", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/gemini-3.1-flash-image-preview", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/gemini-3.1-pro-preview", supported_actions=["embedContent"]),
        SimpleNamespace(name="models/gemini-3.8-flash", supported_actions=["generateContent"]),
        SimpleNamespace(name=None, supported_actions=None),
    ])
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("google.genai.Client", factory)
    monkeypatch.setattr(gemini, "configured_api_key", lambda: "test-key")
    assert gemini.available_models("localhost:2080") == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    options = factory.call_args.kwargs["http_options"]
    assert options.client_args["proxy"] == "socks5://localhost:2080"
    client.__exit__.assert_called_once()


def test_available_models_redacts_errors(monkeypatch):
    from subs_renamer import gemini

    monkeypatch.setattr(gemini, "configured_api_key", lambda: "test-key")
    def fail(**kwargs):
        raise RuntimeError("SECRET proxy request")
    monkeypatch.setattr("google.genai.Client", fail)
    with pytest.raises(RuntimeError, match="Не удалось получить модели") as error:
        gemini.available_models()
    assert "SECRET" not in str(error.value)


def test_chunking_covers_all_and_overlaps():
    records = [{"cue_id": str(i), "text": "я" * 100} for i in range(25)]
    blocks = list(chunks(records, limit=800))
    assert {r["cue_id"] for b in blocks for r in b} == {str(i) for i in range(25)}
    assert set(r["cue_id"] for r in blocks[0]) & set(r["cue_id"] for r in blocks[1])


def test_invalid_references_missing_aliases_and_duplicates(tmp_path):
    doc = parse("example.srt", content="1\n00:00:01,000 --> 00:00:02,000\nРэн — это Лис.\n")
    cue_id = doc.cues[0].id
    calls = []

    def fake(instruction, payload, schema):
        calls.append(schema)
        if schema == Extracted:
            return json.dumps(
                {
                    "mentions": [
                        {"name": "Рэн", "cue_id": cue_id},
                        {"name": "Рэн", "cue_id": cue_id},
                        {"name": "Лис", "cue_id": cue_id},
                        {"name": "Иван", "cue_id": cue_id},
                        {"name": "Рэн", "cue_id": "missing"},
                    ]
                }
            )
        return json.dumps(
            {
                "groups": [
                    {
                        "label": "Рэн",
                        "aliases": ["Рэн", "Лис", "Иван"],
                        "evidence_cue_ids": [cue_id, "missing"],
                    }
                ]
            }
        )

    analyzer = GeminiAnalyzer("fake", cache=tmp_path, transport=fake)
    groups = analyzer.analyze(Project(documents=[doc]), threading.Event())
    assert len(groups) == 1
    assert {a.text for a in groups[0].aliases} == {"Рэн", "Лис"}
    analyzer.analyze(Project(documents=[doc]), threading.Event())
    assert len(calls) == 2


def test_unproven_group_does_not_merge_or_lose_aliases():
    previous = [CharacterGroup(label=n, aliases=[Alias(text=n)]) for n in ["Рэн", "Лис"]]
    response = Grouped.model_validate(
        {"groups": [{"label": "Герой", "aliases": ["Рэн", "Лис"], "evidence_cue_ids": ["missing"]}]}
    )
    assert len(validate_groups(response, {"рэн": "Рэн", "лис": "Лис"}, {}, previous)) == 2


def test_retry_malformed_json_and_cancellation(tmp_path):
    calls = []

    def fake(*args):
        calls.append(1)
        return "broken" if len(calls) == 1 else '{"mentions": []}'

    analyzer = GeminiAnalyzer("fake", cache=tmp_path, transport=fake)
    result = analyzer.request("test", {}, Extracted, threading.Event())
    assert result.mentions == [] and len(calls) == 2
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        analyzer.request("test", {}, Extracted, cancel)


def test_nontransient_errors_are_redacted(tmp_path):
    def fake(*args):
        raise RuntimeError("SECRET-API-KEY should never be displayed")

    analyzer = GeminiAnalyzer("fake", cache=tmp_path, transport=fake)
    with pytest.raises(RuntimeError) as error:
        analyzer.request("test", {}, Extracted, threading.Event())
    assert "SECRET" not in str(error.value)
