import os
import re
import json
import requests
from anthropic import Anthropic
from pathlib import Path
from dotenv import load_dotenv
from google import genai as _genai

load_dotenv(override=True)

# Database helper for user settings
def _get_user_salary_settings():
    """Fetch user's salary expectations from database."""
    try:
        from db_init import get_connection
        conn = get_connection()
        c = conn.cursor()

        current = c.execute("SELECT value FROM user_settings WHERE key = ?", ("current_salary",)).fetchone()
        target_min = c.execute("SELECT value FROM user_settings WHERE key = ?", ("target_salary_min",)).fetchone()
        target_max = c.execute("SELECT value FROM user_settings WHERE key = ?", ("target_salary_max",)).fetchone()

        conn.close()

        return {
            "current": float(current[0]) if current else 150000,
            "target_min": float(target_min[0]) if target_min else 180000,
            "target_max": float(target_max[0]) if target_max else 250000,
        }
    except Exception as e:
        print(f"Warning: Could not fetch user salary settings: {e}")
        return {
            "current": 150000,
            "target_min": 180000,
            "target_max": 250000,
        }

# Initialize Claude client
claude_client = Anthropic()

# Initialize Gemini client (lazy — only used if GEMINI_API_KEY is set)
_gemini_client = None

def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        key = os.getenv("GEMINI_API_KEY", "")
        if key:
            _gemini_client = _genai.Client(api_key=key)
    return _gemini_client

# Ollama configuration
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "mistral"

# Career-ops context paths
ROOT = Path(__file__).parent
PATHS = {
    "shared": ROOT / "modes" / "_shared.md",
    "oferta": ROOT / "modes" / "oferta.md",
    "cv": ROOT / "cv.md",
}

def read_file(path, label=""):
    """Read file with fallback."""
    if not path.exists():
        return f"[{label} not found]"
    return path.read_text().strip()

def get_career_context():
    """Load career-ops context files."""
    return {
        "shared": read_file(PATHS["shared"], "modes/_shared.md"),
        "oferta": read_file(PATHS["oferta"], "modes/oferta.md"),
        "cv": read_file(PATHS["cv"], "cv.md"),
    }

# ============================================================================
# CLAUDE INTEGRATION (Scanning)
# ============================================================================

def claude_scan_jobs(criteria: dict) -> dict:
    """
    Use Claude to scan and filter job opportunities.

    Args:
        criteria: dict with 'keywords', 'location', 'min_salary', etc.

    Returns:
        dict with 'jobs' list and 'status'
    """
    try:
        prompt = f"""
You are a job search assistant. Scan for jobs matching these criteria:
- Keywords: {', '.join(criteria.get('keywords', ['AI Engineer']))}
- Locations: {', '.join(criteria.get('locations', ['Remote', 'San Francisco']))}
- Min Salary: ${criteria.get('min_salary', 150000):,}
- Experience: {criteria.get('experience', '5+')} years

Based on your knowledge, provide a JSON list of {criteria.get('num_results', 10)} realistic job opportunities matching these criteria.
For URLs: provide the company's main careers page URL (e.g., https://careers.dbs.com, https://careers.grab.com) since specific job posting URLs expire quickly. Do NOT guess specific job posting URLs.

Format as JSON only, no markdown:
[
  {{"company": "Company Name", "title": "Job Title", "url": "https://careers.company.com", "salary": "SGD X-Y", "location": "City", "description": "Brief 2-3 sentence description"}},
  ...
]
"""

        message = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = message.content[0].text

        # Clean up response - strip markdown code blocks if present
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            # Remove opening ```json or ``` and closing ```
            cleaned = cleaned.split("\n", 1)[-1]  # Remove first line
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]  # Remove last ```
        cleaned = cleaned.strip()

        # Extract JSON array if there's text before/after it
        if not cleaned.startswith("["):
            start = cleaned.find("[")
            end = cleaned.rfind("]") + 1
            if start != -1 and end != 0:
                cleaned = cleaned[start:end]

        # Parse JSON from response
        jobs = json.loads(cleaned)

        return {
            "status": "success",
            "jobs": jobs,
            "count": len(jobs)
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "jobs": []
        }

# ============================================================================
# OLLAMA INTEGRATION (Evaluation)
# ============================================================================

