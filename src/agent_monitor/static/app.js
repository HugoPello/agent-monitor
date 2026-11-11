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
// Rendering: one element per session. A live session is a "stage" (the main
// agent on the left, its subagents on the right, flows between them); a
// session at rest (finished, idle or ended, with nothing running) folds into
// a compact row under "At rest". Elements and nodes are created once and
// updated in place (keyed by session_id / agent_id); rebuilding them on
// every event would restart their CSS animations.
//
// Connectors are an SVG drawn behind the nodes. The nodes are laid out by CSS
// grid, so the line ends are measured (getBoundingClientRect) and redrawn
// whenever a stage changes or resizes.
//
// Animation: while a subagent runs, dashes stream along its flow and light
// "packets" travel from the main agent to it (CSS offset-path along the
// same curve). One-off moments only play for changes seen live (from the
// stream): a page load, the 30 s refresh or a reconnect paints the current
// state directly, so nothing "replays". A subagent starting draws its flow;
// when it stops a packet travels back to the main agent and the node fades.
// That only says who delegated to whom and when it finished: hooks don't
// tell us what a subagent returned.
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
const SVG_NS = "http://www.w3.org/2000/svg";
const FADE_AFTER_MS = 6_000; // a finished subagent dims this long after it is seen finishing
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
// Packets ride the flow with offset-path; without it they are simply left out.
const PACKETS = CSS.supports("offset-path", "path('M 0 0 L 1 1')");
const PACKETS_PER_FLOW = 2;

