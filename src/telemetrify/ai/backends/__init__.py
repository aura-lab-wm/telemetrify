"""LLMBackend abstractions: a single `complete()` method per tier so the
router can compose Rocco vLLM → Ollama Cloud → Anthropic transparently.
"""
from .base import BackendResponse, BackendUnavailable, BackendTransient, LLMBackend

__all__ = ["BackendResponse", "BackendUnavailable", "BackendTransient", "LLMBackend"]
