import re
import time
import streamlit as st
import streamlit.components.v1 as components
import sqlite3
import pandas as pd
from datetime import datetime
from pathlib import Path

from db_init import init_db, get_connection
from api_integration import (
    claude_scan_jobs,
    ollama_evaluate_job,
    claude_evaluate_job,
    gemini_evaluate_job,
    ollama_quick_screen,
    claude_quick_screen,
    gemini_quick_screen,
    check_ollama_health,
    check_claude_api,
    check_gemini_api,
    verify_job_url
)
from job_scraper import scrape_jobs, COMPANY_GROUPS

# ============================================================================
# JOB RELEVANCE CLASSIFIER
# ============================================================================

# Hard block — drop these regardless of anything else
_BLOCK_TITLES = [
    # Medical / clinical
    "doctor", "physician", "nurse", "pharmacist", "surgeon", "dentist",
    "optometrist", "therapist", "physiotherapist", "radiographer", "dietitian",
    "audiologist", "medical officer", "clinical",
    # Pure financial advisory / sales
    "wealth advisor", "wealth adviser",          # both spellings
    "wealth manager",
    "financial advisor", "financial adviser",    # both spellings
    "financial planner",
    "insurance agent", "insurance advisor", "bancassurance",
    "relationship manager",          # almost always sales in SG banks
    "private banker", "remisier",
    # Employment type noise
    "part time", "part-time", "internship", "intern ",
    "[entry level]", "entry level",
]

# Core target titles — these are your primary role targets
_CORE_TARGETS = [
    "product owner", "product manager", "product director",
    "product management lead", "product management head",
    "digital product",
    "platform owner", "platform manager",
    "payments product", "fraud product", "digital identity",
    "ai product", "head of product", "vp product", "chief product",
    "innovation lead", "innovation manager",
    "digital transformation", "squad lead", "chapter lead",
    "digital banking lead", "digital banking product",
]

# Adjacent titles — worth reviewing, possibly relevant
_ADJACENT = [
    "digital", "agile", "fintech", "transformation",
    "technology lead", "tech lead", "programme manager",
    "delivery manager", "business analyst", "solution owner",
    "innovation", "platform", "product",
]


def classify_job(title: str) -> tuple[str, str]:
    """
    Returns (tier, reason) where tier is:
      'block'    — hard exclude (noise)
      'core'     — directly relevant role
      'adjacent' — worth reviewing
      'low'      — unclear relevance
    """
    t = title.lower()

    for kw in _BLOCK_TITLES:
        if kw in t:
            return ("block", f"Excluded: matches '{kw}'")

    for kw in _CORE_TARGETS:
        if kw in t:
            return ("core", f"Core target: '{kw}'")

    for kw in _ADJACENT:
        if kw in t:
            return ("adjacent", f"Adjacent: '{kw}'")

    return ("low", "Low title relevance")

