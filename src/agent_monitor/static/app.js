// agent-monitor dashboard. Vanilla JS, no build step.
//
// Data flow:
//   1. Open an EventSource on /stream. Every time it (re)connects, reload
//      /agents, so nothing missed while disconnected is lost.
//   2. Each "agent_event" message carries the new event and the session's
//      updated summary: update that session's stage and, if its timeline is
//      open, add the event.
//   3. Reload /agents every 30 s anyway: "stale" is computed by the server
//      when asked, and a silent session sends no events to trigger it.
//
// Rendering: one "stage" per session, with the main agent on the left and
// its subagents on the right. Stages and nodes are created once and then
// updated in place (keyed by session_id / agent_id). Rebuilding them on
// every event would restart their CSS animations.
//
// What an agent is doing (its "activity") is decided by the server; this
// file only maps activity names to labels.
//
// SECURITY: payloads contain prompts, commands and code we don't control.
// All text goes into the page through textContent, never parsed as HTML,
// so a prompt containing <script> is shown, not executed.
// (tests/test_dashboard.py fails if any HTML-parsing API appears here.)

"use strict";

const REFRESH_INTERVAL_MS = 30_000;
const CLOCK_INTERVAL_MS = 10_000;
const RECONNECT_DELAY_MS = 5_000;
const DETAIL_MAX_LENGTH = 160;
const MAX_DONE_VISIBLE = 4; // older finished subagents collapse into "+N done"

const ACTIVITY_LABELS = {
  idle: "Idle",
  thinking: "Thinking",
  researching: "Researching",
  coding: "Coding",
  running: "Running",
  delegating: "Delegating",
  working: "Working",
  waiting_input: "Waiting for you",
  stale: "Stale",
  ended: "Ended",
};

const state = {
  agents: new Map(), // session_id -> AgentSummary
  stages: new Map(), // session_id -> stage DOM refs (see createStage)
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

function activityLabel(activity) {
  return ACTIVITY_LABELS[activity] || activity;
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

/**
 * Stage order: live sessions first, newest session first. It only changes
 * when a session starts or ends, so stages don't jump around (and restart
 * their animations) every time a different session sends an event.
 */
function byStageOrder(a, b) {
  const ended = Number(a.activity === "ended") - Number(b.activity === "ended");
  return ended || new Date(b.started_at) - new Date(a.started_at);
}

// ---- Stages (one per session) ---------------------------------------------

function createStage(sessionId) {
  const item = el("li", "stage");

  const head = el("header", "stage-head");
  const project = el("span", "project");
  const meta = el("span", "stage-meta");
  const time = el("span", "time");
  meta.append(el("span", "session-id", shortId(sessionId)), time);
  const details = el("button", "details-button", "Details");
  details.type = "button";
  details.dataset.sessionId = sessionId;
  details.setAttribute("aria-controls", "timeline");
  head.append(project, meta, details);

  const flow = el("div", "flow");
  const main = el("div", "node node-main");
  const mainActivity = el("span", "node-activity");
  main.append(el("span", "visually-hidden", "Main agent: "), mainActivity);
  const subList = el("ul", "subagent-nodes");
  subList.setAttribute("aria-label", "Subagents");
  const doneChip = el("li", "done-chip");
  doneChip.hidden = true;
  subList.append(doneChip);
  flow.append(main, subList);

  item.append(head, flow);
  return {
    item,
    project,
    time,
    details,
    flow,
    main,
    mainActivity,
    subList,
    doneChip,
    subs: new Map(), // agent_id -> subagent node refs (see createSubNode)
  };
}

function createSubNode(stage) {
  const node = el("li", "node node-sub");
  const type = el("span", "node-type");
  const activity = el("span", "node-activity");
  node.append(type, activity);
  stage.subList.insertBefore(node, stage.doneChip);
  return { node, type, activity, status: null };
}

function updateSubNode(entry, sub) {
  entry.type.textContent = sub.agent_type || shortId(sub.agent_id);
  // A finished subagent has handed its result back: say "Done", not "Ended".
  entry.activity.textContent = sub.status === "done" ? "Done" : activityLabel(sub.activity);
  entry.node.dataset.activity = sub.activity;
  entry.node.dataset.state = sub.status;
  entry.status = sub.status;
}

/** Keep the newest finished subagents visible; fold older ones into a chip. */
function collapseDone(stage, subagents) {
  const done = subagents
    .filter((sub) => sub.status === "done")
    .sort((a, b) => new Date(a.last_event_at) - new Date(b.last_event_at));
  const extra = Math.max(0, done.length - MAX_DONE_VISIBLE);
  const hidden = new Set(done.slice(0, extra).map((sub) => sub.agent_id));
  for (const [agentId, entry] of stage.subs) {
    entry.node.hidden = hidden.has(agentId);
  }
  stage.doneChip.hidden = hidden.size === 0;
  stage.doneChip.textContent = `+${hidden.size} done`;
}

function updateStage(stage, agent) {
  const selected = agent.session_id === state.selected;
  stage.item.title = agent.cwd || "";
  stage.item.classList.toggle("is-ended", agent.activity === "ended");
  stage.item.classList.toggle("is-selected", selected);
  stage.project.textContent = projectName(agent.cwd);
  stage.time.dataset.time = agent.last_event_at;
  stage.time.textContent = relativeTime(agent.last_event_at);
  stage.details.setAttribute("aria-expanded", String(selected));

  stage.main.dataset.activity = agent.activity;
  stage.mainActivity.textContent = activityLabel(agent.activity);

  for (const sub of agent.subagents) {
    let entry = stage.subs.get(sub.agent_id);
    if (!entry) {
      entry = createSubNode(stage);
      stage.subs.set(sub.agent_id, entry);
    }
    updateSubNode(entry, sub);
  }
  collapseDone(stage, agent.subagents);
  stage.flow.classList.toggle("has-subagents", agent.subagents.length > 0);
}

function renderAgents() {
  const agents = [...state.agents.values()].sort(byStageOrder);
  const items = agents.map((agent) => {
    let stage = state.stages.get(agent.session_id);
    if (!stage) {
      stage = createStage(agent.session_id);
      state.stages.set(agent.session_id, stage);
    }
    return stage.item;
  });

  // Move nodes only when the order really changed: moving a node restarts
  // its CSS animations.
  const current = dom.agentList.children;
  if (items.length !== current.length || items.some((item, i) => item !== current[i])) {
    dom.agentList.replaceChildren(...items);
  }
  dom.empty.hidden = agents.length > 0;

  for (const agent of agents) {
    updateStage(state.stages.get(agent.session_id), agent);
  }
}

// ---- Timeline (behind each stage's "Details" button) ----------------------

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
  const opener = state.stages.get(state.selected)?.details;
  state.selected = null;
  state.events = [];
  dom.timeline.hidden = true;
  renderAgents();
  opener?.focus(); // give keyboard focus back to the button that opened it
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
  const button = e.target.closest(".details-button");
  if (!button) return;
  if (button.dataset.sessionId === state.selected) closeTimeline();
  else selectSession(button.dataset.sessionId);
});

dom.closeTimeline.addEventListener("click", closeTimeline);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.selected) closeTimeline();
});

connectStream();
setInterval(refreshAgents, REFRESH_INTERVAL_MS);
setInterval(updateRelativeTimes, CLOCK_INTERVAL_MS);
