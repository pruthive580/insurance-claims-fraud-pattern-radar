"""Provider-aware LLM wrapper for the investigator narratives.

Picks a backend automatically (override with FRAUD_LLM = auto|ollama|anthropic|offline):
  1. Local Ollama  — if the server is up and has a model (preferred: private + free)
  2. Anthropic     — if ANTHROPIC_API_KEY + the SDK are present
  3. Offline       — deterministic summary (the engine never depends on any of this)

Only the *wording* of the narrative comes from here — scores/bands/rings are pure
deterministic logic elsewhere, so the app works identically in offline mode.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

# --- config (all overridable by env) -----------------------------------------
PROVIDER_PREF = os.environ.get("FRAUD_LLM", "auto").lower()      # auto|ollama|anthropic|offline
ANTHROPIC_MODEL = os.environ.get("FRAUD_MODEL", "claude-haiku-4-5-20251001")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL_ENV = os.environ.get("FRAUD_OLLAMA_MODEL") or os.environ.get("OLLAMA_MODEL")

_SYSTEM = "You are a precise insurance SIU (fraud) analyst. Be concise and specific."

_resolved: dict | None = None   # cached backend decision for this process


# --- provider detection ------------------------------------------------------
def _ollama_models() -> list[str] | None:
    """List installed Ollama models, or None if the server is unreachable."""
    try:
        with urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=1.5) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None


def _pick_ollama() -> dict | None:
    models = _ollama_models()
    if models is None:
        return None                                  # server down
    if not models:
        return {"provider": "ollama", "model": None, "models": []}  # up, no models
    model = OLLAMA_MODEL_ENV if OLLAMA_MODEL_ENV in models else models[0]
    return {"provider": "ollama", "model": model, "models": models}


def _anthropic_ready() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _resolve() -> dict:
    global _resolved
    if _resolved is not None:
        return _resolved
    pref = PROVIDER_PREF
    if pref == "offline":
        _resolved = {"provider": "offline"}
        return _resolved
    if pref in ("auto", "ollama"):
        oll = _pick_ollama()
        if oll and oll.get("model"):
            _resolved = oll
            return _resolved
        if pref == "ollama":                          # forced but not ready
            _resolved = oll or {"provider": "ollama", "model": None}
            return _resolved
    if pref in ("auto", "anthropic") and _anthropic_ready():
        _resolved = {"provider": "anthropic", "model": ANTHROPIC_MODEL}
        return _resolved
    _resolved = {"provider": "offline"}
    return _resolved


def available() -> bool:
    r = _resolve()
    return bool((r["provider"] == "ollama" and r.get("model")) or r["provider"] == "anthropic")


def status_label() -> str:
    r = _resolve()
    if r["provider"] == "ollama" and r.get("model"):
        return f"🟢 AI mode (Ollama · {r['model']})"
    if r["provider"] == "ollama":
        return "🟡 Ollama running but no model — run:  ollama pull llama3.2"
    if r["provider"] == "anthropic":
        return "🟢 AI mode (Claude live)"
    return "🟡 Offline demo mode (deterministic)"


# --- completion --------------------------------------------------------------
def _ollama_complete(prompt, system, max_tokens, temperature, model) -> str | None:
    body = {
        "model": model, "stream": False,
        # Disable "thinking" models (e.g. qwen3) emitting reasoning traces —
        # otherwise the reasoning eats the token budget and content comes back empty.
        "think": False,
        "messages": [{"role": "system", "content": system or _SYSTEM},
                     {"role": "user", "content": prompt}],
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    try:
        req = urllib.request.Request(
            OLLAMA_HOST + "/api/chat", data=json.dumps(body).encode(),
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.load(r)
        content = data.get("message", {}).get("content") or ""
        # Safety net: strip any <think>…</think> blocks that slip through.
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content or None
    except Exception:
        return None


def _anthropic_complete(prompt, system, max_tokens, temperature, model) -> str | None:
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system or _SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
        return "\n".join(parts).strip() or None
    except Exception:
        return None


def complete(prompt: str, system: str = "", max_tokens: int = 1024,
             temperature: float = 0.2, model: str | None = None) -> str | None:
    """Return the model's text, or None if no live provider / errored."""
    r = _resolve()
    if r["provider"] == "ollama" and r.get("model"):
        return _ollama_complete(prompt, system, max_tokens, temperature,
                                model or r["model"])
    if r["provider"] == "anthropic":
        return _anthropic_complete(prompt, system, max_tokens, temperature,
                                   model or r["model"])
    return None


# --- JSON helper (kept for compatibility) ------------------------------------
def _extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


def complete_json(prompt: str, system: str = "", max_tokens: int = 1024,
                  model: str | None = None) -> Any:
    guard = ((system + "\n\n" if system else "")
             + "Respond with ONLY valid JSON. No prose, no markdown fences.")
    raw = complete(prompt, system=guard, max_tokens=max_tokens,
                   temperature=0.0, model=model)
    return _extract_json(raw) if raw is not None else None