# ============================================================================
# PAGE CONFIG
# ============================================================================
st.set_page_config(
    page_title="Career-Ops",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize database
init_db()

# ============================================================================
# HELPER — scrollable iframe table (works regardless of Streamlit container)
# ============================================================================
def render_scroll_table(headers: list, rows: list, height: int = 440):
    """Render a scrollable table in a components.html iframe with styled scrollbar."""
    th_cells = "".join(f"<th>{h}</th>" for h in headers)
    tr_cells  = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    html = f"""
    <html><head><style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0e1117; font-family: sans-serif; font-size: 0.87rem; color: #cfcfcf; }}
    .wrap {{
        height: {height}px;
        overflow-y: scroll;
        overflow-x: auto;
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 10px;
    }}
    .wrap::-webkit-scrollbar          {{ width: 14px; height: 14px; }}
    .wrap::-webkit-scrollbar-track    {{ background: #1a1a2e; border-radius: 10px; }}
    .wrap::-webkit-scrollbar-thumb    {{ background: #4a9eff; border-radius: 10px;
                                         border: 3px solid #1a1a2e; }}
    .wrap::-webkit-scrollbar-thumb:hover {{ background: #74b9ff; }}
    table  {{ width: 100%; border-collapse: collapse; }}
    thead th {{
        position: sticky; top: 0; z-index: 2;
        background: #0e1117; color: #e0e0e0;
        padding: 10px 14px; text-align: left;
        border-bottom: 2px solid #4a9eff;
        white-space: nowrap;
    }}
    tbody tr:hover {{ background: rgba(74,158,255,0.09); }}
    tbody td {{
        padding: 8px 14px;
        border-bottom: 1px solid rgba(255,255,255,0.07);
        vertical-align: top;
    }}
    </style></head>
    <body>
      <div class="wrap">
        <table><thead><tr>{th_cells}</tr></thead><tbody>{tr_cells}</tbody></table>
      </div>
    </body></html>
    """
    components.html(html, height=height + 4, scrolling=False)

# ============================================================================
# GLOBAL STYLES — prominent scrollbars + table polish
# ============================================================================
st.markdown("""
<style>
/* ── Page & sidebar scrollbars ─────────────────────────────────────── */
::-webkit-scrollbar          { width: 14px; height: 14px; }
::-webkit-scrollbar-track    { background: #1a1a2e; border-radius: 10px; }
::-webkit-scrollbar-thumb    { background: #4a9eff; border-radius: 10px;
                                border: 3px solid #1a1a2e; }
::-webkit-scrollbar-thumb:hover { background: #74b9ff; }
* { scrollbar-width: thin; scrollbar-color: #4a9eff #1a1a2e; }

/* ── Scrollable HTML table containers ──────────────────────────────── */
.scroll-table-wrap {
    height: 460px !important;
    overflow-y: scroll !important;
    overflow-x: auto !important;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px;
    margin-bottom: 1rem;
    display: block !important;
}
.scroll-table-wrap::-webkit-scrollbar          { width: 14px !important; height: 14px !important; }
.scroll-table-wrap::-webkit-scrollbar-track    { background: #1a1a2e !important; border-radius: 10px; }
.scroll-table-wrap::-webkit-scrollbar-thumb    { background: #4a9eff !important; border-radius: 10px;
                                                  border: 3px solid #1a1a2e !important; }
.scroll-table-wrap::-webkit-scrollbar-thumb:hover { background: #74b9ff !important; }

.scroll-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
.scroll-table thead th {
    position: sticky; top: 0; z-index: 2;
    background: #0e1117; color: #e0e0e0;
    padding: 10px 14px; text-align: left;
    border-bottom: 2px solid #4a9eff;
    white-space: nowrap;
}
.scroll-table tbody tr { transition: background 0.15s; }
.scroll-table tbody tr:hover { background: rgba(74,158,255,0.08); }
.scroll-table tbody td {
    padding: 8px 14px;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    color: #cfcfcf;
    vertical-align: top;
}
.badge {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 0.75rem; font-weight: 600;
}
.badge-eval  { background: #1a6b3c; color: #6fcf97; }
.badge-pend  { background: #4a3700; color: #f0b429; }
</style>
""", unsafe_allow_html=True)

# ============================================================================
# SIDEBAR
# ============================================================================
st.sidebar.title("🎯 Career-Ops")

# Health checks
ollama_ok  = check_ollama_health()
claude_ok  = check_claude_api()
gemini_ok  = check_gemini_api()

hc1, hc2, hc3 = st.sidebar.columns(3)
with hc1:
    st.sidebar.metric("Ollama", "✅" if ollama_ok else "❌", help="Local models")
with hc2:
    st.sidebar.metric("Claude", "✅" if claude_ok else "❌", help="Claude API")
with hc3:
    st.sidebar.metric("Gemini", "✅" if gemini_ok else "❌", help="Gemini API")

st.sidebar.divider()

# Navigation
page = st.sidebar.radio(
    "Navigation",
    ["🔍 Search", "📋 My Jobs", "📊 Dashboard", "⚙️ Settings"]
)

st.sidebar.divider()

# Stats
conn = get_connection()
c = conn.cursor()
c.execute("SELECT COUNT(*) FROM jobs")
total_jobs = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM evaluations")
total_evals = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM applications WHERE status='applied'")
applied = c.fetchone()[0]
conn.close()

st.sidebar.metric("Jobs Discovered", total_jobs)
st.sidebar.metric("Jobs Evaluated", total_evals)
st.sidebar.metric("Applied", applied)

st.sidebar.divider()

# Clear unevaluated jobs button
if st.sidebar.button("🗑️ Clear Unevaluated Jobs", width="stretch"):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM jobs WHERE id NOT IN (SELECT job_id FROM evaluations)")
    count = c.fetchone()[0]

    if count > 0:
        c.execute("DELETE FROM jobs WHERE id NOT IN (SELECT job_id FROM evaluations)")
        conn.commit()
        st.sidebar.success(f"✅ Deleted {count} unevaluated jobs")
        time.sleep(1)
        st.rerun()
    else:
        st.sidebar.info("ℹ️ No unevaluated jobs to delete")
    conn.close()

# Cache clearing button
if st.sidebar.button("🔄 Clear Cache", width="stretch"):
    st.cache_data.clear()
    st.cache_resource.clear()
    st.success("✅ Cache cleared! Refresh the page.")
    time.sleep(1)
    st.rerun()

# ============================================================================
# PAGE: SEARCH
# ============================================================================
if page == "🔍 Search":
    st.header("🔍 Search & Scrape Jobs")
    st.caption("🕷️ Live scraping from real job boards and company career pages using Playwright")

    # ── Keywords & result count ──────────────────────────────────────────────
    # Pre-defined search terms
    predefined_terms = [
        "AI Product Manager Banking",
        "Digital Banking Platform",
        "AI Product Leadership",
        "AI Transformation",
        "Digital Risk Management",
        "Anti-Fraud & Compliance"
    ]

    dd_col, kw_col, cnt_col = st.columns([2, 2, 1])

    with dd_col:
        selected_term = st.selectbox(
            "Quick Select",
            options=["Custom..."] + predefined_terms,
            help="Choose a pre-defined search term or select Custom to enter your own"
        )

    with kw_col:
        # If a predefined term is selected, use it; otherwise show text input
        if selected_term != "Custom...":
            keywords = st.text_input(
                "Job Keywords",
                value=selected_term,
                help="Job title or keywords to search across all selected sources"
            )
        else:
            keywords = st.text_input(
                "Job Keywords",
                "Senior Manager",
                help="Job title or keywords to search across all selected sources"
            )

    with cnt_col:
        num_results = st.slider(
            "Max Results",
            min_value=5, max_value=50, value=15,
            help="Total jobs returned across all selected sources"
        )

    st.divider()

    # ── Job Board sources ────────────────────────────────────────────────────
    _all_boards = ["MyCareersFuture", "Indeed", "JobStreet", "LinkedIn", "Glassdoor", "Glints", "Tech in Asia"]
    _all_direct  = ["DBS", "Grab", "Sea Group", "Airwallex", "Thought Machine", "Thunes", "Anthropic"]
    _all_mcf     = ["OCBC", "UOB", "Standard Chartered", "Citibank", "HSBC", "Wise", "Nium", "Revolut", "Singtel"]
    _all_gov     = ["HTX", "MAS", "IMDA", "CSA"]
    _all_careers_gov = ["GovTech"]  # Separate list for agencies using Careers@Gov (Workday)

    def _toggle_boards():
        val = st.session_state["select_all_boards"]
        for b in _all_boards:
            st.session_state[f"board_{b}"] = val

    def _toggle_companies():
        val = st.session_state["select_all_companies"]
        for c in _all_direct:
            st.session_state[f"co_{c}"] = val
        for c in _all_mcf:
            st.session_state[f"co_{c}"] = val
        for c in _all_gov:
            st.session_state[f"co_{c}"] = val
        for c in _all_careers_gov:
            st.session_state[f"co_{c}"] = val

    hdr_col, all_col = st.columns([5, 1])
    with hdr_col:
        st.subheader("📰 Public Job Boards")
        st.caption("Search across public job portals. Scraped directly from each site.")
    with all_col:
        st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
        st.checkbox("All", value=False, key="select_all_boards", on_change=_toggle_boards)

    _board_defaults = {}  # All job boards unchecked by default — use Company Direct instead
    board_cols1 = st.columns(4)
    board_cols2 = st.columns(4)
    _board_cols = {
        "MyCareersFuture": board_cols1[0], "Indeed": board_cols1[1],
        "JobStreet": board_cols1[2],       "LinkedIn": board_cols1[3],
        "Glassdoor": board_cols2[0],       "Glints": board_cols2[1],
        "Tech in Asia": board_cols2[2],
    }
    sources = []
    for board, col in _board_cols.items():
        with col:
            if st.checkbox(board, value=_board_defaults.get(board, False), key=f"board_{board}"):
                sources.append(board)

    if "LinkedIn" in sources or "Glassdoor" in sources or "Glints" in sources or "Tech in Asia" in sources:
        st.caption("⚠️ LinkedIn & Glassdoor have bot-detection — results may vary run to run")

    st.divider()

    # ── Company Direct sources ────────────────────────────────────────────────
    hdr_col2, all_col2 = st.columns([5, 1])
    with hdr_col2:
        st.subheader("🏢 Company Direct Search")
        st.caption("Search specific companies' job listings. Some via their own portals, some via MyCareersFuture.")
    with all_col2:
        st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
        st.checkbox("All", value=False, key="select_all_companies", on_change=_toggle_companies)

    selected_companies = []

    # Direct portals
    st.markdown("**🔗 Direct Company Portals** *(own careers site or API)*")
    cols_direct = st.columns(len(_all_direct))
    for i, company in enumerate(_all_direct):
        with cols_direct[i]:
            if st.checkbox(company, value=False, key=f"co_{company}"):
                selected_companies.append(company)

    # Via MyCareersFuture
    st.markdown("**📋 Via MyCareersFuture** *(job listing aggregator)*")
    cols_mcf = st.columns(5)
    for i, company in enumerate(_all_mcf):
        with cols_mcf[i % 5]:
            if st.checkbox(company, value=False, key=f"co_{company}"):
                selected_companies.append(company)

    # Singapore Government & Statutory Boards — Careers@Gov (Workday)
    st.markdown("**🏛️ Singapore Gov & Statutory Boards — Careers@Gov** *(direct Workday portal)*")
    st.caption("HTX (anti-scam/tech) · MAS (financial regulation) · IMDA (digital economy) · CSA (cybersecurity)")
    cols_gov = st.columns(len(_all_gov))
    for i, company in enumerate(_all_gov):
        with cols_gov[i]:
            # HTX defaults to checked — initialize session state if needed
            if f"co_{company}" not in st.session_state:
                st.session_state[f"co_{company}"] = (company == "HTX")

            if st.checkbox(company, value=st.session_state[f"co_{company}"], key=f"co_{company}"):
                selected_companies.append(company)

    # Other Careers@Gov agencies
    st.markdown("**🏢 Other Careers@Gov Agencies** *(direct Workday portal)*")
    cols_cg = st.columns(len(_all_careers_gov))
    for i, company in enumerate(_all_careers_gov):
        with cols_cg[i]:
            if st.checkbox(company, value=False, key=f"co_{company}"):
                selected_companies.append(company)

    st.divider()

    # ── Salary & Experience filters ───────────────────────────────────────────
    st.subheader("💰 Salary & Experience")
    sal_col, exp_col = st.columns(2)

    with sal_col:
        st.markdown("**Monthly Salary Range (SGD)**")
        sc1, sc2 = st.columns(2)
        with sc1:
            salary_min = st.number_input("Min", min_value=0, max_value=50000,
                                         value=8000, step=500, key="sal_min",
                                         help="Minimum monthly salary in SGD")
        with sc2:
            salary_max = st.number_input("Max", min_value=0, max_value=50000,
                                         value=20000, step=500, key="sal_max",
                                         help="Maximum monthly salary in SGD (0 = no upper limit)")
        annual_hint = f"≈ SGD {salary_min*12:,} – {salary_max*12:,} / year" if salary_max else f"≈ SGD {salary_min*12:,}+ / year"
        st.caption(annual_hint)

    with exp_col:
        st.markdown("**Years of Experience**")
        exp_option = st.selectbox(
            "Minimum experience",
            ["Any", "1+ years", "3+ years", "5+ years", "8+ years", "10+ years"],
            index=4,
            key="exp_select",
            label_visibility="collapsed"
        )
        min_years = {"Any": 0, "1+ years": 1, "3+ years": 3,
                     "5+ years": 5, "8+ years": 8, "10+ years": 10}[exp_option]
        if min_years:
            st.caption(f"MCF will filter for roles requiring {min_years}+ years")

    st.divider()

    total_sources = len(sources) + len(selected_companies)
    source_summary = ", ".join(sources + [f"Direct:{c}" for c in selected_companies])
    est_seconds = max(20, total_sources * 15)

    if st.button("🔎 Scrape Live Jobs", width="stretch", type="primary"):
        if total_sources == 0:
            st.error("Please select at least one job board or company")
        else:
            with st.spinner(f"🕷️ Scraping {total_sources} source(s) — est. {est_seconds}s…"):
                result = scrape_jobs(keywords, sources, num_results, selected_companies,
                                     salary_min=salary_min,
                                     salary_max=salary_max if salary_max > 0 else None,
                                     min_years=min_years)

                if result["status"] == "success" and result["count"] > 0:
                    st.success(f"✅ Found {result['count']} jobs from: {source_summary}")

                    # Save to database
                    conn = get_connection()
                    c = conn.cursor()
                    saved = 0

                    for job in result["jobs"]:
                        # Skip if company+title already exists
                        # Dedup by URL — skip if this job already exists
                        job_url = job.get("url", "").strip()
                        if job_url:
                            from urllib.parse import urlparse
                            parsed = urlparse(job_url)
                            # For hash-based URLs (e.g. SAP UI5 HRP portal like careers.hrp.gov.sg),
                            # include the fragment so each job gets a unique dedup key.
                            # For regular URLs, strip query params only (handles Glints traceInfo, etc.)
                            if parsed.fragment:
                                base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}#{parsed.fragment}"
                            else:
                                base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

                            c.execute("SELECT id FROM jobs WHERE url LIKE ?", (f"{base_url}%",))
                            if c.fetchone():
                                continue  # Skip duplicate

                        c.execute('''
                            INSERT INTO jobs (company, title, description, salary, location, url, source, notes)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            job.get("company", ""),
                            job.get("title", ""),
                            job.get("description", ""),
                            job.get("salary", ""),
                            job.get("location", "Singapore"),
                            job.get("url", ""),
                            job.get("source", "scraper"),
                            f"Scraped from {job.get('source', 'unknown')}"
                        ))
                        saved += 1

                    conn.commit()
                    conn.close()

                    st.success(f"✅ {saved} new jobs saved to database!")
                    st.info("✅ Jobs saved — head to **My Jobs** to review and evaluate")

                elif result["count"] == 0:
                    st.warning("⚠️ No jobs found. Try different keywords or job boards.")
                else:
                    st.error(f"❌ Scraping failed: {result.get('error', 'Unknown error')}")

# ============================================================================
# PAGE: MY JOBS
# ============================================================================
elif page == "📋 My Jobs":
    st.header("📋 My Jobs")

    # ── Session state initialisation ─────────────────────────────────────────
    if "eval_results" not in st.session_state:
        st.session_state.eval_results = {}
    if "spotlight_id" not in st.session_state:
        st.session_state.spotlight_id = None

    # ── Shared badge helpers ──────────────────────────────────────────────────
    tier_map = {
        "core":     "🎯 Core target",
        "adjacent": "🔍 Adjacent",
        "low":      "⚠️ Low relevance",
        "block":    "🚫 Blocked",
    }

    def _qs_badge(score):
        if score is None:
            return "—"
        color = "#1a6b3c" if score >= 4 else "#4a3700" if score >= 3 else "#6b1a1a"
        tc    = "#6fcf97" if score >= 4 else "#f0b429" if score >= 3 else "#ff7675"
        return (f"<span style='background:{color};color:{tc};padding:2px 7px;"
                f"border-radius:10px;font-size:0.78rem;font-weight:700'>⚡{score:.1f}</span>")

    def _tier_badge(tier):
        cfg = {
            "core":     ("#0a3d62", "#74b9ff", "🎯"),
            "adjacent": ("#1a3a1a", "#55efc4", "🔍"),
            "low":      ("#3a3a00", "#fdcb6e", "⚠️"),
            "block":    ("#3a0000", "#ff7675", "🚫"),
        }
        bg, fg, icon = cfg.get(tier, ("#333", "#ccc", ""))
        label = tier_map.get(tier, tier).split(" ", 1)[-1]
        return (f"<span style='background:{bg};color:{fg};padding:2px 7px;"
                f"border-radius:10px;font-size:0.75rem;font-weight:600'>{icon} {label}</span>")

    # ── Section A: Jobs table + controls ─────────────────────────────────────
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        SELECT j.id, j.company, j.title, j.salary, j.location,
               COALESCE(e.score, 0) as eval_score,
               CASE WHEN e.id IS NOT NULL THEN '✅ Evaluated' ELSE '⏳ Pending' END as eval_status,
               j.source,
               j.quick_score,
               j.quick_reason,
               j.description,
               j.status
        FROM jobs j
        LEFT JOIN evaluations e ON j.id = e.job_id
        WHERE COALESCE(j.status, 'discovered') != 'rejected'
        ORDER BY COALESCE(e.score, 0) DESC, j.quick_score DESC NULLS LAST, j.created_at DESC
    ''')
    rows = c.fetchall()

    # Also fetch archived/rejected jobs for the separate section
    c.execute('''
        SELECT j.id, j.company, j.title, j.salary, j.location,
               COALESCE(e.score, 0) as eval_score,
               CASE WHEN e.id IS NOT NULL THEN '✅ Evaluated' ELSE '⏳ Pending' END as eval_status,
               j.source,
               j.quick_score,
               j.quick_reason,
               j.description,
               j.status,
               j.notes
        FROM jobs j
        LEFT JOIN evaluations e ON j.id = e.job_id
        WHERE j.status = 'rejected'
        ORDER BY COALESCE(e.score, 0) DESC, j.created_at DESC
    ''')
    archived_rows = c.fetchall()
    conn.close()

    if not rows and not archived_rows:
        st.info("No jobs discovered yet. Go to **Search** to scrape some jobs!")
    else:
        total     = len(rows)
        archived  = len(archived_rows)
        evaluated = sum(1 for r in rows if r[6] == '✅ Evaluated')
        screened  = sum(1 for r in rows if r[8] is not None)
        pending   = total - evaluated

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Active Jobs", total)
        m2.metric("⚡ Screened", screened)
        m3.metric("✅ Evaluated", evaluated)
        m4.metric("⏳ Pending", pending)
        m5.metric("🗂️ Archived", archived)

        # ── Quick Screen button ───────────────────────────────────────────
        unscreened = [r for r in rows if r[8] is None]

        # Build model options for quick screen
        qs_model_opts = []
        if gemini_ok:
            qs_model_opts += ["⚡ Gemini 2.5 Flash (fast · free tier)"]
        if claude_ok:
            qs_model_opts += ["☁️ Claude Haiku (accurate · ~$0.002/job)"]
        if ollama_ok:
            qs_model_opts += ["🖥️ Ollama Mistral (local · free)"]

        if not qs_model_opts:
            st.warning("⚠️ No screening models available — configure Gemini, Claude, or Ollama")
        elif unscreened:
            sc1, sc2, sc3 = st.columns([2, 1.5, 2])
            with sc1:
                qs_model_label = st.selectbox(
                    "Screen with",
                    qs_model_opts,
                    key="qs_model_select",
                    label_visibility="collapsed",
                )
            with sc2:
                run_qs = st.button(
                    f"⚡ Screen {len(unscreened)} job(s)",
                    width="stretch", type="primary"
                )
            with sc3:
                qs_hints = {
                    "⚡ Gemini 2.5 Flash (fast · free tier)": "~2–4s per job · uses Google AI free quota",
                    "☁️ Claude Haiku (accurate · ~$0.002/job)": "~3–6s per job · billed to Anthropic key",
                    "🖥️ Ollama Mistral (local · free)": "~5–15s per job · requires Ollama running",
                }
                st.caption(qs_hints.get(qs_model_label, ""))

            if run_qs:
                prog = st.progress(0, text="Starting quick screen…")
                conn2 = get_connection()
                c2 = conn2.cursor()
                for i, row in enumerate(unscreened):
                    job_id_qs, company_qs, title_qs, desc_qs = row[0], row[1], row[2], row[10] or ""
                    prog.progress(i / len(unscreened),
                                  text=f"Screening {i+1}/{len(unscreened)}: {company_qs} — {title_qs[:35]}")
                    if "Gemini" in qs_model_label:
                        qs_result = gemini_quick_screen(title_qs, desc_qs, model="gemini-2.5-flash")
                    elif "Claude" in qs_model_label:
                        qs_result = claude_quick_screen(title_qs, desc_qs, model="claude-haiku-4-5-20251001")
                    else:
                        qs_result = ollama_quick_screen(title_qs, desc_qs)
                    if qs_result["status"] == "success":
                        c2.execute(
                            "UPDATE jobs SET quick_score=?, quick_reason=? WHERE id=?",
                            (qs_result["score"], qs_result["reason"], job_id_qs)
                        )
                        conn2.commit()
                prog.progress(1.0, text="✅ Quick screen complete!")
                conn2.close()
                st.rerun()
        else:
            st.success("⚡ All jobs have been quick-screened — sorted by relevance score above")

        # ── Filter controls ───────────────────────────────────────────────
        f1, f2 = st.columns([2, 2])
        with f1:
            search = st.text_input("🔍 Search", placeholder="Company or job title...", key="myjobs_search")
        with f2:
            tier_filter = st.multiselect(
                "Show tiers",
                ["🎯 Core target", "🔍 Adjacent", "⚠️ Low relevance", "🚫 Blocked"],
                default=["🎯 Core target", "🔍 Adjacent"],
                key="myjobs_tier_filter",
                help="Core = direct role match · Adjacent = worth reviewing · Low = unclear · Blocked = noise"
            )

        # Classify and filter
        classified = []
        for r in rows:
            tier, reason = classify_job(r[2])
            classified.append((r, tier, reason))

        blocked_count = sum(1 for _, t, _ in classified if t == "block")

        display_rows = []
        for r, tier, cl_reason in classified:
            if tier_map[tier] not in tier_filter:
                continue
            if search and search.lower() not in r[1].lower() and search.lower() not in r[2].lower():
                continue
            display_rows.append((r, tier, cl_reason))

        st.caption(
            f"Showing {len(display_rows)} of {total} · "
            f"{blocked_count} blocked · "
            "👆 Click any row to open the spotlight below"
        )

        # ── Clickable dataframe — replaces iframe table + selectbox ──────
        # First, fetch all applied jobs
        conn_applied = get_connection()
        c_applied = conn_applied.cursor()
        c_applied.execute("SELECT job_id FROM applications WHERE status = 'applied'")
        applied_job_ids = {row[0] for row in c_applied.fetchall()}
        conn_applied.close()

        tier_short = {"core": "🎯 Core", "adjacent": "🔍 Adjacent", "low": "⚠️ Low", "block": "🚫 Blocked"}
        df_data = []
        for r, tier, _ in display_rows:
            applied_mark = "📮" if r[0] in applied_job_ids else "—"
            df_data.append({
                "ID":      r[0],
                "Company": " ".join(r[1].split()),
                "Title":   " ".join(r[2].split()),
                "Salary":  r[3] or "—",
                "Tier":    tier_short.get(tier, tier),
                "Fit ⚡":  f"{r[8]:.1f}" if r[8] is not None else "—",
                "Eval":    f"{r[5]:.1f}" if r[5] else "—",
                "Status":  r[6],
                "📮":      applied_mark,
                "Source":  r[7] or "—",
            })

        df = pd.DataFrame(df_data)

        tbl_event = st.dataframe(
            df,
            on_select="rerun",
            selection_mode="single-row",
            width="stretch",
            height=420,
            hide_index=True,
            column_config={
                "ID":      st.column_config.NumberColumn(width=60),
                "Company": st.column_config.TextColumn(width="medium"),
                "Title":   st.column_config.TextColumn(width="large"),
                "Salary":  st.column_config.TextColumn(width="medium"),
                "Tier":    st.column_config.TextColumn(width="small"),
                "Fit ⚡":  st.column_config.TextColumn(width=70),
                "Eval":    st.column_config.TextColumn(width=70),
                "Status":  st.column_config.TextColumn(width="small"),
                "📮":      st.column_config.TextColumn(width=40),
                "Source":  st.column_config.TextColumn(width="medium"),
            }
        )

        # Resolve which job is in the spotlight
        selected_rows = tbl_event.selection.rows
        if selected_rows:
            # Row clicked — update spotlight to that row
            selected_spot_id = display_rows[selected_rows[0]][0][0]
            st.session_state.spotlight_id = selected_spot_id
        elif st.session_state.spotlight_id is not None:
            # Keep last selected job if it's still in the visible list
            ids_visible = {r[0] for r, _, _ in display_rows}
            if st.session_state.spotlight_id not in ids_visible:
                st.session_state.spotlight_id = None

        # ── Spotlight panel ───────────────────────────────────────────────
        if st.session_state.spotlight_id is None:
            st.info("👆 Click any row above to view job details and run a deep evaluation.")
        else:
            selected_spot_id = st.session_state.spotlight_id

            # Load full job data from DB
            conn = get_connection()
            c = conn.cursor()
            c.execute('''
                SELECT j.id, j.company, j.title, j.salary, j.location, j.source,
                       j.description, j.url, j.notes, j.quick_score, j.quick_reason,
                       CASE WHEN e.id IS NOT NULL THEN 1 ELSE 0 END as is_evaluated
                FROM jobs j
                LEFT JOIN evaluations e ON j.id = e.job_id
                WHERE j.id = ?
            ''', (selected_spot_id,))
            spot_row = c.fetchone()
            conn.close()

            if spot_row:
                (spot_id, spot_company, spot_title, spot_salary, spot_location,
                 spot_source, spot_desc, spot_url, spot_notes,
                 spot_qs, spot_qr, spot_is_eval) = spot_row

                st.divider()
                st.subheader(f"🔎 {spot_company} — {spot_title}")

                # Card layout
                card_c1, card_c2, card_c3 = st.columns([3, 2, 2])
                with card_c1:
                    st.markdown(f"💰 {spot_salary or 'Salary N/A'}  ·  📍 {spot_location or 'Location N/A'}")
                    st.caption(f"Source: {spot_source or '—'}  ·  Job ID: {spot_id}")
                with card_c2:
                    if spot_qs is not None:
                        st.markdown(f"### ⚡ {spot_qs:.1f}")
                        st.caption(spot_qr or "")
                    else:
                        st.markdown("### ⚡ —")
                        st.caption("Not yet quick-screened")
                with card_c3:
                    if spot_url:
                        st.link_button("🔗 Open Job", spot_url, width="stretch")
                    if spot_is_eval:
                        st.success("📋 Evaluated ✅")
                    else:
                        st.warning("⏳ Not evaluated")

                # ── Action buttons (Delete / Apply / Save Interest) ─────────────────
                del_col1, del_col2, del_col3 = st.columns([5, 2.5, 2])

                # Check if already applied
                conn_app = get_connection()
                c_app = conn_app.cursor()
                c_app.execute("SELECT id, applied_at FROM applications WHERE job_id = ?", (selected_spot_id,))
                app_row = c_app.fetchone()
                conn_app.close()

                is_applied = app_row is not None
                applied_date_str = ""
                if is_applied and app_row[1]:
                    try:
                        applied_dt = datetime.fromisoformat(app_row[1])
                        applied_date_str = applied_dt.strftime("%b %d, %Y")
                    except:
                        applied_date_str = "Applied"

                with del_col1:
                    if is_applied:
                        st.caption(f"📮 Applied {applied_date_str} · ID: {spot_id}")
                    else:
                        st.caption(f"ID: {spot_id}")

                with del_col2:
                    if not is_applied:
                        if st.button("✅ Mark Applied", key=f"apply_{selected_spot_id}", width="stretch", type="secondary"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "INSERT INTO applications (job_id, status, applied_at) VALUES (?, ?, ?)",
                                (selected_spot_id, "applied", datetime.now().isoformat())
                            )
                            conn.commit()
                            conn.close()
                            st.success("✅ Marked as applied")
                            st.rerun()
                    else:
                        if st.button("🗂️ Undo Apply", key=f"unapply_{selected_spot_id}", width="stretch", type="secondary", help="Remove applied status"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("DELETE FROM applications WHERE job_id = ?", (selected_spot_id,))
                            conn.commit()
                            conn.close()
                            st.info("Cleared applied status")
                            st.rerun()

                with del_col3:
                    if st.button("🗑️ Delete", key=f"del_{selected_spot_id}", width="stretch", type="secondary"):
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute("DELETE FROM jobs WHERE id = ?", (selected_spot_id,))
                        c.execute("DELETE FROM evaluations WHERE job_id = ?", (selected_spot_id,))
                        c.execute("DELETE FROM applications WHERE job_id = ?", (selected_spot_id,))
                        conn.commit()
                        conn.close()
                        st.success("✅ Job deleted")
                        st.session_state.spotlight_id = None
                        time.sleep(0.5)
                        st.rerun()

                # Description expander
                with st.expander("📄 Job Description", expanded=False):
                    if spot_desc and spot_desc.strip():
                        st.markdown(spot_desc)
                    else:
                        st.info("No description stored. Paste the full JD below to enable evaluation.")

                # ── Quick Screen (single job) ─────────────────────────────────
                st.divider()
                st.subheader("⚡ Quick Relevance Screen")
                st.caption("Fast preliminary check (2–15s) · assess fit before deep evaluation")

                # Build model options for quick screen
                qs_model_opts_single = []
                if gemini_ok:
                    qs_model_opts_single += ["⚡ Gemini 2.5 Flash"]
                if claude_ok:
                    qs_model_opts_single += ["☁️ Claude Haiku"]
                if ollama_ok:
                    qs_model_opts_single += ["🖥️ Ollama Mistral"]

                if not qs_model_opts_single:
                    st.warning("⚠️ No screening models available")
                else:
                    qs_col1, qs_col2 = st.columns([2, 1])
                    with qs_col1:
                        qs_model_single = st.selectbox(
                            "Screen with",
                            qs_model_opts_single,
                            key=f"qs_model_single_{selected_spot_id}",
                            label_visibility="collapsed"
                        )
                    with qs_col2:
                        run_qs_single = st.button("▶️ Screen", width="stretch", type="primary", key=f"qs_btn_{selected_spot_id}")

                    if run_qs_single:
                        if not spot_desc or len(spot_desc.strip()) < 80:
                            st.warning("⚠️ Description too short to screen. Paste the full JD below first.")
                        else:
                            with st.spinner("Screening job…"):
                                if "Gemini" in qs_model_single:
                                    qs_result = gemini_quick_screen(spot_title, spot_desc, model="gemini-2.5-flash")
                                elif "Claude" in qs_model_single:
                                    qs_result = claude_quick_screen(spot_title, spot_desc, model="claude-haiku-4-5-20251001")
                                else:
                                    qs_result = ollama_quick_screen(spot_title, spot_desc)

                            if qs_result["status"] == "success":
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute(
                                    "UPDATE jobs SET quick_score=?, quick_reason=? WHERE id=?",
                                    (qs_result["score"], qs_result["reason"], selected_spot_id)
                                )
                                conn.commit()
                                conn.close()
                                st.success(f"✅ Score: {qs_result['score']:.1f}/5 · {qs_result['reason']}")
                                st.rerun()
                            else:
                                st.error(f"❌ {qs_result.get('error', 'Unknown error')}")

                # ── Evaluation Panel ──────────────────────────────────────
                st.divider()

                # Check DB for existing evaluation
                conn = get_connection()
                c = conn.cursor()
                c.execute(
                    "SELECT score, legitimacy, archetype, blocks FROM evaluations WHERE job_id = ?",
                    (selected_spot_id,)
                )
                eval_db = c.fetchone()
                conn.close()

                # If we just ran an eval, save it then treat as DB result
                if selected_spot_id in st.session_state.eval_results and eval_db is None:
                    fresh = st.session_state.eval_results[selected_spot_id]
                    try:
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute('''INSERT INTO evaluations (job_id, score, blocks, legitimacy, archetype, summary, model)
                                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                  (selected_spot_id, fresh["score"], fresh["evaluation"],
                                   fresh["legitimacy"], fresh["archetype"],
                                   f"{fresh['company']} - {fresh['role']}", fresh["model"]))
                        conn.commit()
                        conn.close()
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute(
                            "SELECT score, legitimacy, archetype, blocks FROM evaluations WHERE job_id = ?",
                            (selected_spot_id,)
                        )
                        eval_db = c.fetchone()
                        conn.close()
                    except Exception as save_err:
                        st.warning(f"Could not save evaluation: {save_err}")

                if eval_db:
                    ev_score, ev_legit, ev_arch, ev_blocks = eval_db
                    bar_color = "#1a6b3c" if ev_score >= 4 else "#4a3700" if ev_score >= 3 else "#6b1a1a"
                    text_color = "#6fcf97" if ev_score >= 4 else "#f0b429" if ev_score >= 3 else "#ff7675"

                    # Map legitimacy to clearer labels with emoji
                    legit_emoji = {"Legitimate": "✅", "Verify": "⚠️", "Fraudulent": "🚩"}.get(ev_legit, "❓")
                    legit_label = {"Legitimate": "Real Job", "Verify": "Has Red Flags", "Fraudulent": "Do Not Apply"}.get(ev_legit, ev_legit or "—")

                    st.markdown(
                        f"""<div style="background:{bar_color};border-radius:10px;padding:14px 20px;margin-bottom:12px">
                        <span style="color:{text_color};font-size:1.6rem;font-weight:700">{ev_score:.1f}/5.0</span>
                        <span style="color:#ccc;font-size:0.95rem;margin-left:16px">
                            {legit_emoji} {legit_label} &nbsp;·&nbsp; {ev_arch or '—'}
                        </span></div>""",
                        unsafe_allow_html=True
                    )
                    with st.expander("📖 Full A–G Evaluation", expanded=True):
                        # Downgrade ## headings → #### so they don't render giant in Streamlit
                        display_eval = re.sub(r'^#{1,3} ', '#### ', ev_blocks or "No evaluation content stored.", flags=re.MULTILINE)
                        st.markdown(display_eval)

                    # Action buttons
                    btn_col1, btn_col2, btn_col3 = st.columns(3)
                    with btn_col1:
                        if st.button("🔄 Re-evaluate", type="secondary", key=f"reeval_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("DELETE FROM evaluations WHERE job_id = ?", (selected_spot_id,))
                            conn.commit()
                            conn.close()
                            if selected_spot_id in st.session_state.eval_results:
                                del st.session_state.eval_results[selected_spot_id]
                            st.rerun()

                    with btn_col2:
                        # Archive button — always available
                        if st.button("🗂️ Archive", type="secondary", key=f"archive_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "UPDATE jobs SET status = 'rejected' WHERE id = ?",
                                (selected_spot_id,)
                            )
                            conn.commit()
                            conn.close()
                            st.success("✅ Job archived")
                            st.session_state.spotlight_id = None
                            time.sleep(0.5)
                            st.rerun()

                    with btn_col3:
                        # Save Interest button — for high-value jobs
                        if ev_score >= 3.5:
                            if st.button("👍 Save Interest", type="secondary", key=f"save_int_{selected_spot_id}"):
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute(
                                    "UPDATE jobs SET status = 'interested' WHERE id = ?",
                                    (selected_spot_id,)
                                )
                                conn.commit()
                                conn.close()
                                st.success("✅ Marked as interested")
                                st.rerun()

                else:
                    st.info("This job hasn't been deep-evaluated yet.")

                    # Quick action buttons (available even before evaluation)
                    quick_btn_col1, quick_btn_col2 = st.columns(2)
                    with quick_btn_col1:
                        if st.button("🗂️ Archive", type="secondary", key=f"archive_unevaluated_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "UPDATE jobs SET status = 'rejected' WHERE id = ?",
                                (selected_spot_id,)
                            )
                            conn.commit()
                            conn.close()
                            st.success("✅ Job archived")
                            st.session_state.spotlight_id = None
                            time.sleep(0.5)
                            st.rerun()
                    with quick_btn_col2:
                        if st.button("👍 Save Interest", type="secondary", key=f"save_int_unevaluated_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "UPDATE jobs SET status = 'interested' WHERE id = ?",
                                (selected_spot_id,)
                            )
                            conn.commit()
                            conn.close()
                            st.success("✅ Marked as interested")
                            st.rerun()

                    st.divider()

                    # Model selector
                    model_col, btn_col, note_col = st.columns([1.5, 1, 2])
                    with model_col:
                        model_options = []
                        if gemini_ok:
                            model_options += [
                                "⚡ Gemini 2.5 Flash (fast · free tier)",
                                "🧠 Gemini 2.5 Pro (best · free tier)",
                            ]
                        if claude_ok:
                            model_options += [
                                "☁️ Claude Haiku (accurate · ~$0.01)",
                                "☁️ Claude Sonnet (nuanced · ~$0.05)",
                            ]
                        if ollama_ok:
                            model_options += [
                                "🖥️ Llama 3.1 8B (local · free)",
                                "🖥️ Mistral 7B (local · free)",
                            ]
                        if not model_options:
                            model_options = ["⚠️ No models available"]
                        eval_model_label = st.selectbox(
                            "🧠 Evaluator",
                            model_options,
                            index=0,
                            key=f"eval_model_{selected_spot_id}",
                        )
                    with btn_col:
                        evaluate_btn = st.button(
                            "🚀 Deep Evaluate", type="primary",
                            width="stretch",
                            key=f"eval_{selected_spot_id}"
                        )
                    with note_col:
                        hints = {
                            "⚡ Gemini 2.5 Flash (fast · free tier)": "Fast · good quality · uses Google free quota · ~15s",
                            "🧠 Gemini 2.5 Pro (best · free tier)": "Highest Gemini quality · may be slower · ~30s",
                            "☁️ Claude Haiku (accurate · ~$0.01)": "Precise instruction-following · ~20s",
                            "☁️ Claude Sonnet (nuanced · ~$0.05)": "Most nuanced analysis · ~30s",
                            "🖥️ Llama 3.1 8B (local · free)": "Local · free · may hallucinate",
                            "🖥️ Mistral 7B (local · free)": "Local · free · weakest instruction-following",
                        }
                        st.caption(hints.get(eval_model_label, ""))

                    if evaluate_btn:
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute("SELECT company, description FROM jobs WHERE id = ?", (selected_spot_id,))
                        job_fetch = c.fetchone()
                        conn.close()

                        company_name = job_fetch[0] if job_fetch else "Unknown Company"
                        job_desc = job_fetch[1] if job_fetch else ""

                        # Prepend company name for context
                        desc_for_eval = f"COMPANY: {company_name}\n\n{job_desc}" if job_desc else ""

                        if not desc_for_eval or len(desc_for_eval.strip()) < 80:
                            st.warning("⚠️ Description too short. Paste the full JD below first.")
                        else:
                            # Route to correct model
                            gemini_model = (
                                "gemini-2.5-pro" if "Pro" in eval_model_label
                                else "gemini-2.5-flash"
                            )
                            claude_model = (
                                "claude-sonnet-4-6" if "Sonnet" in eval_model_label
                                else "claude-haiku-4-5-20251001"
                            )
                            ollama_model = (
                                "llama3.1:8b" if "Llama" in eval_model_label
                                else "mistral:latest"
                            )
                            with st.spinner(f"Running A–G evaluation using {eval_model_label}…"):
                                if eval_model_label.startswith("⚡") or eval_model_label.startswith("🧠 Gemini"):
                                    eval_result = gemini_evaluate_job(desc_for_eval, model=gemini_model)
                                elif eval_model_label.startswith("☁️"):
                                    eval_result = claude_evaluate_job(desc_for_eval, model=claude_model)
                                else:
                                    if not ollama_ok:
                                        st.error("❌ Ollama not running. Run: ollama serve")
                                        eval_result = {"status": "error", "error": "Ollama offline"}
                                    else:
                                        eval_result = ollama_evaluate_job(desc_for_eval, model=ollama_model)
                            if eval_result["status"] == "success":
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute('''INSERT INTO evaluations
                                             (job_id, score, blocks, legitimacy, archetype, summary, model)
                                             VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                          (selected_spot_id, eval_result["score"],
                                           eval_result["evaluation"], eval_result["legitimacy"],
                                           eval_result["archetype"],
                                           f"{eval_result['company']} - {eval_result['role']}",
                                           eval_result["model"]))
                                conn.commit()
                                conn.close()
                                st.session_state.eval_results[selected_spot_id] = eval_result
                                st.rerun()
                            else:
                                st.error(f"❌ {eval_result.get('error')}")

                # ── Paste / Update JD ─────────────────────────────────────
                with st.expander("📎 Paste or update job description", expanded=False):
                    new_jd = st.text_area(
                        "Job Description",
                        value=spot_desc or "",
                        height=260,
                        key=f"jd_area_{selected_spot_id}"
                    )
                    djd_c1, djd_c2 = st.columns(2)
                    with djd_c1:
                        if st.button("💾 Save JD", width="stretch", key=f"save_jd_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("UPDATE jobs SET description = ? WHERE id = ?",
                                      (new_jd, selected_spot_id))
                            conn.commit()
                            conn.close()
                            st.success("✅ Description saved.")
                            st.rerun()
                    with djd_c2:
                        if st.button("💾 Save & Evaluate", width="stretch",
                                     type="primary", key=f"save_eval_jd_{selected_spot_id}"):
                            if not new_jd or len(new_jd.strip()) < 80:
                                st.warning("⚠️ Description too short to evaluate.")
                            elif not ollama_ok:
                                st.error("❌ Ollama not running.")
                            else:
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute("UPDATE jobs SET description = ? WHERE id = ?",
                                          (new_jd, selected_spot_id))
                                conn.commit()
                                conn.close()
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute("DELETE FROM evaluations WHERE job_id = ?", (selected_spot_id,))
                                conn.commit()
                                conn.close()
                                if selected_spot_id in st.session_state.eval_results:
                                    del st.session_state.eval_results[selected_spot_id]
                                with st.spinner(f"Running A–G evaluation for {spot_company}…"):
                                    eval_result = ollama_evaluate_job(new_jd)
                                if eval_result["status"] == "success":
                                    conn = get_connection()
                                    c = conn.cursor()
                                    c.execute('''INSERT INTO evaluations
                                                 (job_id, score, blocks, legitimacy, archetype, summary, model)
                                                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                              (selected_spot_id, eval_result["score"],
                                               eval_result["evaluation"], eval_result["legitimacy"],
                                               eval_result["archetype"],
                                               f"{eval_result['company']} - {eval_result['role']}",
                                               eval_result["model"]))
                                    conn.commit()
                                    conn.close()
                                    st.session_state.eval_results[selected_spot_id] = eval_result
                                    st.rerun()
                                else:
                                    st.error(f"❌ {eval_result.get('error')}")

        # ── Add new job (always visible) ──────────────────────────────────
        with st.expander("➕ Add & evaluate a new job (paste JD)", expanded=False):
            st.caption("Add a job that isn't in the database yet.")
            nj_c1, nj_c2 = st.columns(2)
            with nj_c1:
                nj_company  = st.text_input("Company Name", placeholder="e.g., DBS Bank", key="nj_company")
                nj_title    = st.text_input("Job Title", placeholder="e.g., Senior Product Manager", key="nj_title")
                nj_url      = st.text_input("Job URL (optional)", placeholder="https://…", key="nj_url")
            with nj_c2:
                nj_salary   = st.text_input("Salary (optional)", value="SGD 140k+", key="nj_salary")
                nj_location = st.text_input("Location (optional)", value="Singapore", key="nj_location")
            nj_jd = st.text_area("Full Job Description", placeholder="Paste the complete JD here…",
                                 height=240, key="nj_jd")
            if st.button("🚀 Save & Evaluate New Job", type="primary",
                         width="stretch", key="nj_eval_btn"):
                if not nj_jd.strip():
                    st.error("Please paste a job description.")
                elif not ollama_ok:
                    st.error("❌ Ollama not running. Run: ollama serve")
                else:
                    with st.spinner("Running full A–G evaluation…"):
                        nj_result = ollama_evaluate_job(nj_jd)
                    if nj_result["status"] == "success":
                        conn = get_connection()
                        c = conn.cursor()

                        # Dedup by URL if provided
                        nj_id = None
                        is_duplicate = False
                        if nj_url and nj_url.strip():
                            c.execute("SELECT id FROM jobs WHERE url = ?", (nj_url.strip(),))
                            existing = c.fetchone()
                            if existing:
                                st.warning(f"⚠️ Job with this URL already exists (ID: {existing[0]}). Not adding duplicate.")
                                conn.close()
                                is_duplicate = True

                        if not is_duplicate:
                            # Insert job (dedup check passed)
                            c.execute('''INSERT INTO jobs
                                         (company, title, salary, location, url, description, source)
                                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                      (nj_company or "Unknown", nj_title or "Unknown",
                                       nj_salary or None, nj_location or None,
                                       nj_url or None, nj_jd, "manual_paste"))
                            nj_id = c.lastrowid

                            # Insert evaluation
                            c.execute('''INSERT INTO evaluations
                                         (job_id, score, blocks, legitimacy, archetype, summary, model)
                                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                      (nj_id, nj_result["score"], nj_result["evaluation"],
                                       nj_result["legitimacy"], nj_result["archetype"],
                                       f"{nj_result['company']} - {nj_result['role']}",
                                       nj_result["model"]))
                            conn.commit()
                            conn.close()

                            st.success(f"✅ Saved and evaluated! Score: {nj_result['score']:.1f}/5.0")
                            legit = nj_result['legitimacy']
                            legit_emoji = {"Legitimate": "✅", "Verify": "⚠️", "Fraudulent": "🚩"}.get(legit, "❓")
                            legit_label = {"Legitimate": "Real Job", "Verify": "Has Red Flags", "Fraudulent": "Do Not Apply"}.get(legit, legit)
                            st.markdown(f"**Job Status:** {legit_emoji} {legit_label} | **Archetype:** {nj_result['archetype']}")
                            st.session_state.spotlight_id = nj_id
                            st.rerun()
                    else:
                        st.error(f"❌ {nj_result.get('error', 'Unknown error')}")

        # ── Manage Jobs (always visible) ──────────────────────────────────
        with st.expander("🗑️ Manage Jobs", expanded=False):
            st.caption("Remove jobs from the database. Evaluations linked to deleted jobs will also be removed.")
            mgr_a, mgr_b, mgr_c = st.columns(3)

            with mgr_a:
                if st.button("🧹 Clear Unevaluated Jobs", width="stretch",
                             help="Remove jobs that haven't been evaluated yet",
                             key="mgr_clear_uneval"):
                    conn2 = get_connection()
                    c2 = conn2.cursor()
                    c2.execute('DELETE FROM jobs WHERE id NOT IN (SELECT job_id FROM evaluations)')
                    n = c2.rowcount
                    conn2.commit()
                    conn2.close()
                    st.session_state.spotlight_id = None
                    st.success(f"✅ Removed {n} unevaluated jobs")
                    st.rerun()

            with mgr_b:
                source_list = list({r[7] for r in rows if r[7]})
                src_to_clear = st.selectbox("Clear by source", source_list,
                                             key="mgr_clear_src_select")
                if st.button(f"🗑️ Clear '{src_to_clear}' Jobs", width="stretch",
                             key="mgr_clear_src_btn"):
                    conn2 = get_connection()
                    c2 = conn2.cursor()
                    c2.execute('DELETE FROM evaluations WHERE job_id IN (SELECT id FROM jobs WHERE source = ?)', (src_to_clear,))
                    c2.execute('DELETE FROM jobs WHERE source = ?', (src_to_clear,))
                    n = c2.rowcount
                    conn2.commit()
                    conn2.close()
                    st.session_state.spotlight_id = None
                    st.success(f"✅ Removed {n} jobs from '{src_to_clear}'")
                    st.rerun()

            with mgr_c:
                st.warning("⚠️ This removes everything")
                if st.button("💣 Clear ALL Jobs", width="stretch",
                             type="secondary", key="mgr_clear_all"):
                    conn2 = get_connection()
                    c2 = conn2.cursor()
                    c2.execute('DELETE FROM evaluations')
                    c2.execute('DELETE FROM applications')
                    c2.execute('DELETE FROM jobs')
                    conn2.commit()
                    conn2.close()
                    st.session_state.spotlight_id = None
                    st.session_state.eval_results = {}
                    st.success("✅ All jobs cleared. Go to Search to scrape fresh results!")
                    st.rerun()

        # ── Archived Jobs Section ─────────────────────────────────────────────
        if archived_rows:
            st.divider()
            with st.expander(f"🗂️ Archived Jobs ({len(archived_rows)})", expanded=False):
                st.caption("Jobs marked as not a fit. Click to un-archive and reconsider.")

                # Build archived jobs table
                archived_display = []
                for r in archived_rows:
                    jid, company, title, salary, location, ev_score, ev_status, source, qs_score, qs_reason, desc, status, notes = r
                    archived_display.append({
                        "ID": jid,
                        "Company": company,
                        "Title": title,
                        "Score": f"{ev_score:.1f}" if ev_score > 0 else "—",
                        "Status": ev_status,
                        "Notes": notes or "—"
                    })

                arch_df = pd.DataFrame(archived_display)
                st.dataframe(arch_df, width="stretch", hide_index=True, key="arch_df")

                # Un-archive button
                st.caption("Select job ID to un-archive:")
                arch_id_col, arch_btn_col = st.columns([2, 1])
                with arch_id_col:
                    arch_id = st.number_input("Job ID", min_value=1, step=1, key="arch_id_input")
                with arch_btn_col:
                    if st.button("↩️ Un-Archive", key="unarch_btn"):
                        if arch_id in [r[0] for r in archived_rows]:
                            conn_arch = get_connection()
                            c_arch = conn_arch.cursor()
                            c_arch.execute(
                                "UPDATE jobs SET status = 'discovered', notes = NULL WHERE id = ?",
                                (arch_id,)
                            )
                            conn_arch.commit()
                            conn_arch.close()
                            st.success(f"✅ Job {arch_id} un-archived and restored to active list")
                            st.rerun()
                        else:
                            st.error(f"Job {arch_id} not found in archived list")

# ============================================================================
# PAGE: DASHBOARD
# ============================================================================
elif page == "📊 Dashboard":
    st.header("📊 Dashboard & Tracking")

    tab1, tab2, tab3, tab4 = st.tabs(["Summary", "All Jobs", "⚖️ Compare", "🔧 Health Check"])

    # ── Tab 1: Summary ────────────────────────────────────────────────────────
    with tab1:
        col1, col2, col3, col4 = st.columns(4)

        conn = get_connection()
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM jobs")
        col1.metric("Total Jobs", c.fetchone()[0])

        c.execute("SELECT COUNT(*) FROM evaluations")
        col2.metric("Evaluated", c.fetchone()[0])

        c.execute("SELECT COUNT(*) FROM applications WHERE status='applied'")
        col3.metric("Applied", c.fetchone()[0])

        c.execute("SELECT AVG(score) FROM evaluations")
        avg_score = c.fetchone()[0] or 0
        col4.metric("Avg Score", f"{avg_score:.2f}/5.0")

        st.divider()

        st.subheader("Top Opportunities")
        c.execute('''
            SELECT j.company, j.title, e.score, e.legitimacy, e.archetype, j.salary
            FROM evaluations e
            JOIN jobs j ON e.job_id = j.id
            ORDER BY e.score DESC
            LIMIT 10
        ''')
        top_jobs = c.fetchall()

        if top_jobs:
            df = pd.DataFrame(top_jobs, columns=["Company", "Title", "Score", "Legitimacy", "Archetype", "Salary"])
            st.dataframe(df, width="stretch", hide_index=True)

        conn.close()

    # ── Tab 2: All Jobs ───────────────────────────────────────────────────────
    with tab2:
        st.subheader("All Jobs & Status")

        conn = get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT j.id, j.company, j.title, j.salary, j.location,
                   COALESCE(e.score, 0) as score,
                   COALESCE(e.legitimacy, 'Not Evaluated') as legitimacy,
                   COALESCE(e.archetype, '-') as archetype,
                   COALESCE(a.status, 'Not Applied') as app_status
            FROM jobs j
            LEFT JOIN evaluations e ON j.id = e.job_id
            LEFT JOIN applications a ON j.id = a.job_id
            ORDER BY e.score DESC, j.created_at DESC
        ''')
        dash_rows = c.fetchall()
        conn.close()

        if dash_rows:
            df = pd.DataFrame(dash_rows, columns=[
                "ID", "Company", "Title", "Salary", "Location",
                "Score", "Legitimacy", "Archetype", "Status"
            ])
            st.dataframe(df, width="stretch", hide_index=True)

            # ── Delete jobs ───────────────────────────────────────────────
            st.divider()
            st.subheader("🗑️ Delete Jobs")

            del_c1, del_c2, del_c3 = st.columns([2, 1, 1])
            with del_c1:
                job_to_delete = st.selectbox(
                    "Select job to delete",
                    [(r[0], f"{r[1]} - {r[2]}") for r in dash_rows],
                    format_func=lambda x: x[1],
                    key="delete_select"
                )
            with del_c2:
                if st.button("🗑️ Delete Job", width="stretch", type="secondary"):
                    if job_to_delete:
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute("DELETE FROM jobs WHERE id = ?", (job_to_delete[0],))
                        c.execute("DELETE FROM evaluations WHERE job_id = ?", (job_to_delete[0],))
                        c.execute("DELETE FROM applications WHERE job_id = ?", (job_to_delete[0],))
                        conn.commit()
                        conn.close()
                        st.success(f"✅ Deleted: {job_to_delete[1]}")
                        time.sleep(0.3)
                        st.rerun()
            with del_c3:
                st.caption("⚠️ Removes job + evaluations + applications")

            st.divider()
            st.subheader("✅ Verify Job URLs")

            col1, col2 = st.columns(2)
            with col1:
                conn = get_connection()
                c = conn.cursor()
                c.execute("SELECT id, company, title, url FROM jobs WHERE url IS NOT NULL AND url != ''")
                jobs_with_urls = c.fetchall()
                conn.close()

                if jobs_with_urls:
                    job_to_verify = st.selectbox(
                        "Select job to verify",
                        jobs_with_urls,
                        format_func=lambda x: f"{x[1]} - {x[2]}",
                        key="verify_select"
                    )

                    if st.button("🔍 Check URL Status", width="stretch"):
                        with st.spinner("Checking URL…"):
                            verify_result = verify_job_url(job_to_verify[3])
                            if verify_result["valid"]:
                                st.success(f"✅ {verify_result['message']}")
                                st.markdown(f"**URL:** {job_to_verify[3]}")
                            else:
                                st.error(f"{verify_result['message']}")
                                st.markdown(f"**URL:** {job_to_verify[3]}")
                                st.warning("⚠️ This job posting may not be accessible. Verify before applying.")
                else:
                    st.info("No jobs with URLs to verify.")
            with col2:
                st.write("")  # spacer
        else:
            st.info("No jobs tracked yet.")

    # ── Tab 3: Compare ────────────────────────────────────────────────────────
    with tab3:
        st.subheader("⚖️ Compare Evaluated Jobs")

        conn = get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT j.id, j.company, j.title, e.score, e.legitimacy, e.blocks
            FROM evaluations e
            JOIN jobs j ON e.job_id = j.id
            ORDER BY e.score DESC
        ''')
        cmp_evaluated = c.fetchall()
        conn.close()

        if not cmp_evaluated:
            st.info("No evaluated jobs yet. Evaluate some jobs in My Jobs first!")
        else:
            cmp_options = {f"{j[1]} - {j[2]} (Score: {j[3]:.1f})": j[0] for j in cmp_evaluated}
            cmp_selected = st.multiselect(
                "Choose 2–4 jobs to compare",
                list(cmp_options.keys()),
                max_selections=4
            )

            if len(cmp_selected) >= 2:
                cmp_ids = [cmp_options[s] for s in cmp_selected]

                conn = get_connection()
                c = conn.cursor()
                comparison_data = []
                for cmp_id in cmp_ids:
                    c.execute('''
                        SELECT j.company, j.title, j.salary, j.location,
                               e.score, e.legitimacy, e.archetype
                        FROM jobs j
                        JOIN evaluations e ON j.id = e.job_id
                        WHERE j.id = ?
                    ''', (cmp_id,))
                    cmp_row = c.fetchone()
                    if cmp_row:
                        comparison_data.append({
                            "Company": cmp_row[0],
                            "Title": cmp_row[1],
                            "Salary": cmp_row[2] or "N/A",
                            "Location": cmp_row[3] or "N/A",
                            "Score": cmp_row[4],
                            "Legitimacy": cmp_row[5],
                            "Archetype": cmp_row[6],
                        })
                conn.close()

                cmp_df = pd.DataFrame(comparison_data)
                st.dataframe(cmp_df, width="stretch", hide_index=True)

                st.subheader("Detailed Comparison")
                for i, cmp_id in enumerate(cmp_ids, 1):
                    cmp_job = next((j for j in cmp_evaluated if j[0] == cmp_id), None)
                    if cmp_job:
                        with st.expander(f"{i}. {cmp_job[1]} - {cmp_job[2]}"):
                            display_cmp = re.sub(r'^#{1,3} ', '#### ', cmp_job[5] or "No evaluation content stored.", flags=re.MULTILINE)
                            st.markdown(display_cmp)
            else:
                st.info(f"Select at least 2 jobs (currently selected: {len(cmp_selected)})")

    # ── Tab 4: Health Check ───────────────────────────────────────────────────
    with tab4:
        st.subheader("🔧 Scraper Health Check")
        st.caption("Test each source with a simple keyword to confirm it's returning results. Run monthly or when results feel sparse.")

        TEST_KEYWORD = "manager"

        # Known auth-blocked sources
        AUTH_BLOCKED = {
            "HRP Portal": "careers.hrp.gov.sg — requires SAML2 login, cannot scrape headlessly"
        }

        # All available sources
        ALL_BOARD_SOURCES  = ["MyCareersFuture", "Indeed", "JobStreet", "LinkedIn", "Glints"]
        ALL_COMPANY_SOURCES = {
            "DBS":       "direct Workday",
            "Grab":      "direct portal",
            "GovTech":   "Careers@Gov",
            "HTX":       "Careers@Gov",
            "MAS":       "MCF",
            "Anthropic": "Greenhouse API",
            "Airwallex": "Ashby API",
        }

        # ── Source selector ───────────────────────────────────────────────────
        st.markdown("**📰 Job Boards**")
        hc_b_all, hc_b_none = st.columns([1, 5])
        with hc_b_all:
            if st.button("All", key="hc_boards_all", width="content"):
                for b in ALL_BOARD_SOURCES:
                    st.session_state[f"hc_board_{b}"] = True
        with hc_b_none:
            if st.button("None", key="hc_boards_none", width="content"):
                for b in ALL_BOARD_SOURCES:
                    st.session_state[f"hc_board_{b}"] = False

        board_cols = st.columns(len(ALL_BOARD_SOURCES))
        selected_boards = []
        for i, board in enumerate(ALL_BOARD_SOURCES):
            with board_cols[i]:
                if f"hc_board_{board}" not in st.session_state:
                    st.session_state[f"hc_board_{board}"] = True  # default: all checked
                if st.checkbox(board, key=f"hc_board_{board}"):
                    selected_boards.append(board)

        st.markdown("**🏢 Company Sources**")
        hc_c_all, hc_c_none = st.columns([1, 5])
        with hc_c_all:
            if st.button("All", key="hc_cos_all", width="content"):
                for co in ALL_COMPANY_SOURCES:
                    st.session_state[f"hc_co_{co}"] = True
        with hc_c_none:
            if st.button("None", key="hc_cos_none", width="content"):
                for co in ALL_COMPANY_SOURCES:
                    st.session_state[f"hc_co_{co}"] = False

        co_keys = list(ALL_COMPANY_SOURCES.keys())
        co_cols = st.columns(len(co_keys))
        selected_companies_hc = []
        for i, co in enumerate(co_keys):
            with co_cols[i]:
                if f"hc_co_{co}" not in st.session_state:
                    st.session_state[f"hc_co_{co}"] = True  # default: all checked
                if st.checkbox(co, key=f"hc_co_{co}"):
                    selected_companies_hc.append(co)

        st.divider()

        total_selected = len(selected_boards) + len(selected_companies_hc)
        est_min = max(1, round(total_selected * 0.4))
        col_run, col_info = st.columns([1, 3])
        with col_run:
            run_health = st.button(
                f"▶️ Run Health Check ({total_selected})",
                type="primary", width="stretch",
                disabled=(total_selected == 0)
            )
        with col_info:
            st.caption(
                f'Testing **{total_selected}** source(s) with keyword **"{TEST_KEYWORD}"**'
                f' · Est. ~{est_min}–{est_min*2} min'
            )

        # Show known blocked sources upfront
        st.markdown("**🔒 Known Auth-Blocked (cannot scrape)**")
        for src, reason in AUTH_BLOCKED.items():
            st.markdown(f"- **{src}** — {reason}")

        if run_health:
            if total_selected == 0:
                st.warning("Select at least one source to test.")
            else:
                results = []

                # Test selected job boards
                if selected_boards:
                    st.markdown("---")
                    st.markdown("**Testing job boards…**")
                    board_progress = st.progress(0)
                    for i, board in enumerate(selected_boards):
                        with st.spinner(f"Testing {board}…"):
                            try:
                                r = scrape_jobs(TEST_KEYWORD, [board], num_results=5, companies=[])
                                count  = r.get("count", 0)
                                status = "✅ OK" if count > 0 else "⚠️ 0 results"
                                note   = f"{count} jobs returned" if count > 0 else "May be blocked or broken"
                            except Exception as e:
                                status = "❌ Error"
                                note   = str(e)[:80]
                            results.append({"Source": board, "Type": "Job Board", "Status": status, "Note": note})
                        board_progress.progress((i + 1) / len(selected_boards))

                # Test selected company sources
                if selected_companies_hc:
                    st.markdown("**Testing company sources…**")
                    co_progress = st.progress(0)
                    for i, co in enumerate(selected_companies_hc):
                        co_type = ALL_COMPANY_SOURCES[co]
                        with st.spinner(f"Testing {co}…"):
                            try:
                                r = scrape_jobs(TEST_KEYWORD, [], num_results=5, companies=[co])
                                count  = r.get("count", 0)
                                status = "✅ OK" if count > 0 else "⚠️ 0 results"
                                note   = f"{count} jobs returned" if count > 0 else "May be broken or no matching roles"
                            except Exception as e:
                                status = "❌ Error"
                                note   = str(e)[:80]
                            results.append({"Source": co, "Type": co_type, "Status": status, "Note": note})
                        co_progress.progress((i + 1) / len(selected_companies_hc))

                # Display results
                st.markdown("---")
                st.subheader("📊 Health Check Results")
                df_health = pd.DataFrame(results)
                st.dataframe(df_health, width="stretch", hide_index=True)

                ok_count   = sum(1 for r in results if r["Status"].startswith("✅"))
                warn_count = sum(1 for r in results if r["Status"].startswith("⚠️"))
                err_count  = sum(1 for r in results if r["Status"].startswith("❌"))
                st.caption(f"✅ {ok_count} OK · ⚠️ {warn_count} zero results · ❌ {err_count} errors · 🔒 1 auth-blocked (HRP Portal)")

                if warn_count > 0 or err_count > 0:
                    st.warning("⚠️ Some sources may need attention. Zero results could mean bot-detection, site changes, or no matching jobs for the test keyword.")

# ============================================================================
# PAGE: SETTINGS
# ============================================================================
elif page == "⚙️ Settings":
    st.header("⚙️ Settings & Profile")

    # Helper functions for settings
    def get_setting(key, default=None):
        """Get a setting from the database."""
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT value FROM user_settings WHERE key = ?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default

    def save_setting(key, value):
        """Save or update a setting in the database."""
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO user_settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )
        conn.commit()
        conn.close()

    st.subheader("💰 Salary Expectations")
    st.caption("Used in job evaluations to assess compensation fit")

    # Fetch current settings
    current_salary = float(get_setting("current_salary", 150000))
    target_min = float(get_setting("target_salary_min", 180000))
    target_max = float(get_setting("target_salary_max", 250000))

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Current Annual Compensation (SGD)**")
        new_current = st.number_input(
            "Your current salary (base + bonus)",
            min_value=0,
            max_value=1000000,
            value=int(current_salary),
            step=10000,
            key="current_salary_input",
            help="Used as baseline for comparing job offers"
        )

    with col2:
        st.markdown("**Target Salary Range (SGD/year)**")
        col_min, col_max = st.columns(2)
        with col_min:
            new_target_min = st.number_input(
                "Min target",
                min_value=0,
                max_value=1000000,
                value=int(target_min),
                step=10000,
                key="target_min_input",
                help="Minimum acceptable salary"
            )
        with col_max:
            new_target_max = st.number_input(
                "Max target",
                min_value=0,
                max_value=1000000,
                value=int(target_max),
                step=10000,
                key="target_max_input",
                help="Target maximum (upside)"
            )

    # Display summary
    st.markdown("---")
    st.markdown(f"""
    **Your Salary Profile:**
    - **Current:** SGD {new_current:,}/year
    - **Target range:** SGD {new_target_min:,}–{new_target_max:,}/year
    - **Red flag if below:** SGD {new_current:,} (your current level)
    - **Upside if above:** SGD {new_target_max:,}+
    """)

    # Save button
    if st.button("💾 Save Salary Settings", type="primary", width="stretch"):
        save_setting("current_salary", new_current)
        save_setting("target_salary_min", new_target_min)
        save_setting("target_salary_max", new_target_max)
        st.success("✅ Settings saved! Job evaluations will now use your salary expectations.")
        st.info("💡 Next time you evaluate a job, it will compare compensation against these targets.")
        time.sleep(1.5)
        st.rerun()

    st.divider()
    st.subheader("ℹ️ How This Works")
    st.markdown("""
    When you run a **Deep Evaluation** (A–G) on a job, the evaluation model:
    1. Estimates the realistic salary range for that role in Singapore
    2. Compares it against **your current salary** (baseline)
    3. Flags if it's below your current salary (red flag ⚠️)
    4. Flags if it's above your max target (upside 📈)

    This helps you quickly spot which jobs are worth the pay bump and which are steps backward.
    """)

# ============================================================================
# FOOTER
# ============================================================================
st.divider()
st.caption(f"Career-Ops MVP | Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
