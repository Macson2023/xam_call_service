import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_STUN = [{"urls": ["stun:stun.l.google.com:19302"]}]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "xam_call_service"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    auth_mode: Literal["jwt", "introspect", "backend"] = "jwt"
    secret_key: str = "dev-secret-key-change-me-32-bytes-min"
    jwt_algorithms: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["HS256"])
    jwt_issuer: str | None = None
    jwt_audience: str | None = None

    oauth2_jwks_url: str | None = None
    oauth2_introspection_url: str | None = None
    oauth2_client_id: str | None = None
    oauth2_client_secret: str | None = None

    auth_enrich_userinfo: bool = True
    userinfo_cache_ttl_seconds: int = 300

    backend_base_url: str = "http://localhost:8000"
    backend_userinfo_path: str = "/api/v1/users/me"
    backend_timeout_seconds: float = 5.0

    database_url: str = "sqlite+aiosqlite:///./calls.db"
    database_echo: bool = False
    auto_create_tables: bool = True

    ring_timeout_seconds: int = 45
    disconnect_grace_seconds: int = 10
    max_call_duration_seconds: int = 4 * 60 * 60
    ws_ticket_ttl_seconds: int = 30
    max_ws_message_bytes: int = 96 * 1024

    ice_servers: Annotated[list[dict], NoDecode] = Field(
        default_factory=lambda: [dict(server) for server in DEFAULT_STUN]
    )
    turn_urls: Annotated[list[str], NoDecode] = Field(default_factory=list)
    turn_username: str | None = None
    turn_credential: str | None = None

    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:8080",
            "http://localhost:8081",
            "http://127.0.0.1:8081",
        ]
    )

    @field_validator("jwt_algorithms", mode="before")
    @classmethod
    def split_algorithms(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().upper() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip().upper() for item in value]
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("turn_urls", mode="before")
    @classmethod
    def split_turn_urls(cls, value: object) -> object:
        if isinstance(value, str):
            return [url.strip() for url in value.replace("\n", ",").split(",") if url.strip()]
        return value

    @field_validator("ice_servers", mode="before")
    @classmethod
    def parse_ice_servers(cls, value: object) -> object:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("ICE_SERVERS must be a JSON array") from exc
            if not isinstance(parsed, list):
                raise ValueError("ICE_SERVERS must be a JSON array")
            return parsed
        return value

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        if self.auth_mode == "jwt" and not self.oauth2_jwks_url:
            if len(self.secret_key) < 16:
                raise ValueError("SECRET_KEY must be at least 16 characters in jwt auth mode")
        if self.auth_mode == "introspect" and not self.oauth2_introspection_url:
            raise ValueError("OAUTH2_INTROSPECTION_URL is required in introspect auth mode")
        if self.auth_mode == "backend" and not self.backend_base_url:
            raise ValueError("BACKEND_BASE_URL is required in backend auth mode")
        return self

    @property
    def resolved_ice_servers(self) -> list[dict]:
        servers = [dict(server) for server in self.ice_servers]
        if self.turn_urls:
            turn: dict = {"urls": list(self.turn_urls)}
            if self.turn_username:
                turn["username"] = self.turn_username
            if self.turn_credential:
                turn["credential"] = self.turn_credential
            servers.append(turn)
        return servers

    @property
    def backend_base(self) -> str:
        return self.backend_base_url.rstrip("/")

    @property
    def backend_userinfo_url(self) -> str:
        path = self.backend_userinfo_path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.backend_base}{path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
