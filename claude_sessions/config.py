from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Claude data directory
    claude_data_dir: Path = Path.home() / ".claude"

    # Server settings
    host: str = "127.0.0.1"
    port: int = 8080
    debug: bool = True

    # Active session detection threshold (minutes)
    active_threshold_minutes: int = 5

    # Pagination defaults
    default_page_size: int = 50
    max_page_size: int = 200

    class Config:
        env_prefix = "CLAUDE_SESSIONS_"


settings = Settings()
