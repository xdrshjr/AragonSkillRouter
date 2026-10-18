"""SKILL DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SkillCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    when_to_use: str | None = None
    inputs: list[Any] = Field(default_factory=list)
    outputs: list[Any] = Field(default_factory=list)
    dependencies: list[dict] = Field(default_factory=list)
    instructions: str = ""
    examples: list[Any] = Field(default_factory=list)
    permissions: dict = Field(default_factory=dict)
    execution_plan: dict = Field(default_factory=dict)
    version: str = "0.1.0"
    activation: str = "auto"


class SkillUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    when_to_use: str | None = None
    inputs: list[Any] | None = None
    outputs: list[Any] | None = None
    dependencies: list[dict] | None = None
    instructions: str | None = None
    examples: list[Any] | None = None
    permissions: dict | None = None
    execution_plan: dict | None = None
    activation: str | None = None


class ImportPreviewRequest(BaseModel):
    kind: str = "url"
    url: str | None = None
    ref: str | None = None
    upload_id: str | None = None
    text: str | None = None


class ImportCommitRequest(BaseModel):
    preview_token: str
    name: str | None = None
    title: str | None = None


class ExportRequest(BaseModel):
    format: str = "zip"


class SkillTestRequest(BaseModel):
    input: dict = Field(default_factory=dict)
    version: str | None = None


class PublishSkillRequest(BaseModel):
    version: str | None = None
    changelog: str | None = None
