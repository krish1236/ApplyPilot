"""
Runforge/Prunforge wrapper for ApplyPilot.

Container entrypoint when running on Runforge. Reads config from env (RUN_ID,
INPUT_PAYLOAD, PLATFORM_API_URL, WORKER_SECRET, S3_*), sets up ApplyPilot,
runs the pipeline with per-stage step reporting, uploads artifacts to S3 and
registers them, and reports final status/result via PATCH.

Auth: X-Worker-Secret (platform internal API).
Artifacts: upload to S3 then POST with storage_url (no multipart upload).
"""

import json
import os
import sys
import logging
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("runforge_wrapper")

PLATFORM_API_URL = (os.environ.get("PLATFORM_API_URL") or "").rstrip("/")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
RUN_ID = os.environ.get("RUN_ID", "")

INTERNAL_PREFIX = f"{PLATFORM_API_URL}/internal" if PLATFORM_API_URL else ""


def _headers():
    h = {"Content-Type": "application/json"}
    if WORKER_SECRET:
        h["X-Worker-Secret"] = WORKER_SECRET
    return h


def report_step(step_index: int, step_name: str, status: str, log_excerpt: str | None = None, error_message: str | None = None):
    """Report a step to the platform. step_index is required (0=setup, 1=discover, ...)."""
    if not RUN_ID or not INTERNAL_PREFIX:
        log.warning("No RUN_ID or PLATFORM_API_URL — skipping step report")
        return
    try:
        payload = {
            "name": step_name,
            "step_type": "safe",
            "step_index": step_index,
            "status": status,
        }
        if log_excerpt is not None:
            payload["log_excerpt"] = log_excerpt
        if error_message is not None:
            payload["error_message"] = error_message
        httpx.post(
            f"{INTERNAL_PREFIX}/runs/{RUN_ID}/steps",
            headers=_headers(),
            json=payload,
            timeout=10,
        )
    except Exception as e:
        log.error("Failed to report step '%s': %s", step_name, e)


def _upload_file_to_s3(file_path: str, key: str, content_type: str) -> str | None:
    """Upload file to S3 using env S3_*. Returns storage_url or None."""
    import boto3
    from botocore.client import Config

    endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    access = os.environ.get("S3_ACCESS_KEY", "")
    secret = os.environ.get("S3_SECRET_KEY", "")
    bucket = os.environ.get("S3_BUCKET", "agent-runtime")
    if not endpoint or not bucket:
        return None
    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access or None,
            aws_secret_access_key=secret or None,
            region_name=os.environ.get("S3_REGION", "us-east-1"),
            config=Config(signature_version="s3v4"),
        )
        with open(file_path, "rb") as f:
            body = f.read()
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
        return f"{endpoint.rstrip('/')}/{bucket}/{key}"
    except Exception as e:
        log.error("S3 upload failed for %s: %s", file_path, e)
        return None


def report_artifact(name: str, file_path: str, step_name: str | None = None):
    """Upload file to S3 and register artifact with platform (storage_url)."""
    if not RUN_ID or not INTERNAL_PREFIX:
        return
    path = Path(file_path)
    if not path.exists():
        log.warning("Artifact file not found: %s", file_path)
        return
    key = f"artifacts/{RUN_ID}/{path.name}"
    content_type = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
    storage_url = _upload_file_to_s3(file_path, key, content_type)
    if not storage_url:
        return
    try:
        size = path.stat().st_size
        httpx.post(
            f"{INTERNAL_PREFIX}/runs/{RUN_ID}/artifacts",
            headers=_headers(),
            json={
                "name": name,
                "content_type": content_type,
                "storage_url": storage_url,
                "step_name": step_name,
                "size_bytes": size,
            },
            timeout=30,
        )
    except Exception as e:
        log.error("Failed to register artifact '%s': %s", name, e)


def report_run_status(status: str, result_payload: dict | None = None):
    """PATCH run status and/or result_payload (platform stores in Run.result_payload)."""
    if not RUN_ID or not INTERNAL_PREFIX:
        return
    try:
        body = {}
        if status:
            body["status"] = status
        if result_payload is not None:
            body["result_payload"] = result_payload
        if body:
            httpx.patch(
                f"{INTERNAL_PREFIX}/runs/{RUN_ID}",
                headers=_headers(),
                json=body,
                timeout=10,
            )
    except Exception as e:
        log.error("Failed to report run status: %s", e)


