from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, get_args, get_origin, get_type_hints

from .errors import (
    InvalidToolArgumentsError,
    RepoPilotError,
    ToolExecutionError,
    ToolUnavailableError,
    UnknownToolError,
)

ToolFunction = Callable[..., Any]
JsonSchema = dict[str, Any]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    function: ToolFunction
    parameters: JsonSchema
    read_only: bool = True
    idempotent: bool = True
    retryable: bool = False


class ToolRegistry:
    """Typed registry and execution boundary for model-selected tools."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        function: ToolFunction,
        *,
        parameters: JsonSchema | None = None,
        read_only: bool = True,
        idempotent: bool = True,
        retryable: bool = False,
    ) -> None:
        if not name or name in self._tools:
            raise ValueError(f"Invalid or duplicate tool name: {name!r}")
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            function=function,
            parameters=parameters or self._schema_for(function),
            read_only=read_only,
            idempotent=idempotent,
            retryable=retryable,
        )

    def get(self, name: str) -> RegisteredTool:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(details={"tool": name})
        return tool

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """Legacy synchronous execution used by the deterministic teaching demo."""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        bound = self._bind(tool, arguments, legacy=True)
        return tool.function(*bound.args, **bound.kwargs)

    async def aexecute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.get(name)
        bound = self._bind(tool, arguments, legacy=False)
        try:
            if inspect.iscoroutinefunction(tool.function):
                return await tool.function(*bound.args, **bound.kwargs)
            result = await asyncio.to_thread(tool.function, *bound.args, **bound.kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except RepoPilotError:
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise ToolUnavailableError(details={"tool": name}) from exc
        except Exception as exc:
            raise ToolExecutionError(details={"tool": name}) from exc

    def descriptions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]

    def all_read_only(self, names: list[str]) -> bool:
        return all(name in self._tools and self._tools[name].read_only for name in names)

    @staticmethod
    def _bind(
        tool: RegisteredTool,
        arguments: Mapping[str, Any],
        *,
        legacy: bool,
    ) -> inspect.BoundArguments:
        signature = inspect.signature(tool.function)
        try:
            bound = signature.bind(**arguments)
        except TypeError as exc:
            if legacy:
                raise ValueError(f"Invalid arguments for {tool.name}: {exc}") from exc
            raise InvalidToolArgumentsError(details={"tool": tool.name}) from exc
        bound.apply_defaults()
        return bound

    @classmethod
    def _schema_for(cls, function: ToolFunction) -> JsonSchema:
        signature = inspect.signature(function)
        try:
            type_hints = get_type_hints(function)
        except (NameError, TypeError):
            type_hints = {}
        properties: dict[str, JsonSchema] = {}
        required: list[str] = []
        for name, parameter in signature.parameters.items():
            annotation = type_hints.get(name, parameter.annotation)
            properties[name] = cls._annotation_schema(annotation)
            if parameter.default is inspect.Signature.empty:
                required.append(name)
        schema: JsonSchema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        schema["additionalProperties"] = False
        return schema

    @classmethod
    def _annotation_schema(cls, annotation: object) -> JsonSchema:
        if annotation is inspect.Signature.empty or annotation is Any:
            return {}
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin in {list, tuple, set, frozenset}:
            item = cls._annotation_schema(args[0]) if args else {}
            return {"type": "array", "items": item}
        if origin in {dict, Mapping}:
            return {"type": "object"}
        if origin is not None and type(None) in args:
            non_none = next((arg for arg in args if arg is not type(None)), Any)
            schema = cls._annotation_schema(non_none)
            return {"anyOf": [schema, {"type": "null"}]}
        primitive = {str: "string", int: "integer", float: "number", bool: "boolean"}
        if annotation in primitive:
            return {"type": primitive[annotation]}
        return {"type": "string"}


def add(a: float, b: float) -> float:
    """A safe example tool used by the deterministic demo."""

    return a + b


def multiply(a: float, b: float) -> float:
    """计算两个数字的乘积。"""

    return a * b


def search_notes(query: str, notes: list[str]) -> list[str]:
    """Very small lexical search used before the RAG chapter is implemented."""

    normalized = query.casefold()
    return [note for note in notes if normalized in note.casefold()]
