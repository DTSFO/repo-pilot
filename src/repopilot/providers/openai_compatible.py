from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import SecretStr

from ..errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..models import ModelResponse, TokenUsage, ToolCall
from .base import ModelRequest, ProviderHealth


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = f"{base_url.rstrip('/')}/"
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "User-Agent": "RepoPilot/1.1",
            },
            transport=transport,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in request.tools]
            payload["tool_choice"] = "auto"

        try:
            response = await self._client.post("chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError() from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError() from exc

        self._raise_for_status(response)
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            tool_calls = tuple(
                self._parse_tool_call(item) for item in message.get("tool_calls", [])
            )
            usage_data = body.get("usage") or {}
            usage = TokenUsage(
                prompt_tokens=int(usage_data.get("prompt_tokens", 0)),
                completion_tokens=int(usage_data.get("completion_tokens", 0)),
                total_tokens=int(usage_data.get("total_tokens", 0)),
            )
            return ModelResponse(
                text=message.get("content"),
                tool_calls=tool_calls,
                finish_reason=choice.get("finish_reason"),
                model=body.get("model", self.model),
                usage=usage,
                response_id=body.get("id"),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderResponseError() from exc

    async def health(self) -> ProviderHealth:
        try:
            response = await self._client.get("models")
            self._raise_for_status(response)
        except (ProviderUnavailableError, ProviderTimeoutError) as exc:
            return ProviderHealth(False, self.name, self.model, exc.code)
        except (ProviderAuthenticationError, ProviderRateLimitError, ProviderResponseError) as exc:
            return ProviderHealth(False, self.name, self.model, exc.code)
        return ProviderHealth(True, self.name, self.model)

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _parse_tool_call(item: dict[str, Any]) -> ToolCall:
        function = item["function"]
        arguments = json.loads(function.get("arguments") or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        return ToolCall(
            name=str(function["name"]),
            arguments=arguments,
            call_id=str(item["id"]),
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code in {401, 403}:
            raise ProviderAuthenticationError()
        if response.status_code == 429:
            raise ProviderRateLimitError()
        if response.status_code in {408, 504}:
            raise ProviderTimeoutError()
        if response.status_code >= 500:
            raise ProviderUnavailableError()
        raise ProviderResponseError(details={"status_code": response.status_code})
