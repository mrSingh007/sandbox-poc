"""Pydantic AI talks to Ollama and runs shell/file tools inside E2B."""

import asyncio
import posixpath
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from e2b import E2B, CommandExitException, FileNotFoundException, SandboxNotFoundException
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.usage import UsageLimits

from config import Settings

WORKDIR = "/workspace"
MAX_OUTPUT = 20000


def e2b_client(settings: Settings) -> E2B:
    return E2B(
        api_key=settings.e2b_api_key.get_secret_value() if settings.e2b_api_key else None,
        request_timeout=30,
    )


class E2BSandboxes:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sdk = e2b_client(settings).AsyncSandbox

    async def create(self, on_created: Callable[[str], None]) -> str:
        sandbox = await self.sdk.create(
            template=self.settings.e2b_template,
            timeout=self.settings.sandbox_idle_timeout_seconds,
            lifecycle={"on_timeout": "kill"},
            metadata={"app": "pydantic-ai-chat"},
        )
        # Persist ownership before another await; never enter a sandbox context
        # manager here, since exiting it would kill the conversation's sandbox.
        try:
            on_created(sandbox.sandbox_id)
        except Exception:
            await sandbox.kill()
            raise
        return sandbox.sandbox_id

    async def is_running(self, sandbox_id: str) -> bool:
        try:
            info = await self.sdk.get_info(sandbox_id)
            return info.state == "running"
        except SandboxNotFoundException:
            return False

    async def touch(self, sandbox_id: str, timeout: int):
        # TTL is measured from now. This works even if the backend goes offline.
        await self.sdk.set_timeout(sandbox_id, timeout=timeout)

    async def terminate(self, sandbox_id: str):
        async with asyncio.timeout(30):
            # False means already gone; either outcome confirms cleanup.
            await self.sdk.kill(sandbox_id)


def bounded(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + "\n[Output truncated]"


def workspace_path(path: str) -> str:
    return posixpath.join(WORKDIR, path)


class OllamaAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sdk = e2b_client(settings).AsyncSandbox

    async def reply(
        self,
        message: str,
        history: str,
        acquire_sandbox: Callable[[], Awaitable[tuple[str, bool]]],
    ):
        settings = self.settings
        sandbox = None
        fresh = None

        async def get_sandbox():
            nonlocal sandbox, fresh
            # Tools are sequential, so the first one acquires the shared connection.
            if sandbox is None:
                sandbox_id, fresh = await acquire_sandbox()
                # connect can extend the TTL; supply only the time already remaining.
                info = await self.sdk.get_info(sandbox_id)
                remaining = int((info.end_at - datetime.now(UTC)).total_seconds())
                if remaining < 1:
                    raise SandboxNotFoundException("Sandbox expired before the tool started")
                sandbox = await self.sdk.connect(sandbox_id, timeout=remaining)
            return sandbox

        agent = Agent(
            OllamaModel(
                settings.ollama_model,
                provider=OllamaProvider(
                    base_url=settings.ollama_base_url,
                    api_key=(
                        settings.ollama_api_key.get_secret_value()
                        if settings.ollama_api_key
                        else None
                    ),
                ),
            ),
            instructions=(
                "You are a helpful, concise assistant. Use the E2B sandbox tools only when "
                "the task needs code execution or file operations. Answer greetings, casual "
                "conversation, and questions that need no execution or files directly without "
                "tools. The sandbox starts only when a tool needs it. Commands run in /workspace. "
                "Inspect the environment if you need to know what is installed. "
            ),
        )

        @agent.instructions
        def sandbox_state() -> str:
            if fresh is None:
                return (
                    "The sandbox has not been accessed this turn. Files from earlier turns "
                    "may have expired; check with a tool if the task needs them."
                )
            if fresh:
                return "This is a fresh sandbox: files from earlier turns are no longer present."
            return "The sandbox keeps files from earlier turns in this conversation."

        @agent.tool_plain(sequential=True)
        async def run_command(command: str) -> dict:
            """Run a shell command in /workspace and wait up to 60 seconds."""
            sandbox = await get_sandbox()
            try:
                result = await sandbox.commands.run(command, cwd=WORKDIR, user="root", timeout=60)
            except CommandExitException as exc:
                result = exc
            return {
                "stdout": bounded(result.stdout),
                "stderr": bounded(result.stderr),
                "exit_code": result.exit_code,
            }

        @agent.tool_plain(sequential=True)
        async def read_file(path: str) -> str:
            """Read a UTF-8 sandbox file, at most 64 KB. Paths are relative to /workspace."""
            sandbox = await get_sandbox()
            try:
                path = workspace_path(path)
                info = await sandbox.files.get_info(path, user="root")
                if info.size > 65536:
                    raise ModelRetry("File is too large. Read a section with run_command instead.")
                return bounded(await sandbox.files.read(path, user="root"))
            except FileNotFoundException:
                raise ModelRetry("File not found. Check its path with list_directory.") from None

        @agent.tool_plain(sequential=True)
        async def write_file(path: str, content: str) -> str:
            """Write a UTF-8 file in the sandbox; paths are relative to /workspace."""
            if len(content.encode()) > 65536:
                raise ModelRetry("Write at most 64 KB at a time.")
            sandbox = await get_sandbox()
            await sandbox.files.write(workspace_path(path), content, user="root")
            return f"Wrote {path}"

        @agent.tool_plain(sequential=True)
        async def list_directory(path: str = ".") -> list[dict]:
            """List up to 200 entries in a sandbox directory, relative to /workspace."""
            sandbox = await get_sandbox()
            try:
                entries = await sandbox.files.list(workspace_path(path), user="root", depth=1)
            except FileNotFoundException:
                raise ModelRetry("Directory not found. Check the path.") from None
            return [{"name": entry.name, "type": entry.type} for entry in entries[:200]]

        async with agent:
            result = await agent.run(
                message,
                message_history=ModelMessagesTypeAdapter.validate_json(history),
                usage_limits=UsageLimits(request_limit=20),
            )
        return result.output, result.all_messages_json()
