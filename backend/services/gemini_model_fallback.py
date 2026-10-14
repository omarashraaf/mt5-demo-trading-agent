from __future__ import annotations

import os
import re
import time


def configured_fallback_models() -> list[str]:
    return [
        item.strip()
        for item in str(os.getenv("GEMINI_FALLBACK_MODELS", "") or "").split(",")
        if item.strip()
    ]


def build_model_candidates(primary_model: str, *fallback_models: str) -> list[str]:
    ordered: list[str] = []
    for raw in (primary_model, *configured_fallback_models(), *fallback_models):
        model = str(raw or "").strip()
        if model and model not in ordered:
            ordered.append(model)
    return ordered


def build_structured_output_model_candidates(primary_model: str, *fallback_models: str) -> list[str]:
    if _nvidia_provider_active(primary_model):
        return build_model_candidates(
            _normalize_nvidia_model(primary_model),
            *[
                _normalize_nvidia_model(model)
                for model in (*configured_fallback_models(), *fallback_models)
                if _is_nvidia_compatible_model(model)
            ],
        )

    defaults = (
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    )
    primary = str(primary_model or "").strip()
    if primary and not supports_developer_instruction(primary):
        # Some Gemma SKUs reject developer/system instructions in structured-output flows.
        # Keep the configured model as a fallback, but prefer compatible Gemini models first.
        return build_model_candidates("", *fallback_models, *defaults, primary)
    return build_model_candidates(primary_model, *fallback_models, *defaults)


def next_available_model(candidates: list[str], cooldowns: dict[str, float]) -> str | None:
    now = time.time()
    for model in candidates:
        if float(cooldowns.get(model, 0.0) or 0.0) <= now:
            return model
    return None


def cooldown_remaining_seconds(candidates: list[str], cooldowns: dict[str, float]) -> int:
    now = time.time()
    future_deadlines = [
        max(0.0, float(cooldowns.get(model, 0.0) or 0.0) - now)
        for model in candidates
        if float(cooldowns.get(model, 0.0) or 0.0) > now
    ]
    if not future_deadlines:
        return 0
    return int(max(1.0, min(future_deadlines)))


def is_rate_limit_error(error: object) -> bool:
    text = str(error or "").lower()
    return any(
        token in text
        for token in (
            "429",
            "rate limit",
            "rate_limit",
            "quota",
            "resource_exhausted",
            "too many requests",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "not found",
        )
    )


def mark_model_cooldown(
    cooldowns: dict[str, float],
    model_name: str,
    *,
    error_text: str = "",
    default_seconds: float = 60.0,
) -> float:
    seconds = _cooldown_seconds_from_error(error_text, default_seconds=default_seconds)
    until = time.time() + seconds
    cooldowns[str(model_name or "").strip()] = until
    return until


def supports_developer_instruction(model_name: str | None) -> bool:
    model = str(model_name or "").strip().lower()
    if not model:
        return True
    # Gemini API currently rejects system/developer instructions for Gemma SKUs.
    # Use prompt-embedded guidance instead when a Gemma model is selected.
    return not model.startswith("gemma-")


def _nvidia_provider_active(primary_model: str | None) -> bool:
    provider = (os.getenv("GEMINI_PROVIDER") or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if provider in {"nvidia", "nvidia-nim", "nim"}:
        return True
    model = str(primary_model or "").strip().lower()
    key = (os.getenv("NVIDIA_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    return model.startswith("google/gemma-") or key.startswith("nvapi-")


def _is_nvidia_compatible_model(model_name: str | None) -> bool:
    model = str(model_name or "").strip().lower()
    return bool(model) and not model.startswith("gemini-")


def _normalize_nvidia_model(model_name: str | None) -> str:
    model = str(model_name or "").strip()
    if model.lower().startswith("gemma-"):
        return f"google/{model}"
    return model


def _cooldown_seconds_from_error(error_text: str, *, default_seconds: float) -> float:
    text = str(error_text or "")
    retry_delay_match = re.search(r'"?retry[-_ ]?delay"?\s*[:=]\s*"?(\d+)"?', text, flags=re.IGNORECASE)
    if retry_delay_match:
        return max(1.0, float(retry_delay_match.group(1)))
    seconds_match = re.search(r"(\d+(?:\.\d+)?)\s*(seconds|second|secs|sec|s)\b", text, flags=re.IGNORECASE)
    if seconds_match:
        return max(1.0, float(seconds_match.group(1)))
    minutes_match = re.search(r"(\d+(?:\.\d+)?)\s*(minutes|minute|mins|min|m)\b", text, flags=re.IGNORECASE)
    if minutes_match:
        return max(1.0, float(minutes_match.group(1)) * 60.0)
    return max(1.0, float(default_seconds))