def ollama_evaluate_job(job_description: str, model: str = OLLAMA_MODEL) -> dict:
    """
    Use Ollama/Mistral to evaluate a job description.

    Args:
        job_description: Full JD text
        model: Ollama model to use (default: mistral)

    Returns:
        dict with evaluation results
    """
    # Guard: refuse to evaluate if the JD is essentially empty
    stripped = job_description.strip() if job_description else ""
    if len(stripped) < 80:
        return {
            "status": "error",
            "error": (
                "⚠️ Job description is too short to evaluate reliably "
                f"({len(stripped)} chars). Please paste the full JD from the company's "
                "career page before evaluating."
            )
        }

    try:
        context = get_career_context()

        # ── Pre-fetch salary benchmark via Claude API (for Block D) ──────────
        salary_context = ""
        if check_claude_api():
            try:
                role_title = job_description[:200]  # first 200 chars usually contain the title
                salary_msg = claude_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=400,
                    messages=[{"role": "user", "content":
                        f"What is the typical total compensation (base salary + bonus) in Singapore dollars "
                        f"for the following role in Singapore in 2024-2025? Give a realistic range for "
                        f"senior/VP level. Be specific with SGD numbers. Role context:\n\n{role_title}\n\n"
                        f"Reply in 3-4 sentences maximum. State ranges clearly (e.g. SGD 180k–240k base)."}]
                )
                salary_context = salary_msg.content[0].text.strip()
            except Exception:
                salary_context = ""

        comp_block = (
            f"\n═══════════════════════════════════════════════════════\n"
            f"COMPENSATION BENCHMARK (pre-fetched via web research)\n"
            f"═══════════════════════════════════════════════════════\n"
            f"{salary_context}\n"
            f"Use this as your primary source for Block D. Do NOT say web search is unavailable.\n"
        ) if salary_context else (
            "\nNo compensation benchmark pre-fetched. Use your training data for Block D estimates.\n"
        )

        system_prompt = f"""You are a job evaluation assistant. Respond in ENGLISH ONLY.

CANDIDATE CV:
---
{context['cv']}
---
{comp_block}
CANDIDATE FACTS (do not deviate from these):
- Name: Chen Yingkai, 43 years old, Singapore
- Total career: ~15 years (NOT 20+)
- Background: PRODUCT MANAGEMENT only. He is NOT an engineer or developer.
- He is the business-IT bridge — understands technology but does not build it.
- Domain: retail banking, mobile/internet banking, payments, fraud, AML/KYC
- Current: VP / Deputy Chief Product Owner, OCBC Bank (Dec 2022–present)
- No experience in: wealth management, investment products, private banking, trading platforms, insurance, B2B SaaS

TASK: Evaluate the job description below against the candidate CV. Write all 7 blocks in English.

BLOCK A — Candidate Resume Analysis
Summarise the candidate's actual experience from the CV above. Reference specific roles and skills. Do not invent anything not in the CV.

BLOCK B — Job Description Analysis
Summarise what the role requires: key skills, domain knowledge, seniority level.

BLOCK C — Fit Assessment
Compare the candidate's actual experience against the job requirements. Flag any CRITICAL GAPS explicitly — especially if the role requires domain knowledge the candidate does not have (e.g. wealth management, investment platforms). Do not paper over gaps.

BLOCK D — Compensation & Market Research
{f"Use this pre-fetched benchmark: {salary_context}" if salary_context else "Estimate typical SGD salary range for this role and seniority in Singapore."}

BLOCK E — Growth & Learning Opportunities
What could the candidate learn or gain from this role?

BLOCK F — Risk Factors & Red Flags
List any concerns: domain gaps, culture fit, seniority mismatch, red flags in the JD.

BLOCK G — Legitimacy & Company Research
Assess whether this appears to be a real, active job posting based on the JD text.

SCORING (calculate carefully):
- Start at 5.0
- Subtract 0.5 for each red flag in Block F
- Subtract 0.3 for each fit concern in Block C
- Subtract 1.0 if there is a core domain mismatch (e.g. role needs wealth management experience, candidate has none)
- Final score must be between 0.0 and 5.0

End your response with exactly this block:

---SCORE_SUMMARY---
COMPANY: <company name>
ROLE: <role title>
SCORE: <decimal score>
ARCHETYPE: <role type>
LEGITIMACY: <High Confidence | Proceed with Caution | Suspicious>
---END_SUMMARY---
"""

        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": f"{system_prompt}\n\nJOB DESCRIPTION TO EVALUATE:\n\n{job_description}",
                "stream": False,
                "temperature": 0.2,  # Lower temperature = more deterministic, less creative deviations
            },
            timeout=120
        )

        if response.status_code != 200:
            return {
                "status": "error",
                "error": f"Ollama error ({response.status_code}): {response.text}"
            }

        data = response.json()
        evaluation_text = data.get("response", "")

        # Parse score summary (handle multiple formats)
        summary = {
            "SCORE": 0,
            "COMPANY": "Unknown",
            "ROLE": "Unknown",
            "LEGITIMACY": "Unknown",
            "ARCHETYPE": "Unknown"
        }

        # Try multiple delimiters (---SCORE_SUMMARY---, ## SCORE_SUMMARY---, etc.)
        summary_text = evaluation_text

        # Try standard format
        if "---SCORE_SUMMARY---" in evaluation_text:
            try:
                summary_text = evaluation_text.split("---SCORE_SUMMARY---")[1].split("---END_SUMMARY---")[0]
            except:
                pass
        # Try markdown header format
        elif "## SCORE_SUMMARY" in evaluation_text or "## SCORE_SUMMARY---" in evaluation_text:
            try:
                idx = evaluation_text.find("## SCORE_SUMMARY")
                summary_text = evaluation_text[idx+16:]  # Skip "## SCORE_SUMMARY"
                if "---END_SUMMARY---" in summary_text:
                    summary_text = summary_text.split("---END_SUMMARY---")[0]
            except:
                pass
        # Try any SCORE_SUMMARY variant
        elif "SCORE_SUMMARY" in evaluation_text:
            try:
                idx = evaluation_text.find("SCORE_SUMMARY")
                summary_text = evaluation_text[idx+13:]
                if "---END_SUMMARY---" in summary_text:
                    summary_text = summary_text.split("---END_SUMMARY---")[0]
            except:
                pass

        # Parse key-value pairs from summary
        for line in summary_text.strip().split("\n"):
            if ": " in line:
                key, value = line.split(": ", 1)
                key = key.strip().replace("#", "").strip()
                value = value.strip()
                # Normalize key names
                if key.upper() in ["SCORE", "COMPANY", "ROLE", "LEGITIMACY", "ARCHETYPE"]:
                    summary[key.upper()] = value

        # Try to extract score from anywhere in text if SCORE_SUMMARY failed
        if summary.get("SCORE") == 0 or summary.get("SCORE") == "0" or isinstance(summary.get("SCORE"), str):
            import re
            # Look for "SCORE:" pattern anywhere in text (numeric only)
            score_match = re.search(r'SCORE[:\s]+(\d+\.?\d*)', evaluation_text)
            if score_match:
                try:
                    summary["SCORE"] = float(score_match.group(1))
                except:
                    summary["SCORE"] = 2.5  # Default middle score if extraction fails
            elif "not applicable" in str(summary.get("SCORE", "")).lower():
                summary["SCORE"] = 2.5  # Default score when Ollama says "not applicable"
            else:
                summary["SCORE"] = 0

        # Convert score to float and cap at 5.0
        try:
            score_val = float(summary.get("SCORE", 0))
            # Cap score to 5.0 max
            score_val = min(score_val, 5.0)
        except:
            score_val = 0.0

        return {
            "status": "success",
            "evaluation": evaluation_text,
            "score": score_val,
            "company": str(summary.get("COMPANY", "Unknown")),
            "role": str(summary.get("ROLE", "Unknown")),
            "legitimacy": str(summary.get("LEGITIMACY", "Unknown")),
            "archetype": str(summary.get("ARCHETYPE", "Unknown")),
            "model": model
        }

    except requests.exceptions.ConnectionError:
        return {
            "status": "error",
            "error": "Cannot connect to Ollama. Make sure 'ollama serve' is running on http://localhost:11434"
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

def check_ollama_health() -> bool:
    """Check if Ollama is running."""
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        return response.status_code == 200
    except:
        return False

def check_claude_api() -> bool:
    """Check if Claude API key is set."""
    return bool(os.getenv("ANTHROPIC_API_KEY"))

def check_gemini_api() -> bool:
    """Check if Gemini API key is set."""
    return bool(os.getenv("GEMINI_API_KEY"))


def claude_evaluate_job(job_description: str, model: str = "claude-haiku-4-5-20251001") -> dict:
    """
    Use Claude API to evaluate a job description — same 7-block A-G format as Ollama,
    but with much better instruction-following.

    Args:
        job_description: Full JD text
        model: Claude model to use (haiku = cheap, sonnet = higher quality)

    Returns:
        dict with evaluation results (same shape as ollama_evaluate_job)
    """
    stripped = job_description.strip() if job_description else ""
    if len(stripped) < 80:
        return {
            "status": "error",
            "error": (
                "⚠️ Job description is too short to evaluate reliably "
                f"({len(stripped)} chars). Please paste the full JD first."
            )
        }

    try:
        context = get_career_context()
        salary_settings = _get_user_salary_settings()

        # Pre-fetch salary benchmark (skip for Sonnet — it knows this already)
        salary_context = ""
        if "haiku" in model:
            try:
                salary_msg = claude_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    messages=[{"role": "user", "content":
                        f"What is the typical SGD salary range (base + bonus) for a senior/VP-level "
                        f"role in Singapore matching this context? Be specific with numbers. "
                        f"Reply in 3 sentences max.\n\n{job_description[:300]}"}]
                )
                salary_context = salary_msg.content[0].text.strip()
            except Exception:
                salary_context = ""

        comp_note = (
            f"\nSalary benchmark (pre-fetched): {salary_context}\n"
            if salary_context else
            "\nUse your knowledge to provide SGD salary estimates for Block D.\n"
        )

        prompt = _build_eval_prompt(job_description, context, comp_note, salary_settings)

        message = claude_client.messages.create(
            model=model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}]
        )

        return _parse_score_summary(message.content[0].text, model)

    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Shared helpers ───────────────────────────────────────────────────────────

