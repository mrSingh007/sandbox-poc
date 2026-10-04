# Sandbox Chat

A small **Pydantic AI + Ollama + E2B** chat app you can run locally. Each conversation gets its own remote E2B sandbox only when the agent needs a shell command or file operation. FastAPI serves the plain HTML/CSS/JavaScript frontend; SQLite saves conversations and agent tool history.

```text
Browser → local FastAPI backend → Ollama API (cloud or local model)
                              → E2B cloud sandbox (commands and files)
                              → local SQLite database (chat history)
```

## Requirements

- **Python 3.12 or newer** and [uv](https://docs.astral.sh/uv/getting-started/installation/). You can install Python with `uv python install 3.12` after installing uv.
- **An [E2B account](https://e2b.dev/) and E2B API key are required**, including when using a local Ollama model. Sign up, open the [E2B dashboard](https://e2b.dev/dashboard), and create/copy an API key for your team. See the [E2B quickstart](https://docs.e2b.dev/quickstart).
- **An Ollama model that supports tool calling**, either through an Ollama cloud account/API key or a local Ollama installation. Choose one setup below.
- Internet access to E2B and, for cloud models, Ollama. Provider usage and account limits apply.

No Node.js, frontend build, local Docker installation, or E2B CLI is required. The Python SDK builds the sandbox template remotely on E2B.

## Install and configure

### 1. Get the project and install dependencies

Clone this repository or download and extract its ZIP. Open a terminal in the project root: the folder containing this README, `backend/`, and `frontend/`. Run all project commands below from that folder.

```sh
uv sync --project backend --locked
```

This installs the versions in `backend/uv.lock` into `backend/.venv`. You do not need to activate the virtual environment when using `uv run`.

### 2. Create your environment file

For a new installation, copy the example:

```sh
# macOS / Linux
cp backend/.env.example backend/.env
```

```powershell
# Windows PowerShell
Copy-Item backend/.env.example backend/.env
```

If you already have `backend/.env`, edit it instead of overwriting it. Replace the placeholder keys with your own. `.env` files are ignored by Git; only the example should be committed.

### 3. Choose cloud or local Ollama

**Option A: Ollama cloud**

Create an [Ollama account](https://ollama.com/) and an [API key](https://ollama.com/settings/keys). Set these values in `backend/.env`:

```dotenv
OLLAMA_BASE_URL=https://ollama.com/v1
OLLAMA_MODEL=gpt-oss:120b
OLLAMA_API_KEY=your-ollama-key

E2B_API_KEY=your-e2b-key
E2B_TEMPLATE=pydantic-ai-chat-python312
E2B_BASE_IMAGE=python:3.12-slim
```

A local Ollama installation is not needed for this option. See [Ollama cloud documentation](https://docs.ollama.com/cloud) for model access and account requirements.

**Option B: Local Ollama**

[Install Ollama](https://docs.ollama.com/quickstart), start the Ollama app/service, and download a tool-capable model:

```sh
ollama pull qwen3:8b
```

If the Ollama service is not already running, run `ollama serve` in a separate terminal and leave it running. Set these values in `backend/.env`:

```dotenv
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen3:8b
OLLAMA_API_KEY=

E2B_API_KEY=your-e2b-key
E2B_TEMPLATE=pydantic-ai-chat-python312
E2B_BASE_IMAGE=python:3.12-slim
```

Local models require sufficient RAM for the selected model. The backend calls your local Ollama service, but code execution still happens in E2B's cloud.

### 4. Build your E2B template

Build once using the E2B API key from your own account/team:

```sh
uv run --project backend backend/build_template.py
```

Wait for `E2B template ready: ...` before asking the agent to use sandbox tools. The build creates the template named by `E2B_TEMPLATE`, using `E2B_BASE_IMAGE`, with `/workspace` as its working directory and root as its sandbox user. The default image includes Python 3.12; additional project dependencies are not installed in the sandbox automatically.

To change the sandbox image, edit `E2B_BASE_IMAGE` (for example, `python:3.13-slim`) and rerun the build command. Use an [E2B-supported Linux base image](https://docs.e2b.dev/template/base-image) containing the tools your prompts need. The image is a **build-time setting**: changing it alone does not rebuild the template or modify existing sandboxes. After rebuilding, start a new conversation to use the new image. You can also change `E2B_TEMPLATE` to keep separate templates; restart the backend after changing its settings.

### 5. Start the app

```sh
uv run --project backend backend/main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Keep the terminal running; press Ctrl+C to stop. Restart using the same command. Environment files load automatically, so there is no need to `source` them or export keys manually.

**Configuration precedence:** shell environment variables override `backend/.env`, which overrides the project-root `.env`, which overrides code defaults. Files are found relative to the backend source, independent of the working directory. Restart the backend after editing `.env`.

## Try it

1. Click **New conversation** and send a greeting. Ordinary chat does not create a sandbox; it starts only when the agent calls a tool that needs it.
2. Try: **"Run `python --version` in the sandbox, write `hello.txt` containing `Hello from E2B`, then read the file back."** This checks the model, tool calling, template, and E2B connection together.
3. Send a follow-up asking to read `hello.txt` again. Within the sandbox lifetime, the file should still exist.
4. Reload the browser or restart the backend: saved chat history remains. Switch conversations in the sidebar. Enter sends; Shift+Enter adds a line.
5. Delete a test conversation with the trash button when finished; this also terminates its sandbox.

[Health check](http://127.0.0.1:8000/health) returns `{"status":"ok"}` when the backend responds. It **does not validate E2B or Ollama**. Interactive API documentation is at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Configuration reference

The table lists code defaults. The supplied `.env.example` selects Ollama cloud instead of the local defaults.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_MODEL` | `qwen3:8b` | Model supporting tool calling |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama endpoint; include `/v1` |
| `OLLAMA_API_KEY` | unset | Required for Ollama cloud; leave blank for local Ollama |
| `E2B_API_KEY` | unset | Required for sandbox tools and template builds |
| `E2B_TEMPLATE` | `pydantic-ai-chat-python312` | Template name for builds; name or ID for chat sandboxes |
| `E2B_BASE_IMAGE` | `python:3.12-slim` | Base image used by `build_template.py`; rebuild after changing |
| `SANDBOX_IDLE_TIMEOUT_SECONDS` | `600` | Idle expiry, 60–86400 seconds |
| `SANDBOX_MAX_LIFETIME_SECONDS` | `3600` | App-enforced maximum sandbox age, 60–86400 seconds |
| `CHAT_TIMEOUT_SECONDS` | `180` | Whole-turn deadline, 10–600 seconds |
| `DATABASE_PATH` | `backend/data/chat.sqlite3` | Default is anchored to the backend directory; custom relative paths resolve from the working directory |

`CHAT_TIMEOUT_SECONDS` must be less than `SANDBOX_IDLE_TIMEOUT_SECONDS`. Maximum lifetime must be at least the idle timeout. Keep timeout values within your E2B account's limits. For a ten-minute total lifetime, set both sandbox timeout values to `600`.

## Conversation and sandbox lifetime

- Each conversation owns a sandbox, created on its first sandbox tool call. Later tool calls reconnect to its saved ID, preserving files and processes while it is alive. Greetings and other replies without sandbox tools do not create or connect to a sandbox.
- The default expiry is **ten minutes of inactivity**. The first sandbox tool call in a turn and completion of that turn refresh E2B's timeout. Ordinary chat and viewing history do not extend it. A backend sweep checks expired IDs every 15 seconds.
- E2B kills the sandbox when its last assigned timeout expires, even if the backend is offline. Stopping the backend does not immediately terminate active sandboxes.
- Expiry removes sandbox files, **not chat history**. The next sandbox tool call creates a fresh sandbox and informs the agent that previous files are gone.
- The app checks maximum sandbox age when a tool first needs it and during cleanup sweeps. The UI status uses the saved deadline; the next sandbox tool call detects a sandbox removed externally.
- Failed or cancelled turns leave saved chat history unchanged and retire the sandbox only if that turn requested it. Messages are not replayed automatically.
- Deleting a conversation waits for any active reply, terminates its sandbox, then removes its local history. If termination fails, the conversation remains so deletion can be retried.

The agent exposes four tools: `run_command`, `read_file`, `write_file`, and `list_directory`. Command waits are limited to 60 seconds; file reads/writes to 64 KB; tool output is bounded. Tools operate as root inside the remote sandbox. Host files and commands are not exposed as agent tools.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Chat returns **502** | Check the backend terminal, E2B key and account access, completed template build, Ollama endpoint/key, and model tool support. A healthy `/health` response does not check these services. |
| Works in one terminal but fails after restarting | A shell export may override `.env`. Check shell startup files such as `~/.zshrc` or `~/.bashrc` for stale exports. Remove/update the export and restart. |
| Template build fails or template is not found | Set `E2B_API_KEY`, run the build command successfully, and use the same team/key and template name for the backend. Check E2B account limits and the selected base image. |
| Local Ollama connection fails | Start Ollama, run `ollama pull` for the configured model, and check `OLLAMA_BASE_URL` ends in `/v1`. |
| Chat returns **504** | The whole turn exceeded `CHAT_TIMEOUT_SECONDS`. Try a shorter task or adjust that timeout while keeping it below the sandbox idle timeout. |
| Port 8000 is already in use | Stop the previous backend process before starting another instance. |
| Files disappeared but chat remains | The sandbox expired or was retired after a failed turn. Files are temporary; chat history is stored separately. |

To ignore a stale Ollama key export for one launch on macOS/Linux:

```sh
env -u OLLAMA_API_KEY uv run --project backend backend/main.py
```

In PowerShell, remove the current process's export with `Remove-Item Env:OLLAMA_API_KEY -ErrorAction SilentlyContinue`, then start the backend normally. These commands allow the saved `.env` key to take effect; also correct the startup file if it keeps restoring an old key.

## Development and contributions

Dependencies are locked in `backend/uv.lock`; keep this file committed. Install the development tools with the setup command, then run:

```sh
uv run --project backend ruff check backend
uv run --project backend python -m unittest discover -s backend/tests -v
```

The automated tests cover lazy sandbox creation and lifecycle behavior without calling E2B or Ollama. Use the manual checks in **Try it** to check real service connections after changes. When opening an issue, include the steps to reproduce, Python version, model name, and the error with credentials removed. Contributions should describe the change and how it was verified.

| Path | Purpose |
| --- | --- |
| `backend/main.py` | API and conversation lifecycle |
| `backend/harness.py` | Ollama agent, E2B lifecycle, and sandbox tools |
| `backend/config.py` | Environment and dotenv settings |
| `backend/build_template.py` | Configurable E2B template build |
| `backend/storage.py` | SQLite persistence |
| `backend/.env.example` | Shareable configuration example |
| `frontend/` | Static chat interface |

## Data and deployment scope

This is a single-user local app bound to `127.0.0.1`, without authentication. Keep it local; public deployment needs authentication and access controls. Use one backend process/worker per database because conversation locks live in that process. Streaming and durable sandbox volumes are not implemented.

API keys stay in the backend and are not injected into sandboxes or sent to the browser. Prompts and tool results are sent to the configured model provider; sandbox commands and files are sent to E2B. SQLite stores messages and full agent/tool history in `backend/data/`. Stop the backend before copying that folder for a consistent local backup.

`.gitignore` excludes credentials (`.env`, `.env.*`, except `.env.example`), nested virtual environments, Python caches, local databases/history, logs, and build/test artifacts. Before publishing, review `git status --short --ignored` and the staged diff: `.gitignore` does not remove files already tracked by Git. If you share a ZIP, package only source files; Git ignore rules do not filter ordinary ZIP creation.

## License

[MIT](LICENSE).
