from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # CORS origins (comma-separated string in .env)
    CORS_ORIGINS: str = ""

    # JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str
    JWT_EXPIRE_DAYS: int

    # App
    ENVIRONMENT: str
    DEBUG: bool

    # Database & Auth
    DATABASE_URL: str
    VITE_NEON_AUTH_URL: str
    BETTER_AUTH_SECRET: str

    @property
    def cors_origins_list(self) -> List[str]:
        """Convert comma-separated CORS_ORIGINS string to list."""
        if not self.CORS_ORIGINS:
            return []
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


# Instantiate a global settings object
settings = Settings()
