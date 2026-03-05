"""LLM configuration for LangGraph action item extraction system.

Active provider is selected via ACTIVE_PROVIDER in .env (root).
Provider-specific settings live in configs/<provider>.env.

Supported providers:
  claude        -> configs/claude.env        (all nodes use Anthropic Claude)
  ollama        -> configs/ollama_glm.env    (all nodes use Ollama GLM local model)
  gemini_mixed  -> configs/gemini_mixed.env  (per-node providers: Gemini + Claude)

Per-node provider overrides:
  Each node config reads <NODE>_PROVIDER first; falls back to global PROVIDER.
  This allows mixing providers within a single run (e.g. gemini_mixed).

To add a new provider, create configs/<provider>.env with PROVIDER=<name>
and add the corresponding LLM factory branch in langgraph_nodes.py.
"""
import os
from dotenv import load_dotenv, dotenv_values

# ============================================================================
# LOAD ROOT .env TO READ ACTIVE_PROVIDER
# ============================================================================

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_project_root, ".env"), override=False)

ACTIVE_PROVIDER = os.getenv("ACTIVE_PROVIDER", "ollama").strip().lower()

# ============================================================================
# LOAD PROVIDER-SPECIFIC CONFIG FILE
# ============================================================================

_provider_env_map = {
    "claude": "configs/claude.env",
    "ollama": "configs/ollama_glm.env",
    "gemini_mixed": "configs/gemini_mixed.env",
}

_provider_config_path = _provider_env_map.get(ACTIVE_PROVIDER)
if _provider_config_path is None:
    raise ValueError(
        f"Unknown ACTIVE_PROVIDER='{ACTIVE_PROVIDER}'. "
        f"Supported providers: {list(_provider_env_map.keys())}. "
        f"To add a new provider create configs/<provider>.env and register it here."
    )

_provider_config_file = os.path.join(_project_root, _provider_config_path)
if not os.path.exists(_provider_config_file):
    raise FileNotFoundError(
        f"Provider config file not found: {_provider_config_file}. "
        f"Expected file for ACTIVE_PROVIDER='{ACTIVE_PROVIDER}'."
    )

# Load provider config into the process environment (override so provider
# values win over any stale variables left from a previous provider).
load_dotenv(_provider_config_file, override=True)

# Read raw values (without modifying os.environ) for building config dicts
_cfg = dotenv_values(_provider_config_file)

def _get(key: str, default: str = None) -> str:
    """Prefer provider config file value, then os.environ, then default."""
    return _cfg.get(key) or os.getenv(key) or default


# ============================================================================
# RESOLVED PROVIDER
# ============================================================================

PROVIDER = _get("PROVIDER", ACTIVE_PROVIDER)


# ============================================================================
# DEFAULT / COMMON SETTINGS
# ============================================================================

DEFAULT_MODEL_NAME = _get("LANGGRAPH_MODEL_NAME")
DEFAULT_API_URL = _get("LANGGRAPH_API_URL")
DEFAULT_API_KEY = _get("ANTHROPIC_API_KEY") or _get("GOOGLE_API_KEY") or _get("LANGGRAPH_API_KEY")


def _resolve_api_key(node_specific_key: str, node_provider: str) -> str:
    """Return the correct API key for a node based on its resolved provider.

    Checks the node-specific override first, then picks the provider-appropriate
    key from the environment. This prevents Google/Ollama keys from bleeding into
    Anthropic-backed nodes (and vice versa) when multiple provider keys are set.
    """
    specific = _get(node_specific_key)
    if specific:
        return specific
    p = node_provider.lower()
    if p in ("claude", "anthropic"):
        return _get("ANTHROPIC_API_KEY") or DEFAULT_API_KEY
    if p in ("gemini", "google"):
        return _get("GOOGLE_API_KEY") or DEFAULT_API_KEY
    if p == "ollama":
        return _get("LANGGRAPH_API_KEY") or DEFAULT_API_KEY
    return DEFAULT_API_KEY


# ============================================================================
# RELEVANCE GATE NODE
# ============================================================================

RELEVANCE_GATE_CONFIG = {
    "provider": _get("RELEVANCE_GATE_PROVIDER", PROVIDER),
    "model_name": _get("RELEVANCE_GATE_MODEL_NAME", DEFAULT_MODEL_NAME),
    "api_url": _get("RELEVANCE_GATE_API_URL", DEFAULT_API_URL),
    "api_key": _resolve_api_key("RELEVANCE_GATE_API_KEY", _get("RELEVANCE_GATE_PROVIDER", PROVIDER)),
    "temperature": float(_get("RELEVANCE_GATE_TEMPERATURE", "0.1")),
    "max_tokens": int(_get("RELEVANCE_GATE_MAX_TOKENS", "100")),
    "top_p": float(_get("RELEVANCE_GATE_TOP_P", "0.15")),
    "repeat_penalty": float(_get("RELEVANCE_GATE_REPEAT_PENALTY", "1.2")),
    "presence_penalty": float(_get("RELEVANCE_GATE_PRESENCE_PENALTY", "0.6")),
    "timeout": float(_get("RELEVANCE_GATE_TIMEOUT", "60")),
}


