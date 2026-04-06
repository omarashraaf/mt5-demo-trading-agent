# LinkTrade Thread Backup (2026-04-06)

This file is a persistent backup note so project-side chat context does not get lost.

## What was changed in this thread

- Switched Gemini model usage from `gemini-2.5-flash` to `gemma-3-1b-it`.
- Added centralized model config key: `GEMINI_MODEL` in backend config/env.
- Updated model wiring in:
  - `backend/agent/gemini_agent.py`
  - `backend/services/gemini_strategy_advisor_service.py`
  - `backend/llm/gemini_event_classifier.py`
  - `backend/services/signal_pipeline_service.py`
  - `backend/api/routes.py`
  - `backend/config.py`
  - `backend/.env`
  - `backend/.env.example`
  - `%APPDATA%/linktrade/backend-runtime/.env` (runtime copy)
- Added safe behavior for Gemma models by skipping Google Search tool grounding when model name starts with `gemma-`.

## Runtime verification performed

- Confirmed model resolves to `gemma-3-1b-it` for:
  - main config
  - Gemini agent
  - Event classifier
  - Strategy advisor

## Notes

- This backup note is intentionally committed to git for recovery/reference.
- Local Codex/UI chat history is outside git control, but this file preserves technical context in-repo.
