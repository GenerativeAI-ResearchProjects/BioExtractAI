"""Unified LLM client for OpenAI, Anthropic, and DeepSeek."""

import os
from dataclasses import dataclass, field
from typing import List, Optional

SUPPORTED_PROVIDERS = ("openai", "anthropic", "deepseek")

DEFAULT_MODELS = {
    "openai": "gpt-5",
    "anthropic": "claude-opus-4-5",
    "deepseek": "deepseek-chat",
}

# Canonical list of models per provider, shown as dropdown options in the UI.
# Users can still pick "custom" and type a model name that isn't in this list.
MODEL_CATALOG = {
    "openai": [
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4o",
        "o1-preview",
        "o3-mini",
        "o4-mini",
    ],
    "anthropic": [
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-sonnet-3-7",
        "claude-haiku-4-5-20251001",
        "claude-haiku-3-5",
        "claude-haiku-3",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
}

ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# Prefix-based provider inference. Longer prefixes win on tie (checked first).
_PROVIDER_PREFIXES = [
    ("anthropic", ("claude-", "claude ")),
    ("openai", ("gpt-", "gpt4", "chatgpt-", "o1-", "o1", "o3-", "o3", "o4-", "o4")),
    ("deepseek", ("deepseek-", "deepseek")),
]


def infer_provider(model: str) -> Optional[str]:
    """Return the provider implied by a model name, or ``None`` if ambiguous.

    Matches common naming conventions:
      - ``gpt-*`` / ``o1`` / ``o3`` / ``o4`` / ``chatgpt-*``  → openai
      - ``claude-*``                                         → anthropic
      - ``deepseek-*``                                       → deepseek
    """
    if not model:
        return None
    m = model.lower().strip()
    for provider, prefixes in _PROVIDER_PREFIXES:
        if any(m.startswith(prefix) for prefix in prefixes):
            return provider
    return None


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    # Populated by ``.research(...)`` when web search is used.
    search_queries: List[str] = field(default_factory=list)
    search_results: List[dict] = field(default_factory=list)
    used_web_search: bool = False
    web_search_note: Optional[str] = None


class LLMClient:
    """Thin wrapper that hides provider differences behind ``.complete(prompt)``.

    Handles ``finish_reason == "length"`` by asking the model to continue.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ):
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported provider: {provider}. Choose from {SUPPORTED_PROVIDERS}."
            )
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_key = api_key or os.environ.get(ENV_KEYS[provider])
        if not self.api_key:
            raise RuntimeError(
                f"Missing API key for {provider}. "
                f"Set the {ENV_KEYS[provider]} environment variable or pass --api-key."
            )
        self._client = self._build_client()

    def _build_client(self):
        if self.provider in ("openai", "deepseek"):
            try:
                from openai import OpenAI
            except ImportError as e:
                raise RuntimeError(
                    f"The 'openai' package is required for provider '{self.provider}'. "
                    "Install with: pip install openai"
                ) from e
            if self.provider == "openai":
                return OpenAI(api_key=self.api_key)
            return OpenAI(api_key=self.api_key, base_url="https://api.deepseek.com")
        if self.provider == "anthropic":
            try:
                from anthropic import Anthropic
            except ImportError as e:
                raise RuntimeError(
                    "The 'anthropic' package is required for provider 'anthropic'. "
                    "Install with: pip install anthropic"
                ) from e
            return Anthropic(api_key=self.api_key)
        raise AssertionError(self.provider)  # unreachable

    def complete(self, prompt: str) -> LLMResponse:
        messages = [{"role": "user", "content": prompt}]
        full_text = []
        total_in = total_out = total_cached = 0

        while True:
            text, finish_reason, usage = self._one_call(messages)
            full_text.append(text)
            total_in += usage[0]
            total_cached += usage[1]
            total_out += usage[2]
            if finish_reason == "length":
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "Continue where you left off."})
                continue
            break

        return LLMResponse(
            text="".join(full_text),
            input_tokens=total_in,
            output_tokens=total_out,
            cached_tokens=total_cached,
        )

    def _one_call(self, messages):
        if self.provider in ("openai", "deepseek"):
            kwargs = {"model": self.model, "messages": messages}
            # DeepSeek supports temperature; OpenAI GPT-5 family ignores it.
            if self.provider == "deepseek":
                kwargs["temperature"] = self.temperature
            resp = self._client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            usage = resp.usage
            cached = getattr(usage, "cached_prompt_tokens", 0) or 0
            return (
                choice.message.content or "",
                choice.finish_reason,
                (usage.prompt_tokens, cached, usage.completion_tokens),
            )

        # anthropic
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=messages,
        )
        text = resp.content[0].text if resp.content else ""
        return (
            text,
            getattr(resp, "stop_reason", None) == "max_tokens" and "length" or None,
            (resp.usage.input_tokens, 0, resp.usage.output_tokens),
        )

    # ------------------------------------------------------------------
    # Web-search-enabled completion (used by the domain research agent).
    # ------------------------------------------------------------------
    def research(self, prompt: str, max_searches: int = 5) -> LLMResponse:
        """Complete ``prompt`` with web search enabled where the provider supports it.

        - OpenAI: Responses API with the built-in ``web_search`` tool.
        - Anthropic: Messages API with the ``web_search_20250305`` server tool.
        - DeepSeek: no native web search; falls back to ``complete()`` and flags
          ``used_web_search=False`` so callers can explain that in the output.
        """
        if self.provider == "anthropic":
            return self._anthropic_research(prompt, max_searches)
        if self.provider == "openai":
            return self._openai_research(prompt, max_searches)
        # DeepSeek (and any other future provider without native search)
        resp = self.complete(prompt)
        resp.used_web_search = False
        resp.web_search_note = (
            "DeepSeek does not expose a native web-search tool; the domain agent "
            "produced this briefing from the model's own knowledge only."
        )
        return resp

    def _anthropic_research(self, prompt: str, max_searches: int) -> LLMResponse:
        messages = [{"role": "user", "content": prompt}]
        tools = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_searches,
            }
        ]
        full_text: List[str] = []
        queries: List[str] = []
        results: List[dict] = []
        total_in = total_out = 0

        while True:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                tools=tools,
                messages=messages,
            )
            for block in resp.content or []:
                btype = getattr(block, "type", "")
                if btype == "text":
                    full_text.append(getattr(block, "text", "") or "")
                elif btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
                    inp = getattr(block, "input", {}) or {}
                    q = inp.get("query") if isinstance(inp, dict) else None
                    if q:
                        queries.append(q)
                elif btype == "web_search_tool_result":
                    for item in getattr(block, "content", []) or []:
                        url = getattr(item, "url", None)
                        if url:
                            results.append(
                                {
                                    "url": url,
                                    "title": getattr(item, "title", "") or "",
                                }
                            )

            total_in += getattr(resp.usage, "input_tokens", 0) or 0
            total_out += getattr(resp.usage, "output_tokens", 0) or 0

            if getattr(resp, "stop_reason", None) == "max_tokens":
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": "Continue where you left off."})
                continue
            break

        return LLMResponse(
            text="".join(full_text).strip(),
            input_tokens=total_in,
            output_tokens=total_out,
            search_queries=queries,
            search_results=results,
            used_web_search=True,
        )

    def _openai_research(self, prompt: str, max_searches: int) -> LLMResponse:
        # OpenAI Responses API with the built-in web_search tool. The tool
        # parameter name has varied ("web_search" / "web_search_preview");
        # try the stable name first and fall back.
        queries: List[str] = []
        results: List[dict] = []
        text_parts: List[str] = []

        last_err = None
        for tool_type in ("web_search", "web_search_preview"):
            try:
                resp = self._client.responses.create(
                    model=self.model,
                    input=prompt,
                    tools=[{"type": tool_type}],
                )
                break
            except Exception as e:  # TypeError, NotFoundError, etc.
                last_err = e
                resp = None
        if resp is None:
            # Web search unavailable on this endpoint — fall back silently.
            fallback = self.complete(prompt)
            fallback.used_web_search = False
            fallback.web_search_note = (
                f"OpenAI web_search tool not available for model {self.model!r} "
                f"({last_err}); domain agent used model knowledge only."
            )
            return fallback

        # Collect text + any web_search_call traces.
        output_text = getattr(resp, "output_text", "") or ""
        if output_text:
            text_parts.append(output_text)
        for item in getattr(resp, "output", []) or []:
            itype = getattr(item, "type", "")
            if itype == "web_search_call":
                action = getattr(item, "action", None)
                q = None
                if action is not None:
                    q = getattr(action, "query", None) or (
                        action.get("query") if isinstance(action, dict) else None
                    )
                if q:
                    queries.append(q)
            elif itype == "message" and not output_text:
                for c in getattr(item, "content", []) or []:
                    t = getattr(c, "text", None)
                    if t:
                        text_parts.append(t)

        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text="".join(text_parts).strip(),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            search_queries=queries,
            search_results=results,
            used_web_search=True,
        )
