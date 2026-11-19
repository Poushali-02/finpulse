"""A tiny OpenRouter client (plain httpx) with a hard call budget and token counting.

The API key is read from the environment and never printed or logged.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    """The model call failed or returned something unusable."""


class LLMBudgetExceeded(LLMError):
    """The run already used its maximum number of LLM calls."""


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class OpenRouterClient:
    def __init__(self, api_key: str, model: str, temperature: float = 0, timeout: float = 90):
        self._api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    def complete(self, system: str, user: str) -> LLMResult:
        try:
            response = httpx.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.model,
                    "temperature": self.temperature,
                    "max_tokens": 4096,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            return LLMResult(
                text=data["choices"][0]["message"]["content"] or "",
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            )
        except httpx.HTTPStatusError as exc:
            # status code only: the response body and headers could echo request details
            raise LLMError(f"model call failed with HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"model call failed: {type(exc).__name__}") from None


class BudgetedLLM:
    """Wraps any client with `.complete(system, user)`; refuses calls past `max_calls`."""

    def __init__(self, client, max_calls: int = 3):
        self.client = client
        self.max_calls = max_calls
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, system: str, user: str) -> LLMResult:
        if self.calls >= self.max_calls:
            raise LLMBudgetExceeded(f"limit of {self.max_calls} LLM calls per run reached")
        self.calls += 1
        result = self.client.complete(system, user)
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        return result

    @property
    def usage(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


def llm_from_env(temperature: float = 0) -> OpenRouterClient | None:
    """A client if OPENROUTER_API_KEY and FINPULSE_MODEL are both set, else None."""
    key, model = os.environ.get("OPENROUTER_API_KEY", "").strip(), os.environ.get("FINPULSE_MODEL", "").strip()
    return OpenRouterClient(key, model, temperature) if key and model else None


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating a ``` fence or surrounding prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("the model did not return JSON") from None
        try:
            value = json.loads(cleaned[start : end + 1])
        except ValueError:
            raise LLMError("the model did not return valid JSON") from None
    if not isinstance(value, dict):
        raise LLMError("the model returned JSON that is not an object")
    return value
