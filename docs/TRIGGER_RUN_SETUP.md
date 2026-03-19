# ApplyPilot on Platform — Step-by-Step Setup

Follow the README and Runforge instructions so the agent has your real profile, resume, and search config. Use this for **Trigger run** on the dashboard.

---

## Step 1: One-time local setup (ApplyPilot init)

On your machine, in the ApplyPilot repo (or any env with ApplyPilot installed):

```bash
# Python 3.11+ required
cd /path/to/ApplyPilot-main   # or clone your fork
python3.11 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows

pip install -e .
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex

# One-time wizard: creates ~/.applypilot/profile.json, resume.txt, searches.yaml, .env
applypilot init
```

Answer the wizard:

- **Resume:** Path to your resume (.txt or .pdf). If PDF, also provide a .txt version (needed for LLM).
- **Personal:** Full name, email, phone, city, state, country, LinkedIn/GitHub, etc.
- **Work authorization:** Legally authorized, need sponsorship?, etc.
- **Compensation:** Expected salary, currency, range.
- **Experience:** Current title, target role, years of experience, education level.
- **Skills:** Languages, frameworks, tools (comma-separated).
- **Resume facts:** Companies to preserve, projects, school, real metrics (never invented by AI).
- **Search config:** Target location, job titles, etc.

Then run:

```bash
applypilot doctor
```

Fix any missing items (e.g. API key in `.env`).

---

## Step 2: Get your profile and resume as JSON/text

After `applypilot init`, your data lives in `~/.applypilot/`:

- `profile.json` — full profile (personal, experience, skills_boundary, resume_facts, etc.)
- `resume.txt` — plain-text resume (this is what the agent uses for tailoring)
- `searches.yaml` — queries, locations, boards, defaults

**Option A — Copy from disk (recommended):**

```bash
# On Mac/Linux
cat ~/.applypilot/profile.json | jq . > /tmp/profile.json   # pretty-print
cat ~/.applypilot/resume.txt
cat ~/.applypilot/searches.yaml
```

**Option B — Export script:**

Create a small script that reads those three files and builds the trigger payload (see Step 4 structure).

---

## Step 3: Add API keys to the project (dashboard)

The agent needs an LLM key at runtime (Gemini or OpenAI). Add it as a **project secret** so it’s injected into the run:

1. In the dashboard: **Project → Settings → Environment variables** (or Secrets).
2. Add:
   - `GEMINI_API_KEY` = your key from [aistudio.google.com](https://aistudio.google.com), **or**
   - `OPENAI_API_KEY` = your OpenAI key if you use OpenAI instead.

Optional:

- `LLM_MODEL` — e.g. `gemini-2.0-flash` or `gpt-4o-mini`.
- `CAPSOLVER_API_KEY` — only if you use CapSolver for CAPTCHAs during apply.

---

## Step 4: Build the Trigger run input payload

When you click **Trigger run**, the dashboard asks for **Input JSON**. Use this shape (same as README + Runforge docs):

```json
{
  "profile": { ... },
  "resume_text": "...",
  "searches": { ... },
  "stages": ["discover", "enrich", "score", "tailor", "cover", "pdf"],
  "min_score": 7,
  "workers": 1,
  "validation_mode": "normal"
}
```

- **profile** — Paste the **entire** contents of `~/.applypilot/profile.json` (object). Do not use a minimal stub; tailoring and cover letters need full personal, experience, skills_boundary, resume_facts.
- **resume_text** — Full contents of `~/.applypilot/resume.txt` (string). This is the base resume the LLM tailors; one-liners will produce generic output.
- **searches** — Object that becomes `searches.yaml`. Discovery expects:
  - `queries`: list of `{ "query": "Job title", "tier": 1|2|3 }`
  - `locations`: list of `{ "location": "Remote" or "City, State", "remote": true|false }`
  - `sites`: list of boards, e.g. `["indeed", "linkedin"]` (code reads `sites`; if missing, defaults to indeed, linkedin, zip_recruiter)
  - `defaults`: `{ "results_per_site": 50, "hours_old": 72 }`
  - Optional: `location_accept`, `location_reject_non_remote`, `country`, `exclude_titles`
- **stages** — Which pipeline stages to run. Use all six for full pipeline, or e.g. `["discover"]` for a quick test.
- **min_score** — Only jobs with this score or higher proceed to tailor/cover (default 7).
- **workers** — Parallel workers for discover/enrich (default 1).
- **validation_mode** — `"normal"` or `"lenient"` (e.g. for Gemini free tier).

**Minimal valid example (discover-only test):**

```json
{
  "profile": { "personal": { "full_name": "Your Name", "email": "you@example.com" }, "experience": {}, "skills_boundary": {}, "resume_facts": {} },
  "resume_text": "Your full resume text here...",
  "searches": {
    "queries": [{ "query": "Software Engineer", "tier": 1 }],
    "locations": [{ "location": "Remote", "remote": true }],
    "sites": ["indeed"],
    "defaults": { "results_per_site": 10, "hours_old": 72 }
  },
  "stages": ["discover"]
}
```

For **real** tailored resumes and cover letters, use the full `profile` and full `resume_text` from Step 2.

---

## Step 5: Trigger the run

1. Open the project in the dashboard.
2. Click **Trigger run**.
3. Paste the JSON payload (from Step 4) into **Input JSON**.
4. Start the run.

The agent will:

1. **setup** — Write `profile.json`, `resume.txt`, `searches.yaml`, `.env` from payload + env.
2. **discover** — Scrape job boards per `searches`.
3. **enrich** — Fetch full job descriptions.
4. **score** — LLM scores jobs; only ≥ `min_score` continue.
5. **tailor** — LLM tailors your resume per job (uses profile + resume_text).
6. **cover** — LLM writes cover letters.
7. **pdf** — Generate PDFs.
8. **collect_results** — Attach DB and PDFs as artifacts, report stats.

---

## Step 6: Check results

- **Run detail page:** Steps, logs, and artifacts (e.g. `applypilot.db`, tailored PDFs, cover letters).
- **Result payload:** Summary counts (total_jobs_discovered, jobs_scored, jobs_tailored, etc.).

---

## Quick reference: where the agent gets data

| Data | Source |
|------|--------|
| Who you are, contact | `profile.personal` |
| Years of experience, education | `profile.experience` |
| Skills (allowed in tailoring) | `profile.skills_boundary` |
| Companies/school/metrics (never invented) | `profile.resume_facts` |
| Base resume text | `resume_text` (full plain-text resume) |
| Job search (titles, locations, boards) | `searches` (queries, locations, sites, defaults) |
| LLM API key | Project env var `GEMINI_API_KEY` or `OPENAI_API_KEY` |

If you skip `applypilot init` and pass a minimal stub, the pipeline runs but tailored resumes and cover letters will not reflect your real experience. **Do Step 1 and use that profile + resume_text in Step 4.**
