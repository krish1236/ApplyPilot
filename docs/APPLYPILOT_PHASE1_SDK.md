# ApplyPilot on Runforge — Phase 1 (SDK Approach)

## Summary

Deploy the forked ApplyPilot as a real Runforge agent using the `agent-runtime` SDK. One new file (`agent.py`) wraps the ApplyPilot pipeline in SDK steps. The Dockerfile installs both ApplyPilot and the SDK. No modifications to `src/applypilot/`.

---

## SDK verification (vs actual agent-runtime code)

Verified against `agent-runtime`:

| Doc assumption | Actual SDK | Notes |
|----------------|------------|--------|
| `@runtime.agent(name="...")` + `def run_applypilot(ctx, input)` | `AgentRuntime.agent(name)` decorator; agent signature `(ctx, input) -> dict` | Matches. Worker runs `default_agent` (first registered). |
| `ctx.safe_step("name")` | `RunContext.safe_step(name)` → context manager, no nesting | Matches. |
| `ctx.artifact(name, data, content_type)` | `ctx.artifact(name, data: bytes \| str, content_type=...)` | Matches. Use `path.read_bytes()` for files. |
| `ctx.log(message)` | `ctx.log(message, level="info")` | Matches. |
| `ctx.state` | `ctx.state: dict` for checkpoint/resume | Matches. |
| Entrypoint `agent:run_applypilot` | CLI: `agent_runtime worker <module:function>`; module must define `AgentRuntime()` and register one agent | Module name `agent` (agent.py); function name in entrypoint is for reference, default_agent is run. |
| Steps/heartbeat/artifacts | `PlatformTracer` + `PlatformAPIClient` report steps; `PlatformArtifactStore` uploads to S3 and registers via API | Matches. |
| Run result on platform | SDK did **not** PATCH final status/result; worker only set status from exit code | **Gap:** Implemented `PlatformAPIClient.update_run_status_result(status, result_payload)` and call it from `execute_local_run` on success/failure so dashboard shows result. |
| Worker overwriting status | Worker overwrote run.status from exit code after container exit | **Already fixed** in platform-api: worker does not overwrite if run.status is already `succeeded` or `failed`. |
| `PLATFORM_API_URL` | Runner uses `os.environ.get("PLATFORM_API_URL")`; client builds URLs as `base + "/internal" + path` | Matches. Set by platform worker when starting container. |

---

## How It Works

```
Worker picks run from Redis → docker run <image>
  → CMD: python -m agent_runtime worker agent:run_applypilot
    → SDK boots, connects to platform API (env vars: RUN_ID, WORKER_SECRET, PLATFORM_API_URL, S3_*)
      → SDK calls run_applypilot(ctx, input)
        → Each ApplyPilot stage runs inside a ctx.safe_step()
        → SDK auto-handles: step reporting, heartbeats, artifact uploads, status, checkpoints
```

---

## File 1: `agent.py` (new, in repo root)

This is the Runforge entrypoint. It imports ApplyPilot's pipeline and wraps each stage in an SDK step.

