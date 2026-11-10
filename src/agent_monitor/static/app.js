// agent-monitor dashboard. Vanilla JS, no build step.
//
// Data flow:
//   1. Open an EventSource on /stream. Every time it (re)connects, reload
//      /agents, so nothing missed while disconnected is lost.
//   2. Each "agent_event" message carries the new event and the session's
//      updated summary: update that card and, if its timeline is open,
//      add the event.
//   3. Reload /agents every 30 s anyway: "stale" is computed by the server
//      when asked, and a silent session sends no events to trigger it.
//
// SECURITY: payloads contain prompts, commands and code we don't control.
// All text goes into the page through el() / textContent, never parsed as
// HTML, so a prompt containing <script> is shown, not executed.
// (tests/test_dashboard.py fails if any HTML-parsing API appears here.)

"use strict";

const REFRESH_INTERVAL_MS = 30_000;
const CLOCK_INTERVAL_MS = 10_000;
const RECONNECT_DELAY_MS = 5_000;
const DETAIL_MAX_LENGTH = 160;

const STATUS_LABELS = {
  idle: "Idle",
  working: "Working",
  running_tool: "Running tool",
  waiting_input: "Waiting for you",
  ended: "Ended",
  stale: "Stale",
  unknown: "Unknown",
  running: "running",
  done: "done",
};

const state = {
  agents: new Map(), // session_id -> AgentSummary
  selected: null, // session_id whose timeline is open
  events: [], // timeline of the selected session, oldest first
};

const dom = {
  connection: document.getElementById("connection"),
  empty: document.getElementById("empty"),
  agentList: document.getElementById("agent-list"),
  timeline: document.getElementById("timeline"),
  timelineTitle: document.getElementById("timeline-title"),
  eventList: document.getElementById("event-list"),
  closeTimeline: document.getElementById("close-timeline"),
};

// ---- Pure helpers ---------------------------------------------------------

/** Create an element. Text is always set with textContent (XSS-safe). */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** Last folder of a path, for both "/a/b" and "C:\\a\\b". */
function projectName(cwd) {
  if (!cwd) return "unknown project";
  const parts = cwd.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : cwd;
}

function shortId(id) {
  return id.slice(0, 8);
}

function relativeTime(iso, now = Date.now()) {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

function clockTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour12: false });
}

