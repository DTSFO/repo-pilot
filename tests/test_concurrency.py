from __future__ import annotations

import asyncio
import unittest

from repopilot.concurrency import run_parallel_operations


async def value_after(delay: float, value: object) -> object:
    await asyncio.sleep(delay)
    return value


async def fail_after(delay: float, error: Exception) -> object:
    await asyncio.sleep(delay)
    raise error


class ParallelOperationsTest(unittest.IsolatedAsyncioTestCase):
    async def test_results_keep_their_operation_names(self) -> None:
        results = await run_parallel_operations(
            {
                "search": lambda: value_after(0, "证据"),
                "model": lambda: value_after(0, "回答"),
                "memory": lambda: value_after(0, "偏好"),
            },
            timeout=1,
        )

        self.assertEqual(
            {name: result.value for name, result in results.operations.items()},
            {"search": "证据", "model": "回答", "memory": "偏好"},
        )
        self.assertEqual(results.success_count, 3)
        self.assertEqual(results.failure_count, 0)

    async def test_one_failure_keeps_other_successful_results(self) -> None:
        with self.assertLogs("repopilot.concurrency", level="ERROR") as logs:
            results = await run_parallel_operations(
                {
                    "search": lambda: value_after(0, "证据"),
                    "model": lambda: fail_after(0, RuntimeError("token=secret-value")),
                },
                timeout=1,
            )

        self.assertTrue(results.operations["search"].ok)
        self.assertEqual(results.operations["search"].value, "证据")
        self.assertFalse(results.operations["model"].ok)
        self.assertEqual(results.failure_count, 1)

        error = results.operations["model"].error
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.code, "operation_failed")
        self.assertEqual(error.exception_type, "RuntimeError")
        self.assertNotIn("secret-value", error.message)
        self.assertNotIn("secret-value", repr(error))
        self.assertNotIn("secret-value", "\n".join(logs.output))
        self.assertIn("[REDACTED]", "\n".join(logs.output))

    async def test_one_timeout_keeps_fast_result(self) -> None:
        results = await run_parallel_operations(
            {
                "fast-tool": lambda: value_after(0, "及时完成"),
                "slow-tool": lambda: value_after(0.05, "太晚了"),
            },
            timeout=0.001,
        )

        self.assertTrue(results.operations["fast-tool"].ok)
        self.assertFalse(results.operations["slow-tool"].ok)
        error = results.operations["slow-tool"].error
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.code, "timeout")
        self.assertEqual(error.exception_type, "TimeoutError")

    async def test_successful_none_is_not_mistaken_for_failure(self) -> None:
        results = await run_parallel_operations(
            {"optional-tool": lambda: value_after(0, None)},
            timeout=1,
        )

        self.assertTrue(results.operations["optional-tool"].ok)
        self.assertIsNone(results.operations["optional-tool"].value)
        self.assertEqual(results.failure_count, 0)

    async def test_empty_batch_has_zero_counts(self) -> None:
        results = await run_parallel_operations({}, timeout=1)

        self.assertEqual(results.operations, {})
        self.assertEqual(results.success_count, 0)
        self.assertEqual(results.failure_count, 0)

    async def test_operation_raised_timeout_is_not_framework_timeout(self) -> None:
        with self.assertLogs("repopilot.concurrency", level="ERROR"):
            results = await run_parallel_operations(
                {"tool": lambda: fail_after(0, TimeoutError("upstream response"))},
                timeout=1,
            )

        error = results.operations["tool"].error
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.code, "operation_failed")

    async def test_operation_mapping_is_read_only(self) -> None:
        results = await run_parallel_operations({}, timeout=1)

        with self.assertRaises(TypeError):
            results.operations["new"] = object()  # type: ignore[index, assignment]


if __name__ == "__main__":
    unittest.main()
