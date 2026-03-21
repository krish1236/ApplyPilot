# build_config.py — Translation layer for Runforge experience inputs → ApplyPilot internal config.
# Maps platform form fields into profile.json / searches.yaml shape consumed by agent.py.

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def build_config(inputs: dict[str, Any], db_path: str = "applypilot.db") -> dict[str, Any]:
    """
    Translate platform form inputs → ApplyPilot internal config.

    Platform may provide:
        inputs["full_name"], inputs["resume"] (file path), inputs["job_sources"] (list), ...
    ApplyPilot expects profile, searches, resume_text, stages, min_score, etc.
    """
    target_roles = _coerce_str_list(inputs.get("target_roles", "Software Engineer"))
    if not target_roles:
        target_roles = ["Software Engineer"]
    locations_raw = _coerce_str_list(inputs.get("locations", "Remote"))
    if not locations_raw:
        locations_raw = ["Remote"]

    profile = {
        "personal": {
            "full_name": inputs.get("full_name", "") or "",
            "email": inputs.get("email", "") or "",
            "phone": inputs.get("phone", "") or "",
            "city": inputs.get("city", "") or "",
            "province_state": "",
            "country": "USA",
            "linkedin_url": inputs.get("linkedin_url", "") or "",
            "github_url": inputs.get("github_url", "") or "",
        },
        "experience": {
            "years_of_experience_total": str(inputs.get("years_experience", "5")),
            "education_level": str(inputs.get("education", "Bachelor's in Computer Science")),
            "current_title": str(inputs.get("current_title", "Software Engineer")),
            "target_role": target_roles[0] if target_roles else "Software Engineer",
        },
        "skills_boundary": {
            "programming_languages": _coerce_str_list(inputs.get("programming_languages", "")),
            "frameworks": _coerce_str_list(inputs.get("frameworks", "")),
            "tools": _coerce_str_list(inputs.get("tools", "")),
        },
        "resume_facts": {
            "preserved_companies": [],
            "preserved_projects": [],
            "preserved_school": "",
            "real_metrics": [],
        },
    }

    queries: list[dict[str, Any]] = []
    for i, role in enumerate(target_roles):
        queries.append({"query": role, "tier": 1 if i < 3 else 2})

    locations: list[dict[str, Any]] = []
    for loc in locations_raw:
        s = loc.lower().strip()
        is_remote = s in ("remote", "remote only", "anywhere")
        locations.append({"location": loc.strip(), "remote": is_remote})

    source_map = {
        "indeed": "indeed",
        "linkedin": "linkedin",
        "workday": "workday",
        "greenhouse": "greenhouse",
        "lever": "lever",
    }
    user_sources = inputs.get("job_sources", ["workday", "indeed"])
    if isinstance(user_sources, str):
        user_sources = _coerce_str_list(user_sources)
    if not isinstance(user_sources, list):
        user_sources = ["workday", "indeed"]
    sites = [
        source_map.get(str(s).lower().strip(), str(s).strip())
        for s in user_sources
        if str(s).strip()
    ]

    searches: dict[str, Any] = {
        "queries": queries,
        "locations": locations,
        "sites": sites or ["indeed"],
        "defaults": {
            "results_per_site": 25,
            "hours_old": 72,
        },
    }

    resume_path = inputs.get("resume")
    if isinstance(resume_path, str) and resume_path.strip():
        resume_text = _read_resume(resume_path.strip())
    else:
        resume_text = str(inputs.get("resume_text", "") or "")

    stages = ["discover", "enrich", "score"]

    try:
        min_score = int(inputs.get("min_score", "7"))
    except (TypeError, ValueError):
        min_score = 7

    return {
        "database_path": db_path,
        "profile": profile,
        "searches": searches,
        "resume_text": resume_text,
        "resume_path": resume_path if isinstance(resume_path, str) else None,
        "stages": stages,
        "min_score": min_score,
        "workers": int(inputs.get("workers", 1) or 1),
        "validation_mode": str(inputs.get("validation_mode", "normal") or "normal"),
        "avoid": _split(str(inputs.get("avoid", "") or "")),
        "min_salary": str(inputs.get("min_salary", "") or ""),
        "experience_level": str(inputs.get("experience_level", "senior") or "senior"),
    }


def _split(text: str) -> list[str]:
    if not text:
        return []
    return [x.strip() for x in text.split(",") if x.strip()]


def _coerce_str_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return _split(str(val or ""))


def _read_resume(file_path: str) -> str:
    """Extract text from uploaded resume file (best effort; optional deps)."""
    if not file_path or not os.path.exists(file_path):
        return ""

    p = Path(file_path)
    suf = p.suffix.lower()

    if suf == ".txt":
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    if suf == ".pdf":
        try:
            import pdfplumber

            with pdfplumber.open(file_path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        except ImportError:
            pass
        try:
            import PyPDF2

            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                return "\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            return ""

    if suf == ".docx":
        try:
            import docx

            document = docx.Document(file_path)
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        except ImportError:
            return ""

    return ""
