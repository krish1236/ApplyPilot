# ApplyPilot → Runforge: Phase 1 Deployment Instructions

> **Superseded — do not follow for new deployments.** Production uses the **Runforge SDK** (`agent.py`, `@runtime.agent`, `planned_steps`, platform tracer). The repo **`Dockerfile`** CMD is `python -m agent_runtime worker agent:run_applypilot`. The old standalone **`runforge_wrapper.py`** was removed (it bypassed the SDK). See **`APPLYPILOT_PHASE1_SDK.md`** for the current path.
>
> The sections below are kept as historical context for the pre-SDK “wrapper only” experiment.

## Purpose

This document gave step-by-step instructions to deploy ApplyPilot **without** the Runforge SDK (a standalone HTTP-reporting wrapper). That approach is **no longer used** in this fork.

Phase 2 (separate document) will wrap the pipeline in the Runforge SDK with safe/commit steps, approvals, and observability.

---

## What ApplyPilot Is

ApplyPilot is a 6-stage autonomous job application pipeline:

1. **Discover** — scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday portals + 30 direct career sites
2. **Enrich** — visits each job URL, extracts full description via JSON-LD / CSS / AI extraction
3. **Score** — LLM rates each job 1-10 against user's resume
4. **Tailor** — LLM rewrites resume per job, validates no fabrication
5. **Cover Letter** — LLM generates targeted cover letter per job
6. **Auto-Apply** — Claude Code CLI navigates Chrome, fills application forms, submits

Source: `src/applypilot/` — pure Python 3.11+, ~405K of source code.

---

## What Exists in Runforge Already

Before starting, understand what you're working with:

| Component | Status | Details |
|-----------|--------|---------|
| EC2 server | Running | `23.23.207.237`, Ubuntu, Docker installed |
| Worker execution | Working | `scripts/run_worker.py` picks runs from Redis queue, runs in Docker containers |
| Docker build pipeline | Working | Clones repo → generates Dockerfile → `docker build` → runs container |
| Browserbase integration | Working | `RemoteBrowserProvider` connects Playwright via CDP to Browserbase sessions |
| FastAPI control plane | Running | `api.runforge.sh` — projects, runs, steps, approvals, artifacts |
| Dashboard | Running | `app.runforge.sh` — project overview, runs list, run detail, approvals |
| Postgres | Running | RDS — stores projects, runs, steps, deployments, etc. |
| Redis | Running | ElastiCache — run queue, heartbeats, approval signaling |
| S3 | Running | Artifacts, screenshots, checkpoints |
| Deploy script | Working | `deploy.sh` with rsync + systemd services (`agent-runtime-api`, `agent-runtime-worker`) |
| SSH key | Available | `~/Documents/deploy/agent-runtime-key.pem` |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│  Runforge Dashboard (app.runforge.sh)           │
│  Shows: project, runs, step timeline, artifacts │
└──────────────────┬──────────────────────────────┘
                   │ API calls
┌──────────────────▼──────────────────────────────┐
│  Runforge API (api.runforge.sh)                 │
│  FastAPI — triggers runs, stores results        │
└──────────────────┬──────────────────────────────┘
                   │ Redis queue
┌──────────────────▼──────────────────────────────┐
│  Runforge Worker                                │
│  Picks run from queue → docker run <image>      │
└──────────────────┬──────────────────────────────┘
                   │ runs inside container
