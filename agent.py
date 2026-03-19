"""ApplyPilot — Runforge Agent Wrapper (SDK).

Entrypoint: agent:run_applypilot
Wraps ApplyPilot's 6-stage pipeline in Runforge SDK steps (safe_step, artifact, log).
No modifications to src/applypilot/.
"""

import json
import os
from pathlib import Path

from agent_runtime import AgentRuntime

runtime = AgentRuntime()


def _setup_applypilot(input_payload: dict) -> None:
    """Write ApplyPilot config files from the run input payload.

    ApplyPilot expects files in APPLYPILOT_DIR (~/.applypilot or env):
      - profile.json, resume.txt, searches.yaml, .env (LLM keys)
    """
    import yaml
    from applypilot.config import (
        APP_DIR,
        PROFILE_PATH,
        RESUME_PATH,
        SEARCH_CONFIG_PATH,
        ENV_PATH,
        ensure_dirs,
    )

    ensure_dirs()

    if "profile" in input_payload:
        PROFILE_PATH.write_text(
            json.dumps(input_payload["profile"], indent=2),
            encoding="utf-8",
        )

    if "resume_text" in input_payload:
        RESUME_PATH.write_text(input_payload["resume_text"], encoding="utf-8")

    if "searches" in input_payload:
        SEARCH_CONFIG_PATH.write_text(
            yaml.dump(input_payload["searches"], default_flow_style=False),
            encoding="utf-8",
        )

    env_lines = []
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_MODEL", "CAPSOLVER_API_KEY"):
        val = os.environ.get(key)
        if val:
            env_lines.append(f"{key}={val}")
    if env_lines:
        ENV_PATH.write_text("\n".join(env_lines) + "\n", encoding="utf-8")


def _run_stage(
    stage_name: str,
    min_score: int = 7,
    workers: int = 1,
    validation_mode: str = "normal",
) -> dict:
    """Run a single ApplyPilot pipeline stage."""
    from applypilot.pipeline import run_pipeline
    return run_pipeline(
        stages=[stage_name],
        min_score=min_score,
        workers=workers,
        validation_mode=validation_mode,
    )


@runtime.agent(name="applypilot-job-agent")
def run_applypilot(ctx, input: dict):
    """Main agent. Runs ApplyPilot stages as Runforge safe steps."""
    min_score = input.get("min_score", 7)
    workers = input.get("workers", 1)
    validation_mode = input.get("validation_mode", "normal")
    stages = input.get(
        "stages",
        ["discover", "enrich", "score", "tailor", "cover", "pdf"],
    )

    with ctx.safe_step("setup"):
        _setup_applypilot(input)
        from applypilot.config import load_env, ensure_dirs
        from applypilot.database import init_db
        load_env()
        ensure_dirs()
        init_db()
        ctx.log("ApplyPilot configured and database initialized")

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
            ctx.state[f"{stage_name}_completed"] = True
            ctx.state[f"{stage_name}_elapsed"] = elapsed

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
            "score_distribution": stats.get("score_distribution"),
            "by_site": stats.get("by_site"),
        }
        ctx.log(f"Results: {json.dumps(ctx.state['results'])}")

        if DB_PATH.exists():
            ctx.artifact(
                "applypilot.db",
                DB_PATH.read_bytes(),
                content_type="application/x-sqlite3",
            )
        if TAILORED_DIR.exists():
            for pdf in TAILORED_DIR.glob("*.pdf"):
                ctx.artifact(pdf.name, pdf.read_bytes(), content_type="application/pdf")
        if COVER_LETTER_DIR.exists():
            for pdf in COVER_LETTER_DIR.glob("*.pdf"):
                ctx.artifact(
                    f"cover_{pdf.name}",
                    pdf.read_bytes(),
                    content_type="application/pdf",
                )

    return ctx.state["results"]


if __name__ == "__main__":
    runtime.serve()
