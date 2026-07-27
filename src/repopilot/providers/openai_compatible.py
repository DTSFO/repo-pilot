"""Backward-compatible import for the v1.4 provider class name.

The implementation moved to LangChain in v1.5. Existing integrations importing
``OpenAICompatibleProvider`` continue to work while new code should use
``LangChainOpenAIProvider``.
"""

from .langchain_openai import LangChainOpenAIProvider

OpenAICompatibleProvider = LangChainOpenAIProvider

__all__ = ["OpenAICompatibleProvider"]
