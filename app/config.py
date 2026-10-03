import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_api_id: int = Field(gt=0)
    telegram_api_hash: SecretStr
    telegram_chats: str = ""
    data_dir: Path = Path("data")
    public_base_url: str = "http://localhost:8000"
    addon_token: SecretStr = SecretStr("")
    metadata_lookup: bool = True
    history_limit: int = Field(default=1000, ge=0)
    sync_interval: int = Field(default=300, ge=10)
    max_streams: int = Field(default=4, ge=1, le=32)
    telegram_timeout: float = Field(default=30, gt=0)
    addon_name: str = "Telegram Movies"

    @field_validator("public_base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.netloc:
            raise ValueError("PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
        if url.query or url.fragment or url.username or url.password or url.path.strip("/"):
            raise ValueError("PUBLIC_BASE_URL must contain only the scheme, host, and port")
        return value.rstrip("/")

    @field_validator("addon_token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if token and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            raise ValueError("ADDON_TOKEN must have 16-128 URL-safe letters, digits, '-' or '_'")
        return value

    @property
    def chats(self) -> list[int | str]:
        result = []
        for item in self.telegram_chats.split(","):
            item = item.strip()
            if item:
                result.append(int(item) if re.fullmatch(r"-?\d+", item) else item)
        return result

    @property
    def session_path(self) -> str:
        return str(self.data_dir / "telegram")

    @property
    def route_prefix(self) -> str:
        token = self.addon_token.get_secret_value()
        return f"/{token}" if token else ""

    @property
    def addon_url(self) -> str:
        return f"{self.public_base_url}{self.route_prefix}/manifest.json"
