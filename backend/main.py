"""Single-process local chat app. Run: uv run --project backend backend/main.py."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from weakref import WeakValueDictionary

from e2b import SandboxNotFoundException
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import Settings
from harness import E2BSandboxes, OllamaAgent
from storage import Store

logger = logging.getLogger(__name__)
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)


class ChatService:
    def __init__(self, settings, store, sandboxes, agent, clock=time.time):
        self.settings = settings
        self.store = store
        self.sandboxes = sandboxes
        self.agent = agent
        self.clock = clock
        self.locks = WeakValueDictionary()

    def lock(self, thread_id):
        lock = self.locks.get(thread_id)
        if lock is None:
            lock = self.locks[thread_id] = asyncio.Lock()
        return lock

    def require(self, thread_id):
        row = self.store.get(thread_id)
        if row is None:
            raise HTTPException(404, "Conversation not found.")
        return row

    def deadline(self, row):
        if not row["sandbox_id"]:
            return None
        return min(
            row["sandbox_last_used_at"] + self.settings.sandbox_idle_timeout_seconds,
            row["sandbox_created_at"] + self.settings.sandbox_max_lifetime_seconds,
        )

    def summary(self, row):
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": iso(row["created_at"]),
            "updated_at": iso(row["updated_at"]),
        }

    def detail(self, row):
        deadline = self.deadline(row)
        messages = json.loads(row["messages"])
        status = "not_started"
        if deadline is not None:
            status = "running" if deadline > self.clock() else "expired"
        return {
            **self.summary(row),
            "messages": messages,
            "sandbox": {
                "status": status,
                "expires_at": iso(deadline) if deadline is not None else None,
            },
        }

    async def ensure_sandbox(self, row):
        sandbox_id = row["sandbox_id"]
        if sandbox_id:
            if self.deadline(row) <= self.clock():
                await self.sandboxes.terminate(sandbox_id)
            elif await self.sandboxes.is_running(sandbox_id):
                return sandbox_id, False
            self.store.set_sandbox(row["id"], None, self.clock())
        sandbox_id = await self.sandboxes.create(
            lambda new_id: self.store.set_sandbox(row["id"], new_id, self.clock())
        )
        return sandbox_id, True

    async def retire_failed_sandbox(self, thread_id):
        row = self.require(thread_id)
        if not row["sandbox_id"]:
            return
        # An abandoned remote command can outlive its client. Mark the sandbox
        # expired first, so even failed cleanup cannot let a later turn reuse it.
        self.store.touch(thread_id, 0)
        try:
            await self.sandboxes.terminate(row["sandbox_id"])
        except Exception as exc:
            logger.warning("Failed sandbox cleanup will retry (%s)", type(exc).__name__)
        else:
            self.store.set_sandbox(thread_id, None, self.clock())

    async def refresh_sandbox(self, thread_id):
        row = self.require(thread_id)
        remaining = int(
            row["sandbox_created_at"] + self.settings.sandbox_max_lifetime_seconds - self.clock()
        )
        if remaining < 1:
            raise SandboxNotFoundException("Sandbox reached its maximum lifetime")
        timeout = min(self.settings.sandbox_idle_timeout_seconds, remaining)
        await self.sandboxes.touch(row["sandbox_id"], timeout)
        self.store.touch(thread_id, self.clock())

    async def send(self, thread_id, message):
        message = message.strip()
        if not message:
            raise HTTPException(422, "Message cannot be blank.")
        async with self.lock(thread_id):
            row = self.require(thread_id)
            sandbox_requested = False

            async def acquire_sandbox():
                nonlocal sandbox_requested
                # Set before awaiting so failed/partial acquisitions are also cleaned up.
                sandbox_requested = True
                sandbox_id, fresh = await self.ensure_sandbox(self.require(thread_id))
                await self.refresh_sandbox(thread_id)
                return sandbox_id, fresh

            async def retire_used_sandbox():
                if sandbox_requested:
                    await self.retire_failed_sandbox(thread_id)

            try:
                async with asyncio.timeout(self.settings.chat_timeout_seconds):
                    reply, history = await self.agent.reply(
                        message, row["history"], acquire_sandbox
                    )
                self.store.save_turn(thread_id, message, reply, history, self.clock())
            except asyncio.CancelledError:
                await retire_used_sandbox()
                raise
            except TimeoutError:
                await retire_used_sandbox()
                raise HTTPException(504, "The request timed out. Your chat history is unchanged.")
            except SandboxNotFoundException:
                # Do not replay: tools may already have executed before the failure.
                await retire_used_sandbox()
                raise HTTPException(
                    502,
                    "The sandbox expired during this turn. Send again; a new sandbox "
                    "will start if a tool needs it. Your chat history is unchanged.",
                )
            except Exception as exc:
                logger.warning("Chat request failed (%s)", type(exc).__name__)
                await retire_used_sandbox()
                raise HTTPException(
                    502,
                    "Could not complete the reply. Check the E2B API key and template, "
                    "Ollama availability, and that the model supports tools. "
                    "Your chat history is unchanged.",
                ) from None
            # Only sandbox use extends its lifetime; ordinary chat leaves it idle.
            if sandbox_requested:
                try:
                    async with asyncio.timeout(5):
                        await self.refresh_sandbox(thread_id)
                except Exception as exc:
                    logger.warning("Sandbox idle refresh failed (%s)", type(exc).__name__)
            return self.detail(self.require(thread_id))

    async def delete(self, thread_id):
        # Wait for any active turn before terminating its container.
        async with self.lock(thread_id):
            row = self.require(thread_id)
            if row["sandbox_id"]:
                try:
                    await self.sandboxes.terminate(row["sandbox_id"])
                except Exception as exc:
                    logger.warning("Sandbox deletion failed (%s)", type(exc).__name__)
                    raise HTTPException(
                        502,
                        "Could not confirm sandbox removal. The conversation "
                        "is kept so you can retry deleting it.",
                    ) from None
            self.store.delete(thread_id)

    async def cleanup_expired(self):
        for item in self.store.list():
            if not item["sandbox_id"] or self.deadline(item) > self.clock():
                continue
            lock = self.lock(item["id"])
            if lock.locked():
                continue  # Never terminate an active turn.
            async with lock:
                row = self.store.get(item["id"])
                if not row or not row["sandbox_id"] or self.deadline(row) > self.clock():
                    continue
                try:
                    await self.sandboxes.terminate(row["sandbox_id"])
                except Exception as exc:
                    logger.warning("Sandbox cleanup will retry (%s)", type(exc).__name__)
                else:
                    self.store.set_sandbox(row["id"], None, self.clock())

    async def cleanup_loop(self):
        while True:
            try:
                await self.cleanup_expired()
            except Exception as exc:
                logger.warning("Cleanup will retry (%s)", type(exc).__name__)
            await asyncio.sleep(15)


def create_app(settings=None, sandboxes=None, agent=None, clock=time.time):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        store = Store(settings.database_path)
        service = ChatService(
            settings,
            store,
            sandboxes or E2BSandboxes(settings),
            agent or OllamaAgent(settings),
            clock,
        )
        app.state.service = service
        cleanup = asyncio.create_task(service.cleanup_loop())
        try:
            yield
        finally:
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup
            # Keep sandbox IDs on shutdown. E2B enforces the last TTL even offline.
            store.close()

    app = FastAPI(title="Pydantic AI Chat", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/config")
    async def config():
        return {
            "model": settings.ollama_model,
            "sandbox_idle_timeout_seconds": settings.sandbox_idle_timeout_seconds,
            "sandbox_max_lifetime_seconds": settings.sandbox_max_lifetime_seconds,
        }

    @app.get("/api/threads")
    async def list_threads():
        service = app.state.service
        return [service.summary(row) for row in service.store.list()]

    @app.post("/api/threads", status_code=201)
    async def create_thread():
        service = app.state.service
        return service.detail(service.store.create(clock()))

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str):
        service = app.state.service
        return service.detail(service.require(thread_id))

    @app.post("/api/threads/{thread_id}/messages")
    async def send_message(thread_id: str, request: ChatRequest):
        return await app.state.service.send(thread_id, request.message)

    @app.delete("/api/threads/{thread_id}", status_code=204)
    async def delete_thread(thread_id: str):
        await app.state.service.delete(thread_id)
        return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(FRONTEND / "index.html")

    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