┌──────────────────▼──────────────────────────────┐
│  ApplyPilot Container                           │
│  Python 3.11 + Chrome + Node.js + applypilot    │
│  Connects to Browserbase for remote Chrome      │
│  Uses Gemini/OpenAI for LLM calls               │
│  SQLite DB inside container for pipeline state   │
│  Reports results back to Runforge API           │
└─────────────────────────────────────────────────┘
```

---

## Step 1: Understand the ApplyPilot Codebase

Read these files in order. Do NOT modify anything yet.

### Core files to read:

1. `pyproject.toml` — dependencies and entry point (`applypilot = "applypilot.cli:app"`)
2. `src/applypilot/config.py` — all paths, tier system, Chrome detection, profile/search config loading
3. `src/applypilot/database.py` — SQLite schema (`jobs` table), `init_db()`, `get_stats()`, `store_jobs()`
4. `src/applypilot/llm.py` — LLM client (Gemini/OpenAI/local), auto-detect from env vars, retry with backoff
5. `src/applypilot/pipeline.py` — pipeline orchestrator, runs stages sequentially or concurrently
6. `src/applypilot/cli.py` — CLI entry points: `init`, `run`, `apply`, `status`, `dashboard`, `doctor`
7. `src/applypilot/discovery/jobspy.py` — JobSpy scraper (Indeed, LinkedIn, Glassdoor, etc.)
8. `src/applypilot/discovery/workday.py` — Workday corporate portal scraper
9. `src/applypilot/discovery/smartextract.py` — AI-powered site scraper for direct career pages
10. `src/applypilot/enrichment/detail.py` — full job description extraction (JSON-LD → CSS → AI cascade)
11. `src/applypilot/scoring/scorer.py` — LLM job fit scoring (1-10)
12. `src/applypilot/scoring/tailor.py` — LLM resume tailoring per job
13. `src/applypilot/scoring/cover_letter.py` — LLM cover letter generation
14. `src/applypilot/scoring/pdf.py` — PDF conversion for resumes and cover letters
15. `src/applypilot/apply/chrome.py` — Chrome process lifecycle (launch, CDP port, cleanup)
16. `src/applypilot/apply/launcher.py` — auto-apply orchestrator (spawns Claude Code CLI sessions)
17. `src/applypilot/apply/prompt.py` — builds the prompt sent to Claude Code for form navigation
18. `src/applypilot/wizard/init.py` — first-time setup wizard (profile, resume, search config)

### Key architectural facts:

- **Database:** SQLite at `~/.applypilot/applypilot.db`. Single `jobs` table with columns for every stage. URL is primary key.
- **State directory:** Everything lives in `~/.applypilot/` — DB, profile.json, resume.txt, searches.yaml, tailored resumes, cover letters, Chrome worker dirs.
- **LLM:** Detects provider from env vars (`GEMINI_API_KEY`, `OPENAI_API_KEY`, or `LLM_URL`). Uses OpenAI-compatible API with Gemini native fallback on 403.
- **Browser:** Stages 1-5 do NOT use a browser. Only the `apply` stage (stage 6) launches Chrome + Claude Code CLI. Discovery/enrichment use `httpx` and `beautifulsoup4` for HTTP scraping.
- **Tier system:** Tier 1 = discovery only. Tier 2 = + LLM scoring/tailoring. Tier 3 = + auto-apply (needs Claude Code CLI + Chrome + Node.js).
- **Parallelism:** Discovery and enrichment support `--workers N` for parallel threads. Apply supports `--workers N` for parallel Chrome instances. SQLite uses WAL mode + thread-local connections.

---

## Step 2: Create the Dockerfile

Create `Dockerfile` in the repo root. This container must support ALL 6 stages including auto-apply.

```dockerfile
FROM python:3.11-slim

# System dependencies for Chrome, Playwright, Node.js, and PDF generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chrome dependencies
    wget gnupg2 ca-certificates \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libx11-xcb1 libxcomposite1 libxdamage1 \
    libxrandr2 libxfixes3 libcups2 libdbus-1-3 \
    # PDF generation (for reportlab/weasyprint if needed)
    fonts-liberation \
    # Node.js (for npx / Playwright MCP)
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome stable
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor > /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 LTS (needed for npx to run Playwright MCP server)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Set Chrome path for ApplyPilot's config.py detection
ENV CHROME_PATH=/usr/bin/google-chrome-stable

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Install python-jobspy separately (broken numpy pin)
RUN pip install --no-cache-dir --no-deps python-jobspy \
    && pip install --no-cache-dir pydantic tls-client requests markdownify regex

# Install Playwright browsers (for enrichment that might use playwright)
RUN pip install playwright && playwright install chromium && playwright install-deps chromium

# Copy source code
COPY . .

# Create the applypilot data directory
RUN mkdir -p /root/.applypilot

