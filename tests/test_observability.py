from __future__ import annotations

import json
import logging
import unittest

from repopilot.observability import (
    JsonFormatter,
    RedactingFilter,
    log_exception_safely,
    redact_text,
)


class ObservabilityTest(unittest.TestCase):
    def test_redacts_common_secret_shapes(self) -> None:
        text = "Authorization: Bearer abc123 api_key=secret-value token: xyz sk-1234567890abcdef"

        redacted = redact_text(text)

        self.assertNotIn("abc123", redacted)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("xyz", redacted)
        self.assertNotIn("sk-1234567890abcdef", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 4)

    def test_json_formatter_emits_structured_context(self) -> None:
        record = logging.LogRecord(
            "repopilot.test",
            logging.ERROR,
            __file__,
            1,
            "failed token=%s",
            ("secret-value",),
            None,
        )
        record.task_id = "task-1"
        redactor = RedactingFilter()
        redactor.filter(record)

        payload = json.loads(JsonFormatter().format(record))

        self.assertEqual(payload["level"], "ERROR")
        self.assertEqual(payload["task_id"], "task-1")
        self.assertNotIn("secret-value", payload["message"])

    def test_safe_exception_logging_redacts_chained_traceback(self) -> None:
        logger = logging.getLogger("repopilot.safe-log-test")
        try:
            try:
                raise RuntimeError("token=secret-value")
            except RuntimeError as cause:
                raise ValueError("tool wrapper failed") from cause
        except ValueError as error:
            with self.assertLogs(logger, level="ERROR") as logs:
                log_exception_safely(logger, "Tool call failed", error)

        output = "\n".join(logs.output)
        self.assertIn("RuntimeError", output)
        self.assertIn("ValueError", output)
        self.assertIn("[REDACTED]", output)
        self.assertNotIn("secret-value", output)


if __name__ == "__main__":
    unittest.main()