const ACTIVITY_LABELS = {
  idle: "Idle",
  finished: "Finished",
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

// Stage order, most urgent first. Sessions only move when their urgency
// changes (moving a node restarts its animations), not on every event.
const URGENCY = {
  waiting_input: 0,
  stale: 1,
  thinking: 2,
  researching: 2,
  coding: 2,
  running: 2,
  delegating: 2,
  working: 2,
  finished: 3,
  idle: 4,
  ended: 5,
};
const BASE_TITLE = document.title;
// Top bar summary: what each chip counts, and its keyboard shortcut.
const SUMMARY = [
  { kind: "waiting", label: "waiting", key: "w", matches: (a) => a.activity === "waiting_input" },
  { kind: "failed", label: "failed", key: "f", matches: (a) => a.failed_steps > 0 },
  { kind: "stale", label: "stale", key: null, matches: (a) => a.activity === "stale" },
  { kind: "working", label: "working", key: null, matches: (a) => URGENCY[a.activity] === 2 },
  { kind: "finished", label: "finished", key: null, matches: (a) => a.activity === "finished" },
];
const TARGET_HIGHLIGHT_MS = 1_500;

// Activities of a session with nothing left to watch.
const AT_REST = new Set(["finished", "idle", "ended"]);
// Matches the CSS breakpoint: above it the timeline is a side drawer that
// leaves the overview usable; below it, a modal full-screen sheet.
const wideScreen = window.matchMedia("(min-width: 900px)");

const state = {
  agents: new Map(), // session_id -> AgentSummary
  stages: new Map(), // session_id -> stage DOM refs (see createStage)
  selected: null, // session_id whose timeline is open
  events: [], // timeline of the selected session, oldest first
  reopening: false, // the dialog is being reopened in another form
  jumps: {}, // summary kind -> index of the last session jumped to
};

const dom = {
  connection: document.getElementById("connection"),
  empty: document.getElementById("empty"),
  agentList: document.getElementById("agent-list"),
  liveEmpty: document.getElementById("live-empty"),
  restSection: document.getElementById("rest"),
  restList: document.getElementById("rest-list"),
  summary: document.getElementById("summary"),
  timeline: document.getElementById("timeline"),
  timelineTitle: document.getElementById("timeline-title"),
  timelineSubject: document.getElementById("timeline-subject"),
  eventList: document.getElementById("event-list"),
  closeTimeline: document.getElementById("close-timeline"),
  announcer: document.getElementById("announcer"),
};

// ---- Pure helpers ---------------------------------------------------------

/** Create an element. Text is always set with textContent (XSS-safe). */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** Create an SVG element (SVG needs its namespace; still no HTML parsing). */
function svgEl(tag, className) {
  const node = document.createElementNS(SVG_NS, tag);
  if (className) node.setAttribute("class", className);
  return node;
}

/**
 * A horizontal S-curve from point a to point b, as SVG path data. The
 * control points sit halfway across, so the line leaves and arrives level.
 * connectorPath(b, a) is the same curve walked the other way.
 */
function connectorPath(a, b) {
  const midX = (a.x + b.x) / 2;
  return `M ${a.x} ${a.y} C ${midX} ${a.y}, ${midX} ${b.y}, ${b.x} ${b.y}`;
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

// What each hook event means, in words. Unknown events show their raw name.
const EVENT_LABELS = {
  SessionStart: "Session started",
  UserPromptSubmit: "You asked",
  PreToolUse: "Tool started",
  PostToolUse: "Tool finished",
  PostToolUseFailure: "Tool failed",
  Stop: "Finished its turn",
  SubagentStart: "Subagent started",
  SubagentStop: "Subagent finished",
  SessionEnd: "Session ended",
};

const NOTIFICATION_LABELS = {
  permission_prompt: "Needs your permission",
  idle_prompt: "Waiting for your input",
};

/** A plain-language label for an event. */
function eventLabel(event) {
  if (event.hook_event_name === "Notification") {
    return NOTIFICATION_LABELS[event.payload?.notification_type] || "Notification";
  }
  return EVENT_LABELS[event.hook_event_name] || event.hook_event_name;
}

/** Visual category of an event, used for the timeline's left border. */
function eventKind(name) {
  if (name === "UserPromptSubmit") return "prompt";
  if (name === "PostToolUseFailure") return "error";
  if (name === "Notification") return "attention";
  if (name.endsWith("ToolUse")) return "tool";
  return "lifecycle";
}

/** Most urgent first (see URGENCY), then newest session first. */
function byStageOrder(a, b) {
  const urgency = (URGENCY[a.activity] ?? 4) - (URGENCY[b.activity] ?? 4);
  return urgency || new Date(b.started_at) - new Date(a.started_at);
}

/** A session moves to "At rest" once nothing in it is running. */
function isAtRest(agent) {
  return AT_REST.has(agent.activity) && !agent.subagents.some((s) => s.status === "running");
}

/** "Finished · Explore done": the state line of a row at rest. */
function restState(agent) {
  const parts = [activityLabel(agent.activity)];
  const done = agent.subagents.filter((s) => s.status === "done").map((s) => s.agent_type);
  if (done.length) parts.push(`${done.filter(Boolean).join(", ") || "subagents"} done`);
  return parts.join(" · ");
}

/** "agent-monitor", or "(2) waiting · agent-monitor" when sessions need you. */
function pageTitle(agents) {
  const waiting = agents.filter((agent) => agent.activity === "waiting_input").length;
  return waiting ? `(${waiting}) waiting · ${BASE_TITLE}` : BASE_TITLE;
}

/** Say something to screen readers without moving focus. */
function announce(message) {
  dom.announcer.textContent = message;
}

// ---- Stages (one per session) ---------------------------------------------

function createStage(sessionId) {
  const item = el("li", "stage");
  item.dataset.sessionId = sessionId;

  const head = el("header", "stage-head");
  const project = el("h3", "project"); // a heading, so sessions are reachable by heading
  const meta = el("span", "stage-meta");
  const rest = el("span", "rest-state");
  const time = el("span", "time");
  meta.append(rest, el("span", "session-id", shortId(sessionId)), time);
  const details = el("button", "details-button", "Details");
  details.type = "button";
  details.dataset.sessionId = sessionId;
  details.setAttribute("aria-controls", "timeline");
  head.append(project, meta, details);

  // Why the session needs you, or that something went wrong. Shown only
  // when there is something to act on; the nodes stay free of tool names.
  const notes = el("p", "stage-notes");
  const waitingNote = el("span", "note note-waiting");
  const failureNote = el("span", "note note-failure");
  notes.append(waitingNote, failureNote);

  const flow = el("div", "flow");
  const links = svgEl("svg", "links");
  links.setAttribute("aria-hidden", "true"); // decorative: the nodes carry the text
  const main = el("div", "node node-main");
  const mainActivity = el("span", "node-activity");
  main.append(el("span", "node-kind", "Main"), mainActivity);
  const subList = el("ul", "subagent-nodes");
  subList.setAttribute("aria-label", "Subagents");
  const doneChip = el("li", "done-chip");
  doneChip.hidden = true;
  subList.append(doneChip);
  flow.append(links, main, subList);

  item.append(head, notes, flow);
  const stage = {
    item,
    project,
    rest,
    time,
    details,
    flow,
    links,
    main,
    mainActivity,
    subList,
    doneChip,
    subs: new Map(), // agent_id -> subagent node refs (see createSubNode)
    notes,
    waitingNote,
    failureNote,
    activity: null, // last activity shown, to announce changes
    failedSteps: 0, // last failure count shown, to announce new ones
  };
  stageByFlow.set(flow, stage);
  resizeObserver.observe(flow);
  return stage;
}

function createSubNode(stage, animate) {
  const node = el("li", "node node-sub");
  const type = el("span", "node-kind");
  const activity = el("span", "node-activity");
  node.append(type, activity);
  stage.subList.insertBefore(node, stage.doneChip);

  // One flow per subagent: a faint track, the streaming dashes on top,
  // packets travelling to the subagent, and one packet for the way back.
  const group = svgEl("g", "link");
  const track = svgEl("path", "link-track");
  const flow = svgEl("path", "link-flow");
  group.append(track, flow);
  const packets = [];
  let backPacket = null;
  if (PACKETS) {
    for (let i = 0; i < PACKETS_PER_FLOW; i++) {
      const packet = svgEl("circle", "packet");
      packet.setAttribute("r", "3.5");
      // Spread the packets evenly along the flow.
      packet.style.animationDelay = `${(-i * 1.8) / PACKETS_PER_FLOW}s`;
      packets.push(packet);
    }
    backPacket = svgEl("circle", "packet-back");
    backPacket.setAttribute("r", "4");
    group.append(...packets, backPacket);
  }
  stage.links.append(group);

  return {
    node,
    type,
    activity,
    group,
    track,
    flow,
    packets,
    backPacket,
    status: null,
    drawIn: animate && !reducedMotion.matches, // draw the connector once it has a path
    fadeTimer: null,
  };
}

/** Run a one-shot CSS animation by adding a class, and clean up after it. */
function playOnce(element, className, then) {
  element.classList.remove(className);
  element.addEventListener(
    "animationend",
    () => {
      element.classList.remove(className);
      then?.();
    },
    { once: true },
  );
  element.classList.add(className);
}

/** Result delivered: a packet travels back along the flow to the main agent. */
function handBack(stage, entry) {
  if (reducedMotion.matches) return; // the colour change says it already
  const arrive = () => playOnce(stage.main, "is-receiving");
  if (entry.backPacket) playOnce(entry.backPacket, "is-travelling", arrive);
  else arrive();
}

function fadeLater(entry, delay) {
  clearTimeout(entry.fadeTimer);
  const fade = () => {
    entry.node.classList.add("is-faded");
    entry.group.classList.add("is-faded");
  };
  if (delay > 0) entry.fadeTimer = setTimeout(fade, delay);
  else fade();
}

function updateSubNode(stage, entry, sub, animate) {
  if (sub.status === "done" && entry.status !== "done") {
    const seenRunning = entry.status === "running";
    if (animate && seenRunning) handBack(stage, entry);
    fadeLater(entry, animate && seenRunning ? FADE_AFTER_MS : 0);
  }

  entry.type.textContent = sub.agent_type || shortId(sub.agent_id);
  // A finished subagent has handed its result back: say "Done", not "Ended".
  entry.activity.textContent = sub.status === "done" ? "Done" : activityLabel(sub.activity);
  for (const target of [entry.node, entry.group]) {
    target.dataset.activity = sub.activity;
    target.dataset.state = sub.status;
  }
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
    entry.group.classList.toggle("is-collapsed", hidden.has(agentId));
  }
  stage.doneChip.hidden = hidden.size === 0;
  stage.doneChip.textContent = `+${hidden.size} done`;
}

// Changes worth announcing when seen live: the moments the user is needed.
const ANNOUNCEMENTS = {
  waiting_input: (project) => `${project} is waiting for you`,
  finished: (project) => `${project} finished its turn`,
};

function updateNotes(stage, agent) {
  const reason =
    agent.activity === "waiting_input"
      ? oneLine(agent.waiting_reason || "Needs your permission", 120)
      : "";
  stage.waitingNote.textContent = reason;
  stage.waitingNote.hidden = !reason;

  const failed = agent.failed_steps;
  stage.failureNote.textContent = failed
    ? `${failed} ${failed === 1 ? "step" : "steps"} failed · ${clockTime(agent.last_failure_at)}`
    : "";
  stage.failureNote.hidden = !failed;
  stage.notes.hidden = !reason && !failed;
}

function updateStage(stage, agent, animate) {
  const selected = agent.session_id === state.selected;
  const project = projectName(agent.cwd);
  if (animate && agent.activity !== stage.activity && ANNOUNCEMENTS[agent.activity]) {
    announce(ANNOUNCEMENTS[agent.activity](project));
  }
  stage.activity = agent.activity;
  if (animate && agent.failed_steps > stage.failedSteps) {
    announce(`${project}: a step failed`);
  }
  stage.failedSteps = agent.failed_steps;
  updateNotes(stage, agent);
  stage.item.title = agent.cwd || "";
  stage.item.dataset.activity = agent.activity;
  stage.rest.textContent = restState(agent);
  stage.item.classList.toggle("is-ended", agent.activity === "ended");
  stage.item.classList.toggle("is-waiting", agent.activity === "waiting_input");
  stage.item.classList.toggle("is-selected", selected);
  stage.project.textContent = project;
  stage.time.dataset.time = agent.last_event_at;
  stage.time.textContent = relativeTime(agent.last_event_at);
  stage.details.setAttribute("aria-expanded", String(selected));
  stage.details.setAttribute("aria-label", `Details for ${project}`);

  stage.main.dataset.activity = agent.activity;
  stage.mainActivity.textContent = activityLabel(agent.activity);

  for (const sub of agent.subagents) {
    let entry = stage.subs.get(sub.agent_id);
    if (!entry) {
      entry = createSubNode(stage, animate);
      stage.subs.set(sub.agent_id, entry);
    }
    updateSubNode(stage, entry, sub, animate);
  }
  collapseDone(stage, agent.subagents);
  stage.flow.classList.toggle("has-subagents", agent.subagents.length > 0);
}

/** Point the connectors at where the nodes are now. */
function drawLinks(stage) {
  const box = stage.flow.getBoundingClientRect();
  if (box.width === 0) return; // not laid out yet; the ResizeObserver will call back
  stage.links.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);

  const main = stage.main.getBoundingClientRect();
  const from = { x: main.right - box.left, y: main.top + main.height / 2 - box.top };
  for (const entry of stage.subs.values()) {
    if (entry.node.hidden) continue;
    const node = entry.node.getBoundingClientRect();
    const to = { x: node.left - box.left, y: node.top + node.height / 2 - box.top };
    const path = connectorPath(from, to);
    entry.track.setAttribute("d", path);
    entry.flow.setAttribute("d", path);
    // Packets follow the same curve (CSS offset-path takes the path data).
    for (const packet of entry.packets) packet.style.offsetPath = `path("${path}")`;
    if (entry.backPacket) {
      entry.backPacket.style.offsetPath = `path("${connectorPath(to, from)}")`;
    }
    entry.group.style.setProperty("--len", `${entry.track.getTotalLength()}px`);
    if (entry.drawIn) {
      entry.drawIn = false; // only now: the draw-in animation needs the path length
      playOnce(entry.group, "is-new");
    }
  }
}

