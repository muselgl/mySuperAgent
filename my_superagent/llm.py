from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import ModelConfig


class ModelAPIError(RuntimeError):
    """A safe diagnostic that excludes request headers and credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


class ModelClient:
    """Minimal OpenAI-compatible text transport; all calls use local configuration."""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self._opener = build_opener(_NoRedirect())

    def generate(self, prompt: str) -> ModelReply:
        if self.config.api_mode == "responses":
            endpoint = "/responses"
            payload = {"model": self.config.model, "input": prompt, "store": False, "stream": False}
        else:
            endpoint = "/chat/completions"
            payload = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        request = Request(
            self.config.base_url.rstrip("/") + endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "mySuperAgent/0.1.0",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.config.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            status = error.code
            error.close()
            raise ModelAPIError(f"Model request failed: HTTP {status} ({self.config.api_mode})") from None
        except (URLError, TimeoutError, OSError):
            raise ModelAPIError("Model request failed: network, TLS or timeout error") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelAPIError("Model API returned invalid JSON") from None

        if not isinstance(data, dict) or data.get("error") or data.get("status") in {"failed", "incomplete"}:
            raise ModelAPIError("Model API returned an unsuccessful response")
        try:
            if self.config.api_mode == "responses":
                text = "".join(
                    part["text"]
                    for item in data.get("output", []) if item.get("type") == "message"
                    for part in item.get("content", []) if part.get("type") == "output_text"
                )
            else:
                text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ModelAPIError("Model API returned no usable text") from None
        return ModelReply(text.strip(), data.get("model", self.config.model), data.get("usage") or {})