```python
"""ApplyPilot — Runforge Agent Wrapper.

Entrypoint: agent:run_applypilot
Wraps ApplyPilot's 6-stage pipeline in Runforge SDK steps.
"""

import json
import os
from pathlib import Path

from agent_runtime import AgentRuntime

runtime = AgentRuntime()


def _setup_applypilot(input_payload: dict):
    """Write ApplyPilot config files from the run input payload.

    ApplyPilot expects files in ~/.applypilot/:
      - profile.json (user profile data)
      - resume.txt (plain text resume)
      - searches.yaml (job search configuration)
      - .env (LLM API keys)
    """
    import yaml
    from applypilot.config import (
        APP_DIR, PROFILE_PATH, RESUME_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, ensure_dirs,
    )

    ensure_dirs()

    if "profile" in input_payload:
        PROFILE_PATH.write_text(
            json.dumps(input_payload["profile"], indent=2), encoding="utf-8"
        )

    if "resume_text" in input_payload:
        RESUME_PATH.write_text(input_payload["resume_text"], encoding="utf-8")

    if "searches" in input_payload:
        SEARCH_CONFIG_PATH.write_text(
            yaml.dump(input_payload["searches"], default_flow_style=False),
            encoding="utf-8",
        )

    # Write .env so ApplyPilot's load_env() picks up LLM keys
    env_lines = []
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_MODEL"):
        val = os.environ.get(key)
        if val:
            env_lines.append(f"{key}={val}")
    if env_lines:
        ENV_PATH.write_text("\n".join(env_lines) + "\n", encoding="utf-8")


def _run_stage(stage_name: str, min_score: int = 7, workers: int = 1,
               validation_mode: str = "normal"):
    """Run a single ApplyPilot pipeline stage."""
    from applypilot.pipeline import run_pipeline
    return run_pipeline(
        stages=[stage_name],
        min_score=min_score,
        workers=workers,
        validation_mode=validation_mode,
    )


@runtime.agent(name="applypilot-job-agent")
def run_applypilot(ctx, input):
    """Main agent function. Runs ApplyPilot stages as Runforge safe steps."""

    min_score = input.get("min_score", 7)
    workers = input.get("workers", 1)
    validation_mode = input.get("validation_mode", "normal")
    stages = input.get("stages", ["discover", "enrich", "score", "tailor", "cover", "pdf"])

    # --- Setup ---
    with ctx.safe_step("setup"):
        _setup_applypilot(input)
        from applypilot.config import load_env, ensure_dirs
        from applypilot.database import init_db
        load_env()
        ensure_dirs()
        init_db()
        ctx.log("ApplyPilot configured and database initialized")

    # --- Run each requested stage ---
    for stage_name in stages:
        with ctx.safe_step(stage_name):
            result = _run_stage(
                stage_name,
                min_score=min_score,
                workers=workers,
                validation_mode=validation_mode,
            )
            elapsed = result.get("elapsed", 0)
            errors = result.get("errors", {})
            ctx.log(f"Stage '{stage_name}' completed in {elapsed:.1f}s")
            if errors:
                ctx.log(f"Stage errors: {errors}")
            # Store progress in state for checkpoint recovery
            ctx.state[f"{stage_name}_completed"] = True
            ctx.state[f"{stage_name}_elapsed"] = elapsed

    # --- Collect results ---
    with ctx.safe_step("collect_results"):
        from applypilot.database import get_stats
        from applypilot.config import DB_PATH, TAILORED_DIR, COVER_LETTER_DIR

        stats = get_stats()
        ctx.state["results"] = {
            "total_jobs_discovered": stats["total"],
            "jobs_with_description": stats["with_description"],
            "jobs_scored": stats["scored"],
            "jobs_tailored": stats["tailored"],
            "jobs_with_cover_letter": stats["with_cover_letter"],
            "jobs_ready_to_apply": stats["ready_to_apply"],
            "jobs_applied": stats["applied"],
        }
        ctx.log(f"Results: {json.dumps(ctx.state['results'])}")

        # Upload SQLite DB as artifact
        if DB_PATH.exists():
            ctx.artifact("applypilot.db", DB_PATH.read_bytes(), content_type="application/x-sqlite3")

        # Upload tailored resume PDFs
        if TAILORED_DIR.exists():
            for pdf in TAILORED_DIR.glob("*.pdf"):
                ctx.artifact(pdf.name, pdf.read_bytes(), content_type="application/pdf")

        # Upload cover letter PDFs
        if COVER_LETTER_DIR.exists():
            for pdf in COVER_LETTER_DIR.glob("*.pdf"):
                ctx.artifact(f"cover_{pdf.name}", pdf.read_bytes(), content_type="application/pdf")

    return ctx.state["results"]


if __name__ == "__main__":
    runtime.serve()
```

### Key points about `agent.py`:

- **Import path:** `agent:run_applypilot` — the entrypoint the platform uses
- **Each ApplyPilot stage is a `ctx.safe_step()`** — shows as a step in the dashboard timeline
- **`ctx.state`** tracks which stages completed — enables checkpoint resume if a stage crashes
- **`ctx.artifact()`** uploads PDFs and the SQLite DB — downloadable from run detail
- **`ctx.log()`** messages appear in the step trace
- **No `commit_step` in Phase 1** — the apply stage (Phase 2) will use `commit_step` for approval before submitting applications
- **Does NOT modify anything under `src/applypilot/`**

---

## File 2: `Dockerfile` (new, in repo root)

