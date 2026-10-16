"""Explicit service limits and declared competition model configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit


def _integer(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, "1" if default else "0").strip().lower()
    if value not in {"1", "0", "true", "false"}:
        raise ValueError(f"{name} must be 1 or 0")
    return value in {"1", "true"}


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/forgetrace.sqlite3"
    api_key: str = field(default="", repr=False)
    strategy: str = "proposed"
    model_mode: str = "gpt-4o-mini"
    openai_api_key: str = field(default="", repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    model_timeout_seconds: int = 90
    max_model_concurrency: int = 2
    embedding_mode: str = "none"
    embedding_api_key: str = field(default="", repr=False)
    embedding_base_url: str = ""
    max_add_concurrency: int = 2
    max_search_concurrency: int = 2
    max_request_bytes: int = 64 * 1024**2
    max_response_bytes: int = 16 * 1024**2
    max_results: int = 100
    min_free_bytes: int = 128 * 1024**2
    read_timeout_seconds: int = 120
    production: bool = True
    version: str = "0.1.0"
    commit: str = "unfixed"
    host: str = "0.0.0.0"
    port: int = 8000

    def __post_init__(self) -> None:
        if self.strategy not in {"raw", "structured", "hybrid", "proposed"}:
            raise ValueError("STRATEGY must be raw, structured, hybrid or proposed")
        if self.model_mode not in {"offline", "gpt-4o-mini"}:
            raise ValueError("MODEL_MODE must be offline or gpt-4o-mini")
        if self.embedding_mode not in {"none", "v4"}:
            raise ValueError("EMBEDDING_MODE must be none or v4 (text-embedding-v4)")
        for name in ("max_add_concurrency", "max_search_concurrency", "max_request_bytes",
                     "max_response_bytes", "max_results", "read_timeout_seconds", "port",
                     "model_timeout_seconds", "max_model_concurrency"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.min_free_bytes < 0 or self.port > 65535 or self.max_results > 100:
            raise ValueError("Invalid disk reserve, port or result limit")
        if self.production and not self.api_key.strip():
            raise ValueError("PRODUCTION=1 requires a non-empty MEMORY_API_KEY")
        if self.production and self.model_mode != "offline" and not self.openai_api_key.strip():
            raise ValueError("MODEL_MODE=gpt-4o-mini requires OPENAI_API_KEY")
        if self.embedding_mode != "none" and not (self.embedding_api_key or self.openai_api_key).strip():
            raise ValueError("EMBEDDING_MODE requires EMBEDDING_API_KEY or OPENAI_API_KEY")
        if self.strategy == "hybrid" and self.embedding_mode == "none":
            raise ValueError("STRATEGY=hybrid requires EMBEDDING_MODE=v4")
        for name in ("api_key", "openai_api_key", "embedding_api_key"):
            value = getattr(self, name)
            if value != value.strip() or any(c in value for c in "\r\n"):
                raise ValueError(f"{name} cannot have surrounding whitespace or newlines")
        parsed = urlsplit(self.openai_base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("OPENAI_BASE_URL must be an HTTP(S) URL without credentials/query/fragment")
        if self.production and self.model_mode != "offline" and parsed.scheme != "https":
            raise ValueError("Production model requests require HTTPS")
        if self.embedding_base_url:
            embedding_url = urlsplit(self.embedding_base_url)
            if (embedding_url.scheme not in {"http", "https"} or not embedding_url.netloc or
                    embedding_url.username or embedding_url.password or embedding_url.query or embedding_url.fragment):
                raise ValueError("Invalid EMBEDDING_BASE_URL")
            if self.production and self.embedding_mode != "none" and embedding_url.scheme != "https":
                raise ValueError("Production embedding requests require HTTPS")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.getenv("MEMORY_DB_PATH", "data/forgetrace.sqlite3"),
            api_key=os.getenv("MEMORY_API_KEY", ""),
            strategy=os.getenv("STRATEGY", "proposed"),
            model_mode=os.getenv("MODEL_MODE", "gpt-4o-mini"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model_timeout_seconds=_integer("MODEL_TIMEOUT_SECONDS", 90),
            max_model_concurrency=_integer("MAX_MODEL_CONCURRENCY", 2),
            embedding_mode=os.getenv("EMBEDDING_MODE", "none"),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", ""),
            max_add_concurrency=_integer("MAX_ADD_CONCURRENCY", 2),
            max_search_concurrency=_integer("MAX_SEARCH_CONCURRENCY", 2),
            max_request_bytes=_integer("MAX_REQUEST_BYTES", 64 * 1024**2),
            max_response_bytes=_integer("MAX_RESPONSE_BYTES", 16 * 1024**2),
            max_results=_integer("MAX_RESULTS", 100),
            min_free_bytes=_integer("MIN_FREE_BYTES", 128 * 1024**2),
            read_timeout_seconds=_integer("READ_TIMEOUT_SECONDS", 120),
            production=_boolean("PRODUCTION", True),
            version=os.getenv("SYSTEM_VERSION", "0.1.0"),
            commit=os.getenv("SOURCE_COMMIT", "unfixed"),
            host=os.getenv("HOST", "0.0.0.0"),
            port=_integer("PORT", 8000),
        )
