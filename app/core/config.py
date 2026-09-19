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

    # P7 onboarding uploads (app/api/onboarding.py, app/core/storage.py).
    # Deliberately two separate, small settings rather than one shared value:
    # a store map (potentially a scanned floor plan or PDF) is reasonably
    # larger than a single camera reference still frame.
    max_map_upload_bytes: int = 20 * 1024 * 1024  # 20 MB
    max_camera_reference_upload_bytes: int = 10 * 1024 * 1024  # 10 MB

    # P9 (video processing). No reverse proxy sits in front of uvicorn in this
    # deployment (Dockerfile/docker-compose.yml expose it directly, no nginx/
    # Caddy config anywhere in this repo) and uvicorn itself imposes no
    # default request-body ceiling, so there is no infrastructure-imposed
    # number to defer to here -- this is a product choice, kept configurable
    # (like the two settings above) rather than hardcoded, so a deployment
    # with different constraints can override it via .env/environment
    # without a code change. 2 GB comfortably covers a several-minute CCTV
    # clip at common recording resolutions/bitrates while still bounding a
    # single upload's disk/memory footprint (save_upload streams to disk in
    # 1 MB chunks and aborts mid-write past this limit, so it never buffers
    # the whole file -- see app/core/storage.py).
    max_video_upload_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GB

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

@lru_cache
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
