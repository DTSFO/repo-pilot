from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from repopilot.storage import Database, TaskStore


class TaskStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "storage.db"
        self.database = Database(f"sqlite+aiosqlite:///{path}")
        await self.database.initialize()
        self.store = TaskStore(self.database)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.directory.cleanup()

    async def test_task_event_and_checkpoint_round_trip(self) -> None:
        task = await self.store.create_task(
            "Find the agent loop",
            constraints={"citations_required": True},
            budget={"max_steps": 8},
        )
        await self.store.update_task(task.id, status="running", current_node="planner")
        first = await self.store.append_event(task.id, "task.started", {"node": "planner"})
        second = await self.store.append_event(task.id, "node.completed", {"node": "planner"})
        checkpoint = await self.store.save_checkpoint(
            task.id,
            "researcher",
            {"goal": task.goal, "plan": ["search"]},
        )

        loaded = await self.store.get_task(task.id)
        events = await self.store.list_events(task.id)
        latest = await self.store.latest_checkpoint(task.id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.status, "running")
        self.assertEqual(loaded.current_node, "researcher")
        self.assertEqual([first.sequence, second.sequence], [1, 2])
        self.assertEqual([event.event_type for event in events], ["task.started", "node.completed"])
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.version, checkpoint.version)
        self.assertEqual(latest.state_json["plan"], ["search"])

    async def test_event_cursor_and_task_listing(self) -> None:
        first_task = await self.store.create_task("first")
        await self.store.create_task("second")
        await self.store.append_event(first_task.id, "one")
        await self.store.append_event(first_task.id, "two")

        events = await self.store.list_events(first_task.id, after_sequence=1)
        tasks = await self.store.list_tasks()

        self.assertEqual([event.event_type for event in events], ["two"])
        self.assertEqual(len(tasks), 2)

    async def test_concurrent_event_appends_keep_a_gapless_task_sequence(self) -> None:
        task = await self.store.create_task("concurrent telemetry")

        await asyncio.gather(
            *(
                self.store.append_event(task.id, "provider.progress", {"tick": tick})
                for tick in range(20)
            )
        )

        events = await self.store.list_events(task.id)
        self.assertEqual([event.sequence for event in events], list(range(1, 21)))
        self.assertEqual({event.payload_json["tick"] for event in events}, set(range(20)))

    async def test_missing_task_is_not_silently_created(self) -> None:
        with self.assertRaises(KeyError):
            await self.store.update_task("missing", status="running")
        with self.assertRaises(KeyError):
            await self.store.append_event("missing", "event")
        with self.assertRaises(KeyError):
            await self.store.save_checkpoint("missing", "planner", {})


if __name__ == "__main__":
    unittest.main()
