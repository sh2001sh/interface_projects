# shared/llm/__init__.py
from .local_llm import LocalLLM

try:
    from .prompt_templates import PromptTemplates
except ModuleNotFoundError:
    PromptTemplates = None

__all__ = ["LocalLLM", "PromptTemplates"]