// Redraw a stage's connectors when its size changes (window resize, the
// timeline opening, text wrapping...).
const stageByFlow = new WeakMap(); // .flow element -> stage
const resizeObserver = new ResizeObserver((entries) => {
  for (const { target } of entries) {
    const stage = stageByFlow.get(target);
    if (stage) drawLinks(stage);
  }
});

/** Put `items` in `list` in this order, moving nodes only when it changed. */
function syncList(list, items) {
  // Moving a node restarts its CSS animations, so leave a list alone when
  // its order is already right.
  const current = list.children;
  if (items.length !== current.length || items.some((item, i) => item !== current[i])) {
    list.replaceChildren(...items);
  }
}

/** Sync every session with state.agents. `animate`: the change was seen live. */
function renderAgents(animate = false) {
  const agents = [...state.agents.values()].sort(byStageOrder);
  for (const agent of agents) {
    if (!state.stages.has(agent.session_id)) {
      state.stages.set(agent.session_id, createStage(agent.session_id));
    }
  }

  // Update before inserting: a new element then enters the page already in
  // its activity colours, instead of transitioning from the default grey.
  for (const agent of agents) {
    updateStage(state.stages.get(agent.session_id), agent, animate);
  }

  const live = [];
  const resting = [];
  for (const agent of agents) {
    const stage = state.stages.get(agent.session_id);
    const atRest = isAtRest(agent);
    stage.item.classList.toggle("stage", !atRest);
    stage.item.classList.toggle("rest-row", atRest);
    (atRest ? resting : live).push(stage.item);
  }
  syncList(dom.agentList, live);
  syncList(dom.restList, resting);

  dom.empty.hidden = agents.length > 0;
  dom.liveEmpty.hidden = live.length > 0 || agents.length === 0;
  dom.restSection.hidden = resting.length === 0;
  document.title = pageTitle(agents);
  renderSummary(agents);

  // Flows last: they measure the nodes, so the page must be final.
  for (const agent of agents) {
    if (!isAtRest(agent)) drawLinks(state.stages.get(agent.session_id));
  }
}

