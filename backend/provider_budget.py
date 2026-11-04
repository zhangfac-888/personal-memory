"""Per-job provider accounting, propagated to the active request only."""
from contextvars import ContextVar

active = ContextVar("personal_memory_provider_budget", default=None)


def before_call():
    callback = active.get()
    if callback:
        callback("call", None)


def response(model, usage):
    callback = active.get()
    if callback:
        safe_usage = {k: v for k, v in (usage or {}).items() if k in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(v) is int and 0 <= v <= 100000000} if isinstance(usage, dict) else {}
        callback("response", {"model": model[:200] if isinstance(model, str) else None, "usage": safe_usage})
