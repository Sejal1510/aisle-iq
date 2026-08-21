from __future__ import annotations

from pydantic import BaseModel

from app.models.enums import Role


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class StoreGrantOut(BaseModel):
    store_id: str
    role: Role


class MeResponse(BaseModel):
    user_id: str
    email: str
    store_access: list[StoreGrantOut]


class StoreAccessGrantRequest(BaseModel):
    email: str
    role: Role


class StoreAccessGrantResponse(BaseModel):
    store_id: str
    email: str
    role: Role