def _parse_score_summary(evaluation_text: str, model: str) -> dict:
    """Parse the ---SCORE_SUMMARY--- block from any LLM's evaluation output."""
    summary = {"SCORE": 0, "COMPANY": "Unknown", "ROLE": "Unknown",
               "LEGITIMACY": "Unknown", "ARCHETYPE": "Unknown"}

    # Try exact delimiters first
    summary_text = None
    if "---SCORE_SUMMARY---" in evaluation_text:
        try:
            summary_text = evaluation_text.split("---SCORE_SUMMARY---")[1]
            if "---END_SUMMARY---" in summary_text:
                summary_text = summary_text.split("---END_SUMMARY---")[0]
        except Exception:
            summary_text = None

    # If exact delimiters don't work, look for markdown section or "SCORE_SUMMARY" pattern
    if not summary_text and "SCORE_SUMMARY" in evaluation_text:
        try:
            idx = evaluation_text.find("SCORE_SUMMARY")
            # Find where the summary section starts (usually after a heading)
            summary_start = evaluation_text.rfind("\n", 0, idx) + 1
            # Look for content after SCORE_SUMMARY, ideally until next section or end
            content = evaluation_text[idx:]
            # Find the end — either next markdown heading (##, ###, ###) or end of text
            next_section = len(content)
            for pattern in ["\n##", "\n---", "\n\nRecommen"]:
                pos = content.find(pattern)
                if pos != -1:
                    next_section = min(next_section, pos)
            summary_text = content[:next_section]
        except Exception:
            summary_text = None

    # Parse key-value pairs from summary
    if summary_text:
        try:
            for line in summary_text.strip().split("\n"):
                # Remove markdown formatting (* and ** for bold)
                clean = re.sub(r"[*_`]", "", line).strip()
                if ": " in clean:
                    key, value = clean.split(": ", 1)
                    key = key.strip().replace("-", "").strip().upper()
                    if key in summary:
                        summary[key] = value.strip()
        except Exception:
            pass

    try:
        score_val = min(float(str(summary.get("SCORE", 0)).strip()), 5.0)
    except Exception:
        score_val = 0.0

    return {
        "status": "success",
        "evaluation": evaluation_text,
        "score": score_val,
        "company": str(summary.get("COMPANY", "Unknown")),
        "role": str(summary.get("ROLE", "Unknown")),
        "legitimacy": str(summary.get("LEGITIMACY", "Unknown")),
        "archetype": str(summary.get("ARCHETYPE", "Unknown")),
        "model": model,
    }


