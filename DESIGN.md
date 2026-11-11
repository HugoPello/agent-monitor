---
name: agent-monitor
description: A live circuit of Claude Code agents, where work visibly flows to subagents and back.
colors:
  bg: "#070b11"
  panel: "#0c121b"
  node: "#0f1824"
  line: "#1a2533"
  text: "#dbe7f3"
  muted: "#8293a8"
  busy: "#22d3ee"
  wait: "#fbbf24"
  fail: "#fb4f6b"
  done: "#34d399"
  rest: "#64748b"
  bg-light: "#f3f6f9"
  panel-light: "#ffffff"
  line-light: "#d9e2ec"
  text-light: "#0f1b2a"
  muted-light: "#556578"
  busy-light: "#0e7490"
  wait-light: "#a16207"
  fail-light: "#be123c"
  done-light: "#047857"
  scrim: "rgb(0 0 0 / 0.5)"
typography:
  brand:
    fontFamily: "Segoe UI Variable Text, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 650
  ui:
    fontFamily: "Segoe UI Variable Text, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  name:
    fontFamily: "Segoe UI Variable Text, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
  activity:
    fontFamily: "Segoe UI Variable Text, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 600
  note:
    fontFamily: "Segoe UI Variable Text, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 500
  label:
    fontFamily: "ui-monospace, Cascadia Code, JetBrains Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 500
    letterSpacing: "0.16em"
  data:
    fontFamily: "ui-monospace, Cascadia Code, JetBrains Mono, SF Mono, Menlo, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  panel: "12px"
  node: "10px"
  control: "7px"
  tag: "4px"
  chip: "999px"
spacing:
  page: "24px"
  panel: "14px 18px 18px"
  node: "7px 14px"
  stack: "14px"
components:
  node:
    background: "{colors.node}"
    border: "1px solid {colors.busy} at 70%"
    radius: "{rounded.node}"
  chip:
    border: "1px solid the chip's colour at 40%"
    radius: "{rounded.chip}"
    font: "{typography.data}, uppercase"
---

# Design System: agent-monitor

## Overview
A live circuit on a dark instrument ground. Each session is a small diagram: the main agent on the left, its subagents on the right, and flows between them along which work visibly travels. The world contributes four things only: a near-black ground with a faint dot reticle, one colour per job, monospace for anything measured, and the signature move (streams and packets of light on the flows). Everything else is a standard, quiet product screen. Dark is the default because the dashboard lives on a second monitor beside an editor; a light variant keeps every colour role.

## Colors
### Primary
- **Busy cyan** (`busy`): every working activity (thinking, researching, coding, running, delegating, working). One hue on purpose: the word on the node carries the nuance.

### Secondary
- **Needs-you amber** (`wait`): only "Waiting for you", its note, its chip and its stage frame.
- **Failure red** (`fail`): failed steps, stale agents, the failure note and badge.
- **Delivered green** (`done`): finished turns, finished subagents, the packet that returns a result, and the live status.

### Neutral
`scrim` darkens the page behind the modal sheet and shades the drawer's edge. `bg`, `panel`, `node` and `line` build the ground, panels, nodes and hairlines; `text` and `muted` carry all prose. `rest` marks idle and ended sessions.

### Named Rules
- **One job per colour.** Amber means "a person is needed" and nothing else; a decorative amber is a false alarm.
- **Colour never speaks alone.** Every coloured state also has its word (node label, note, chip text).
- **Text never fades.** Finished work recedes through its frame and flow; its text keeps 4.5:1.

## Typography
### Hierarchy
- Brand: UI sans 17/650.
- Session names: UI sans 15/600, as `h3` headings.
- Notes (why it needs you, what failed): UI sans 13/500.
- Activities on nodes: UI sans 14/600 in the activity colour.
- Labels (section titles, node kinds such as MAIN or CODER): monospace 11/500, uppercase, tracked 0.14–0.16em, muted.
- Data (ids, times, counts, chips): monospace 12 with tabular numerals.

### Named Rules
- **Monospace is for measurement**, never for prose: ids, times, counts, kinds.
- No web fonts: a local tool should not call an external server on every load.

## Layout
A sticky top bar (brand, attention chips, live status), then two sections: **Live** (full-width stages, most urgent first) and **At rest** (compact rows, one per session with nothing running). Content is capped at 1200px. In a stage, the gap between the main node and the subagent column is where the flows run; it shrinks on narrow screens. The timeline is a side drawer from 900px (it pushes the page) and a modal full-screen sheet below.

## Elevation & Depth
Depth comes from light, not shadow stacks: busy nodes glow softly in cyan, the waiting stage carries an amber halo and the only pulse, and panels sit on a single soft drop shadow. No layered cards; the reticle ground stays visible between panels.

## Shapes
Panels 12px, nodes 10px, controls 7px, small tags (failure badge, agent tag, key caps) 4px, chips fully round. Flows are cubic curves that leave and arrive level. Strokes are hairline (1px borders, 1.6–2px flows).

## Components
### Buttons
"Details" is a quiet outlined monospace button; hover and expanded states turn it cyan. The failure note is a text button (red, underlined) that opens the timeline at the failed row.

### Chips
The top bar's attention summary: round, outlined in their colour family, uppercase monospace, hidden at zero. Each is a button that jumps to the next matching session.

### Cards / Containers
Live stages are panels with a header (name, id, time, Details), an optional note line, and the flow. Rest rows are single lines with a coloured dot.

### Navigation
No navigation beyond the chips and keyboard shortcuts: w next waiting, f next failure, j/k move, Esc closes details.

### Flow (signature component)
Each subagent has a flow from the main node: a faint track, dashes streaming towards the subagent while it runs, and two packets of light travelling along the same curve (CSS offset-path). A new flow draws itself; a finished one returns one green packet to the main node, which acknowledges it with a brief ring. Stale flows are red and dashed, with no stream. All of it stops under `prefers-reduced-motion`, leaving a dashed static flow.

## Do's and Don'ts
### Do:
- Keep "Waiting for you" the only element that pulses.
- Show only what hook events prove: who delegated to whom, and when it finished.
- Put a failure on the node that failed, in words.

### Don't:
- Don't add a second accent or a decorative glow; light means state.
- Don't show tool names on nodes; they live in the timeline.
- Don't animate on load or refresh: only changes seen live animate.
- Don't use colour as the only signal, or fade text to show that something is finished.
