"use strict";

const byId = (id) => document.getElementById(id);
const ui = {
  list: byId("thread-list"), listEmpty: byId("thread-list-empty"),
  newThread: byId("new-thread"), welcomeNew: byId("welcome-new-thread"),
  title: byId("conversation-title"), sandboxStatus: byId("sandbox-status"),
  welcome: byId("welcome"), welcomeTitle: byId("welcome-title"),
  welcomeDescription: byId("welcome-description"), messages: byId("messages"),
  scroll: byId("chat-scroll"), input: byId("message-input"), send: byId("send-message"),
  threadError: byId("thread-error"), announcer: byId("announcer"),
};
const state = {
  threads: [], details: new Map(), drafts: new Map(), errors: new Map(),
  pending: new Map(), deleting: new Set(), selected: null, selectionVersion: 0,
  loading: false, creating: false, ready: false, model: "Ollama",
};
const selectionKey = "sandbox-chat-selected-thread";

function rememberSelection(id) {
  try {
    if (id) localStorage.setItem(selectionKey, id);
    else localStorage.removeItem(selectionKey);
  } catch { /* Chat still works when browser storage is unavailable. */ }
}

function previousSelection() {
  try { return localStorage.getItem(selectionKey); } catch { return null; }
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Accept": "application/json", ...options.headers } });
  if (!response.ok) {
    let message = `Request failed (${response.status}). Please try again.`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (typeof body.error === "string") message = body.error;
    } catch { /* Keep the useful HTTP status for non-JSON errors. */ }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function showError(error) {
  byId("global-error-text").textContent = error.message || "Unable to connect. Please try again.";
  byId("global-error").hidden = false;
}

function updateSummary(detail) {
  const { id, title, created_at, updated_at } = detail;
  const summary = { id, title, created_at, updated_at };
  const index = state.threads.findIndex((thread) => thread.id === id);
  if (index === -1) state.threads.push(summary);
  else state.threads[index] = summary;
  state.threads.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

function renderThreads() {
  ui.list.replaceChildren();
  for (const thread of state.threads) {
    const row = document.createElement("div");
    row.className = `thread-row${thread.id === state.selected ? " active" : ""}`;
    const select = document.createElement("button");
    select.className = "thread-select";
    select.textContent = thread.title || "New conversation";
    select.title = select.textContent;
    if (thread.id === state.selected) select.setAttribute("aria-current", "page");
    select.disabled = state.deleting.has(thread.id);
    select.addEventListener("click", () => selectThread(thread.id));
    const remove = document.createElement("button");
    remove.className = "thread-delete";
    remove.title = "Delete conversation";
    remove.setAttribute("aria-label", `Delete ${thread.title || "conversation"}`);
    remove.disabled = state.deleting.has(thread.id);
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor");
    icon.setAttribute("stroke-width", "1.5");
    icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7");
    icon.append(path);
    remove.append(icon);
    remove.addEventListener("click", () => deleteThread(thread.id));
    row.append(select, remove);
    ui.list.append(row);
  }
  ui.listEmpty.hidden = state.threads.length > 0;
  ui.listEmpty.textContent = state.ready ? "Your conversations will appear here." : "Loading conversations…";
  ui.newThread.disabled = !state.ready || state.creating;
  ui.welcomeNew.disabled = ui.newThread.disabled;
}

function messageElement(role, content, pending = false) {
  const article = document.createElement("article");
  article.className = `message ${role === "user" ? "user" : "assistant"}${pending ? " pending" : ""}`;
  const heading = document.createElement("div");
  heading.className = "message-heading";
  const avatar = document.createElement("span");
  avatar.className = "message-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = role === "user" ? "Y" : "s";
  const label = document.createElement("span");
  label.textContent = role === "user" ? "You" : "Assistant";
  heading.append(avatar, label);
  const body = document.createElement("div");
  body.className = "message-content";
  if (pending) {
    const dot = document.createElement("span");
    dot.className = "typing-dot";
    dot.setAttribute("aria-hidden", "true");
    body.append(dot);
  }
  body.append(document.createTextNode(content));
  article.append(heading, body);
  return article;
}

function renderSandboxStatus() {
  const detail = state.details.get(state.selected);
  if (!state.selected) ui.sandboxStatus.textContent = "A little room to think and build.";
  else if (state.deleting.has(state.selected)) ui.sandboxStatus.textContent = "Deleting conversation…";
  else if (state.loading) ui.sandboxStatus.textContent = "Loading conversation…";
  else if (state.pending.has(state.selected)) ui.sandboxStatus.textContent = "Assistant is working…";
  else if (!detail || detail.sandbox.status === "not_started") ui.sandboxStatus.textContent = "Sandbox starts when a tool needs it";
  else if (detail.sandbox.status === "expired" || (detail.sandbox.expires_at && Date.parse(detail.sandbox.expires_at) <= Date.now())) {
    ui.sandboxStatus.textContent = "Sandbox expired · restarts when a tool needs it";
  } else ui.sandboxStatus.textContent = "Sandbox active · dedicated to this conversation";
}

function renderComposer() {
  const busy = state.pending.has(state.selected);
  ui.input.disabled = !state.selected || !state.details.has(state.selected) || state.loading || busy || state.deleting.has(state.selected);
  ui.input.placeholder = !state.selected ? "Start a conversation to send a message" : busy ? "Waiting for your assistant…" : "Message your assistant…";
  ui.send.disabled = ui.input.disabled || !ui.input.value.trim();
}

function resizeInput() {
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, 170)}px`;
}

function renderConversation(scrollToBottom = true) {
  const detail = state.details.get(state.selected);
  const pending = state.pending.get(state.selected);
  const summary = state.threads.find((thread) => thread.id === state.selected);
  ui.title.textContent = detail?.title || summary?.title || "Your workspace";
  const messages = detail?.messages || [];
  ui.welcome.hidden = messages.length > 0 || !!pending;
  ui.welcomeTitle.textContent = state.loading && !detail ? "Loading conversation…" : "What’s on your mind?";
  ui.welcomeDescription.textContent = state.selected
    ? "Ask a question, explore an idea, or give your assistant something to build."
    : "Chat with your Ollama model. Run code in a sandbox that stays with your conversation.";
  ui.welcomeNew.hidden = !!state.selected;
  ui.messages.replaceChildren(...messages.map((message) => messageElement(message.role, message.content)));
  if (pending) {
    ui.messages.append(messageElement("user", pending.message));
    ui.messages.append(messageElement("assistant", "Thinking…", true));
  }
  const error = state.errors.get(state.selected);
  ui.threadError.hidden = !error;
  ui.threadError.textContent = error || "";
  ui.input.value = state.drafts.get(state.selected) || "";
  renderSandboxStatus();
  renderComposer();
  resizeInput();
  if (scrollToBottom) ui.scroll.scrollTop = ui.scroll.scrollHeight;
}

function setSidebar(open) {
  const mobile = window.matchMedia("(max-width: 580px)").matches;
  document.body.classList.toggle("sidebar-open", open);
  byId("sidebar-backdrop").hidden = !open;
  byId("toggle-sidebar").setAttribute("aria-expanded", String(open));
  byId("toggle-sidebar").setAttribute("aria-label", open ? "Close conversations" : "Open conversations");
  byId("sidebar").inert = mobile && !open;
  if (mobile && !open) byId("sidebar").setAttribute("aria-hidden", "true");
  else byId("sidebar").removeAttribute("aria-hidden");
  if (open && mobile) ui.newThread.focus();
}

async function selectThread(id) {
  const version = ++state.selectionVersion;
  state.selected = id;
  rememberSelection(id);
  setSidebar(false);
  state.loading = !!id && !state.pending.has(id);
  renderThreads();
  renderConversation();
  if (!id || state.pending.has(id)) return;
  try {
    const detail = await api(`/api/threads/${encodeURIComponent(id)}`);
    if (version !== state.selectionVersion || !state.threads.some((thread) => thread.id === id)) return;
    state.details.set(id, detail);
    updateSummary(detail);
    state.loading = false;
    renderThreads();
    renderConversation();
  } catch (error) {
    if (version !== state.selectionVersion) return;
    state.loading = false;
    if (error.status === 404) {
      state.threads = state.threads.filter((thread) => thread.id !== id);
      state.details.delete(id);
      await selectThread(state.threads[0]?.id || null);
      showError(new Error("This conversation is no longer available."));
    } else {
      state.errors.set(id, error.message);
      renderConversation();
    }
  }
}

async function createThread() {
  if (!state.ready || state.creating) return;
  state.creating = true;
  renderThreads();
  try {
    const detail = await api("/api/threads", { method: "POST" });
    ++state.selectionVersion;
    state.loading = false;
    state.details.set(detail.id, detail);
    updateSummary(detail);
    state.selected = detail.id;
    rememberSelection(detail.id);
    setSidebar(false);
    renderThreads();
    renderConversation();
    ui.input.focus();
    ui.announcer.textContent = "New conversation started.";
  } catch (error) {
    showError(error);
  } finally {
    state.creating = false;
    renderThreads();
  }
}

async function deleteThread(id) {
  if (state.deleting.has(id)) return;
  const thread = state.threads.find((entry) => entry.id === id);
  if (!thread || !window.confirm(`Delete “${thread.title || "New conversation"}” and its sandbox? This cannot be undone.`)) return;
  state.deleting.add(id);
  renderThreads();
  renderComposer();
  renderSandboxStatus();
  try {
    await api(`/api/threads/${encodeURIComponent(id)}`, { method: "DELETE" });
    state.pending.get(id)?.controller.abort();
    state.pending.delete(id);
    state.threads = state.threads.filter((entry) => entry.id !== id);
    state.details.delete(id);
    state.drafts.delete(id);
    state.errors.delete(id);
    if (state.selected === id) await selectThread(state.threads[0]?.id || null);
    else renderThreads();
    ui.announcer.textContent = "Conversation deleted.";
  } catch (error) {
    showError(error);
  } finally {
    state.deleting.delete(id);
    renderThreads();
    renderComposer();
    renderSandboxStatus();
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const id = state.selected;
  const message = ui.input.value.trim();
  if (!id || !message || state.pending.has(id) || state.loading || state.deleting.has(id)) return;
  const request = { message, controller: new AbortController() };
  state.pending.set(id, request);
  state.drafts.delete(id);
  state.errors.delete(id);
  renderConversation();
  ui.announcer.textContent = "Message sent. Assistant is working.";
  try {
    const detail = await api(`/api/threads/${encodeURIComponent(id)}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }), signal: request.controller.signal,
    });
    if (state.pending.get(id) !== request || !state.threads.some((thread) => thread.id === id)) return;
    state.details.set(id, detail);
    updateSummary(detail);
    ui.announcer.textContent = state.selected === id ? "Assistant response received." : `Response received in ${detail.title}.`;
  } catch (error) {
    if (error.name === "AbortError" || state.pending.get(id) !== request) return;
    state.drafts.set(id, message);
    state.errors.set(id, `${error.message} Your message is ready to retry.`);
    const title = state.threads.find((thread) => thread.id === id)?.title || "conversation";
    ui.announcer.textContent = state.selected === id
      ? "Message failed. Your message is ready to retry."
      : `Message failed in ${title}. Your message is ready to retry.`;
  } finally {
    if (state.pending.get(id) === request) {
      state.pending.delete(id);
      renderThreads();
      if (state.selected === id) {
        ++state.selectionVersion;
        state.loading = false;
        renderConversation();
        ui.input.focus();
      }
    }
  }
}

