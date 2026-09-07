from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from app.auth.models import Role
from app.auth.passwords import MIN_PASSWORD_LENGTH


class Credentials(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def _strong_enough(cls, v: str) -> str:
        if len(v) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    """Never carries the hash. The model is built from named fields only."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: Role
    is_active: bool
    created_at: datetime


class RoleUpdate(BaseModel):
    role: Role


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetSubmit(BaseModel):
    token: str
    password: str

    @field_validator("password")
    @classmethod
    def _strong_enough(cls, v: str) -> str:
        if len(v) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        return v

    @field_validator("token")
    @classmethod
    def _present(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("token is required")
        return v
