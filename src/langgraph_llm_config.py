"""LLM configuration for LangGraph action item extraction system.

This file contains separate configurations for each LLM implementation in the workflow.
Each node can have its own optimized settings.
"""
import os
from dotenv import load_dotenv

load_dotenv()


# ============================================================================
# DEFAULT/COMMON CONFIGURATION
# ============================================================================
# These are used as fallbacks if node-specific configs are not set

DEFAULT_MODEL_NAME = os.getenv("LANGGRAPH_MODEL_NAME", os.getenv("MODEL_NAME", "glm4-7"))
DEFAULT_API_URL = os.getenv("LANGGRAPH_API_URL", os.getenv("GLM_API_URL", "http://localhost:8000/v1"))
DEFAULT_API_KEY = os.getenv("LANGGRAPH_API_KEY", os.getenv("GLM_API_KEY", None))


# ============================================================================
# RELEVANCE GATE NODE CONFIGURATION
# ============================================================================
# Used for filtering chunks (work-relevant vs not relevant)
# Needs: Fast, low cost, binary decision

RELEVANCE_GATE_MODEL_NAME = os.getenv("RELEVANCE_GATE_MODEL_NAME", DEFAULT_MODEL_NAME)
RELEVANCE_GATE_API_URL = os.getenv("RELEVANCE_GATE_API_URL", DEFAULT_API_URL)
RELEVANCE_GATE_API_KEY = os.getenv("RELEVANCE_GATE_API_KEY", DEFAULT_API_KEY)
RELEVANCE_GATE_TEMPERATURE = float(os.getenv("RELEVANCE_GATE_TEMPERATURE", "0.1"))  # Low temp for consistent filtering
RELEVANCE_GATE_MAX_TOKENS = int(os.getenv("RELEVANCE_GATE_MAX_TOKENS", "100"))  # Small output (YES/NO)
RELEVANCE_GATE_TOP_P = float(os.getenv("RELEVANCE_GATE_TOP_P", "0.15"))
RELEVANCE_GATE_REPEAT_PENALTY = float(os.getenv("RELEVANCE_GATE_REPEAT_PENALTY", "1.2"))
RELEVANCE_GATE_PRESENCE_PENALTY = float(os.getenv("RELEVANCE_GATE_PRESENCE_PENALTY", "0.6"))
RELEVANCE_GATE_TIMEOUT = float(os.getenv("RELEVANCE_GATE_TIMEOUT", "60"))  # seconds; avoid indefinite hang


# ============================================================================
# LOCAL EXTRACTOR NODE CONFIGURATION
# ============================================================================
# Used for extracting segments from chunks
# Needs: Structured output, good at following format

LOCAL_EXTRACTOR_MODEL_NAME = os.getenv("LOCAL_EXTRACTOR_MODEL_NAME", DEFAULT_MODEL_NAME)
LOCAL_EXTRACTOR_API_URL = os.getenv("LOCAL_EXTRACTOR_API_URL", DEFAULT_API_URL)
LOCAL_EXTRACTOR_API_KEY = os.getenv("LOCAL_EXTRACTOR_API_KEY", DEFAULT_API_KEY)
LOCAL_EXTRACTOR_TEMPERATURE = float(os.getenv("LOCAL_EXTRACTOR_TEMPERATURE", "0.2"))  # Low temp for consistent extraction
LOCAL_EXTRACTOR_MAX_TOKENS = int(os.getenv("LOCAL_EXTRACTOR_MAX_TOKENS", "2000"))  # Medium output (structured segments)
LOCAL_EXTRACTOR_TOP_P = float(os.getenv("LOCAL_EXTRACTOR_TOP_P", "0.15"))
LOCAL_EXTRACTOR_REPEAT_PENALTY = float(os.getenv("LOCAL_EXTRACTOR_REPEAT_PENALTY", "1.2"))
LOCAL_EXTRACTOR_PRESENCE_PENALTY = float(os.getenv("LOCAL_EXTRACTOR_PRESENCE_PENALTY", "0.6"))
LOCAL_EXTRACTOR_TIMEOUT = float(os.getenv("LOCAL_EXTRACTOR_TIMEOUT", "120"))


# ============================================================================
# CONTEXT RESOLVER NODE CONFIGURATION
# ============================================================================
# Used for cross-chunk reasoning and linking
# Needs: Higher reasoning capability, context understanding