# Default command — overridden by Runforge worker
CMD ["applypilot", "run"]
```

### IMPORTANT notes on the Dockerfile:

- Chrome is needed for the `apply` stage (Claude Code connects via CDP)
- Node.js is needed for `npx @playwright/mcp@latest` which Claude Code uses
- `python-jobspy` must be installed separately with `--no-deps` due to a numpy version conflict
- The `APPLYPILOT_DIR` env var can override `~/.applypilot` — useful for container isolation
- Playwright is installed for enrichment (some pages need JS rendering)

---

## Step 3: Create the API Wrapper *(obsolete)*

**Do not create this file.** It was removed from the fork. Use **`agent.py`** + SDK worker CMD instead. The following code block is left only as reference for what the old wrapper did.

```python
"""
Runforge wrapper for ApplyPilot.

This script is the container entrypoint when running on Runforge.
It:
1. Reads configuration from environment variables (set by Runforge worker)
2. Sets up ApplyPilot's profile, resume, and search config from the input payload
3. Runs the ApplyPilot pipeline (discover → enrich → score → tailor → cover → pdf)
4. Reports results back to the Runforge API
5. Optionally runs the apply stage if approved

Environment variables provided by Runforge:
  RUN_ID              — Runforge run identifier
  INPUT_PAYLOAD       — JSON string with user configuration
  PLATFORM_API_URL    — Runforge API URL (https://api.runforge.sh)
  WORKER_SECRET       — Auth token for reporting back to platform
  GEMINI_API_KEY      — LLM provider key (passed through from project env vars)
  BROWSERBASE_API_KEY — For managed browser (apply stage)
  BROWSERBASE_PROJECT_ID — For managed browser (apply stage)
"""

import json
import os
import sys
import time
import logging
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("runforge_wrapper")

# ---------------------------------------------------------------------------
# Runforge reporting helpers
# ---------------------------------------------------------------------------

PLATFORM_API_URL = os.environ.get("PLATFORM_API_URL", "https://api.runforge.sh")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
RUN_ID = os.environ.get("RUN_ID", "")


def report_step(step_name: str, status: str, details: dict | None = None):
    """Report a step event to the Runforge platform."""
    if not RUN_ID or not WORKER_SECRET:
        log.warning("No RUN_ID or WORKER_SECRET — skipping platform reporting")
        return

    try:
        httpx.post(
            f"{PLATFORM_API_URL}/internal/runs/{RUN_ID}/steps",
            headers={"Authorization": f"Bearer {WORKER_SECRET}"},
            json={
                "name": step_name,
                "status": status,
                "step_type": "safe",
                "details": details or {},
            },
            timeout=10,
        )
    except Exception as e:
        log.error("Failed to report step '%s': %s", step_name, e)


def report_artifact(name: str, file_path: str):
    """Upload an artifact file to the Runforge platform."""
    if not RUN_ID or not WORKER_SECRET:
        return

    try:
        with open(file_path, "rb") as f:
            httpx.post(
                f"{PLATFORM_API_URL}/internal/runs/{RUN_ID}/artifacts",
                headers={"Authorization": f"Bearer {WORKER_SECRET}"},
                files={"file": (name, f)},
                timeout=30,
            )
    except Exception as e:
        log.error("Failed to upload artifact '%s': %s", name, e)


def report_run_status(status: str, result: dict | None = None):
    """Report the final run status to the Runforge platform."""
    if not RUN_ID or not WORKER_SECRET:
        return

    try:
        httpx.patch(
            f"{PLATFORM_API_URL}/internal/runs/{RUN_ID}",
            headers={"Authorization": f"Bearer {WORKER_SECRET}"},
            json={"status": status, "result": result or {}},
            timeout=10,
        )
    except Exception as e:
        log.error("Failed to report run status: %s", e)