def _build_eval_prompt(job_description: str, context: dict, comp_note: str, salary_settings: dict = None) -> str:
    """Build the shared 7-block evaluation prompt used by Claude and Gemini."""
    if salary_settings is None:
        salary_settings = _get_user_salary_settings()

    return f"""You are a career evaluation assistant. Evaluate the job description below against this candidate's CV.
Respond entirely in English.

CANDIDATE CV:
---
{context['cv']}
---

CANDIDATE FACTS:
- Name: Chen Yingkai, 43 years old, Singapore
- Career span: ~15 years total
- Background: PRODUCT MANAGEMENT only. NOT an engineer or developer.
- He bridges business and IT — understands technology but does not build it.
- Domain expertise: retail banking, mobile/internet banking, payments, fraud prevention, AML/KYC
- Current role: VP / Deputy Chief Product Owner, OCBC Bank (Dec 2022–present)
- Prior: AVP PO FRANK App (OCBC), Sr PM PingAn OneConnect, AVP Retail Digital UOB (2011–2020), HSBC
- Studying Generative AI at NUS (2025–2026)
- NO experience in: wealth management, investment products, private banking, trading platforms, insurance, B2B SaaS
- Current annual compensation: ~SGD {salary_settings['current']:,.0f} (base + bonus)
- Target range for next role: SGD {salary_settings['target_min']:,.0f}–{salary_settings['target_max']:,.0f} total (minimum SGD {salary_settings['current']:,.0f})
{comp_note}
Write a concise 7-block evaluation (3-5 bullet points per block max). Be accurate and honest — flag gaps clearly, do not be optimistic about poor fits. Keep total response under 1500 words so the SCORE_SUMMARY block always fits.

## Block A: Candidate Resume Analysis
Summarise the candidate's relevant experience from the CV above. Be specific — cite actual roles and skills. Do not invent anything.

## Block B: Job Description Analysis
Summarise the key requirements, domain knowledge needed, and seniority level expected.

## Block C: Fit Assessment
Compare candidate experience against job requirements point by point. Call out any CRITICAL GAPS explicitly — especially missing domain knowledge.

## Block D: Compensation & Market Research
State the realistic SGD salary range for this role at market rate. Compare against the candidate's current ~SGD {salary_settings['current']:,.0f}/yr and target of SGD {salary_settings['target_min']:,.0f}–{salary_settings['target_max']:,.0f}. Flag clearly if this role likely pays below SGD {salary_settings['current']:,.0f} (red flag) or above SGD {salary_settings['target_max']:,.0f} (upside).

## Block E: Growth & Learning Opportunities
What can the candidate genuinely gain from this role?

## Block F: Risk Factors & Red Flags
List concerns honestly: domain gaps, culture signals, role mismatch, anything in the JD that warrants caution.

## Block G: Legitimacy & Company Research
Assess whether this is a real, active opportunity based on the JD text alone.
- **Legitimate**: Real job from established company; JD is specific and professional
- **Verify**: Real job but has red flags (vague JD, unclear contact, suspicious urgency, overpromising salary)
- **Fraudulent**: Likely a scam (job board spam, overly generic, asking for upfront fees, obvious typos/poor grammar, unrealistic salary)

SCORING:
- Start at 5.0
- Subtract 0.5 per red flag in Block F
- Subtract 0.3 per fit concern in Block C
- Subtract 1.0 if there is a core domain mismatch (e.g. role needs wealth management, candidate has none)
- Cap at 5.0 minimum 0.0

End with exactly this block:

---SCORE_SUMMARY---
COMPANY: <company name>
ROLE: <role title>
SCORE: <decimal>
ARCHETYPE: <role type>
LEGITIMACY: <Legitimate | Verify | Fraudulent>
---END_SUMMARY---

JOB DESCRIPTION:
{job_description}"""


