"""ApplyPilot — Runforge Agent Wrapper (SDK).

Entrypoint: agent:run_applypilot
Wraps ApplyPilot's 6-stage pipeline in Runforge SDK steps (safe_step, artifact, log).
No modifications to src/applypilot/.
"""

import json
import os
import shutil
from pathlib import Path

from agent_runtime import AgentRuntime

from build_config import _read_resume, build_config as build_applypilot_config

runtime = AgentRuntime()

_DEFAULT_STAGES = ["discover", "enrich", "score", "tailor", "cover", "pdf"]


def _applypilot_planned_steps(inp: dict) -> list[str]:
    """Match run_applypilot: setup → each stage from input (or defaults) → collect_results."""
    stages = inp.get("stages", list(_DEFAULT_STAGES))
    if isinstance(stages, str):
        stages = [stages]
    return ["setup", *list(stages), "collect_results"]


def _parse_experience_json_strings(d: dict) -> None:
    """Turn dashboard textarea JSON into objects before setup / stages (Runforge experience inputs)."""
    for src, dest in (
        ("profile_json", "profile"),
        ("searches_json", "searches"),
    ):
        raw = d.get(src)
        if isinstance(raw, str) and raw.strip():
            try:
                d[dest] = json.loads(raw)
            except json.JSONDecodeError:
                pass

    stj = d.get("stages_json")
    if isinstance(stj, str) and stj.strip():
        try:
            parsed = json.loads(stj)
            if isinstance(parsed, list):
                d["stages"] = parsed
        except json.JSONDecodeError:
            pass

    st = d.get("stages")
    if isinstance(st, str) and st.strip().startswith("["):
        try:
            parsed = json.loads(st)
            if isinstance(parsed, list):
                d["stages"] = parsed
        except json.JSONDecodeError:
            pass


def _merge_experience_into_effective(effective: dict) -> None:
    """Apply legacy JSON fields, then build_config() for Runforge form → ApplyPilot shape."""
    _parse_experience_json_strings(effective)
    cfg = build_applypilot_config(effective)
    if not isinstance(effective.get("profile"), dict):
        effective["profile"] = cfg["profile"]
    if not isinstance(effective.get("searches"), dict):
        effective["searches"] = cfg["searches"]
    rt = effective.get("resume_text")
    if not (isinstance(rt, str) and rt.strip()) and cfg.get("resume_text"):
        effective["resume_text"] = cfg["resume_text"]
    st = effective.get("stages")
    if not isinstance(st, list) or len(st) == 0:
        effective["stages"] = cfg["stages"]
    try:
        effective["min_score"] = int(effective.get("min_score", cfg["min_score"]))
    except (TypeError, ValueError):
        effective["min_score"] = int(cfg["min_score"])
    try:
        effective["workers"] = int(effective.get("workers", cfg.get("workers", 1)))
    except (TypeError, ValueError):
        effective["workers"] = int(cfg.get("workers", 1))
    effective["validation_mode"] = str(
        effective.get("validation_mode", cfg.get("validation_mode", "normal"))
    )