/** Collapse whitespace and cut long text to one readable line. */
function oneLine(text, max = DETAIL_MAX_LENGTH) {
  if (text === undefined || text === null) return "";
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

/** The most useful single field of a tool's input. */
function toolDetail(toolInput) {
  const input = toolInput || {};
  const keys = ["command", "file_path", "pattern", "url", "query", "description", "prompt"];
  for (const key of keys) {
    if (typeof input[key] === "string" && input[key]) return input[key];
  }
  return "";
}

/** One line describing an event, or "" if there is nothing useful to say. */
function eventDetail(event) {
  const p = event.payload || {};
  switch (event.hook_event_name) {
    case "PreToolUse":
    case "PostToolUse":
    case "PostToolUseFailure":
      return oneLine(toolDetail(p.tool_input));
    case "UserPromptSubmit":
      return oneLine(p.prompt);
    case "Notification":
      return oneLine(p.message);
    case "Stop":
    case "SubagentStop":
      return oneLine(p.last_assistant_message);
    case "SessionStart":
      return oneLine(p.source);
    case "SessionEnd":
      return oneLine(p.reason);
    default:
      return "";
  }
}

/** Visual category of an event, used for the timeline's left border. */
function eventKind(name) {
  if (name === "UserPromptSubmit") return "prompt";
  if (name === "PostToolUseFailure") return "error";
  if (name === "Notification") return "attention";
  if (name.endsWith("ToolUse")) return "tool";
  return "lifecycle";
}

function byMostRecent(a, b) {
  return new Date(b.last_event_at) - new Date(a.last_event_at);
}

// ---- Rendering ------------------------------------------------------------

function badge(status) {
  return el("span", `badge status-${status}`, STATUS_LABELS[status] || status);
}

function renderCard(agent) {
  const item = el("li");
  const card = el("button", "card");
  card.type = "button";
  card.dataset.sessionId = agent.session_id;
  card.setAttribute("aria-pressed", String(agent.session_id === state.selected));
  card.title = agent.cwd || "";

  const head = el("div", "card-head");
  head.append(el("span", "project", projectName(agent.cwd)), badge(agent.status));

  const meta = el("div", "card-meta");
  meta.append(el("span", "session-id", shortId(agent.session_id)));
  if (agent.current_tool) meta.append(el("span", "tool", agent.current_tool));
  const time = el("span", "time", relativeTime(agent.last_event_at));
  time.dataset.time = agent.last_event_at;
  meta.append(time, el("span", null, `${agent.event_count} events`));

  card.append(head, meta);

  if (agent.subagents.length) {
    const list = el("ul", "subagents");
    for (const sub of agent.subagents) {
      const entry = el("li", "subagent");
      entry.append(el("span", null, sub.agent_type || shortId(sub.agent_id)), badge(sub.status));
      list.append(entry);
    }
    card.append(list);
  }

  item.append(card);
  return item;
}

function renderAgents() {
  // Rebuilding the list is simple and cheap for a handful of sessions.
  // Remember which card had keyboard focus so updates don't steal it.
  const focused = document.activeElement?.dataset?.sessionId;
  const agents = [...state.agents.values()].sort(byMostRecent);

  dom.agentList.replaceChildren(...agents.map(renderCard));
  dom.empty.hidden = agents.length > 0;

  if (focused) {
    dom.agentList.querySelector(`[data-session-id="${CSS.escape(focused)}"]`)?.focus();
  }
}

function renderEvent(event) {
  const fromSubagent = Boolean(event.agent_id);
  const item = el("li", `event kind-${eventKind(event.hook_event_name)}`);
  if (fromSubagent) item.classList.add("from-subagent");

  const title = el("div", "event-title");
  title.append(el("span", null, event.hook_event_name));
  if (event.tool_name) title.append(el("span", "tool", event.tool_name));
  if (fromSubagent) {
    title.append(el("span", "agent-tag", event.payload?.agent_type || shortId(event.agent_id)));
  }

  item.append(el("time", "event-time", clockTime(event.received_at)), title);
  const detail = eventDetail(event);
  if (detail) item.append(el("div", "event-detail", detail));
  return item;
}

function renderTimeline() {
  const agent = state.agents.get(state.selected);
  dom.timelineTitle.textContent = agent
    ? `Timeline · ${projectName(agent.cwd)} · ${shortId(agent.session_id)}`
    : "Timeline";
  // Newest first, so live events appear at the top.
  dom.eventList.replaceChildren(...state.events.toReversed().map(renderEvent));
}

function updateRelativeTimes() {
  for (const node of dom.agentList.querySelectorAll("[data-time]")) {
    node.textContent = relativeTime(node.dataset.time);
  }
}

function setConnection(status, label) {
  dom.connection.dataset.state = status;
  dom.connection.textContent = label;
}

// ---- Data -----------------------------------------------------------------

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

async function refreshAgents() {
  try {
    const agents = await fetchJson("/agents");
    state.agents = new Map(agents.map((agent) => [agent.session_id, agent]));
    renderAgents();
  } catch (error) {
    console.warn("Could not load sessions", error); // keep showing the last state
  }
}

async function loadTimeline(sessionId) {
  try {
    const events = await fetchJson(`/agents/${encodeURIComponent(sessionId)}/events`);
    if (state.selected !== sessionId) return; // the user picked another one meanwhile
    state.events = events;
    renderTimeline();
  } catch (error) {
    console.warn("Could not load timeline", error);
  }
}

function selectSession(sessionId) {
  state.selected = sessionId;
  state.events = [];
  dom.timeline.hidden = false;
  renderAgents();
  renderTimeline();
  loadTimeline(sessionId);
}

function closeTimeline() {
  state.selected = null;
  state.events = [];
  dom.timeline.hidden = true;
  renderAgents();
}

function handleStreamMessage(message) {
  const { event, agent } = JSON.parse(message.data);
  state.agents.set(agent.session_id, agent);
  renderAgents();

  const alreadyShown = state.events.some((e) => e.id === event.id);
  if (event.session_id === state.selected && !alreadyShown) {
    state.events.push(event);
    renderTimeline();
  }
}

function connectStream() {
  const source = new EventSource("/stream");

  source.addEventListener("open", () => {
    setConnection("live", "live");
    // Catch up on anything that happened while we were disconnected.
    refreshAgents();
    if (state.selected) loadTimeline(state.selected);
  });

  source.addEventListener("agent_event", handleStreamMessage);

  source.addEventListener("error", () => {
    setConnection("reconnecting", "reconnecting…");
    // EventSource retries by itself unless the server answered with an
    // error status; in that case it gives up (CLOSED) and we retry.
    if (source.readyState === EventSource.CLOSED) {
      source.close();
      setTimeout(connectStream, RECONNECT_DELAY_MS);
    }
  });
}

// ---- Start ----------------------------------------------------------------

dom.agentList.addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (card) selectSession(card.dataset.sessionId);
});

dom.closeTimeline.addEventListener("click", closeTimeline);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.selected) closeTimeline();
});

connectStream();
setInterval(refreshAgents, REFRESH_INTERVAL_MS);
setInterval(updateRelativeTimes, CLOCK_INTERVAL_MS);
