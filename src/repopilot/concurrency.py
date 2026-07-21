from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from time import perf_counter

from .models import OperationError, OperationResult, ParallelOperationsResult
from .observability import log_exception_safely

OperationFactory = Callable[[], Awaitable[object]]
logger = logging.getLogger(__name__)


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)


async def _run_operation(
    name: str,
    operation_factory: OperationFactory,
    timeout: float,
) -> tuple[str, OperationResult]:
    started_at = perf_counter()
    timeout_scope = asyncio.timeout(timeout)
    try:
        async with timeout_scope:
            value = await operation_factory()
    except asyncio.CancelledError:
        raise
    except TimeoutError as exc:
        if not timeout_scope.expired():
            log_exception_safely(
                logger,
                f"Operation {name!r} failed with TimeoutError",
                exc,
            )
            return (
                name,
                OperationResult(
                    ok=False,
                    error=OperationError(
                        code="operation_failed",
                        message=f"Operation {name!r} failed.",
                        exception_type=type(exc).__name__,
                    ),
                    duration_ms=_elapsed_ms(started_at),
                ),
            )

        logger.warning("Operation %r timed out", name)
        return (
            name,
            OperationResult(
                ok=False,
                error=OperationError(
                    code="timeout",
                    message=f"Operation {name!r} timed out.",
                    exception_type=type(exc).__name__,
                ),
                duration_ms=_elapsed_ms(started_at),
            ),
        )
    except Exception as exc:
        log_exception_safely(logger, f"Operation {name!r} failed", exc)
        return (
            name,
            OperationResult(
                ok=False,
                error=OperationError(
                    code="operation_failed",
                    message=f"Operation {name!r} failed.",
                    exception_type=type(exc).__name__,
                ),
                duration_ms=_elapsed_ms(started_at),
            ),
        )

    return (
        name,
        OperationResult(
            ok=True,
            value=value,
            duration_ms=_elapsed_ms(started_at),
        ),
    )


async def run_parallel_operations(
    operations: Mapping[str, OperationFactory],
    *,
    timeout: float,
) -> ParallelOperationsResult:
    """Run operations concurrently with per-operation timeout isolation."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    results = await asyncio.gather(
        *(
            _run_operation(name, operation_factory, timeout)
            for name, operation_factory in operations.items()
        )
    )
    return ParallelOperationsResult(dict(results))