CONTEXT_RESOLVER_MODEL_NAME = os.getenv("CONTEXT_RESOLVER_MODEL_NAME", DEFAULT_MODEL_NAME)
CONTEXT_RESOLVER_API_URL = os.getenv("CONTEXT_RESOLVER_API_URL", DEFAULT_API_URL)
CONTEXT_RESOLVER_API_KEY = os.getenv("CONTEXT_RESOLVER_API_KEY", DEFAULT_API_KEY)
CONTEXT_RESOLVER_TEMPERATURE = float(os.getenv("CONTEXT_RESOLVER_TEMPERATURE", "0.3"))  # Slightly higher for reasoning
CONTEXT_RESOLVER_MAX_TOKENS = int(os.getenv("CONTEXT_RESOLVER_MAX_TOKENS", "2000"))  # Medium output (resolution results)
CONTEXT_RESOLVER_TOP_P = float(os.getenv("CONTEXT_RESOLVER_TOP_P", "0.2"))  # Slightly higher for creative linking
CONTEXT_RESOLVER_REPEAT_PENALTY = float(os.getenv("CONTEXT_RESOLVER_REPEAT_PENALTY", "1.2"))
CONTEXT_RESOLVER_PRESENCE_PENALTY = float(os.getenv("CONTEXT_RESOLVER_PRESENCE_PENALTY", "0.6"))
CONTEXT_RESOLVER_TIMEOUT = float(os.getenv("CONTEXT_RESOLVER_TIMEOUT", "180"))  # seconds; avoid long hangs on large prompts
# Skip LLM when segment count exceeds this (avoids timeouts on large chunks; use fallback only)
CONTEXT_RESOLVER_MAX_SEGMENTS_FOR_LLM = int(os.getenv("CONTEXT_RESOLVER_MAX_SEGMENTS_FOR_LLM", "8"))
# Max previous actions to include in context (large values = big prompt = timeouts on local models)
CONTEXT_RESOLVER_MAX_PREVIOUS_ACTIONS = int(os.getenv("CONTEXT_RESOLVER_MAX_PREVIOUS_ACTIONS", "5"))


# ============================================================================
# CONFIGURATION DICTIONARIES (for easy access)
# ============================================================================

RELEVANCE_GATE_CONFIG = {
    "model_name": RELEVANCE_GATE_MODEL_NAME,
    "api_url": RELEVANCE_GATE_API_URL,
    "api_key": RELEVANCE_GATE_API_KEY,
    "temperature": RELEVANCE_GATE_TEMPERATURE,
    "max_tokens": RELEVANCE_GATE_MAX_TOKENS,
    "top_p": RELEVANCE_GATE_TOP_P,
    "repeat_penalty": RELEVANCE_GATE_REPEAT_PENALTY,
    "presence_penalty": RELEVANCE_GATE_PRESENCE_PENALTY,
    "timeout": RELEVANCE_GATE_TIMEOUT,
}

LOCAL_EXTRACTOR_CONFIG = {
    "model_name": LOCAL_EXTRACTOR_MODEL_NAME,
    "api_url": LOCAL_EXTRACTOR_API_URL,
    "api_key": LOCAL_EXTRACTOR_API_KEY,
    "temperature": LOCAL_EXTRACTOR_TEMPERATURE,
    "max_tokens": LOCAL_EXTRACTOR_MAX_TOKENS,
    "top_p": LOCAL_EXTRACTOR_TOP_P,
    "repeat_penalty": LOCAL_EXTRACTOR_REPEAT_PENALTY,
    "presence_penalty": LOCAL_EXTRACTOR_PRESENCE_PENALTY,
    "timeout": LOCAL_EXTRACTOR_TIMEOUT,
}

CONTEXT_RESOLVER_CONFIG = {
    "model_name": CONTEXT_RESOLVER_MODEL_NAME,
    "api_url": CONTEXT_RESOLVER_API_URL,
    "api_key": CONTEXT_RESOLVER_API_KEY,
    "temperature": CONTEXT_RESOLVER_TEMPERATURE,
    "max_tokens": CONTEXT_RESOLVER_MAX_TOKENS,
    "top_p": CONTEXT_RESOLVER_TOP_P,
    "repeat_penalty": CONTEXT_RESOLVER_REPEAT_PENALTY,
    "presence_penalty": CONTEXT_RESOLVER_PRESENCE_PENALTY,
    "timeout": CONTEXT_RESOLVER_TIMEOUT,
    "max_segments_for_llm": CONTEXT_RESOLVER_MAX_SEGMENTS_FOR_LLM,
    "max_previous_actions": CONTEXT_RESOLVER_MAX_PREVIOUS_ACTIONS,
}