def send_heartbeat():
    """Send a heartbeat to prevent the run from being marked as timed out."""
    if not RUN_ID or not WORKER_SECRET:
        return

    try:
        httpx.post(
            f"{PLATFORM_API_URL}/internal/runs/{RUN_ID}/heartbeat",
            headers={"Authorization": f"Bearer {WORKER_SECRET}"},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Setup ApplyPilot from Runforge input payload
# ---------------------------------------------------------------------------

def setup_applypilot(input_payload: dict):
    """
    Configure ApplyPilot from the Runforge run input.

    Expected input_payload structure:
    {
        "profile": { ... },           // profile.json content
        "resume_text": "...",          // plain text resume
        "searches": { ... },           // searches.yaml content
        "stages": ["all"],             // which stages to run
        "min_score": 7,                // minimum fit score
        "workers": 2,                  // parallel workers for discovery
        "validation_mode": "normal"    // tailor validation mode
    }
    """
    from applypilot.config import APP_DIR, PROFILE_PATH, RESUME_PATH, SEARCH_CONFIG_PATH, ENV_PATH, ensure_dirs
    import yaml

    ensure_dirs()

    # Write profile.json
    if "profile" in input_payload:
        PROFILE_PATH.write_text(
            json.dumps(input_payload["profile"], indent=2),
            encoding="utf-8",
        )
        log.info("Wrote profile.json")

    # Write resume.txt
    if "resume_text" in input_payload:
        RESUME_PATH.write_text(input_payload["resume_text"], encoding="utf-8")
        log.info("Wrote resume.txt")

    # Write searches.yaml
    if "searches" in input_payload:
        SEARCH_CONFIG_PATH.write_text(
            yaml.dump(input_payload["searches"], default_flow_style=False),
            encoding="utf-8",
        )
        log.info("Wrote searches.yaml")

    # Write .env for LLM keys (already in env vars, but ApplyPilot reads from file too)
    env_lines = []
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_MODEL", "CAPSOLVER_API_KEY"):
        val = os.environ.get(key)
        if val:
            env_lines.append(f"{key}={val}")
    if env_lines:
        ENV_PATH.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        log.info("Wrote .env with %d keys", len(env_lines))


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

def main():
    """Main entry point when running inside Runforge container."""

    # Parse input payload
    input_raw = os.environ.get("INPUT_PAYLOAD", "{}")
    try:
        input_payload = json.loads(input_raw)
    except json.JSONDecodeError:
        log.error("Invalid INPUT_PAYLOAD JSON")
        report_run_status("failed", {"error": "Invalid INPUT_PAYLOAD JSON"})
        sys.exit(1)

    log.info("Starting ApplyPilot run (RUN_ID=%s)", RUN_ID)
    report_run_status("running")

    # Step 1: Setup
    report_step("setup", "running")
    try:
        setup_applypilot(input_payload)
        report_step("setup", "completed")
    except Exception as e:
        log.exception("Setup failed")
        report_step("setup", "failed", {"error": str(e)})
        report_run_status("failed", {"error": f"Setup failed: {e}"})
        sys.exit(1)

    # Step 2: Run the pipeline
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db, get_stats
    from applypilot.pipeline import run_pipeline

    load_env()
    ensure_dirs()
    init_db()

    stages = input_payload.get("stages", ["all"])
    min_score = input_payload.get("min_score", 7)
    workers = input_payload.get("workers", 1)
    validation_mode = input_payload.get("validation_mode", "normal")

    # Run each stage with reporting
    stage_list = ["discover", "enrich", "score", "tailor", "cover", "pdf"] if "all" in stages else stages

    for stage_name in stage_list:
        send_heartbeat()
        report_step(stage_name, "running")

        try:
            result = run_pipeline(
                stages=[stage_name],
                min_score=min_score,
                workers=workers,
                validation_mode=validation_mode,
            )

            stage_errors = result.get("errors", {})
            if stage_errors:
                report_step(stage_name, "failed", {"errors": stage_errors})
                log.error("Stage '%s' had errors: %s", stage_name, stage_errors)
            else:
                report_step(stage_name, "completed", {
                    "elapsed": result.get("elapsed", 0),
                })
                log.info("Stage '%s' completed in %.1fs", stage_name, result.get("elapsed", 0))

        except Exception as e:
            log.exception("Stage '%s' crashed", stage_name)
            report_step(stage_name, "failed", {"error": str(e)})
            # Continue to next stage — partial results are still valuable

    # Step 3: Collect and report results
    send_heartbeat()
    stats = get_stats()

    run_result = {
        "total_jobs_discovered": stats["total"],
        "jobs_with_description": stats["with_description"],
        "jobs_scored": stats["scored"],
        "jobs_tailored": stats["tailored"],
        "jobs_with_cover_letter": stats["with_cover_letter"],
        "jobs_ready_to_apply": stats["ready_to_apply"],
        "jobs_applied": stats["applied"],
        "score_distribution": stats["score_distribution"],
        "by_site": stats["by_site"],
    }

    log.info("Pipeline complete: %s", json.dumps(run_result, indent=2))

    # Upload the SQLite database as an artifact (for debugging)
    from applypilot.config import DB_PATH
    if DB_PATH.exists():
        report_artifact("applypilot.db", str(DB_PATH))

    # Upload tailored resumes as artifacts
    from applypilot.config import TAILORED_DIR
    if TAILORED_DIR.exists():
        for pdf_file in TAILORED_DIR.glob("*.pdf"):
            report_artifact(pdf_file.name, str(pdf_file))

    # Upload cover letters as artifacts
    from applypilot.config import COVER_LETTER_DIR
    if COVER_LETTER_DIR.exists():
        for pdf_file in COVER_LETTER_DIR.glob("*.pdf"):
            report_artifact(pdf_file.name, str(pdf_file))

    report_run_status("succeeded", run_result)
    log.info("Run complete. Exiting.")


if __name__ == "__main__":
    main()
```

### Key design decisions in the wrapper:

- **Stages run individually** (not as a single `run_pipeline(["all"])`) so each stage gets its own step report in the Runforge dashboard
- **Heartbeats** sent between stages to prevent timeout
- **Artifacts uploaded**: SQLite DB (for debugging), all tailored resumes (PDFs), all cover letters (PDFs)
- **The `apply` stage is NOT included in Phase 1** — it requires Claude Code CLI and approval flow, which comes in Phase 2 with SDK integration
- **Input payload** provides the user's profile, resume, and search configuration — the user configures this when triggering a run from the Runforge dashboard

---

## Step 4: Dockerfile CMD (historical — wrong for current repo)

**Current repo:** use the SDK worker only:

```dockerfile
CMD ["python", "-m", "agent_runtime", "worker", "agent:run_applypilot"]
```

Do **not** use `CMD ["python", "runforge_wrapper.py"]` — that bypassed the SDK and is removed.

---

## Step 5: Create a Runforge Project for ApplyPilot

This step is done through the Runforge API/dashboard, not in code. Document it for reference:

1. Create a new project in Runforge called "applypilot-job-agent"
2. Connect the forked GitHub repo
3. Set these environment variables in project settings:
   - `GEMINI_API_KEY` — free tier API key from aistudio.google.com
   - `APPLYPILOT_DIR` — `/root/.applypilot` (inside container)
4. Deploy — the platform builds the Docker image from the Dockerfile
5. Trigger a test run with this input payload:

```json
{
  "profile": {
    "first_name": "Krishna",
    "last_name": "...",
    "email": "...",
    "phone": "...",
    "location": "Lewisville, TX",
    "work_authorization": "authorized",
    "experience_years": 5,
    "skills": ["Python", "FastAPI", "AWS", "Docker", "AI/ML"],
    "resume_facts": ["Built Runforge - AI agent deployment platform", "..."]
  },
  "resume_text": "...(full resume text)...",
  "searches": {
    "searches": [
      {
        "title": "Software Engineer",
        "location": "Remote",
        "boards": ["indeed", "linkedin", "glassdoor"],
        "results_per_board": 25
      }
    ]
  },
  "stages": ["discover", "enrich", "score"],
  "min_score": 7,
  "workers": 2
}
```

Start with only `discover`, `enrich`, `score` stages first. This validates the pipeline works without needing Chrome or Claude Code.

---

## Step 6: Verify and Debug

After the first run completes, check:

1. **Dashboard**: Run should show steps: `setup → discover → enrich → score` with status for each
2. **Artifacts**: The `applypilot.db` SQLite file should be downloadable from the run detail page
3. **Run result**: Should show job counts (total discovered, scored, etc.)
4. **Logs**: Check `sudo journalctl -u agent-runtime-worker -f` on the EC2 server for container output

### Common issues to watch for:

- **python-jobspy import errors**: The `--no-deps` install may miss transitive deps. Check container logs for `ImportError` and install missing packages in the Dockerfile.
- **Gemini rate limits**: Free tier is 15 RPM. With many jobs to score, the LLM client will retry with backoff. This is normal but slow. Logs will show "LLM rate limited (HTTP 429). Waiting Xs."
- **SQLite path**: Make sure `APPLYPILOT_DIR` env var is set so the DB is created in the right place inside the container.
- **Network access**: The container needs outbound HTTPS to: `indeed.com`, `linkedin.com`, `glassdoor.com`, `ziprecruiter.com`, `google.com` (job search), `generativelanguage.googleapis.com` (Gemini API), Workday employer domains.

---

## Step 7: Test Tailoring Stage

Once discover/enrich/score works, add the `tailor` and `cover` stages:

```json
{
  "stages": ["discover", "enrich", "score", "tailor", "cover", "pdf"],
  "min_score": 7,
  "workers": 2,
  "validation_mode": "normal"
}
```

This will:
- Discover jobs
- Enrich descriptions
- Score each job 1-10
- For jobs scoring 7+: tailor the resume and generate a cover letter
- Convert to PDF

Check artifacts: tailored resume PDFs and cover letter PDFs should appear in the run detail.

---

## What Phase 1 Does NOT Include

These are explicitly deferred to Phase 2 (Runforge SDK integration):

1. **Auto-apply stage** — requires Claude Code CLI + Chrome + approval flow
2. **Runforge SDK steps** — `ctx.safe_step()`, `ctx.commit_step()`, etc.
3. **Approval before apply** — the key differentiator for Runforge
4. **Browserbase integration** — only needed for the apply stage
5. **Telegram notifications** — "Found 47 jobs, 12 scored 7+. Approve to apply?"
6. **Scheduled runs** — cron-based daily job search
7. **Shareable result cards** — "My agent found me 47 matching jobs"
8. **Explore page listing** — public showcase of the agent

---

## File Summary

Current fork layout (SDK path — `runforge_wrapper.py` removed):

```
ApplyPilot/
├── Dockerfile                  # CMD: agent_runtime worker agent:run_applypilot
├── agent.py                    # SDK agent + planned_steps
├── pyproject.toml              # entrypoint agent:run_applypilot
├── src/applypilot/             # UNCHANGED — no modifications to ApplyPilot core
│   ├── cli.py
│   ├── config.py
│   ├── database.py
│   ├── llm.py
│   ├── pipeline.py
│   ├── discovery/
│   ├── enrichment/
│   ├── scoring/
│   ├── apply/
│   ├── wizard/
│   └── ...
└── ...
```

**Critical rule: Do NOT modify any file under `src/applypilot/`.** The SDK agent in `agent.py` calls the package programmatically.

---

## Success Criteria

Phase 1 is complete when:

- [ ] Docker image builds successfully on EC2
- [ ] Container starts and runs `discover` stage, finding real jobs from job boards
- [ ] Container runs `enrich` stage, extracting full job descriptions
- [ ] Container runs `score` stage, scoring jobs with Gemini LLM
- [ ] Container runs `tailor` stage, generating tailored resumes
- [ ] Container runs `cover` stage, generating cover letters
- [ ] Container runs `pdf` stage, converting to PDF
- [ ] Each stage appears as a step in the Runforge dashboard run detail
- [ ] Tailored resume PDFs are downloadable as artifacts
- [ ] Cover letter PDFs are downloadable as artifacts
- [ ] SQLite DB is downloadable as artifact (for debugging)
- [ ] Run result shows job counts in the dashboard
- [ ] No modifications to `src/applypilot/` — integration is via `agent.py` only
