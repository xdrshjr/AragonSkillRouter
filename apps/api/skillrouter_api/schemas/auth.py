"""认证 DTO（§9.2）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class AdminInfo(BaseModel):
    id: str
    username: str
    must_change_password: bool


class LoginResponse(BaseModel):
    admin: AdminInfo
    csrf_token: str
    expires_at: str | None = None


class BootstrapResponse(BaseModel):
    initialized: bool
    must_change_password: bool = False
    product: str = "Skill Router"
    version: str
    default_credentials: bool = False