def _build_quick_screen_prompt(job_title: str, description: str, cv_text: str) -> str:
    """Build the shared quick-screen prompt used by Claude, Gemini, and Ollama."""
    return f"""You are a career advisor doing a rapid relevance screen.

CANDIDATE SNAPSHOT:
- 43-year-old, Singapore. VP-level Product Owner in banking with 15 years experience.
- Core expertise: digital banking platforms, mobile/internet banking, payments, fraud prevention,
  digital risk management, AML/KYC, regulatory risk, digital identity.
- Currently studying AI — interested in AI product roles at the intersection of banking and technology.
- Targets: Product Owner, Digital Product Manager, Platform Owner, Payments/Fraud Product,
  AI Product Manager, Head of Product, Digital Transformation Lead.
- Avoids: financial advisory/sales, clinical roles, entry-level, part-time positions.
- Current comp: ~SGD 150K/yr. Minimum acceptable: SGD 150K/yr.

CV (for additional context):
{cv_text[:2000]}

JOB TO SCREEN:
Title: {job_title}
Description: {description[:800] if description else '(no description available)'}

Rate on TWO dimensions combined into one score 1–5:
- ROLE FIT: Does the role match the candidate's expertise and targets?
- CULTURE/ENVIRONMENT FIT: Do the signals in the JD suggest a good environment for this person?

5 = Excellent fit on both role and environment
4 = Strong match — minor gaps in role or environment
3 = Moderate — relevant domain but notable concerns (culture, seniority, scope)
2 = Weak — limited relevance or clear culture mismatch
1 = Poor — unrelated role or clearly wrong environment

Reply in this exact format, nothing else:
SCORE: <number>
REASON: <one sentence covering both role and environment fit>"""


