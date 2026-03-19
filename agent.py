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

_DEFAULT_STAGES = ["discover", "enrich", "score", "tailor", "cover", "pdf"]


def _applypilot_planned_steps(inp: dict) -> list[str]:
    """Match run_applypilot: setup → each stage from input (or defaults) → collect_results."""
    stages = inp.get("stages", list(_DEFAULT_STAGES))
    if isinstance(stages, str):
        stages = [stages]
    return ["setup", *list(stages), "collect_results"]


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


@runtime.agent(name="applypilot-job-agent", planned_steps=_applypilot_planned_steps)
def run_applypilot(ctx, input: dict):
    """Main agent. Runs ApplyPilot stages as Runforge safe steps."""
    min_score = input.get("min_score", 7)
    workers = input.get("workers", 1)
    validation_mode = input.get("validation_mode", "normal")
    stages = input.get("stages", list(_DEFAULT_STAGES))

    with ctx.safe_step("setup"):
        _setup_applypilot(input)
        from applypilot.config import DB_PATH, load_env, ensure_dirs
        from applypilot.database import init_db

        # Phase E: restore persistent DB snapshot from Runforge storage.
        restored = ctx.storage.get_file("applypilot.db")
        if restored:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            DB_PATH.write_bytes(restored)
            ctx.log(
                f"Storage restore: loaded applypilot.db ({len(restored):,} bytes) from persistent storage"
            )
        else:
            ctx.log("Storage restore: no previous applypilot.db found (first run or cleared storage)")

        load_env()
        ensure_dirs()
        init_db()
        ctx.log("ApplyPilot configured and database initialized (schema ensured)")

    from applypilot.database import get_stats

    for stage_name in stages:
        with ctx.safe_step(stage_name):
            stats_before = get_stats()
            ctx.log(f"Starting {stage_name}...")

            result = _run_stage(
                stage_name,
                min_score=min_score,
                workers=workers,
                validation_mode=validation_mode,
            )

            stats_after = get_stats()
            elapsed = result.get("elapsed", 0)
            errors = result.get("errors", {})

            ctx.log(f"Stage '{stage_name}' completed in {elapsed:.1f}s")
            if errors:
                ctx.log(f"Stage errors: {errors}")

            # Per-stage summary for live feedback
            total = stats_after["total"]
            new_in_stage = stats_after["total"] - stats_before["total"]
            if stage_name == "discover":
                ctx.log(f"Discovery: {total} total jobs (+{new_in_stage} new)")
            elif stage_name == "enrich":
                ctx.log(f"Enriched: {stats_after['with_description']} jobs with full description")
            elif stage_name == "score":
                eligible = stats_after.get("untailored_eligible", 0) or stats_after.get("tailored", 0)
                ctx.log(f"Scored: {stats_after['scored']} jobs | High fit (≥{min_score}): {eligible} eligible for tailoring")
            elif stage_name == "tailor":
                ctx.log(f"Tailored: {stats_after['tailored']} resumes")
            elif stage_name == "cover":
                ctx.log(f"Cover letters: {stats_after['with_cover_letter']}")
            elif stage_name == "pdf":
                ctx.log(f"PDFs ready: {stats_after['ready_to_apply']}")

            ctx.state[f"{stage_name}_completed"] = True
            ctx.state[f"{stage_name}_elapsed"] = elapsed

    with ctx.safe_step("collect_results"):
        from applypilot.config import DB_PATH, TAILORED_DIR, COVER_LETTER_DIR

        stats = get_stats()
        ctx.log(f"Jobs discovered: {stats['total']} | With description: {stats['with_description']} | Scored: {stats['scored']} | Tailored: {stats['tailored']} | Cover letters: {stats['with_cover_letter']} | Ready to apply: {stats['ready_to_apply']}")
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
        ctx.log("Results collected; see run result payload and artifacts.")

        if DB_PATH.exists():
            db_bytes = DB_PATH.read_bytes()
            ctx.storage.put_file(
                "applypilot.db",
                db_bytes,
                content_type="application/x-sqlite3",
            )
            ctx.log(
                f"Storage persist: saved applypilot.db ({len(db_bytes):,} bytes) to persistent storage"
            )
            ctx.artifact(
                "applypilot.db",
                db_bytes,
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