// ---- Summary and keyboard navigation ---------------------------------------

/** Create the summary chips once; renderSummary only updates them. */
function createSummary() {
  const chips = new Map();
  for (const { kind, key } of SUMMARY) {
    const chip = el("button", "chip");
    chip.type = "button";
    chip.dataset.kind = kind;
    if (key) chip.setAttribute("aria-keyshortcuts", key);
    chip.hidden = true;
    chips.set(kind, chip);
  }
  dom.summary.append(...chips.values());
  return chips;
}

const summaryChips = createSummary();

function renderSummary(agents) {
  for (const { kind, label, matches } of SUMMARY) {
    const count = agents.filter(matches).length;
    const chip = summaryChips.get(kind);
    chip.hidden = count === 0;
    chip.textContent = `${count} ${label}`;
    chip.setAttribute("aria-label", `${count} ${label}: go to the next one`);
  }
}

/** Sessions in the order they appear on the page (live, then at rest). */
function sessionsInPageOrder() {
  return [...document.querySelectorAll("#agent-list > li, #rest-list > li")]
    .map((item) => state.agents.get(item.dataset.sessionId))
    .filter(Boolean);
}

/** Bring a session into view and put keyboard focus on it. */
function focusSession(sessionId) {
  const stage = state.stages.get(sessionId);
  if (!stage) return;
  const behavior = reducedMotion.matches ? "auto" : "smooth";
  stage.item.scrollIntoView({ block: "center", behavior });
  stage.details.focus({ preventScroll: true });
  stage.item.classList.add("is-target");
  setTimeout(() => stage.item.classList.remove("is-target"), TARGET_HIGHLIGHT_MS);
}