def _parse_quick_screen_text(text: str) -> dict:
    """Parse SCORE/REASON from quick screen response."""
    score = 0.0
    reason = ""
    for line in text.strip().splitlines():
        clean = re.sub(r"[*_`]", "", line).strip()
        if clean.upper().startswith("SCORE:"):
            try:
                score = float(clean.split(":", 1)[1].strip())
                score = max(1.0, min(5.0, score))
            except Exception:
                pass
        elif clean.upper().startswith("REASON:"):
            reason = clean.split(":", 1)[1].strip()
    if score == 0:
        m = re.search(r'\b([1-5])\b', text)
        score = float(m.group(1)) if m else 2.5
    return {"status": "success", "score": score, "reason": reason}


# ── Gemini functions ─────────────────────────────────────────────────────────

def gemini_evaluate_job(job_description: str, model: str = "gemini-2.5-flash") -> dict:
    """
    Use Gemini to evaluate a job description — same 7-block A-G format as Claude.
    """
    stripped = job_description.strip() if job_description else ""
    if len(stripped) < 80:
        return {
            "status": "error",
            "error": (
                f"⚠️ Job description is too short to evaluate reliably "
                f"({len(stripped)} chars). Please paste the full JD first."
            )
        }

    try:
        client = _get_gemini_client()
        if not client:
            return {"status": "error", "error": "Gemini API key not configured"}

        context = get_career_context()
        salary_settings = _get_user_salary_settings()
        comp_note = "\nUse your knowledge to provide SGD salary estimates for Block D.\n"
        prompt = _build_eval_prompt(job_description, context, comp_note, salary_settings)

        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        evaluation_text = response.text
        return _parse_score_summary(evaluation_text, model)

    except Exception as e:
        return {"status": "error", "error": str(e)}


def gemini_quick_screen(job_title: str, description: str,
                        model: str = "gemini-2.5-flash") -> dict:
    """
    Fast profile-relevance screen using Gemini. Returns score 1-5 + one-line reason.
    """
    try:
        client = _get_gemini_client()
        if not client:
            return {"status": "error", "error": "Gemini API key not configured"}

        context = get_career_context()
        prompt = _build_quick_screen_prompt(job_title, description, context["cv"])

        response = client.models.generate_content(model=model, contents=prompt)
        return _parse_quick_screen_text(response.text)

    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Claude quick screen ──────────────────────────────────────────────────────

def claude_quick_screen(job_title: str, description: str,
                        model: str = "claude-haiku-4-5-20251001") -> dict:
    """
    Fast profile-relevance screen using Claude. Returns score 1-5 + one-line reason.
    """
    try:
        context = get_career_context()
        prompt = _build_quick_screen_prompt(job_title, description, context["cv"])

        message = claude_client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return _parse_quick_screen_text(message.content[0].text)

    except Exception as e:
        return {"status": "error", "error": str(e)}


