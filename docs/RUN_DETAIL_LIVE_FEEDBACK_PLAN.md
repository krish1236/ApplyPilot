# Run Detail Live Feedback — Implementation Plan

This plan implements the spec in `RUN_DETAIL_LIVE_FEEDBACK_SPEC.md` so that **results appear on screen during a live run** for pipeline agents like ApplyPilot.

---

## Current State (Findings)

| Component | Behavior |
|-----------|----------|
| **agent.py** | Each stage is one `ctx.safe_step()` block with a single `ctx.log()` after the stage. No per-event logs during the stage. |
| **ctx.log()** | Emits a `StepEvent(event_type="log", message=..., level=...)`. Appends to `_current_step.log_entries` in memory. |
| **PlatformTracer** | Handles only `step_start`, `step_complete`, `step_failed`, `step_skipped`, `run_start`, `run_complete`, `run_failed`. **Does not handle `log`** — so log messages are never sent to the API. |
| **API** | Steps have `log_excerpt` (single text field). `report_step(..., log_excerpt=x)` sets `step.log_excerpt = log_excerpt or step.log_excerpt` (replace when provided). |
| **Dashboard** | Run detail page: `useRunDetail` polls every 3s when run is active. Step timeline shows step name, status, duration. `StepCard` shows first line of `log_excerpt` if present. `ExecutionLogPanel` builds a text log from run + steps (including `log_excerpt`). Right panel is always **Browser** (screenshot); no Live Results panel. |

**Gap:** Logs from `ctx.log()` never reach the platform because the SDK’s `PlatformTracer` ignores `log` events. Even if they did, we only send one summary line per stage today.

---

## Phase 1: Agent Logging + SDK Sending Logs (ApplyPilot + agent-runtime)

**Goal:** Every meaningful `ctx.log()` call is sent to the API and stored on the current step’s `log_excerpt`, so the dashboard can show progress during the run.

### 1.1 ApplyPilot `agent.py` — richer logging (Option A)

**Repo:** ApplyPilot fork — `agent.py`

**Tasks:**

1. Before each `_run_stage()` call, get `get_stats()` and log a “Starting &lt;stage&gt;…” line.
2. After each `_run_stage()` returns, get `get_stats()` again and log a stage summary with deltas, e.g.:
   - discover: total jobs, +new, duplicates
   - score: jobs scored, high-fit count (e.g. eligible for tailor)
   - tailor: resumes tailored
   - cover: cover letters written
3. In `collect_results`, keep the existing `ctx.log(json.dumps(results))` and/or add short human-readable lines (e.g. “Discovery: 203 jobs, 185 with description…”).

**Effort:** ~30 min. No platform changes; once SDK sends logs (1.2), these will appear.

**Optional (Phase 2):** Option B — hook into ApplyPilot’s pipeline to stream progress (e.g. “Indeed: 47 jobs”, “Workday: 23 jobs”) during discover. Requires callback or console capture in ApplyPilot.

---

### 1.2 agent-runtime — send `ctx.log()` to platform as step `log_excerpt`

**Repo:** agent-runtime — `agent_runtime/platform/tracer.py` and optionally `core/context.py`

**Current:** `PlatformTracer.emit()` does not handle `event_type == "log"`.

**Options:**

- **A) Accumulate in tracer, send on each log:**  
  - In `PlatformTracer`, maintain a buffer of log lines for the *current* step (step_index + step_name).  
  - On `log` event: append `message` to buffer (with optional `[level]` prefix), then call `_client.report_step(..., status="running", log_excerpt="\n".join(buffer))`.  
  - On `step_start`: clear buffer for the new step.  
  - On `step_complete` / `step_failed`: send final `log_excerpt` (buffer) so the last state is persisted.  
  - API already accepts `report_step(..., log_excerpt=...)` and merges into existing step; we send the full accumulated text each time so the step’s `log_excerpt` is the full log for that step.

- **B) Accumulate in context, tracer sends on step_complete:**  
  - Keep current behavior: context appends to `_current_step.log_entries`.  
  - On `step_complete`, tracer sends `log_excerpt = "\n".join(entry.message for entry in step.log_entries)`.  
  - Simpler but no live updates during the step — only at step end.

**Recommendation:** Implement **A** so logs appear **during** the step (live). Buffer in `PlatformTracer` keyed by (run_id, step_index); on each `log` event append and call `report_step` with full buffer. Cap buffer size (e.g. last 500 lines) to avoid huge payloads.

**Files to change:**

- `agent_runtime/platform/tracer.py`: handle `event_type == "log"`, maintain per-step log buffer, call `report_step(..., log_excerpt=...)`.
- Optionally `agent_runtime/platform/client.py`: ensure `report_step` can be called with only `log_excerpt` and `step_index` (and optionally `name`) without changing `status` if we want to avoid overwriting “running” — confirm API allows PATCH-style update of just `log_excerpt`.

**API note:** Current `report_step` always sends `status`. For “log only” updates we can send `status="running"` plus `log_excerpt`; the step stays running and the new excerpt is stored. No API change required.

**Effort:** ~1 hour.

---

## Phase 2: Dashboard — Step Timeline Shows Log Lines Under Each Step

**Goal:** Step timeline shows expandable log lines under each step; running step auto-expanded with new lines appearing in real time.

### 2.1 Data

- Steps already have `log_excerpt` (string, newline-separated lines). Once Phase 1 is done, this is populated during the run.
- Run detail fetches run + steps every 3s when active (`useRunDetail`). No new API needed.

### 2.2 Step timeline UI

**Repo:** platform-dashboard

**Files:**

- `src/components/run-detail/StepTimeline.tsx` — render steps; for each step, render an expandable section that shows `step.log_excerpt` split into lines.
- `src/components/run-detail/StepCard.tsx` — extend so a step can show multiple log lines (not just first line) and expand/collapse.

