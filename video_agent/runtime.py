"""Explicit process settings, independent from legacy model JSON configuration."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimeSettings:
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "video_agent"
    db_user: str = "root"
    db_password: str = field(default="", repr=False)
    home: Path = field(default_factory=lambda: Path.cwd() / ".video-agent")
    business_token: str = field(default="", repr=False)
    admin_token: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, validate_tokens: bool = True) -> "RuntimeSettings":
        values = os.environ if env is None else env
        try:
            port = int(values.get("VIDEO_AGENT_DB_PORT", "3306"))
        except ValueError:
            raise ValueError("VIDEO_AGENT_DB_PORT must be an integer") from None
        if not 1 <= port <= 65535:
            raise ValueError("VIDEO_AGENT_DB_PORT must be between 1 and 65535")
        settings = cls(
            db_host=values.get("VIDEO_AGENT_DB_HOST", "127.0.0.1"), db_port=port,
            db_name=values.get("VIDEO_AGENT_DB_NAME", "video_agent"),
            db_user=values.get("VIDEO_AGENT_DB_USER", "root"),
            db_password=values.get("VIDEO_AGENT_DB_PASSWORD", ""),
            home=Path(values.get("VIDEO_AGENT_HOME", str(Path.cwd() / ".video-agent"))),
            business_token=values.get("VIDEO_AGENT_BUSINESS_TOKEN", ""),
            admin_token=values.get("VIDEO_AGENT_ADMIN_TOKEN", ""),
            secret_key=values.get("VIDEO_AGENT_SECRET_KEY", ""),
        )
        if not settings.db_host or not settings.db_name or not settings.db_user:
            raise ValueError("Database host, name and user must be nonempty")
        if validate_tokens:
            settings.validate_tokens()
        return settings

    def validate_tokens(self) -> None:
        if not self.business_token or not self.admin_token:
            raise ValueError("Business and admin tokens must be nonempty")
        if self.business_token == self.admin_token:
            raise ValueError("Business and admin tokens must be different")
