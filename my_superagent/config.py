from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Local model configuration is missing or invalid."""


@dataclass(frozen=True)
class ModelConfig:
    api_key: str = field(repr=False)
    base_url: str = "https://ruoli.dev/v1"
    model: str = "gpt-6.1-terra"
    api_mode: str = "responses"
    timeout_seconds: float = 60

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ConfigurationError("Set SUPERAGENT_API_KEY in .env or the process environment")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ConfigurationError("SUPERAGENT_BASE_URL requires an HTTPS URL without credentials, query or fragment")
        if not self.model.strip():
            raise ConfigurationError("SUPERAGENT_MODEL requires a model name")
        if self.api_mode not in {"responses", "chat_completions"}:
            raise ConfigurationError("SUPERAGENT_API_MODE must be responses or chat_completions")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ConfigurationError("SUPERAGENT_API_TIMEOUT_SECONDS must be positive and finite")

    @classmethod
    def load(
        cls,
        env_file: str | Path = ".env",
        environ: Mapping[str, str] | None = None,
    ) -> "ModelConfig":
        values: dict[str, str] = {}
        path = Path(env_file)
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                name, separator, value = line.partition("=")
                if not separator:
                    raise ConfigurationError("Invalid .env line: expected NAME=value")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[name.strip()] = value
        values.update(os.environ if environ is None else environ)
        try:
            timeout = float(values.get("SUPERAGENT_API_TIMEOUT_SECONDS", "60"))
        except ValueError:
            raise ConfigurationError("SUPERAGENT_API_TIMEOUT_SECONDS must be numeric") from None
        return cls(
            api_key=values.get("SUPERAGENT_API_KEY", "").strip(),
            base_url=values.get("SUPERAGENT_BASE_URL", "https://ruoli.dev/v1").strip().rstrip("/"),
            model=values.get("SUPERAGENT_MODEL", "gpt-6.1-terra").strip(),
            api_mode=values.get("SUPERAGENT_API_MODE", "responses").strip(),
            timeout_seconds=timeout,
        )
