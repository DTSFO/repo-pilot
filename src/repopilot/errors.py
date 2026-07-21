from __future__ import annotations

from http import HTTPStatus
from typing import Any


class RepoPilotError(Exception):
    """Base error with a stable code and a safe public message."""

    code = "internal_error"
    safe_message = "RepoPilot could not complete the operation."
    retryable = False
    http_status = HTTPStatus.INTERNAL_SERVER_ERROR

    def __init__(self, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(self.safe_message)
        self.details = details or {}


class ConfigurationError(RepoPilotError):
    code = "configuration_error"
    safe_message = "RepoPilot is not configured for this operation."
    http_status = HTTPStatus.SERVICE_UNAVAILABLE


class ProviderError(RepoPilotError):
    code = "provider_error"
    safe_message = "The model provider request failed."
    http_status = HTTPStatus.BAD_GATEWAY


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication_error"
    safe_message = "The model provider rejected the configured credentials."
    http_status = HTTPStatus.SERVICE_UNAVAILABLE


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    safe_message = "The model provider timed out."
    retryable = True
    http_status = HTTPStatus.GATEWAY_TIMEOUT


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limit"
    safe_message = "The model provider is temporarily rate limited."
    retryable = True
    http_status = HTTPStatus.SERVICE_UNAVAILABLE


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    safe_message = "The model provider is temporarily unavailable."
    retryable = True
    http_status = HTTPStatus.SERVICE_UNAVAILABLE


class ProviderResponseError(ProviderError):
    code = "provider_invalid_response"
    safe_message = "The model provider returned an invalid response."


class CircuitOpenError(ProviderUnavailableError):
    code = "provider_circuit_open"
    safe_message = "The model provider is temporarily disabled after repeated failures."


class ToolError(RepoPilotError):
    code = "tool_error"
    safe_message = "The tool call failed."
    http_status = HTTPStatus.BAD_REQUEST


class UnknownToolError(ToolError):
    code = "unknown_tool"
    safe_message = "The requested tool is not available."


class InvalidToolArgumentsError(ToolError):
    code = "invalid_tool_arguments"
    safe_message = "The tool arguments are invalid."


class ToolExecutionError(ToolError):
    code = "tool_execution_failed"
    safe_message = "The tool could not complete the operation."
    http_status = HTTPStatus.INTERNAL_SERVER_ERROR


class ToolUnavailableError(ToolExecutionError):
    code = "tool_unavailable"
    safe_message = "The tool is temporarily unavailable."
    retryable = True
    http_status = HTTPStatus.SERVICE_UNAVAILABLE


class ToolTimeoutError(ToolUnavailableError):
    code = "tool_timeout"
    safe_message = "The tool timed out."
    http_status = HTTPStatus.GATEWAY_TIMEOUT
