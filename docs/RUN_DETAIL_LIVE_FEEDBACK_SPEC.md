# Run Detail Page — Live Feedback Spec

## Problem

The run detail page was designed for browser agents where screenshots are the primary feedback. For data pipeline agents like ApplyPilot, the user sees an opaque step name with a spinner and no useful information. 80% of the screen is wasted.

## Goal

Make the run detail page useful for ALL agent types — browser agents get screenshots, data pipeline agents get live progress, numbers, and streaming logs. The page should adapt based on what the agent actually produces.

---

## Change 1: Richer Logging in agent.py

**Where:** ApplyPilot fork — `agent.py`

**What:** Add granular `ctx.log()` calls so the user sees real-time progress. Currently each stage is a silent block. After this change, every meaningful event inside a stage emits a log.

```python
with ctx.safe_step("discover"):
    ctx.log("Starting job discovery...")
    ctx.log("Searching Indeed for 'Software Engineer' in 'Remote'...")
    # after jobspy runs:
    ctx.log(f"Indeed: found {indeed_count} jobs")
    ctx.log(f"Workday: scanning {len(employers)} employer portals...")
    ctx.log(f"Workday: found {workday_count} jobs")
    ctx.log(f"Smart extract: found {smart_count} jobs from direct sites")
    ctx.log(f"Discovery complete: {total} total jobs, {new} new, {dupes} duplicates")
```

**Implementation approach:** ApplyPilot's pipeline functions print to stdout via `rich.console`. We need to capture those outputs OR hook into the pipeline at key points. Two options:

**Option A (quick):** After each `_run_stage()` call returns, query `get_stats()` and log the delta. This gives per-stage summary but not real-time within-stage progress.

**Option B (better):** Wrap ApplyPilot's pipeline to intercept progress. Add a callback or monkey-patch the console output to also call `ctx.log()`. More work but gives real-time line-by-line progress.

**Recommendation:** Start with Option A — it's 10 lines of code and gives immediate value. Option B is a Phase 2 enhancement.

```python
# Option A implementation in agent.py
for stage_name in stages:
    with ctx.safe_step(stage_name):
        # Log before
        stats_before = get_stats()
        ctx.log(f"Starting {stage_name}...")

        result = _run_stage(stage_name, ...)

        # Log after with delta
        stats_after = get_stats()
        new_jobs = stats_after["total"] - stats_before["total"]
        ctx.log(f"{stage_name} complete: {stats_after['total']} total jobs (+{new_jobs} new)")

        if stage_name == "score":
            ctx.log(f"Scored: {stats_after['scored']} jobs | High fit (7+): {stats_after['untailored_eligible']}")
        if stage_name == "tailor":
            ctx.log(f"Tailored: {stats_after['tailored']} resumes")
        if stage_name == "cover":
            ctx.log(f"Cover letters: {stats_after['with_cover_letter']}")
```

---

## Change 2: Stream Logs Under Steps in Timeline

**Where:** Dashboard — Run detail page, Step Timeline component

**What:** Show `ctx.log()` messages nested under each step as they arrive. Currently steps are just `name | SAFE | duration`. After this change, each step is expandable with log lines underneath.

**Current:**
```
● setup     SAFE  0.6s
● discover  SAFE  (running...)
```

**After:**
```
✓ setup     SAFE  0.6s

● discover  SAFE  (running... 45s)
  ├─ Starting job discovery...
  ├─ Searching Indeed for 'Software Engineer' in 'Remote'...
  ├─ Indeed: found 47 jobs
  ├─ Workday: scanning 48 employer portals...
  ├─ Workday: found 23 jobs
  └─ (running...)
```

**Implementation:**

1. Steps already have a `log_excerpt` field in the API. Check if `ctx.log()` messages are being stored there or somewhere accessible per-step.
2. If logs are stored per-step: fetch them and render as expandable lines under each step in the timeline.
3. If logs are only in the execution log at the bottom: associate each log line with its parent step (by timestamp or step_index) and render inline.
4. Auto-expand the currently running step. Collapse completed steps (click to expand).
5. New log lines should appear in real-time (poll or WebSocket — use whatever the execution log already uses).

**UI spec:**
- Running step: auto-expanded, new lines animate in
- Completed step: collapsed by default, click to expand, shows all log lines
- Failed step: auto-expanded, last log line highlighted in red
- Log lines: monospace font, dimmed color, small text — they're secondary to the step name
- Max visible lines per step: 10, then "show more" link

---

## Change 3: Adaptive Right Panel

**Where:** Dashboard — Run detail page, right column (currently the Browser panel)

**What:** The right panel shows browser screenshots for browser agents. For non-browser agents (like ApplyPilot), replace it with a **Live Results** panel that shows useful numbers.

**Detection:** If `agent.yaml` has `browser: false` OR the run has zero browser sessions OR no screenshots exist after 30 seconds — show the Live Results panel instead of the Browser panel.

**Live Results panel content:**

Pull data from `ctx.log()` messages or `run.result_payload` (when available) and render as a summary card:

```
┌─────────────────────────────┐
│  LIVE RESULTS               │
│                             │
│  Jobs Discovered    203     │
│  With Description   185     │
│  Scored             185     │
│  High Fit (7+)       23     │
│  Resumes Tailored    23     │
│  Cover Letters       23     │
│  Ready to Apply      23     │
│                             │
│  ── Score Distribution ──   │
│  10 ███          3          │
│   9 █████        5          │
│   8 ████████    8           │
│   7 ███████     7           │
│   6 ██████████ 12           │
│  ≤5 ████████████████ 150    │
│                             │
│  ── Sources ──              │
│  Indeed        89           │
│  LinkedIn      45           │
│  Glassdoor     32           │
│  Workday       23           │
│  Direct sites  14           │
└─────────────────────────────┘
```

**Implementation:**

1. The agent's final step (`collect_results`) sets `ctx.state["results"]` with all these numbers and returns them as `result_payload`.
2. For live updates DURING the run: parse `ctx.log()` messages for known patterns (e.g., lines containing "found X jobs") or query the step details.
3. **Simplest approach:** After each step completes, the dashboard re-fetches run data. If `result_payload` is set (it won't be until the run ends) show the full summary. During the run, show whatever numbers are available from completed step logs.
4. When the run completes: show the full result with score distribution and source breakdown.

**Fallback:** If no structured data is available, show the execution log in the right panel instead of the browser screenshot. Even raw logs are more useful than "No screenshot for discover."

---

## Change Summary

| Where | What | Effort |
|-------|------|--------|
| ApplyPilot `agent.py` | Add `ctx.log()` calls with stats deltas after each stage | 30 min |
| Dashboard — Step Timeline | Show log lines nested under each step, auto-expand running step | 2-3 hours |
| Dashboard — Right Panel | Adaptive panel: browser screenshot OR live results summary based on agent type | 2-3 hours |

---

## Priority Order

1. **agent.py logging** — immediate value, no platform changes, 30 minutes
2. **Step timeline with inline logs** — makes the page feel alive during long runs
3. **Adaptive right panel** — the wow factor, turns a useless panel into a live dashboard

Do #1 first, test it, then #2 and #3 can be built in parallel.
