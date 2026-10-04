"""Exercise lazy provisioning with real Pydantic AI tool dispatch and no network."""

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from e2b import SandboxNotFoundException  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from config import Settings  # noqa: E402
from harness import OllamaAgent  # noqa: E402
from main import ChatService  # noqa: E402
from storage import Store  # noqa: E402


def answer(text="Done"):
    return ModelResponse(parts=[TextPart(text)])


def call_tools(*calls):
    return ModelResponse(
        parts=[
            ToolCallPart(name, arguments, tool_call_id=f"call-{index}")
            for index, (name, arguments) in enumerate(calls)
        ]
    )


COMMAND = ("run_command", {"command": "printf hello"})


class LazySandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = 10000.0
        self.settings = Settings(
            _env_file=None,
            database_path=Path(directory.name) / "chat.sqlite3",
            sandbox_idle_timeout_seconds=600,
            sandbox_max_lifetime_seconds=3600,
            chat_timeout_seconds=180,
        )
        self.store = Store(self.settings.database_path)
        self.addCleanup(self.store.close)
        self.created = 0
        self.sandboxes = SimpleNamespace(
            create=AsyncMock(side_effect=self.create_sandbox),
            is_running=AsyncMock(return_value=True),
            touch=AsyncMock(),
            terminate=AsyncMock(),
        )
        self.sandbox = SimpleNamespace(
            commands=SimpleNamespace(
                run=AsyncMock(return_value=SimpleNamespace(stdout="hello", stderr="", exit_code=0))
            ),
            files=SimpleNamespace(
                get_info=AsyncMock(return_value=SimpleNamespace(size=5)),
                read=AsyncMock(return_value="hello"),
                write=AsyncMock(),
                list=AsyncMock(return_value=[SimpleNamespace(name="hello.txt", type="file")]),
            ),
        )
        self.sdk = SimpleNamespace(
            get_info=AsyncMock(
                return_value=SimpleNamespace(end_at=datetime.now(UTC) + timedelta(minutes=10))
            ),
            connect=AsyncMock(return_value=self.sandbox),
        )
        self.steps = []
        self.instructions = []

        async def model(messages, info):
            self.instructions.append(info.instructions or "")
            self.assertTrue(self.steps, "Unexpected extra model request")
            step = self.steps.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step

        self.enterContext(patch("harness.OllamaModel", return_value=FunctionModel(model)))
        self.enterContext(
            patch("harness.e2b_client", return_value=SimpleNamespace(AsyncSandbox=self.sdk))
        )
        self.service = ChatService(
            self.settings, self.store, self.sandboxes, OllamaAgent(self.settings), lambda: self.now
        )

    async def create_sandbox(self, on_created):
        self.created += 1
        sandbox_id = f"sandbox-{self.created}"
        on_created(sandbox_id)
        return sandbox_id

    def thread(self, sandbox_id=None, created_at=None):
        thread_id = self.store.create(self.now)["id"]
        if sandbox_id:
            self.store.set_sandbox(
                thread_id, sandbox_id, self.now if created_at is None else created_at
            )
        return thread_id

    def assert_no_e2b(self):
        for name, method in vars(self.sandboxes).items():
            with self.subTest(lifecycle_method=name):
                method.assert_not_awaited()
        for name, method in vars(self.sdk).items():
            with self.subTest(sdk_method=name):
                method.assert_not_awaited()

    def reset_e2b(self):
        for method in vars(self.sandboxes).values():
            method.reset_mock()
        for method in vars(self.sdk).values():
            method.reset_mock()

    async def test_greeting_never_accesses_e2b_even_with_existing_or_expired_sandbox(self):
        for state, age in (("absent", None), ("running", 0), ("expired", 700)):
            with self.subTest(state=state):
                thread_id = self.thread(
                    sandbox_id=None if age is None else "existing",
                    created_at=None if age is None else self.now - age,
                )
                before = self.store.get(thread_id)
                self.steps = [answer("I'm well, thanks!")]
                detail = await self.service.send(thread_id, "hi how are you")
                self.assert_no_e2b()
                self.assertEqual(len(detail["messages"]), 2)
                self.assertEqual(detail["messages"][-1]["content"], "I'm well, thanks!")
                self.assertEqual(
                    detail["sandbox"]["status"], "not_started" if age is None else state
                )
                after = self.store.get(thread_id)
                for field in ("sandbox_id", "sandbox_created_at", "sandbox_last_used_at"):
                    self.assertEqual(after[field], before[field])
                self.assertTrue(json.loads(after["history"]))

    async def test_each_tool_can_trigger_first_sandbox_use(self):
        for tool in (
            COMMAND,
            ("read_file", {"path": "hello.txt"}),
            ("write_file", {"path": "hello.txt", "content": "hello"}),
            ("list_directory", {}),
        ):
            with self.subTest(tool=tool[0]):
                self.reset_e2b()
                thread_id = self.thread()
                self.steps = [call_tools(tool), answer()]
                detail = await self.service.send(thread_id, "Use the sandbox")
                self.sandboxes.create.assert_awaited_once()
                self.sdk.get_info.assert_awaited_once()
                self.sdk.connect.assert_awaited_once()
                self.assertEqual(detail["sandbox"]["status"], "running")
                self.assertEqual(self.store.get(thread_id)["sandbox_id"], f"sandbox-{self.created}")

    async def test_multiple_tools_share_one_creation_and_connection(self):
        thread_id = self.thread()
        self.steps = [
            call_tools(
                COMMAND,
                ("write_file", {"path": "hello.txt", "content": "hello"}),
                ("read_file", {"path": "hello.txt"}),
                ("list_directory", {}),
            ),
            answer(),
        ]
        await self.service.send(thread_id, "Run commands and work with files")
        self.sandboxes.create.assert_awaited_once()
        self.sdk.get_info.assert_awaited_once()
        self.sdk.connect.assert_awaited_once()
        self.sandbox.commands.run.assert_awaited_once()
        self.sandbox.files.write.assert_awaited_once()
        self.sandbox.files.read.assert_awaited_once()
        self.sandbox.files.list.assert_awaited_once()
        self.assertIn("fresh sandbox", self.instructions[-1].lower())

    async def test_later_tool_turn_reuses_sandbox_and_plain_chat_does_not_refresh_it(self):
        thread_id = self.thread()
        self.steps = [call_tools(COMMAND), answer()]
        await self.service.send(thread_id, "Run a command")
        original = self.store.get(thread_id)
        self.now += 30
        self.reset_e2b()
        self.steps = [answer("You're welcome")]
        await self.service.send(thread_id, "Thanks")
        self.assert_no_e2b()
        self.assertEqual(
            self.store.get(thread_id)["sandbox_last_used_at"], original["sandbox_last_used_at"]
        )
        self.now += 30
        self.steps = [call_tools(("read_file", {"path": "hello.txt"})), answer()]
        await self.service.send(thread_id, "Read the file")
        self.sandboxes.create.assert_not_awaited()
        self.sandboxes.is_running.assert_awaited_once_with(original["sandbox_id"])
        self.sdk.connect.assert_awaited_once()
        self.assertEqual(self.store.get(thread_id)["sandbox_id"], original["sandbox_id"])
        self.assertEqual(len(json.loads(self.store.get(thread_id)["messages"])), 6)

    async def test_expired_sandbox_is_replaced_only_when_a_tool_is_used(self):
        thread_id = self.thread("expired", self.now - 700)
        self.steps = [answer("Hello")]
        await self.service.send(thread_id, "Hi")
        self.assert_no_e2b()
        self.steps = [call_tools(COMMAND), answer()]
        await self.service.send(thread_id, "Run a command")
        self.sandboxes.terminate.assert_awaited_once_with("expired")
        self.sandboxes.create.assert_awaited_once()
        self.assertEqual(self.store.get(thread_id)["sandbox_id"], "sandbox-1")

    async def test_sandbox_removed_externally_is_replaced_on_tool_use(self):
        thread_id = self.thread("gone")
        self.sandboxes.is_running.return_value = False
        self.steps = [call_tools(COMMAND), answer()]
        await self.service.send(thread_id, "Run a command")
        self.sandboxes.is_running.assert_awaited_once_with("gone")
        self.sandboxes.create.assert_awaited_once()
        self.assertEqual(self.store.get(thread_id)["sandbox_id"], "sandbox-1")

    async def test_failures_before_tool_use_preserve_existing_sandbox_and_history(self):
        for failure, status in (
            (RuntimeError("model unavailable"), 502),
            (TimeoutError(), 504),
            (asyncio.CancelledError(), None),
        ):
            with self.subTest(failure=type(failure).__name__):
                thread_id = self.thread("existing")
                before = self.store.get(thread_id)
                self.steps = [failure]
                error_type = asyncio.CancelledError if status is None else HTTPException
                with self.assertRaises(error_type) as caught:
                    await self.service.send(thread_id, "Hi")
                if status is not None:
                    self.assertEqual(caught.exception.status_code, status)
                self.assert_no_e2b()
                self.assertEqual(self.store.get(thread_id), before)

    async def test_failures_after_tool_use_retire_sandbox_without_saving_turn(self):
        for failure, status in (
            (RuntimeError("model unavailable"), 502),
            (TimeoutError(), 504),
            (asyncio.CancelledError(), None),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.reset_e2b()
                thread_id = self.thread()
                self.steps = [call_tools(COMMAND), failure]
                error_type = asyncio.CancelledError if status is None else HTTPException
                with self.assertRaises(error_type) as caught:
                    await self.service.send(thread_id, "Run a command")
                if status is not None:
                    self.assertEqual(caught.exception.status_code, status)
                self.sandboxes.terminate.assert_awaited_once_with(f"sandbox-{self.created}")
                row = self.store.get(thread_id)
                self.assertIsNone(row["sandbox_id"])
                self.assertEqual(json.loads(row["messages"]), [])
                self.assertEqual(json.loads(row["history"]), [])

    async def test_connection_failure_retires_created_sandbox_without_replaying_tool(self):
        thread_id = self.thread()
        self.sdk.connect.side_effect = SandboxNotFoundException("Expired while connecting")
        self.steps = [call_tools(COMMAND)]
        with self.assertRaises(HTTPException) as caught:
            await self.service.send(thread_id, "Run a command")
        self.assertEqual(caught.exception.status_code, 502)
        self.sandboxes.create.assert_awaited_once()
        self.sandboxes.terminate.assert_awaited_once_with("sandbox-1")
        self.sandbox.commands.run.assert_not_awaited()
        self.assertIsNone(self.store.get(thread_id)["sandbox_id"])

    async def test_partial_creation_failure_retires_persisted_sandbox(self):
        async def fail_after_persisting(on_created):
            on_created("partial")
            raise RuntimeError("creation failed after persisting ownership")

        self.sandboxes.create.side_effect = fail_after_persisting
        thread_id = self.thread()
        self.steps = [call_tools(COMMAND)]
        with self.assertRaises(HTTPException):
            await self.service.send(thread_id, "Run a command")
        self.sandboxes.terminate.assert_awaited_once_with("partial")
        self.sdk.connect.assert_not_awaited()
        self.assertIsNone(self.store.get(thread_id)["sandbox_id"])

    async def test_concurrent_turns_share_one_conversation_sandbox(self):
        thread_id = self.thread()
        self.steps = [call_tools(COMMAND), answer(), call_tools(COMMAND), answer()]
        await asyncio.gather(
            self.service.send(thread_id, "First command"),
            self.service.send(thread_id, "Second command"),
        )
        self.sandboxes.create.assert_awaited_once()
        self.sandboxes.is_running.assert_awaited_once_with("sandbox-1")
        self.assertEqual(self.sdk.connect.await_count, 2)
        self.assertEqual(len(json.loads(self.store.get(thread_id)["messages"])), 4)

    async def test_invalid_oversized_write_does_not_provision_sandbox(self):
        thread_id = self.thread()
        self.steps = [
            call_tools(("write_file", {"path": "large.txt", "content": "x" * 65537})),
            answer("The file is too large"),
        ]
        detail = await self.service.send(thread_id, "Write a large file")
        self.assert_no_e2b()
        self.sandbox.files.write.assert_not_awaited()
        self.assertEqual(detail["sandbox"]["status"], "not_started")


if __name__ == "__main__":
    unittest.main()