/** Go to the next session a summary chip counts, cycling through them. */
function jumpTo(kind) {
  const { matches } = SUMMARY.find((entry) => entry.kind === kind);
  const targets = sessionsInPageOrder().filter(matches);
  if (!targets.length) return;
  const next = ((state.jumps[kind] ?? -1) + 1) % targets.length;
  state.jumps[kind] = next;
  focusSession(targets[next].session_id);
}

/** Move focus to the next (+1) or previous (-1) session on the page. */
function moveFocus(step) {
  const sessions = sessionsInPageOrder();
  if (!sessions.length) return;
  const current = document.activeElement?.closest?.("[data-session-id]")?.dataset.sessionId;
  const index = sessions.findIndex((agent) => agent.session_id === current);
  const next = index === -1 ? 0 : (index + step + sessions.length) % sessions.length;
  focusSession(sessions[next].session_id);
}

const SHORTCUTS = {
  w: () => jumpTo("waiting"),
  f: () => jumpTo("failed"),
  j: () => moveFocus(1),
  k: () => moveFocus(-1),
};

function onShortcut(e) {
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
  if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
  if (dom.timeline.matches(":modal")) return; // the sheet owns the keyboard
  const action = SHORTCUTS[e.key];
  if (!action) return;
  e.preventDefault();
  action();
}

