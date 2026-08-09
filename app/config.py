"""Application settings loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration — values are read from environment variables or .env file."""

    # Database
    database_url: str = "postgresql://postgres:postgres@localhost:5432/payment_recon"

    # Application
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # Logging
    log_level: str = "INFO"

    # Pagination
    default_page_size: int = 20
    max_page_size: int = 100

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


# Singleton instance — import this wherever settings are needed.
settings = Settings()
