from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def uid() -> str:
    return uuid4().hex


def stable_id(*parts: object) -> str:
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:24]


class Cue(BaseModel):
    id: str
    start: int
    end: int
    timing: str


class SubtitleDocument(BaseModel):
    id: str = Field(default_factory=uid)
    source_path: str
    format: Literal["ass", "srt"]
    content: str
    encoding: str = "utf-8"
    cues: list[Cue]
    track_id: int | None = None
    track_label: str = ""
    font_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_spans(self):
        previous = 0
        for cue in self.cues:
            if not 0 <= previous <= cue.start <= cue.end <= len(self.content):
                raise ValueError("Некорректные границы реплик")
            previous = cue.end
        return self


class Alias(BaseModel):
    id: str = Field(default_factory=uid)
    text: str
    replacement: str = ""


class CharacterGroup(BaseModel):
    id: str = Field(default_factory=uid)
    label: str
    replacement_name: str = ""
    aliases: list[Alias] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class Occurrence(BaseModel):
    id: str
    document_id: str
    cue_id: str
    alias_id: str
    start: int
    end: int
    text: str
    safe: bool = True
    reason: str = ""


class ReplacementRule(BaseModel):
    alias_id: str
    source: str
    target: str


class Project(BaseModel):
    schema_version: Literal[1] = 1
    base_root: str = ""
    model: str = "gemini-3.8-flash"
    documents: list[SubtitleDocument] = Field(default_factory=list)
    groups: list[CharacterGroup] = Field(default_factory=list)
    excluded: set[str] = Field(default_factory=set)
    analysis_skipped_document_ids: set[str] = Field(default_factory=set)

    def save(self, path: str | Path):
        path = Path(path)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path: str | Path) -> Project:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
