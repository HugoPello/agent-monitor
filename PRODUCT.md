# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Two audiences, weighted equally:

- **The developer running Claude Code agents** on their own machine. They keep
  the dashboard on a second monitor beside the editor and terminal, glancing at
  it while agents work, or leave it in a background browser tab and come back
  when something needs them. Their job: know at a glance what each agent is
  doing, how work was delegated to subagents, and above all when an agent is
  waiting for them or something went wrong.
- **People evaluating the project** (recruiters, interviewers, other
  developers) who meet it through the README, the demo GIF and the code. It is
  a portfolio project: it has to be genuinely useful and read well in a demo.

## Product Purpose

A real-time monitor for Claude Code agents. Claude Code hooks send lifecycle
events to a local server, which stores them and streams each session's state to
a dashboard. Success: the developer never has to watch the terminal to know
which agent needs them, and can see how a task fanned out to subagents and when
each finished.

## Positioning

It shows agent work as a flow (who delegated to whom, what each agent is doing
now, when a result came back) derived honestly from hook events alone. It never
pretends to know what data a subagent returned, because hooks don't say.

## Operating Context

- Runs locally next to Claude Code; the server binds to 127.0.0.1.
- Viewed on a second monitor for at-a-glance checks, or as a background tab
  whose title signals when a session is waiting.
- Several sessions can run at once; a session can start several subagents in
  parallel.

## Capabilities and Constraints

- Events: session start/end, prompts, tool use (pre/post/failure), subagent
  start/stop, notifications (permission and idle prompts), stop.
- Activities derived on the server: Idle, Finished, Thinking, Researching,
  Coding, Running, Delegating, Working, Waiting for you, Stale, Ended.
- Stack (existing, binding): Python 3.12+, FastAPI, SQLite, SSE; dashboard in
  plain HTML/CSS/vanilla JS, no frameworks, no libraries, no build step.
- Payloads can contain code and secrets: all text is rendered as text, never
  parsed as HTML.
- Out of scope: auth, multi-user, cloud deployment.

## Brand Commitments

The name "agent-monitor" stays. No visual identity is binding.

## Evidence on Hand

- `scripts/demo.py` replays fictional sessions (made-up projects and prompts).
- `docs/demo.gif` is recorded by the owner; never generated or faked.
- No real users, testimonials or benchmarks exist; none may be invented.

## Product Principles

1. The person comes first: anything that needs them must win attention, even
   from a background tab.
2. Honest by construction: show only what the events prove.
3. Glanceable, then detailed: the overview answers "what's happening", the
   timeline answers "why".
4. Never get in the way: the hook must not slow or break Claude Code, and the
   dashboard must stay calm when nothing needs attention.

## Accessibility & Inclusion

Colour is never the only signal; WCAG AA contrast in light and dark themes;
keyboard and screen-reader use (announcements for states that need the person);
`prefers-reduced-motion` respected.