# ============================================================================
# LOCAL EXTRACTOR NODE
# ============================================================================

LOCAL_EXTRACTOR_CONFIG = {
    "provider": _get("LOCAL_EXTRACTOR_PROVIDER", PROVIDER),
    "model_name": _get("LOCAL_EXTRACTOR_MODEL_NAME", DEFAULT_MODEL_NAME),
    "api_url": _get("LOCAL_EXTRACTOR_API_URL", DEFAULT_API_URL),
    "api_key": _resolve_api_key("LOCAL_EXTRACTOR_API_KEY", _get("LOCAL_EXTRACTOR_PROVIDER", PROVIDER)),
    "temperature": float(_get("LOCAL_EXTRACTOR_TEMPERATURE", "0.2")),
    "max_tokens": int(_get("LOCAL_EXTRACTOR_MAX_TOKENS", "2000")),
    "top_p": float(_get("LOCAL_EXTRACTOR_TOP_P", "0.15")),
    "repeat_penalty": float(_get("LOCAL_EXTRACTOR_REPEAT_PENALTY", "1.2")),
    "presence_penalty": float(_get("LOCAL_EXTRACTOR_PRESENCE_PENALTY", "0.6")),
    "timeout": float(_get("LOCAL_EXTRACTOR_TIMEOUT", "120")),
}


# ============================================================================
# CROSS-CHUNK RESOLVER NODE
# ============================================================================

CROSS_CHUNK_RESOLVER_CONFIG = {
    "provider": _get("CROSS_CHUNK_RESOLVER_PROVIDER", PROVIDER),
    "model_name": _get("CROSS_CHUNK_RESOLVER_MODEL_NAME", DEFAULT_MODEL_NAME),
    "api_url": _get("CROSS_CHUNK_RESOLVER_API_URL", DEFAULT_API_URL),
    "api_key": _resolve_api_key("CROSS_CHUNK_RESOLVER_API_KEY", _get("CROSS_CHUNK_RESOLVER_PROVIDER", PROVIDER)),
    "temperature": float(_get("CROSS_CHUNK_RESOLVER_TEMPERATURE", "0.2")),
    "max_tokens": int(_get("CROSS_CHUNK_RESOLVER_MAX_TOKENS", "4096")),
    "top_p": float(_get("CROSS_CHUNK_RESOLVER_TOP_P", "0.15")),
    "repeat_penalty": float(_get("CROSS_CHUNK_RESOLVER_REPEAT_PENALTY", "1.2")),
    "presence_penalty": float(_get("CROSS_CHUNK_RESOLVER_PRESENCE_PENALTY", "0.6")),
    "timeout": float(_get("CROSS_CHUNK_RESOLVER_TIMEOUT", "120")),
}


# ============================================================================
# CONTEXT RESOLVER NODE  (legacy — no longer an active graph node)
# ============================================================================

CONTEXT_RESOLVER_CONFIG = {
    "provider": _get("CONTEXT_RESOLVER_PROVIDER", PROVIDER),
    "model_name": _get("CONTEXT_RESOLVER_MODEL_NAME", DEFAULT_MODEL_NAME),
    "api_url": _get("CONTEXT_RESOLVER_API_URL", DEFAULT_API_URL),
    "api_key": _resolve_api_key("CONTEXT_RESOLVER_API_KEY", _get("CONTEXT_RESOLVER_PROVIDER", PROVIDER)),
    "temperature": float(_get("CONTEXT_RESOLVER_TEMPERATURE", "0.3")),
    "max_tokens": int(_get("CONTEXT_RESOLVER_MAX_TOKENS", "2000")),
    "top_p": float(_get("CONTEXT_RESOLVER_TOP_P", "0.2")),
    "repeat_penalty": float(_get("CONTEXT_RESOLVER_REPEAT_PENALTY", "1.2")),
    "presence_penalty": float(_get("CONTEXT_RESOLVER_PRESENCE_PENALTY", "0.6")),
    "timeout": float(_get("CONTEXT_RESOLVER_TIMEOUT", "180")),
    "max_segments_for_llm": int(_get("CONTEXT_RESOLVER_MAX_SEGMENTS_FOR_LLM", "8")),
    "max_previous_actions": int(_get("CONTEXT_RESOLVER_MAX_PREVIOUS_ACTIONS", "5")),
}
