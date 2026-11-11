---
version: 1
slug: "src-agent-monitor-static-index-html"
primary_target: "src/agent_monitor/static/index.html"
related_targets: ["src/agent_monitor/static/app.js","src/agent_monitor/static/styles.css"]
---

# Surface brief: dashboard

Scope: the single dashboard page (src/agent_monitor/static). Mode: Operate.
Audience and job: see PRODUCT.md (developer on a second monitor or background tab; evaluators via README/GIF). Task: know at a glance what each agent does, how work fanned out to subagents, and which session needs you.
Constraints: vanilla HTML/CSS/JS, no libraries or build; SVG via createElementNS only; text via textContent; prefers-reduced-motion; dark and light; mobile.

## Direction contract

THESIS: Agent work is a live circuit: flows of work visibly travel from the main agent to its subagents and results travel back. Refuses the grid of identical status cards with a badge each.

OWN-WORLD: Near-black blue ground with a faint dot reticle, hairline borders, small-radius panels. One cyan for every busy state with a soft glow; amber only for "needs you" and the only thing that pulses; rose-red for failures; green for delivered results; slate for rest. Monospace for ids, times, counts and node kinds; a plain UI sans for names and activities. A light variant keeps the same roles on a pale ground.

STORY: The visitor sees immediately whether anything needs them (amber), what is running (cyan circuits), what failed (red), and what has finished (quiet rows below). They open Details only to ask why.

FIRST VIEWPORT: Top bar with the brand and a row of attention chips (waiting, failed, working, finished) that jump to sessions, and the live state. Below, full-width panels for live sessions, most urgent first: main node left, subagent nodes right, flows between. Under them an "At rest" list of compact rows.

FORM: seed: none (pinned). "algo simple, pero que parezca moderno y 'cibernético' de alguna forma, con flujos marcados y dinámicos con movimiento entre agente y subagentes"; approved from mock .impeccable/mocks/cyber-flow.html ("Me gusta, constrúyelo").

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