async function initialize() {
  const [configResult, threadsResult] = await Promise.allSettled([api("/api/config"), api("/api/threads")]);
  if (configResult.status === "fulfilled") {
    const config = configResult.value;
    state.model = config.model;
    byId("model-name").textContent = config.model;
    byId("model-name").title = `Ollama · ${config.model}`;
    const minutes = config.sandbox_idle_timeout_seconds / 60;
    byId("sandbox-hint").textContent = `Sandbox expires after ${minutes} ${minutes === 1 ? "minute" : "minutes"} of inactivity.`;
  } else showError(configResult.reason);
  if (threadsResult.status === "fulfilled") state.threads = threadsResult.value;
  else showError(threadsResult.reason);
  state.ready = true;
  const preferred = previousSelection();
  const selection = state.threads.find((thread) => thread.id === preferred)?.id || state.threads[0]?.id || null;
  await selectThread(selection);
}

ui.newThread.addEventListener("click", createThread);
ui.welcomeNew.addEventListener("click", createThread);
byId("message-form").addEventListener("submit", sendMessage);
ui.input.addEventListener("input", () => {
  if (state.selected) state.drafts.set(state.selected, ui.input.value);
  resizeInput();
  renderComposer();
});
ui.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    byId("message-form").requestSubmit();
  }
});
byId("toggle-sidebar").addEventListener("click", () => setSidebar(!document.body.classList.contains("sidebar-open")));
byId("sidebar-backdrop").addEventListener("click", () => {
  setSidebar(false);
  byId("toggle-sidebar").focus();
});
byId("dismiss-error").addEventListener("click", () => { byId("global-error").hidden = true; });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
    setSidebar(false);
    byId("toggle-sidebar").focus();
  }
});
window.matchMedia("(min-width: 581px)").addEventListener("change", () => setSidebar(false));
setSidebar(false);
setInterval(renderSandboxStatus, 15000);
initialize();
