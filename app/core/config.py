from pydantic import Field
from pydantic_settings import BaseSettings
from functools import lru_cache
import os


class Settings(BaseSettings):
    # Youverify
    youverify_token: str = Field(
        default="",
        alias="YOUVERIFY_API_KEY",
        description="Youverify API key for identity verification"
    )
    youverify_base_url: str = Field(
        default="https://api.sandbox.youverify.co",
        alias="YOUVERIFY_BASE_URL",
        description="Youverify API base URL"
    )

    # AssemblyAI
    assemblyai_api_key: str = Field(
        default="",
        alias="ASSEMBLY_AI_API_KEY",
        description="AssemblyAI API key for voice processing"
    )

    # Squad
    squad_secret_key: str = Field(
        default="",
        alias="SQUAD_SECRET_KEY",
        description="Squad API secret key for payments"
    )
    squad_base_url: str = Field(
        default="https://sandbox-api-d.squadco.com",
        alias="SQUAD_BASE_URL",
        description="Squad API base URL"
    )

    # App
    app_env: str = Field(
        default="development",
        alias="APP_ENV",
        description="Application environment (development/production/staging)"
    )
    secret_key: str = Field(
        default="changeme",
        alias="SECRET_KEY",
        description="Secret key for application security"
    )
    redis_url: str = "redis://localhost:6379/0"
 
    # ── Liveness hardening ────────────────────────────────────────────────────
    # Minimum composite identity score (0-100) to pass verification
    min_identity_score:  float = 70.0
 
    # Challenge session TTL in seconds
    liveness_ttl_secs:   int   = 90
 
    # Hard minimum elapsed time (seconds) — below this = bot
    liveness_min_elapsed: float = 3.0
 
    # Entropy variance floors (soft flag, not hard reject)
    entropy_brightness_min: float = 0.4
    entropy_noise_min:       float = 0.2

    redis_url: str = "redis://localhost:6379/0"
 
    # ── Liveness hardening ────────────────────────────────────────────────────
    # Minimum composite identity score (0-100) to pass verification
    min_identity_score:  float = 70.0
 
    # Challenge session TTL in seconds
    liveness_ttl_secs:   int   = 90
 
    # Hard minimum elapsed time (seconds) — below this = bot
    liveness_min_elapsed: float = 3.0
 
    # Entropy variance floors (soft flag, not hard reject)
    entropy_brightness_min: float = 0.4
    entropy_noise_min:       float = 0.2
 
    class Config:
        env_file = ".env"
        case_sensitive = False
        populate_by_name = True


@lru_cache()
def get_settings() -> Settings:
    """Load settings from .env file with fallback defaults."""
    return Settings()