**Behavior:**

- **Running step:** Auto-expand, show log lines, scroll to bottom as new lines appear (polling every 3s already brings new data).
- **Completed step:** Collapsed by default; click to expand and show full `log_excerpt` lines.
- **Failed step:** Auto-expand, last line or error_message highlighted.
- **Styling:** Log lines: monospace, small, dimmed; max height with “Show more” if &gt; 10 lines, or scrollable.

**Implementation:**

- Add state per step: `expandedSteps: Set<stepId>` (or step_index). Running step is always in the set; completed/failed can be toggled.
- In `StepCard` (or a wrapper in StepTimeline), if step has `log_excerpt`, render a block below the one-line summary: split `log_excerpt.trim().split("\n")`, render each line with a small prefix (e.g. `├─` or bullet). When expanded and run is active, scroll the log block to bottom (ref + useEffect on steps/runId).

**Effort:** 2–3 hours.

---

## Phase 3: Dashboard — Adaptive Right Panel (Live Results vs Browser)

**Goal:** For non-browser agents (e.g. ApplyPilot), the right panel shows a **Live Results** card (numbers, score distribution, by source) instead of the Browser screenshot panel.

### 3.1 When to show Live Results

- **Detection:** Run has no browser session (`!run.browser_session_id`) and (optional) project or agent has `browser: false`, OR run has been active &gt; 30s with no steps that have `screenshot_url`.  
- **Simpler rule:** If `run.result_payload` is present (after run completes), show Live Results. During run, if no step has a screenshot yet after 30s, show a “Live results” placeholder that will fill when `result_payload` is set, or show execution log excerpt.

### 3.2 Live Results content

- **Source:** `run.result_payload` (set by ApplyPilot’s `collect_results` / agent return value). Already contains: `total_jobs_discovered`, `jobs_with_description`, `jobs_scored`, `jobs_tailored`, `jobs_with_cover_letter`, `jobs_ready_to_apply`, `score_distribution`, `by_site`.
- **During run:** Either hide right panel, or show a compact “Running…” card that shows the latest step’s summary (from step `log_excerpt` or from parsing last log line). Optional: parse log lines for “found X jobs” and show a minimal live counter.
- **After run:** Render a card: key metrics (table or list), score distribution (simple bar or list), by_site breakdown.

### 3.3 Implementation

**Repo:** platform-dashboard

**Files:**

- `src/app/(dashboard)/projects/[projectId]/runs/[runId]/page.tsx` — decide which right panel to show (Browser vs Live Results). If `run.result_payload` has ApplyPilot-like keys or `run.browser_session_id` is falsy and run is completed, show Live Results; else show Browser.
- New component: `src/components/run-detail/LiveResultsPanel.tsx` — accepts `result_payload: object | null` and optionally `steps` (for during-run hint). Renders the metrics card, score distribution, by_site. Handles null/empty payload.

**Effort:** 2–3 hours.

---

## Phase 4: Optional — Streaming log lines (finer granularity)

**Spec mentioned:** “New log lines should appear in real time (poll or WebSocket).”

- **Current:** Polling every 3s in `useRunDetail` is enough to show new log lines within a few seconds.
- **Enhancement:** Reduce polling interval to 1–2s when run is active so logs feel more “live.” Or add a dedicated “steps” or “run log” endpoint that returns only steps (or step log_excerpts) with caching headers so the dashboard can poll more aggressively without refetching full run. No WebSocket required for MVP.

---

## Order of Work

| Order | Task | Repo | Effort |
|-------|------|------|--------|
| 1 | ApplyPilot `agent.py`: add `ctx.log()` before/after each stage with stats | ApplyPilot | 30 min |
| 2 | agent-runtime: PlatformTracer handle `log` events, buffer and send `log_excerpt` | agent-runtime | ~1 hr |
| 3 | Dashboard: Step timeline — expandable log lines under each step, auto-expand running | platform-dashboard | 2–3 hr |
| 4 | Dashboard: Adaptive right panel — Live Results from `result_payload` when no browser | platform-dashboard | 2–3 hr |

**Total (MVP):** ~6–8 hours. After 1+2, logs appear live on the run detail page (in Execution Log and, once 3 is done, under each step). After 4, the right panel shows ApplyPilot results instead of an empty browser panel.

---

## Verification

1. **Phase 1:** Trigger an ApplyPilot run (e.g. discover only). Confirm run detail step cards show updated `log_excerpt` during the run (poll and inspect network or UI).
2. **Phase 2:** Confirm running step expands and new lines appear every few seconds; completed steps are collapsible and show full log.
3. **Phase 3:** For a completed ApplyPilot run, confirm right panel shows Live Results (numbers, score distribution, by_site). For a browser run, confirm right panel still shows Browser/screenshot.
4. **Phase 4 (optional):** Reduce poll interval and confirm logs feel more real-time.

---

## File Checklist

- [ ] **ApplyPilot** `agent.py` — add `get_stats()` before/after each stage, `ctx.log(...)` with summary and deltas.
- [ ] **agent-runtime** `platform/tracer.py` — on `log` event, append to buffer, call `report_step(..., log_excerpt=accumulated)`; clear buffer on step_start; send final log on step_complete/step_failed.
- [ ] **platform-dashboard** `StepTimeline.tsx` / `StepCard.tsx` — expandable log block per step, auto-expand running, style log lines.
- [ ] **platform-dashboard** `runs/[runId]/page.tsx` — choose Browser vs Live Results for right panel.
- [ ] **platform-dashboard** `LiveResultsPanel.tsx` — new component for `result_payload` (metrics, score distribution, by_site).