// ---- Timeline (behind each stage's "Details" button) ----------------------

function renderEvent(event) {
  const fromSubagent = Boolean(event.agent_id);
  const item = el("li", `event kind-${eventKind(event.hook_event_name)}`);
  if (fromSubagent) item.classList.add("from-subagent");

  const title = el("div", "event-title");
  title.append(el("span", null, eventLabel(event)));
  if (event.tool_name) title.append(el("span", "tool", event.tool_name));
  if (fromSubagent) {
    title.append(el("span", "agent-tag", event.payload?.agent_type || shortId(event.agent_id)));
  }
  // The raw hook name stays, small, for debugging.
  title.append(el("span", "hook-name", event.hook_event_name));

  item.append(el("time", "event-time", clockTime(event.received_at)), title);
  const detail = eventDetail(event);
  if (detail) item.append(el("div", "event-detail", detail));
  return item;
}

function renderTimeline() {
  const agent = state.agents.get(state.selected);
  // The heading stays "Timeline"; the subject sits below it in normal case,
  // so the session id reads exactly as it does on the stage.
  dom.timelineSubject.textContent = agent
    ? `${projectName(agent.cwd)} · ${shortId(agent.session_id)}`
    : "";
  // Newest first, so live events appear at the top.
  dom.eventList.replaceChildren(...state.events.toReversed().map(renderEvent));
}

function updateRelativeTimes() {
  for (const node of document.querySelectorAll(".layout [data-time]")) {
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

/**
 * Open the timeline dialog in the form that suits the screen. showModal()
 * gives the small-screen sheet focus containment, an inert page behind it
 * and Esc for free; show() keeps the overview usable beside the drawer.
 */
function openTimelineDialog() {
  if (wideScreen.matches) dom.timeline.show();
  else dom.timeline.showModal();
  dom.timelineTitle.focus(); // land on what just opened
}

function selectSession(sessionId) {
  state.selected = sessionId;
  state.events = [];
  if (!dom.timeline.open) openTimelineDialog();
  renderAgents();
  renderTimeline();
  loadTimeline(sessionId);
}

function closeTimeline() {
  dom.timeline.close(); // cleanup runs in the "close" listener below
}

/** The dialog closed (button, Esc, or the browser): reset and refocus. */
function onTimelineClosed() {
  if (state.reopening) return;
  const opener = state.stages.get(state.selected)?.details;
  state.selected = null;
  state.events = [];
  renderAgents();
  opener?.focus(); // give keyboard focus back to the button that opened it
}

function handleStreamMessage(message) {
  const { event, agent } = JSON.parse(message.data);
  state.agents.set(agent.session_id, agent);
  renderAgents(true);

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

document.querySelector(".layout").addEventListener("click", (e) => {
  const button = e.target.closest(".details-button");
  if (!button) return;
  if (button.dataset.sessionId === state.selected) closeTimeline();
  else selectSession(button.dataset.sessionId);
});

dom.closeTimeline.addEventListener("click", closeTimeline);

dom.summary.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip) jumpTo(chip.dataset.kind);
});

document.addEventListener("keydown", onShortcut);

// A modal dialog closes on Esc by itself; the drawer (non-modal) does not.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && dom.timeline.open) closeTimeline();
});

dom.timeline.addEventListener("close", onTimelineClosed);

// Crossing the breakpoint with the timeline open: reopen it in the other form.
wideScreen.addEventListener("change", () => {
  if (!dom.timeline.open) return;
  state.reopening = true;
  dom.timeline.close();
  state.reopening = false;
  openTimelineDialog();
});

connectStream();
setInterval(refreshAgents, REFRESH_INTERVAL_MS);
setInterval(updateRelativeTimes, CLOCK_INTERVAL_MS);
