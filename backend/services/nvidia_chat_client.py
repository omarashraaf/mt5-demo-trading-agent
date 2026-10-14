from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_NVIDIA_MODEL = "google/gemma-4-31b-it"


def nvidia_provider_enabled(*, api_key: str | None = None, model_name: str | None = None) -> bool:
    provider = (os.getenv("GEMINI_PROVIDER") or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if provider in {"nvidia", "nvidia-nim", "nim"}:
        return True
    key = (api_key or os.getenv("NVIDIA_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    model = (model_name or os.getenv("NVIDIA_MODEL") or os.getenv("GEMINI_MODEL") or "").strip().lower()
    return key.startswith("nvapi-") or model.startswith("google/gemma-")


def nvidia_api_key(fallback: str = "") -> str:
    return (os.getenv("NVIDIA_API_KEY") or fallback or os.getenv("GEMINI_API_KEY") or "").strip()


def nvidia_model_name(fallback: str = "") -> str:
    return (os.getenv("NVIDIA_MODEL") or fallback or os.getenv("GEMINI_MODEL") or DEFAULT_NVIDIA_MODEL).strip()


@dataclass
class NvidiaGenerateContentResponse:
    text: str
    raw: dict[str, Any]


class NvidiaChatCompletionsClient:
    """Small Google GenAI-compatible wrapper for NVIDIA chat completions."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ):
        self.api_key = api_key.strip()
        self.base_url = (base_url or os.getenv("NVIDIA_BASE_URL") or DEFAULT_NVIDIA_CHAT_URL).strip()
        self.timeout_seconds = float(timeout_seconds or os.getenv("NVIDIA_TIMEOUT_SECONDS") or 60.0)
        self.models = _NvidiaModels(self)


class _NvidiaModels:
    def __init__(self, owner: NvidiaChatCompletionsClient):
        self._owner = owner

    def generate_content(self, *, model: str, contents: Any, config: Any = None) -> NvidiaGenerateContentResponse:
        system_instruction = _config_value(config, "system_instruction")
        prompt = _content_to_text(contents)
        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": _content_to_text(system_instruction)})
        messages.append({"role": "user", "content": prompt})

        stream = _bool_env("NVIDIA_STREAM", True)
        payload: dict[str, Any] = {
            "model": model or DEFAULT_NVIDIA_MODEL,
            "messages": messages,
            "max_tokens": int(_config_value(config, "max_output_tokens") or os.getenv("NVIDIA_MAX_TOKENS") or 16384),
            "temperature": float(_config_value(config, "temperature") if _config_value(config, "temperature") is not None else os.getenv("NVIDIA_TEMPERATURE") or 0.2),
            "top_p": float(_config_value(config, "top_p") if _config_value(config, "top_p") is not None else os.getenv("NVIDIA_TOP_P") or 0.95),
            "stream": stream,
            "chat_template_kwargs": {"enable_thinking": _bool_env("NVIDIA_ENABLE_THINKING", True)},
        }

        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._owner.base_url,
            data=data,
            headers={
                "Authorization": f"Bearer {self._owner.api_key}",
                "Accept": "text/event-stream" if stream else "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        request_timeout = self._owner.timeout_seconds
        if "gemma-4-31b" in str(model or "").lower():
            request_timeout = min(
                request_timeout,
                float(os.getenv("NVIDIA_PRIMARY_TIMEOUT_SECONDS") or 8.0),
            )
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                if stream:
                    return _read_streaming_response(response)
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"NVIDIA chat completion failed with HTTP {exc.code}: {error_body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"NVIDIA chat completion failed: {exc}") from exc
        except TimeoutError as exc:
            raise RuntimeError("NVIDIA chat completion timed out.") from exc

        parsed = json.loads(body)
        choices = parsed.get("choices") if isinstance(parsed, dict) else None
        if not choices:
            raise RuntimeError("NVIDIA chat completion returned no choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        text = (message or {}).get("content") if isinstance(message, dict) else None
        if text is None:
            text = choices[0].get("text") if isinstance(choices[0], dict) else ""
        return NvidiaGenerateContentResponse(text=str(text or ""), raw=parsed)


def _config_value(config: Any, name: str) -> Any:
    if config is None:
        return None
    return getattr(config, name, None)


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return "\n".join(_content_to_text(item) for item in value if item is not None)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        if text is not None:
            return _content_to_text(text)
        return json.dumps(value, ensure_ascii=False)
    text = getattr(value, "text", None)
    if text is not None:
        return _content_to_text(text)
    return str(value)


def _read_streaming_response(response: Any) -> NvidiaGenerateContentResponse:
    chunks: list[str] = []
    raw_events: list[dict[str, Any]] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        raw_events.append(event)
        choices = event.get("choices") if isinstance(event, dict) else None
        if not choices:
            continue
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta") if isinstance(first, dict) else None
        message = first.get("message") if isinstance(first, dict) else None
        text = ""
        if isinstance(delta, dict):
            text = str(delta.get("content") or "")
        if not text and isinstance(message, dict):
            text = str(message.get("content") or "")
        if not text and isinstance(first, dict):
            text = str(first.get("text") or "")
        if text:
            chunks.append(text)
    return NvidiaGenerateContentResponse(text="".join(chunks), raw={"stream_events": raw_events[-5:]})


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}
