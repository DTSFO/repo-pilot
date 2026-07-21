from __future__ import annotations

import json
from dataclasses import asdict
from os import PathLike
from typing import Any

from .agent import AgentResult

PathValue = str | PathLike[str]


def save_trace(result: AgentResult, path: PathValue) -> None:
    """Save an agent trace as readable UTF-8 JSON."""
    trace_data = [asdict(event) for event in result.trace]

    with open(path, "w", encoding="utf-8") as file:
        json.dump(trace_data, file, ensure_ascii=False, indent=2)


def load_trace(path: PathValue) -> list[dict[str, Any]]:
    """Load trace JSON as ordinary Python dictionaries."""
    with open(path, encoding="utf-8") as file:
        trace_data = json.load(file)

    if not isinstance(trace_data, list) or not all(isinstance(item, dict) for item in trace_data):
        raise ValueError("trace JSON must be a list of objects")

    return trace_data
