from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ───────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/frammer"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def ensure_asyncpg_driver(cls, v: str) -> str:
        """Normalise bare postgresql:// URLs to use the asyncpg driver."""
        if v.startswith("postgres://"):
            v = "postgresql+asyncpg://" + v[len("postgres://"):]
        elif v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    # ── Supabase ───────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # ── App ────────────────────────────────────────────────────────────────────
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-secret-key"
    PROJECT_NAME: str = "Frammer Analytics API"
    API_V1_PREFIX: str = "/api/v1"
    AGENT_STATEMENT_TIMEOUT_MS: int = 5000
    AGENT_MAX_ROWS: int = 500
    AGENT_MAX_LIMIT: int = 100
    AGENT_MAX_JOINS: int = 4
    AGENT_MAX_CHART_CATEGORIES: int = 20
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_PLANNER_MODEL: str = "gpt-5"
    OPENAI_SECONDARY_MODEL: str = "gpt-5-mini"
    OPENAI_SUMMARIZER_MODEL: str = "gpt-5-mini"
    OPENAI_TIMEOUT_S: float = 60.0
    OPENAI_MAX_RETRIES: int = 2
    AGENT_MIN_CONFIDENCE: float = 0.40
    AGENT_MAX_REPAIR_ROUNDS: int = 2
    AGENT_CACHE_TTL_S: int = 120
    AGENT_AUDIT_LOG_PATH: str = "agent_audit.jsonl"

    # ── AWS / SES ──────────────────────────────────────────────────────────────
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    SES_FROM_EMAIL: str = "noreply@frammer.ai"
    SES_ENABLED: bool = False
    INTERNAL_NOTIFICATIONS_TOKEN: str = ""
    NOTIFICATIONS_RECIPIENT_ALLOWLIST: str = ""

    @property
    def ses_recipient_allowlist(self) -> List[str]:
        if not self.NOTIFICATIONS_RECIPIENT_ALLOWLIST:
            return []
        return [e.strip().lower() for e in self.NOTIFICATIONS_RECIPIENT_ALLOWLIST.split(",") if e.strip()]

    # ── CORS ───────────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:8080,http://localhost:8081,http://localhost:5173"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]



@lru_cache
def get_settings() -> Settings:
    return Settings()