```dockerfile
FROM python:3.11-slim

# System deps for Chrome, Node.js, Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 ca-certificates \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libx11-xcb1 libxcomposite1 libxdamage1 \
    libxrandr2 libxfixes3 libcups2 libdbus-1-3 \
    fonts-liberation curl \
    && rm -rf /var/lib/apt/lists/*

# Chrome (needed for Phase 2 apply stage; install now to avoid rebuild later)
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor > /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (needed for Phase 2 Playwright MCP)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_PATH=/usr/bin/google-chrome-stable

WORKDIR /app

# Install ApplyPilot dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Install jobspy separately (broken numpy pin in metadata)
RUN pip install --no-cache-dir --no-deps python-jobspy \
    && pip install --no-cache-dir pydantic tls-client requests markdownify regex

# Install Runforge SDK
RUN pip install --no-cache-dir agent-runtime

# Install Playwright
RUN pip install --no-cache-dir playwright && playwright install chromium && playwright install-deps chromium

# Copy all source code
COPY . .

# ApplyPilot data directory
RUN mkdir -p /root/.applypilot
ENV APPLYPILOT_DIR=/root/.applypilot

# Runforge SDK runs the agent
ENV AGENT_RUNTIME_ENV=production
CMD ["python", "-m", "agent_runtime", "worker", "agent:run_applypilot"]
```

---

## File 3: `agent.yaml` (new, in repo root)

```yaml
name: applypilot-job-agent
entrypoint: agent:run_applypilot
python_version: "3.11"
browser: false
max_concurrency: 2
run_timeout_minutes: 60
```

`browser: false` for Phase 1 (stages 1-5 don't use a browser). Phase 2 sets this to `true` for the apply stage.

---

## Platform-API Change (1 change)

### Worker: don't overwrite SDK-reported status

The SDK reports run status (`succeeded`/`failed`) to the platform API before the container exits. The worker currently overwrites status based on container exit code.

**File:** `platform-api/scripts/run_worker.py`

**Change:** After `docker run` finishes, check if `run.status` is already `succeeded` or `failed`. If yes, do not overwrite. This is a 3-line if-statement.

```python
# After container exits:
run = get_run(run_id)
if run.status not in ("succeeded", "failed"):
    run.status = "succeeded" if returncode == 0 else "failed"
```

No other platform-api changes needed. The SDK uses the existing internal API endpoints (steps, heartbeats, artifacts) that already exist.

---

## Deployment Steps

1. **Add the 3 files** to the forked ApplyPilot repo: `agent.py`, `Dockerfile`, `agent.yaml`
2. **Push to GitHub**
3. **In Runforge dashboard:** Create project "applypilot-job-agent", connect the fork
4. **Set environment variables:** `GEMINI_API_KEY`, `APPLYPILOT_DIR=/root/.applypilot`
5. **Deploy** — platform builds Docker image from the Dockerfile
6. **Trigger test run** with input:

```json
{
  "profile": {
    "first_name": "Test",
    "email": "test@example.com",
    "location": "Remote",
    "skills": ["Python", "AWS"]
  },
  "resume_text": "Software engineer with 5 years experience...",
  "searches": {
    "searches": [
      {
        "title": "Software Engineer",
        "location": "Remote",
        "boards": ["indeed"],
        "results_per_board": 10
      }
    ]
  },
  "stages": ["discover", "enrich", "score"],
  "min_score": 7,
  "workers": 1
}
```

Start with `discover`, `enrich`, `score` only. Add `tailor`, `cover`, `pdf` after those work.

---

## What You Should See

- **Dashboard run detail:** Steps appear: `setup → discover → enrich → score → collect_results`
- **Each step** shows status (running → completed), duration
- **Artifacts:** `applypilot.db` downloadable after run completes
- **Run result:** Job counts (total discovered, scored, etc.)
- When you add tailor/cover/pdf stages: tailored resume PDFs and cover letter PDFs appear as artifacts

---

## What's NOT in Phase 1

- **Apply stage** — needs `commit_step` + approval + Browserbase (Phase 2)
- **Telegram notifications** — "Found 47 jobs, approve to apply?" (Phase 2)
- **Scheduled runs** — daily cron job search (Phase 2)
- **Explore page listing** — public showcase (Phase 2)

---

## Files Changed Summary

| Repo | File | Action |
|------|------|--------|
| ApplyPilot fork | `agent.py` | NEW — SDK agent wrapper |
| ApplyPilot fork | `Dockerfile` | NEW — container build |
| ApplyPilot fork | `agent.yaml` | NEW — Runforge config |
| platform-api | `scripts/run_worker.py` | MODIFY — 3-line guard on status overwrite |
| ApplyPilot fork | `src/applypilot/*` | NO CHANGES |

---

## Success Criteria

- [ ] Docker image builds
- [ ] Container starts, SDK connects to platform
- [ ] Steps appear in dashboard: setup, discover, enrich, score, collect_results
- [ ] `discover` finds real jobs from job boards
- [ ] `score` rates jobs using Gemini LLM
- [ ] Artifacts downloadable (applypilot.db, PDFs when tailor/cover run)
- [ ] Run completes with status "succeeded" and result showing job counts
- [ ] No modifications to `src/applypilot/`
