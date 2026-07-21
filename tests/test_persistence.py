from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repopilot.agent import AgentResult
from repopilot.models import TraceEvent
from repopilot.persistence import load_trace, save_trace


class TracePersistenceTest(unittest.TestCase):
    def test_trace_round_trip_uses_plain_dictionaries(self) -> None:
        result = AgentResult(
            answer="完成",
            messages=(),
            trace=(TraceEvent(1, "finish", "最终回答", {"ok": True}),),
            steps=1,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.json"

            save_trace(result, path)
            trace_data = load_trace(path)

        self.assertEqual(trace_data[0]["step"], 1)
        self.assertEqual(trace_data[0]["detail"], "最终回答")
        self.assertEqual(trace_data[0]["metadata"], {"ok": True})

    def test_missing_trace_file_is_not_silently_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"

            with self.assertRaises(FileNotFoundError):
                load_trace(path)

    def test_invalid_json_is_not_silently_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("not-json", encoding="utf-8")

            with self.assertRaises(json.JSONDecodeError):
                load_trace(path)


if __name__ == "__main__":
    unittest.main()