def ollama_quick_screen(job_title: str, description: str,
                        model: str = OLLAMA_MODEL) -> dict:
    """
    Fast profile-relevance screen using Ollama.
    Returns a score 1-5 and a one-line reason.
    Much faster than full A-G evaluation — used to rank scan results.
    """
    try:
        context = get_career_context()
        cv_text  = context["cv"]

        prompt = f"""You are a career advisor doing a rapid relevance screen.

CANDIDATE SNAPSHOT:
- 43-year-old male, Singapore. Agile Product Owner in banking with 10+ years experience.
- Core expertise: digital banking platforms, mobile/internet banking, payments, fraud prevention,
  digital risk management, regulatory risk, app development, anti-fraud operations, digital identity.
- Currently studying AI — strongly interested in AI product roles at the intersection of banking and technology.
- Values: autonomy, psychological safety, meaningful work, continuous learning, less bureaucracy.
- Targets: Product Owner, Digital Product Manager, Platform Owner, Payments/Fraud Product roles,
  AI Product Manager, Innovation Lead, Head of Product, Digital Transformation Lead.
- Avoids: pure financial advisory/sales, clinical roles, heavily hierarchical/compliance-heavy cultures,
  entry-level or part-time positions.
- Work environment: prefers collaborative, growth-oriented, human-centred teams. Dislikes
  rigid process-heavy cultures that separate business and technology.

CV (for additional context):
{cv_text[:2000]}

JOB TO SCREEN:
Title: {job_title}
Description: {description[:800] if description else '(no description available)'}

Rate on TWO dimensions combined into one score 1–5:
- ROLE FIT: Does the role match the candidate's expertise and targets?
- CULTURE/ENVIRONMENT FIT: Do the signals in the JD suggest a good environment for this person?

5 = Excellent fit on both role and environment
4 = Strong match — minor gaps in role or environment
3 = Moderate — relevant domain but notable concerns (culture, seniority, scope)
2 = Weak — limited relevance or clear culture mismatch
1 = Poor — unrelated role or clearly wrong environment

Reply in this exact format, nothing else:
SCORE: <number>
REASON: <one sentence covering both role and environment fit>"""

        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model":       model,
                "prompt":      prompt,
                "stream":      False,
                "temperature": 0.2,
            },
            timeout=60
        )

        if response.status_code != 200:
            return {"status": "error", "error": f"Ollama {response.status_code}"}

        text = response.json().get("response", "")

        score  = 0.0
        reason = ""
        for line in text.strip().splitlines():
            if line.startswith("SCORE:"):
                try:
                    score = float(line.split(":", 1)[1].strip())
                    score = max(1.0, min(5.0, score))
                except:
                    pass
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()

        if score == 0:
            import re
            m = re.search(r'\b([1-5])\b', text)
            score = float(m.group(1)) if m else 2.5

        return {"status": "success", "score": score, "reason": reason}

    except requests.exceptions.ConnectionError:
        return {"status": "error", "error": "Ollama not running"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def verify_job_url(url: str) -> dict:
    """
    Verify if a job posting URL is accessible and valid.

    Args:
        url: Job posting URL

    Returns:
        dict with 'valid', 'status_code', 'message'
    """
    if not url:
        return {
            "valid": False,
            "status_code": None,
            "message": "No URL provided"
        }

    try:
        # Add timeout and user agent to avoid being blocked
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.head(url, timeout=10, headers=headers, allow_redirects=True)

        # Check if response is successful (200-299 range)
        is_valid = 200 <= response.status_code < 400

        status_messages = {
            200: "✅ URL is valid and accessible",
            301: "⚠️ URL redirects (may be valid)",
            302: "⚠️ URL redirects (may be valid)",
            404: "❌ Page not found (dead link)",
            403: "❌ Access forbidden",
            500: "❌ Server error",
            503: "❌ Service unavailable"
        }

        message = status_messages.get(response.status_code, f"Status: {response.status_code}")

        return {
            "valid": is_valid,
            "status_code": response.status_code,
            "message": message
        }

    except requests.exceptions.Timeout:
        return {
            "valid": False,
            "status_code": None,
            "message": "❌ URL timeout (slow/unreachable)"
        }
    except requests.exceptions.ConnectionError:
        return {
            "valid": False,
            "status_code": None,
            "message": "❌ Connection failed (invalid/unreachable)"
        }
    except Exception as e:
        return {
            "valid": False,
            "status_code": None,
            "message": f"❌ Error: {str(e)[:50]}"
        }