def _apply_experience_inputs(input_payload: dict) -> None:
    """Map uploaded resume file path into ApplyPilot resume.txt / resume.pdf."""
    from applypilot.config import RESUME_PATH, RESUME_PDF_PATH

    resume_path = input_payload.get("resume")
    if isinstance(resume_path, str) and resume_path.strip():
        p = Path(resume_path)
        if p.is_file():
            suf = p.suffix.lower()
            if suf == ".pdf":
                RESUME_PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
                RESUME_PDF_PATH.write_bytes(p.read_bytes())
            else:
                RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
                try:
                    RESUME_PATH.write_text(
                        p.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                except UnicodeDecodeError:
                    RESUME_PATH.write_bytes(p.read_bytes())


def _materialize_applypilot_workspace(ctx, effective: dict) -> None:
    """Ensure ~/.applypilot (APPLYPILOT_DIR) has resume.txt, profile.json, searches.yaml.

    The scorer reads RESUME_PATH (resume.txt). Uploaded Runforge paths may differ; build_config
    provides resume_path / resume_text after merge.
    """
    import yaml
    from applypilot.config import PROFILE_PATH, RESUME_PATH, SEARCH_CONFIG_PATH, ensure_dirs

    ensure_dirs()
    cfg = build_applypilot_config(effective)
    ctx.log(f"DEBUG materialize: effective.get('resume')={effective.get('resume')!r}")
    ctx.log(f"DEBUG materialize: config resume_path={cfg.get('resume_path')!r}")
    _cfg_rt = cfg.get("resume_text") or ""
    ctx.log(
        f"DEBUG materialize: config resume_text length={len(_cfg_rt) if isinstance(_cfg_rt, str) else 0}"
    )

    resume_path = cfg.get("resume_path")
    if not (isinstance(resume_path, str) and resume_path.strip()):
        r = effective.get("resume")
        resume_path = r.strip() if isinstance(r, str) and r.strip() else None
    else:
        resume_path = resume_path.strip()

    if resume_path and Path(resume_path).is_file():
        suf = Path(resume_path).suffix.lower()
        RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
        if suf == ".pdf" or suf == ".docx":
            text = _read_resume(resume_path) or ""
            RESUME_PATH.write_text(text, encoding="utf-8")
        else:
            try:
                RESUME_PATH.write_text(
                    Path(resume_path).read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            except UnicodeDecodeError:
                shutil.copy2(resume_path, RESUME_PATH)
    else:
        text = effective.get("resume_text") if isinstance(effective.get("resume_text"), str) else ""
        if not text.strip():
            text = cfg.get("resume_text") or ""
        if isinstance(text, str) and text.strip():
            RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESUME_PATH.write_text(text, encoding="utf-8")

    profile = effective.get("profile")
    if not isinstance(profile, dict):
        profile = cfg.get("profile")
    if isinstance(profile, dict):
        PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")

    searches = effective.get("searches")
    if not isinstance(searches, dict):
        searches = cfg.get("searches")
    if isinstance(searches, dict):
        SEARCH_CONFIG_PATH.write_text(
            yaml.dump(searches, default_flow_style=False),
            encoding="utf-8",
        )


def _reset_failed_scores_for_rescoring() -> int:
    """Clear all scores so the scorer can re-run every job with the current resume on disk."""
    from applypilot.database import get_connection

    conn = get_connection()
    reset_count = conn.execute(
        """
        UPDATE jobs
        SET fit_score = NULL, score_reasoning = NULL, scored_at = NULL
        WHERE scored_at IS NOT NULL
        """
    ).rowcount
    conn.commit()
    return reset_count


def _setup_applypilot(ctx, input_payload: dict) -> None:
    """Write ApplyPilot config files from the run input payload.

    ApplyPilot expects files in APPLYPILOT_DIR (~/.applypilot or env):
      - profile.json, resume.txt, searches.yaml, .env (LLM keys)
    """
    from applypilot.config import ENV_PATH, ensure_dirs

    ensure_dirs()
    _apply_experience_inputs(input_payload)
    _materialize_applypilot_workspace(ctx, input_payload)

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
    effective = {**(input or {}), **dict(ctx.inputs)}
    _merge_experience_into_effective(effective)

    min_score = effective.get("min_score", 7)
    workers = effective.get("workers", 1)
    validation_mode = effective.get("validation_mode", "normal")
    stages = effective.get("stages", list(_DEFAULT_STAGES))
    if not isinstance(stages, list):
        stages = list(_DEFAULT_STAGES)

    with ctx.safe_step("setup"):
        from applypilot.config import DB_PATH, load_env, ensure_dirs
        from applypilot.database import close_connection, init_db

        # DEBUG: container resume path / mount (remove after diagnosing dashboard runs)
        resume_path = ctx.inputs.get("resume", "MISSING")
        ctx.log(f"DEBUG: resume_path={resume_path!r}")
        _can_stat = (
            resume_path not in (None, "", "MISSING")
            and isinstance(resume_path, str)
        )
        ctx.log(
            f"DEBUG: exists={os.path.exists(resume_path) if _can_stat else 'N/A'}"
        )
        ctx.log(
            f"DEBUG: /run-inputs contents={os.listdir('/run-inputs') if os.path.exists('/run-inputs') else 'DIR NOT FOUND'}"
        )
        if os.path.exists("/run-inputs/resume"):
            ctx.log(
                f"DEBUG: /run-inputs/resume contents={os.listdir('/run-inputs/resume')}"
            )

        # Order: DB restore + init first, then materialize workspace (resume.txt, profile, …).
        # There is no Pipeline constructed here — ApplyPilot's pipeline runs per stage — and
        # run_scoring() reads RESUME_PATH from disk once at the start of the score stage.
        # Writing resume.txt after restore avoids any chance of an older empty resume lingering
        # as the "last write" before scoring in odd edge cases; .env from setup must load after
        # _setup_applypilot writes keys.
        ensure_dirs()
        close_connection()
        restored = ctx.storage.get_file("applypilot.db")
        if restored:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            DB_PATH.write_bytes(restored)
            ctx.log(
                f"Storage restore: loaded applypilot.db ({len(restored):,} bytes) from persistent storage"
            )
        else:
            ctx.log("Storage restore: no previous applypilot.db found (first run or cleared storage)")

        init_db()
        _setup_applypilot(ctx, effective)
        load_env()
        ensure_dirs()
        ctx.log("ApplyPilot configured and database initialized (schema ensured)")

    from applypilot.database import get_stats

    for stage_name in stages:
        with ctx.safe_step(stage_name):
            stats_before = get_stats()
            ctx.log(f"Starting {stage_name}...")

            if stage_name == "score":
                reset_n = _reset_failed_scores_for_rescoring()
                if reset_n > 0:
                    ctx.log(
                        f"Cleared prior scores on {reset_n} job(s); re-scoring with current resume"
                    )

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
        from applypilot.database import get_connection

        conn = get_connection()
        min_s = int(min_score)

        # Stats aligned with agent.yaml outputs.summary.metrics
        summary_stats = {
            "total_discovered": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "with_description": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
            ).fetchone()[0],
            "scored": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND fit_score > 0"
            ).fetchone()[0],
            "high_match": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND fit_score >= ?",
                (min_s,),
            ).fetchone()[0],
            "tailored": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
            ).fetchone()[0],
            "applied": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
            ).fetchone()[0],
        }

        extra = get_stats()
        ctx.log(
            f"Jobs discovered: {summary_stats['total_discovered']} | "
            f"With description: {summary_stats['with_description']} | "
            f"Scored (>0): {summary_stats['scored']} | "
            f"High fit (≥{min_s}): {summary_stats['high_match']} | "
            f"Tailored: {summary_stats['tailored']} | "
            f"Applied: {summary_stats['applied']} | "
            f"Cover letters: {extra.get('with_cover_letter', 0)} | "
            f"Ready to apply: {extra.get('ready_to_apply', 0)}"
        )

        ctx.state["results"] = {
            **summary_stats,
            "total_jobs_discovered": summary_stats["total_discovered"],
            "jobs_with_description": summary_stats["with_description"],
            "jobs_scored": summary_stats["scored"],
            "jobs_tailored": summary_stats["tailored"],
            "jobs_applied": summary_stats["applied"],
            "jobs_with_cover_letter": extra.get("with_cover_letter", 0),
            "jobs_ready_to_apply": extra.get("ready_to_apply", 0),
            "score_distribution": extra.get("score_distribution"),
            "by_site": extra.get("by_site"),
        }
        try:
            ctx.results.set_stats(dict(summary_stats))
        except Exception:
            pass

        try:
            rows = conn.execute(
                """
                SELECT url, title, salary, location, site, fit_score, apply_status
                FROM jobs
                WHERE fit_score IS NOT NULL AND fit_score > 0
                ORDER BY fit_score DESC
                LIMIT 100
                """
            ).fetchall()
            jobs_table = []
            for row in rows:
                url, title, salary, location, site, fit_score, apply_status = row
                title_s = (title or "").strip()
                company = ""
                if " at " in title_s:
                    parts = title_s.split(" at ", 1)
                    if len(parts) == 2:
                        company = parts[1].strip()
                jobs_table.append(
                    {
                        "company": company,
                        "title": title_s,
                        "fit_score": fit_score,
                        "salary": salary or "",
                        "location": location or "",
                        "source": site or "",
                        "apply_status": apply_status or "—",
                        "url": url,
                    }
                )
            ctx.results.set_table("jobs", jobs_table)
        except Exception:
            pass

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
