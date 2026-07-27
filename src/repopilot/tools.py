from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin, get_type_hints

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

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
    validator: Draft202012Validator = field(repr=False, compare=False)
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
        resolved_parameters = parameters or self._schema_for(function)
        try:
            Draft202012Validator.check_schema(resolved_parameters)
        except SchemaError as exc:
            raise ValueError(f"Invalid JSON Schema for tool {name!r}") from exc
        validator = Draft202012Validator(resolved_parameters)
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            function=function,
            parameters=resolved_parameters,
            validator=validator,
            read_only=read_only,
            idempotent=idempotent,
            retryable=retryable,
        )

    def get(self, name: str) -> RegisteredTool:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(details={"tool": name})
        return tool

    async def aexecute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.get(name)
        try:
            tool.validator.validate(arguments)
        except ValidationError as exc:
            raise InvalidToolArgumentsError(details={"tool": tool.name}) from exc
        bound = self._bind(tool, arguments)
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
    ) -> inspect.BoundArguments:
        signature = inspect.signature(tool.function)
        try:
            bound = signature.bind(**arguments)
        except TypeError as exc:
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