def send_heartbeat():
    if not RUN_ID or not INTERNAL_PREFIX:
        return
    try:
        httpx.post(
            f"{INTERNAL_PREFIX}/runs/{RUN_ID}/heartbeat",
            headers=_headers(),
            timeout=5,
        )
    except Exception:
        pass


def setup_applypilot(input_payload: dict):
    from applypilot.config import PROFILE_PATH, RESUME_PATH, SEARCH_CONFIG_PATH, ENV_PATH, ensure_dirs
    import yaml

    ensure_dirs()
    if "profile" in input_payload:
        PROFILE_PATH.write_text(json.dumps(input_payload["profile"], indent=2), encoding="utf-8")
        log.info("Wrote profile.json")
    if "resume_text" in input_payload:
        RESUME_PATH.write_text(input_payload["resume_text"], encoding="utf-8")
        log.info("Wrote resume.txt")
    if "searches" in input_payload:
        SEARCH_CONFIG_PATH.write_text(
            yaml.dump(input_payload["searches"], default_flow_style=False),
            encoding="utf-8",
        )
        log.info("Wrote searches.yaml")
    env_lines = []
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_MODEL", "CAPSOLVER_API_KEY"):
        val = os.environ.get(key)
        if val:
            env_lines.append(f"{key}={val}")
    if env_lines:
        ENV_PATH.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        log.info("Wrote .env with %d keys", len(env_lines))


def main():
    input_raw = os.environ.get("INPUT_PAYLOAD", "{}")
    try:
        input_payload = json.loads(input_raw)
    except json.JSONDecodeError:
        log.error("Invalid INPUT_PAYLOAD JSON")
        report_run_status("failed", {"error": "Invalid INPUT_PAYLOAD JSON"})
        sys.exit(1)

    log.info("Starting ApplyPilot run (RUN_ID=%s)", RUN_ID)
    report_run_status("running")

    # Step 0: Setup
    report_step(0, "setup", "running")
    try:
        setup_applypilot(input_payload)
        report_step(0, "setup", "completed")
    except Exception as e:
        log.exception("Setup failed")
        report_step(0, "setup", "failed", error_message=str(e))
        report_run_status("failed", {"error": f"Setup failed: {e}"})
        sys.exit(1)

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
    stage_list = ["discover", "enrich", "score", "tailor", "cover", "pdf"] if "all" in stages else stages

    for idx, stage_name in enumerate(stage_list, start=1):
        send_heartbeat()
        report_step(idx, stage_name, "running")
        try:
            result = run_pipeline(
                stages=[stage_name],
                min_score=min_score,
                workers=workers,
                validation_mode=validation_mode,
            )
            stage_errors = result.get("errors", {})
            if stage_errors:
                report_step(idx, stage_name, "failed", error_message=json.dumps(stage_errors)[:2000])
                log.error("Stage '%s' had errors: %s", stage_name, stage_errors)
            else:
                elapsed = result.get("elapsed", 0)
                report_step(idx, stage_name, "completed", log_excerpt=f"Elapsed: {elapsed:.1f}s")
                log.info("Stage '%s' completed in %.1fs", stage_name, elapsed)
        except Exception as e:
            log.exception("Stage '%s' crashed", stage_name)
            report_step(idx, stage_name, "failed", error_message=str(e))

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
        "score_distribution": stats.get("score_distribution"),
        "by_site": stats.get("by_site"),
    }
    log.info("Pipeline complete: %s", json.dumps(run_result, indent=2))

    from applypilot.config import DB_PATH, TAILORED_DIR, COVER_LETTER_DIR

    if DB_PATH.exists():
        report_artifact("applypilot.db", str(DB_PATH))
    if TAILORED_DIR.exists():
        for pdf_file in TAILORED_DIR.glob("*.pdf"):
            report_artifact(pdf_file.name, str(pdf_file), step_name="tailor")
    if COVER_LETTER_DIR.exists():
        for pdf_file in COVER_LETTER_DIR.glob("*.pdf"):
            report_artifact(pdf_file.name, str(pdf_file), step_name="cover")

    report_run_status("succeeded", run_result)
    log.info("Run complete. Exiting.")


if __name__ == "__main__":
    main()
