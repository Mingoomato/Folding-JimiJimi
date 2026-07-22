from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OAuthStartRequest(BaseModel):
    redirect_uri: str = Field(max_length=512)
    code_challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class OAuthStartResponse(BaseModel):
    authorization_url: str
    state: str
    expires_at: datetime


class OAuthExchangeRequest(BaseModel):
    state: str = Field(min_length=32, max_length=256)
    code: str = Field(min_length=1, max_length=4_096)
    code_verifier: str = Field(pattern=r"^[A-Za-z0-9._~-]{43,128}$")


class SessionRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class SessionResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    authenticated: bool = True
    subject_id: str
    email: str
    name: str | None = None
    picture: str | None = None


class GoogleIdentity(BaseModel):
    subject_id: str
    email: str
    name: str | None = None
    picture: str | None = None
