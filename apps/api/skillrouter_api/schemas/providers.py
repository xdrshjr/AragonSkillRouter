"""提供商与凭证 DTO（响应中 secret 只出 mask）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProviderCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=128)
    category: str = "llm"
    base_url: str | None = None
    docs_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    environment: str = "prod"
    notes: str | None = None
    status: str = "active"


class ProviderUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    base_url: str | None = None
    docs_url: str | None = None
    tags: list[str] | None = None
    environment: str | None = None
    notes: str | None = None
    status: str | None = None


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = "llm"
    auth_type: str = "api_key"
    header_name: str | None = None
    secret: str = Field(min_length=1, max_length=8192)
    meta: dict = Field(default_factory=dict)
    expires_at: str | None = None


class CredentialUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    auth_type: str | None = None
    header_name: str | None = None
    meta: dict | None = None
    expires_at: str | None = None
    status: str | None = None


class SecretReplace(BaseModel):
    secret: str = Field(min_length=1, max_length=8192)


class CredentialRotate(BaseModel):
    secret: str = Field(min_length=1, max_length=8192)
    name: str | None = None
