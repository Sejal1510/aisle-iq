from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dev-only placeholder. Never a valid secret outside development -- Settings
# below fails loudly at startup if this is still in use in any other
# environment, rather than silently signing tokens with a known value.
_DEV_ONLY_JWT_SECRET = "dev-only-insecure-secret-do-not-use-in-production"

class Settings(BaseSettings):
    app_name: str = "Store Intelligence System"
    environment: str = "development"
    debug: bool = False
    database_url: str = "sqlite:///./store_intelligence.db"
    jwt_secret_key: str = _DEV_ONLY_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

@lru_cache()
def get_settings() -> Settings:
    """Returns a cached singleton instance of the settings."""
    settings = Settings()
    if settings.environment != "development" and settings.jwt_secret_key == _DEV_ONLY_JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET_KEY must be set to a real secret outside development "
            f"(environment={settings.environment!r}). Refusing to start with the "
            "dev-only placeholder."
        )
    return settings
