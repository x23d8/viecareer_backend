from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: Literal["user", "admin"]
    avatar_url: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class AuthSyncUserRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    full_name: str | None = None
    email: EmailStr
    avatar_url: str | None = None
    auth_provider: str = "clerk"


class AcceptLegalPoliciesRequest(BaseModel):
    terms_version: str = Field(..., min_length=1)
    privacy_policy_version: str = Field(..., min_length=1)
