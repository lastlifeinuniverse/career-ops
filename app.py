import os
import re
import time
import streamlit as st
import streamlit.components.v1 as components
import sqlite3
import pandas as pd
from datetime import datetime
from pathlib import Path

from db_init import init_db, get_connection, set_active_db
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
    verify_job_url,
    set_active_profile as _api_set_profile,
)
from job_scraper import scrape_jobs, COMPANY_GROUPS
from profile_loader import (
    AVAILABLE_PROFILES,
    load_profile,
    get_env_profile,
    profile_db_path,
    get_classifier,
    get_defaults,
)

# ============================================================================
# PASSWORD GATE
# ============================================================================

def _check_password() -> bool:
    """Return True if user is authenticated."""
    try:
        app_password = st.secrets.get("app_password", "")
    except Exception:
        app_password = ""

    if not app_password:
        return True  # no password configured — open access (local dev)

    if st.session_state.get("authenticated"):
        return True

    st.title("🎯 Career-Ops")
    st.caption("Job search intelligence for Singapore professionals")
    pwd = st.text_input("Password", type="password", placeholder="Enter access password")
    if st.button("Login", type="primary"):
        if pwd == app_password:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

# ============================================================================
# PROFILE BOOTSTRAP  — runs once per session
# ============================================================================

def _bootstrap_profile():
    """Load and cache the active profile in session state."""
    if "profile_name" not in st.session_state:
        # Env var wins (useful for dedicated per-person deployments)
        env = get_env_profile()
        # If running on Streamlit Cloud, also check secrets
        try:
            env = st.secrets.get("CAREER_OPS_PROFILE", env) or env
        except Exception:
            pass
        st.session_state.profile_name = env if env in AVAILABLE_PROFILES else "default"

    if "profile" not in st.session_state:
        st.session_state.profile = load_profile(st.session_state.profile_name)

    # Re-init DB whenever profile changes so each user has isolated data
    db_path = profile_db_path(st.session_state.profile_name)
    init_db(db_path=db_path)
    set_active_db(db_path)   # all get_connection() calls now point here
    return db_path

# ============================================================================
# JOB RELEVANCE CLASSIFIER  (profile-aware)
# ============================================================================

# Fallback lists used when no profile is loaded (your own profile)
_DEFAULT_BLOCK = [
    "doctor", "physician", "nurse", "pharmacist", "surgeon", "dentist",
    "optometrist", "therapist", "physiotherapist", "radiographer", "dietitian",
    "audiologist", "medical officer", "clinical",
    "wealth advisor", "wealth adviser", "wealth manager",
    "financial advisor", "financial adviser", "financial planner",
    "insurance agent", "insurance advisor", "bancassurance",
    "relationship manager", "private banker", "remisier",
    "part time", "part-time", "internship", "intern ", "[entry level]", "entry level",
]
_DEFAULT_CORE = [
    "product owner", "product manager", "product director",
    "product management lead", "product management head",
    "digital product", "platform owner", "platform manager",
    "payments product", "fraud product", "digital identity",
    "ai product", "head of product", "vp product", "chief product",
    "innovation lead", "innovation manager",
    "digital transformation", "squad lead", "chapter lead",
    "digital banking lead", "digital banking product",
]
_DEFAULT_ADJACENT = [
    "digital", "agile", "fintech", "transformation",
    "technology lead", "tech lead", "programme manager",
    "delivery manager", "business analyst", "solution owner",
    "innovation", "platform", "product",
]


def _get_classifier_lists() -> tuple:
    """Return (block, core, adjacent) lists from active profile or defaults."""
    profile = st.session_state.get("profile", {})
    if profile and profile.get("classifier"):
        c = get_classifier(profile)
        return c["block"], c["core"], c["adjacent"]
    return _DEFAULT_BLOCK, _DEFAULT_CORE, _DEFAULT_ADJACENT


def classify_job(title: str) -> tuple:
    block, core, adjacent = _get_classifier_lists()
    t = title.lower()
    for kw in block:
        if kw in t:
            return ("block", f"Excluded: matches '{kw}'")
    for kw in core:
        if kw in t:
            return ("core", f"Core target: '{kw}'")
    for kw in adjacent:
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
    initial_sidebar_state="expanded",
)

# Inject Streamlit Cloud secrets into env vars so api_integration picks them up
try:
    for _key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        if _key in st.secrets and not os.environ.get(_key):
            os.environ[_key] = st.secrets[_key]
except Exception:
    pass

# Auth + profile — must run before any other st.* calls that render content
_check_password()
_active_db = _bootstrap_profile()

# ============================================================================
# GLOBAL SETTINGS HELPERS  (module-level, used by all pages)
# ============================================================================

def get_setting(key, default=None):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM user_settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default


def save_setting(key, value):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO user_settings (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    conn.commit()
    conn.close()

# ============================================================================
# MODEL HELPERS
# ============================================================================

def _build_eval_model_options(gemini_ok, claude_ok, ollama_ok):
    opts = []
    if gemini_ok:
        opts += [
            "⚡ Gemini 2.5 Flash (fast · free tier)",
            "🧠 Gemini 2.5 Pro (best · free tier)",
        ]
    if claude_ok:
        opts += [
            "☁️ Claude Haiku (accurate · ~$0.01)",
            "☁️ Claude Sonnet (nuanced · ~$0.05)",
        ]
    if ollama_ok:
        opts += [
            "🖥️ Llama 3.1 8B (local · free)",
            "🖥️ Mistral 7B (local · free)",
        ]
    return opts or ["⚠️ No models available — add Gemini or Claude API key"]


def _run_eval_with_label(label, job_desc, ollama_ok):
    """Dispatch an evaluation to the correct model based on the label string."""
    gemini_model = "gemini-2.5-pro" if "Pro" in label else "gemini-2.5-flash"
    claude_model = (
        "claude-sonnet-4-6" if "Sonnet" in label else "claude-haiku-4-5-20251001"
    )
    ollama_model = "llama3.1:8b" if "Llama" in label else "mistral:latest"

    if label.startswith("⚡") or label.startswith("🧠 Gemini"):
        return gemini_evaluate_job(job_desc, model=gemini_model)
    elif label.startswith("☁️"):
        return claude_evaluate_job(job_desc, model=claude_model)
    elif label.startswith("🖥️"):
        if not ollama_ok:
            return {
                "status": "error",
                "error": "Ollama is not running. Start it with: ollama serve",
            }
        return ollama_evaluate_job(job_desc, model=ollama_model)
    else:
        return {
            "status": "error",
            "error": "No evaluation model available. Add a Gemini or Claude API key.",
        }


def _best_eval_label(gemini_ok, claude_ok, ollama_ok):
    """Return the top model label respecting user's default_evaluator preference."""
    pref = get_setting("default_evaluator", "gemini")
    opts = _build_eval_model_options(gemini_ok, claude_ok, ollama_ok)
    if opts[0].startswith("⚠️"):
        return None
    # Try to find an option matching the preference
    for o in opts:
        if pref.lower() in o.lower():
            return o
    return opts[0]


# ============================================================================
# SCROLLABLE TABLE HELPER
# ============================================================================

def render_scroll_table(headers: list, rows: list, height: int = 440):
    th_cells = "".join(f"<th>{h}</th>" for h in headers)
    tr_cells = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    html = f"""
    <html><head><style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0e1117; font-family: sans-serif; font-size: 0.87rem; color: #cfcfcf; }}
    .wrap {{
        height: {height}px;
        overflow-y: scroll; overflow-x: auto;
        border: 1px solid rgba(255,255,255,0.12); border-radius: 10px;
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
        border-bottom: 2px solid #4a9eff; white-space: nowrap;
    }}
    tbody tr:hover {{ background: rgba(74,158,255,0.09); }}
    tbody td {{ padding: 8px 14px;
                border-bottom: 1px solid rgba(255,255,255,0.07); vertical-align: top; }}
    </style></head>
    <body>
      <div class="wrap">
        <table><thead><tr>{th_cells}</tr></thead><tbody>{tr_cells}</tbody></table>
      </div>
    </body></html>
    """
    components.html(html, height=height + 4, scrolling=False)


# ============================================================================
# GLOBAL STYLES
# ============================================================================
st.markdown("""
<style>
::-webkit-scrollbar          { width: 14px; height: 14px; }
::-webkit-scrollbar-track    { background: #1a1a2e; border-radius: 10px; }
::-webkit-scrollbar-thumb    { background: #4a9eff; border-radius: 10px;
                                border: 3px solid #1a1a2e; }
::-webkit-scrollbar-thumb:hover { background: #74b9ff; }
* { scrollbar-width: thin; scrollbar-color: #4a9eff #1a1a2e; }

.scroll-table-wrap {
    height: 460px !important; overflow-y: scroll !important;
    overflow-x: auto !important;
    border: 1px solid rgba(255,255,255,0.12); border-radius: 10px;
    margin-bottom: 1rem; display: block !important;
}
.scroll-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
.scroll-table thead th {
    position: sticky; top: 0; z-index: 2;
    background: #0e1117; color: #e0e0e0;
    padding: 10px 14px; text-align: left;
    border-bottom: 2px solid #4a9eff; white-space: nowrap;
}
.scroll-table tbody tr { transition: background 0.15s; }
.scroll-table tbody tr:hover { background: rgba(74,158,255,0.08); }
.scroll-table tbody td {
    padding: 8px 14px;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    color: #cfcfcf; vertical-align: top;
}
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px;
         font-size: 0.75rem; font-weight: 600; }
.badge-eval  { background: #1a6b3c; color: #6fcf97; }
.badge-pend  { background: #4a3700; color: #f0b429; }
</style>
""", unsafe_allow_html=True)

# ============================================================================
# SIDEBAR
# ============================================================================
st.sidebar.title("🎯 Career-Ops")

# ── Profile selector (hidden when env var locks the profile) ──────────────────
_env_locked = bool(get_env_profile() != "default" or
                   (hasattr(st, "secrets") and st.secrets.get("CAREER_OPS_PROFILE")))
if not _env_locked:
    _profile_options = list(AVAILABLE_PROFILES.keys())
    _profile_labels  = [
        f"{v['emoji']} {v['display']}" for v in AVAILABLE_PROFILES.values()
    ]
    _cur_idx = _profile_options.index(st.session_state.get("profile_name", "default"))
    _selected_label = st.sidebar.selectbox(
        "Profile", _profile_labels, index=_cur_idx, key="profile_selector"
    )
    _selected_name = _profile_options[_profile_labels.index(_selected_label)]
    if _selected_name != st.session_state.get("profile_name"):
        st.session_state.profile_name = _selected_name
        st.session_state.profile = load_profile(_selected_name)
        st.session_state.pop("authenticated", None)  # keep auth but reload DB
        st.rerun()
else:
    _p = AVAILABLE_PROFILES.get(st.session_state.get("profile_name", "default"), {})
    st.sidebar.caption(f"{_p.get('emoji','')} {_p.get('display','')}")

st.sidebar.divider()

# Sync active profile persona into api_integration
_api_set_profile(st.session_state.get("profile", {}))

ollama_ok = check_ollama_health()
claude_ok = False  # disabled on Streamlit Cloud — Gemini covers all eval needs
gemini_ok = check_gemini_api()

# Model status — green dot = ready, grey = unavailable, no alarming red ❌ for Ollama
def _dot(ok):
    return "🟢" if ok else "🔘"

st.sidebar.markdown(
    f"{_dot(gemini_ok)} Gemini &nbsp; "
    f"{'🟢' if ollama_ok else '⚫'} Ollama *(local · optional)*",
    unsafe_allow_html=True,
)

if not gemini_ok and not claude_ok and not ollama_ok:
    st.sidebar.warning("⚠️ No AI models available. Add a key in Settings → AI.")

st.sidebar.divider()

page = st.sidebar.radio(
    "Navigation",
    ["📋 Pipeline", "⚖️ Compare", "⚙️ Settings"],
)

st.sidebar.divider()

# Live stats
conn = get_connection()
c = conn.cursor()
c.execute("SELECT COUNT(*) FROM jobs WHERE COALESCE(status,'discovered') != 'rejected'")
total_jobs = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM evaluations")
total_evals = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM applications WHERE status='applied'")
applied_count = c.fetchone()[0]
conn.close()

st.sidebar.metric("Active Jobs", total_jobs)
st.sidebar.metric("Evaluated",   total_evals)
st.sidebar.metric("Applied",     applied_count)

st.sidebar.divider()

if st.sidebar.button("🔄 Clear Cache", width="stretch"):
    st.cache_data.clear()
    st.cache_resource.clear()
    st.success("✅ Cache cleared!")
    time.sleep(0.8)
    st.rerun()

# ============================================================================
# PAGE: PIPELINE   (Search + My Jobs merged)
# ============================================================================
if page == "📋 Pipeline":

    st.header("📋 Pipeline")

    # Session state
    if "eval_results"   not in st.session_state: st.session_state.eval_results   = {}
    if "spotlight_id"   not in st.session_state: st.session_state.spotlight_id   = None
    if "last_scan_ids"  not in st.session_state: st.session_state.last_scan_ids  = set()

    # ── Tier badge helpers ────────────────────────────────────────────────────
    tier_map = {
        "core":     "🎯 Core target",
        "adjacent": "🔍 Adjacent",
        "low":      "⚠️ Low relevance",
        "block":    "🚫 Blocked",
    }

    def _qs_badge(score):
        if score is None: return "—"
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

    # ── COLLAPSIBLE SCAN PANEL ────────────────────────────────────────────────
    # Auto-open if no jobs yet; otherwise collapsed by default
    _scan_auto_open = (total_jobs == 0)

    with st.expander("🔍 Run New Scan", expanded=_scan_auto_open):
        st.caption("🕷️ Scrape live jobs from public boards and company portals")

        # Load search defaults — profile overrides saved settings
        from profile_loader import get_defaults as _get_defaults
        _prof_defaults = _get_defaults(st.session_state.get("profile", {}))
        _def_keyword  = get_setting("default_keyword",  _prof_defaults["keyword"])
        _def_sal_min  = int(get_setting("default_sal_min", str(_prof_defaults["salary_min"])))
        _def_sal_max  = int(get_setting("default_sal_max", str(_prof_defaults["salary_max"])))

        predefined_terms = _prof_defaults["quick_terms"] or [
            "AI Product Manager Banking",
            "Digital Banking Platform",
            "AI Product Leadership",
            "AI Transformation",
            "Digital Risk Management",
            "Anti-Fraud & Compliance",
        ]

        dd_col, kw_col, cnt_col = st.columns([2, 2, 1])
        with dd_col:
            selected_term = st.selectbox(
                "Quick Select",
                options=["Custom..."] + predefined_terms,
                help="Choose a pre-set search term or type your own",
            )
        with kw_col:
            keywords = st.text_input(
                "Job Keywords",
                value=selected_term if selected_term != "Custom..." else _def_keyword,
                help="Job title or keywords to search across selected sources",
            )
        with cnt_col:
            num_results = st.slider(
                "Max Results", min_value=5, max_value=50, value=15,
                help="Total jobs returned across all sources",
            )

        st.divider()

        # ── Job Board sources ─────────────────────────────────────────────────
        _all_boards    = ["MyCareersFuture", "Indeed", "JobStreet", "LinkedIn",
                          "Glassdoor", "Glints", "Tech in Asia"]
        _all_direct    = ["DBS", "Grab", "Sea Group", "Airwallex",
                          "Thought Machine", "Thunes", "Anthropic"]
        _all_mcf       = ["OCBC", "UOB", "Standard Chartered", "Citibank",
                          "HSBC", "Wise", "Nium", "Revolut", "Singtel"]
        _all_gov       = ["HTX", "MAS", "IMDA", "CSA"]
        _all_careers_gov = ["GovTech"]

        def _toggle_boards():
            v = st.session_state["select_all_boards"]
            for b in _all_boards: st.session_state[f"board_{b}"] = v

        def _toggle_companies():
            v = st.session_state["select_all_companies"]
            for c in _all_direct + _all_mcf + _all_gov + _all_careers_gov:
                st.session_state[f"co_{c}"] = v

        hdr_c, all_c = st.columns([5, 1])
        with hdr_c:
            st.subheader("📰 Public Job Boards")
            st.caption("Scraped directly from each site.")
        with all_c:
            st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
            st.checkbox("All", value=False, key="select_all_boards",
                        on_change=_toggle_boards)

        board_cols1 = st.columns(4)
        board_cols2 = st.columns(4)
        _board_cols = {
            "MyCareersFuture": board_cols1[0], "Indeed":      board_cols1[1],
            "JobStreet":       board_cols1[2], "LinkedIn":    board_cols1[3],
            "Glassdoor":       board_cols2[0], "Glints":      board_cols2[1],
            "Tech in Asia":    board_cols2[2],
        }
        sources = []
        for board, col in _board_cols.items():
            with col:
                if st.checkbox(board, value=False, key=f"board_{board}"):
                    sources.append(board)

        if any(b in sources for b in ["LinkedIn", "Glassdoor", "Glints", "Tech in Asia"]):
            st.caption("⚠️ LinkedIn & Glassdoor have bot-detection — results may vary")

        st.divider()

        hdr_c2, all_c2 = st.columns([5, 1])
        with hdr_c2:
            st.subheader("🏢 Company Direct Search")
            st.caption("Search specific companies' portals. Some via MCF, some via direct APIs.")
        with all_c2:
            st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
            st.checkbox("All", value=False, key="select_all_companies",
                        on_change=_toggle_companies)

        selected_companies = []

        st.markdown("**🔗 Direct Company Portals**")
        cols_direct = st.columns(len(_all_direct))
        for i, company in enumerate(_all_direct):
            with cols_direct[i]:
                if st.checkbox(company, value=False, key=f"co_{company}"):
                    selected_companies.append(company)

        st.markdown("**📋 Via MyCareersFuture**")
        cols_mcf = st.columns(5)
        for i, company in enumerate(_all_mcf):
            with cols_mcf[i % 5]:
                if st.checkbox(company, value=False, key=f"co_{company}"):
                    selected_companies.append(company)

        st.markdown("**🏛️ Singapore Gov & Statutory Boards — Careers@Gov**")
        st.caption("HTX · MAS · IMDA · CSA · GovTech")
        cols_gov = st.columns(len(_all_gov))
        for i, company in enumerate(_all_gov):
            with cols_gov[i]:
                if f"co_{company}" not in st.session_state:
                    st.session_state[f"co_{company}"] = (company == "HTX")
                if st.checkbox(company, value=st.session_state[f"co_{company}"],
                               key=f"co_{company}"):
                    selected_companies.append(company)

        cols_cg = st.columns(len(_all_careers_gov))
        for i, company in enumerate(_all_careers_gov):
            with cols_cg[i]:
                if st.checkbox(company, value=False, key=f"co_{company}"):
                    selected_companies.append(company)

        st.divider()

        sal_col, exp_col = st.columns(2)
        with sal_col:
            st.markdown("**💰 Monthly Salary Range (SGD)**")
            sc1, sc2 = st.columns(2)
            with sc1:
                salary_min = st.number_input(
                    "Min", min_value=0, max_value=50000,
                    value=_def_sal_min, step=500, key="sal_min",
                )
            with sc2:
                salary_max = st.number_input(
                    "Max", min_value=0, max_value=50000,
                    value=_def_sal_max, step=500, key="sal_max",
                )
            if salary_max:
                st.caption(f"≈ SGD {salary_min*12:,}–{salary_max*12:,}/yr")
            else:
                st.caption(f"≈ SGD {salary_min*12:,}+/yr")

        with exp_col:
            st.markdown("**🎓 Years of Experience**")
            exp_option = st.selectbox(
                "Minimum experience",
                ["Any", "1+ years", "3+ years", "5+ years", "8+ years", "10+ years"],
                index=4, label_visibility="collapsed",
            )
            min_years = {"Any": 0, "1+ years": 1, "3+ years": 3,
                         "5+ years": 5, "8+ years": 8, "10+ years": 10}[exp_option]
            if min_years:
                st.caption(f"MCF filters for {min_years}+ years experience")

        total_sources  = len(sources) + len(selected_companies)
        est_seconds    = max(20, total_sources * 15)

        if st.button("🔎 Scrape Live Jobs", width="stretch", type="primary"):
            if total_sources == 0:
                st.error("Select at least one job board or company.")
            else:
                source_summary = ", ".join(
                    sources + [f"Direct:{c}" for c in selected_companies]
                )
                with st.spinner(f"🕷️ Scraping {total_sources} source(s) — est. {est_seconds}s…"):
                    result = scrape_jobs(
                        keywords, sources, num_results, selected_companies,
                        salary_min=salary_min,
                        salary_max=salary_max if salary_max > 0 else None,
                        min_years=min_years,
                    )

                if result["status"] == "success" and result["count"] > 0:
                    conn = get_connection()
                    c = conn.cursor()
                    new_ids = []
                    for job in result["jobs"]:
                        job_url = job.get("url", "").strip()
                        if job_url:
                            from urllib.parse import urlparse
                            parsed = urlparse(job_url)
                            if parsed.fragment:
                                base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}#{parsed.fragment}"
                            else:
                                base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                            c.execute("SELECT id FROM jobs WHERE url LIKE ?", (f"{base_url}%",))
                            if c.fetchone():
                                continue
                        c.execute(
                            "INSERT INTO jobs (company, title, description, salary, location, url, source, notes) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                job.get("company", ""),
                                job.get("title", ""),
                                job.get("description", ""),
                                job.get("salary", ""),
                                job.get("location", "Singapore"),
                                job.get("url", ""),
                                job.get("source", "scraper"),
                                f"Scraped from {job.get('source', 'unknown')}",
                            ),
                        )
                        new_ids.append(c.lastrowid)
                    conn.commit()
                    conn.close()

                    st.session_state.last_scan_ids = set(new_ids)
                    st.success(
                        f"✅ {len(new_ids)} new jobs saved from: {source_summary} — "
                        f"shown below with 🆕"
                    )
                    st.rerun()
                elif result.get("count", 0) == 0:
                    st.warning("⚠️ No jobs found. Try different keywords or sources.")
                else:
                    st.error(f"❌ Scraping failed: {result.get('error', 'Unknown error')}")

    # ── Load all active jobs ──────────────────────────────────────────────────
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        SELECT j.id, j.company, j.title, j.salary, j.location,
               COALESCE(e.score, 0) as eval_score,
               CASE WHEN e.id IS NOT NULL THEN '✅ Evaluated' ELSE '⏳ Pending' END as eval_status,
               j.source, j.quick_score, j.quick_reason, j.description, j.status
        FROM jobs j
        LEFT JOIN evaluations e ON j.id = e.job_id
        WHERE COALESCE(j.status, 'discovered') != 'rejected'
        ORDER BY COALESCE(e.score, 0) DESC, j.quick_score DESC NULLS LAST, j.created_at DESC
    ''')
    rows = c.fetchall()

    c.execute('''
        SELECT j.id, j.company, j.title, j.salary, j.location,
               COALESCE(e.score, 0), CASE WHEN e.id IS NOT NULL THEN '✅ Evaluated' ELSE '⏳ Pending' END,
               j.source, j.quick_score, j.quick_reason, j.description, j.status, j.notes
        FROM jobs j
        LEFT JOIN evaluations e ON j.id = e.job_id
        WHERE j.status = 'rejected'
        ORDER BY COALESCE(e.score, 0) DESC, j.created_at DESC
    ''')
    archived_rows = c.fetchall()
    conn.close()

    if not rows and not archived_rows:
        if not _scan_auto_open:
            st.info("No jobs yet — use **🔍 Run New Scan** above to get started.")
    else:
        total    = len(rows)
        archived = len(archived_rows)
        evaluated = sum(1 for r in rows if r[6] == '✅ Evaluated')
        screened  = sum(1 for r in rows if r[8] is not None)
        pending   = total - evaluated

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Active",    total)
        m2.metric("⚡ Screened", screened)
        m3.metric("✅ Evaluated", evaluated)
        m4.metric("⏳ Pending",  pending)
        m5.metric("🗂️ Archived", archived)

        # ── Bulk Quick Screen ─────────────────────────────────────────────────
        unscreened = [r for r in rows if r[8] is None]
        qs_model_opts = []
        if gemini_ok: qs_model_opts += ["⚡ Gemini 2.5 Flash (fast · free tier)"]
        if claude_ok: qs_model_opts += ["☁️ Claude Haiku (accurate · ~$0.002/job)"]
        if ollama_ok: qs_model_opts += ["🖥️ Ollama Mistral (local · free)"]

        if not qs_model_opts:
            st.warning("⚠️ No screening models available — configure Gemini or Claude in Settings → AI.")
        elif unscreened:
            sc1, sc2, sc3 = st.columns([2, 1.5, 2])
            with sc1:
                qs_model_label = st.selectbox(
                    "Screen with", qs_model_opts, key="qs_model_select",
                    label_visibility="collapsed",
                )
            with sc2:
                run_qs = st.button(
                    f"⚡ Screen {len(unscreened)} unscreened",
                    width="stretch", type="primary",
                )
            with sc3:
                qs_hints = {
                    "⚡ Gemini 2.5 Flash (fast · free tier)": "~2–4s per job · Google free quota",
                    "☁️ Claude Haiku (accurate · ~$0.002/job)": "~3–6s per job · billed to API key",
                    "🖥️ Ollama Mistral (local · free)": "~5–15s per job · requires Ollama running",
                }
                st.caption(qs_hints.get(qs_model_label, ""))

            if run_qs:
                prog = st.progress(0, text="Starting quick screen…")
                conn2 = get_connection()
                c2 = conn2.cursor()
                for i, row in enumerate(unscreened):
                    job_id_qs, company_qs, title_qs, desc_qs = row[0], row[1], row[2], row[10] or ""
                    prog.progress(
                        i / len(unscreened),
                        text=f"Screening {i+1}/{len(unscreened)}: {company_qs} — {title_qs[:35]}",
                    )
                    if "Gemini" in qs_model_label:
                        qs_result = gemini_quick_screen(title_qs, desc_qs, model="gemini-2.5-flash")
                    elif "Claude" in qs_model_label:
                        qs_result = claude_quick_screen(title_qs, desc_qs, model="claude-haiku-4-5-20251001")
                    else:
                        qs_result = ollama_quick_screen(title_qs, desc_qs)
                    if qs_result["status"] == "success":
                        c2.execute(
                            "UPDATE jobs SET quick_score=?, quick_reason=? WHERE id=?",
                            (qs_result["score"], qs_result["reason"], job_id_qs),
                        )
                        conn2.commit()
                prog.progress(1.0, text="✅ Quick screen complete!")
                conn2.close()
                st.rerun()
        else:
            st.success("⚡ All jobs screened — sorted by relevance score")

        # ── Filter controls ───────────────────────────────────────────────────
        f1, f2, f3 = st.columns([2, 2, 1])
        with f1:
            search = st.text_input(
                "🔍 Search", placeholder="Company or title…", key="myjobs_search",
            )
        with f2:
            tier_filter = st.multiselect(
                "Show tiers",
                ["🎯 Core target", "🔍 Adjacent", "⚠️ Low relevance", "🚫 Blocked"],
                default=["🎯 Core target", "🔍 Adjacent"],
                key="myjobs_tier_filter",
            )
        with f3:
            show_new_only = False
            if st.session_state.last_scan_ids:
                show_new_only = st.checkbox(
                    f"🆕 New only ({len(st.session_state.last_scan_ids)})",
                    value=False, key="show_new_only",
                )

        # Classify + filter
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
            if show_new_only and r[0] not in st.session_state.last_scan_ids:
                continue
            display_rows.append((r, tier, cl_reason))

        st.caption(
            f"Showing {len(display_rows)} of {total} · "
            f"{blocked_count} blocked · "
            "👆 Click a row to open the spotlight"
        )

        # ── Job table ─────────────────────────────────────────────────────────
        conn_applied = get_connection()
        c_applied = conn_applied.cursor()
        c_applied.execute("SELECT job_id FROM applications WHERE status = 'applied'")
        applied_job_ids = {row[0] for row in c_applied.fetchall()}
        conn_applied.close()

        tier_short = {
            "core": "🎯 Core", "adjacent": "🔍 Adjacent",
            "low": "⚠️ Low",  "block": "🚫 Blocked",
        }
        df_data = []
        for r, tier, _ in display_rows:
            new_mark     = "🆕" if r[0] in st.session_state.last_scan_ids else ""
            applied_mark = "📮" if r[0] in applied_job_ids else "—"
            df_data.append({
                "ID":      r[0],
                "Company": " ".join(r[1].split()),
                "Title":   " ".join(r[2].split()),
                "Salary":  r[3] or "—",
                "Tier":    tier_short.get(tier, tier),
                "Fit ⚡":  f"{r[8]:.1f}" if r[8] is not None else "—",
                "Eval":    f"{r[5]:.1f}" if r[5] else "—",
                "📮":      applied_mark,
                "🆕":      new_mark,
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
                "📮":      st.column_config.TextColumn(width=40),
                "🆕":      st.column_config.TextColumn(width=40),
                "Source":  st.column_config.TextColumn(width="medium"),
            },
        )

        selected_rows = tbl_event.selection.rows
        if selected_rows:
            selected_spot_id = display_rows[selected_rows[0]][0][0]
            st.session_state.spotlight_id = selected_spot_id
        elif st.session_state.spotlight_id is not None:
            ids_visible = {r[0] for r, _, _ in display_rows}
            if st.session_state.spotlight_id not in ids_visible:
                st.session_state.spotlight_id = None

        # ── SPOTLIGHT PANEL ───────────────────────────────────────────────────
        if st.session_state.spotlight_id is None:
            st.info("👆 Click any row above to view job details and run an evaluation.")
        else:
            selected_spot_id = st.session_state.spotlight_id

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

                # Action row
                del_col1, del_col2, del_col3 = st.columns([5, 2.5, 2])

                conn_app = get_connection()
                c_app = conn_app.cursor()
                c_app.execute(
                    "SELECT id, applied_at FROM applications WHERE job_id = ?",
                    (selected_spot_id,),
                )
                app_row = c_app.fetchone()
                conn_app.close()

                is_applied = app_row is not None
                applied_date_str = ""
                if is_applied and app_row[1]:
                    try:
                        applied_date_str = datetime.fromisoformat(app_row[1]).strftime("%b %d, %Y")
                    except:
                        applied_date_str = "Applied"

                with del_col1:
                    if is_applied:
                        st.caption(f"📮 Applied {applied_date_str} · ID: {spot_id}")
                    else:
                        st.caption(f"ID: {spot_id}")

                with del_col2:
                    if not is_applied:
                        if st.button("✅ Mark Applied", key=f"apply_{selected_spot_id}",
                                     width="stretch", type="secondary"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "INSERT INTO applications (job_id, status, applied_at) VALUES (?, ?, ?)",
                                (selected_spot_id, "applied", datetime.now().isoformat()),
                            )
                            conn.commit(); conn.close()
                            st.success("✅ Marked as applied"); st.rerun()
                    else:
                        if st.button("🗂️ Undo Apply", key=f"unapply_{selected_spot_id}",
                                     width="stretch", type="secondary"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("DELETE FROM applications WHERE job_id = ?",
                                      (selected_spot_id,))
                            conn.commit(); conn.close()
                            st.info("Cleared applied status"); st.rerun()

                with del_col3:
                    if st.button("🗑️ Delete", key=f"del_{selected_spot_id}",
                                 width="stretch", type="secondary"):
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute("DELETE FROM jobs WHERE id = ?", (selected_spot_id,))
                        c.execute("DELETE FROM evaluations WHERE job_id = ?", (selected_spot_id,))
                        c.execute("DELETE FROM applications WHERE job_id = ?", (selected_spot_id,))
                        conn.commit(); conn.close()
                        st.success("✅ Job deleted")
                        st.session_state.spotlight_id = None
                        time.sleep(0.5); st.rerun()

                # Description
                with st.expander("📄 Job Description", expanded=False):
                    if spot_desc and spot_desc.strip():
                        st.markdown(spot_desc)
                    else:
                        st.info("No description stored. Paste the full JD below.")

                # ── Quick Screen (single job) ─────────────────────────────────
                st.divider()
                st.subheader("⚡ Quick Relevance Screen")
                st.caption("Fast preliminary check (2–15s)")

                qs_single_opts = []
                if gemini_ok: qs_single_opts += ["⚡ Gemini 2.5 Flash"]
                if claude_ok: qs_single_opts += ["☁️ Claude Haiku"]
                if ollama_ok: qs_single_opts += ["🖥️ Ollama Mistral"]

                if not qs_single_opts:
                    st.warning("⚠️ No screening models available")
                else:
                    qs_c1, qs_c2 = st.columns([2, 1])
                    with qs_c1:
                        qs_model_single = st.selectbox(
                            "Model", qs_single_opts,
                            key=f"qs_model_single_{selected_spot_id}",
                            label_visibility="collapsed",
                        )
                    with qs_c2:
                        run_qs_single = st.button(
                            "▶️ Screen", width="stretch", type="primary",
                            key=f"qs_btn_{selected_spot_id}",
                        )

                    if run_qs_single:
                        if not spot_desc or len(spot_desc.strip()) < 80:
                            st.warning("⚠️ Description too short. Paste the full JD first.")
                        else:
                            with st.spinner("Screening job…"):
                                if "Gemini" in qs_model_single:
                                    qs_r = gemini_quick_screen(spot_title, spot_desc, model="gemini-2.5-flash")
                                elif "Claude" in qs_model_single:
                                    qs_r = claude_quick_screen(spot_title, spot_desc, model="claude-haiku-4-5-20251001")
                                else:
                                    qs_r = ollama_quick_screen(spot_title, spot_desc)
                            if qs_r["status"] == "success":
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute(
                                    "UPDATE jobs SET quick_score=?, quick_reason=? WHERE id=?",
                                    (qs_r["score"], qs_r["reason"], selected_spot_id),
                                )
                                conn.commit(); conn.close()
                                st.success(f"✅ Score: {qs_r['score']:.1f}/5 · {qs_r['reason']}")
                                st.rerun()
                            else:
                                st.error(f"❌ {qs_r.get('error', 'Unknown error')}")

                # ── Deep Evaluation Panel ─────────────────────────────────────
                st.divider()

                conn = get_connection()
                c = conn.cursor()
                c.execute(
                    "SELECT score, legitimacy, archetype, blocks FROM evaluations WHERE job_id = ?",
                    (selected_spot_id,),
                )
                eval_db = c.fetchone()
                conn.close()

                if selected_spot_id in st.session_state.eval_results and eval_db is None:
                    fresh = st.session_state.eval_results[selected_spot_id]
                    try:
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute(
                            "INSERT INTO evaluations (job_id, score, blocks, legitimacy, archetype, summary, model) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (selected_spot_id, fresh["score"], fresh["evaluation"],
                             fresh["legitimacy"], fresh["archetype"],
                             f"{fresh['company']} - {fresh['role']}", fresh["model"]),
                        )
                        conn.commit(); conn.close()
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute(
                            "SELECT score, legitimacy, archetype, blocks FROM evaluations WHERE job_id = ?",
                            (selected_spot_id,),
                        )
                        eval_db = c.fetchone()
                        conn.close()
                    except Exception as save_err:
                        st.warning(f"Could not save evaluation: {save_err}")

                if eval_db:
                    ev_score, ev_legit, ev_arch, ev_blocks = eval_db
                    bar_color  = "#1a6b3c" if ev_score >= 4 else "#4a3700" if ev_score >= 3 else "#6b1a1a"
                    text_color = "#6fcf97" if ev_score >= 4 else "#f0b429" if ev_score >= 3 else "#ff7675"

                    legit_emoji = {"Legitimate": "✅", "Verify": "⚠️", "Fraudulent": "🚩"}.get(ev_legit, "❓")
                    legit_label = {
                        "Legitimate": "Real Job", "Verify": "Has Red Flags",
                        "Fraudulent": "Do Not Apply",
                    }.get(ev_legit, ev_legit or "—")

                    st.markdown(
                        f"""<div style="background:{bar_color};border-radius:10px;
                        padding:14px 20px;margin-bottom:12px">
                        <span style="color:{text_color};font-size:1.6rem;font-weight:700">
                        {ev_score:.1f}/5.0</span>
                        <span style="color:#ccc;font-size:0.95rem;margin-left:16px">
                        {legit_emoji} {legit_label} &nbsp;·&nbsp; {ev_arch or '—'}
                        </span></div>""",
                        unsafe_allow_html=True,
                    )
                    with st.expander("📖 Full A–G Evaluation", expanded=True):
                        display_eval = re.sub(
                            r'^#{1,3} ', '#### ', ev_blocks or "No evaluation content stored.",
                            flags=re.MULTILINE,
                        )
                        st.markdown(display_eval)

                    btn_col1, btn_col2, btn_col3 = st.columns(3)
                    with btn_col1:
                        if st.button("🔄 Re-evaluate", type="secondary",
                                     key=f"reeval_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("DELETE FROM evaluations WHERE job_id = ?", (selected_spot_id,))
                            conn.commit(); conn.close()
                            if selected_spot_id in st.session_state.eval_results:
                                del st.session_state.eval_results[selected_spot_id]
                            st.rerun()
                    with btn_col2:
                        if st.button("🗂️ Archive", type="secondary",
                                     key=f"archive_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("UPDATE jobs SET status = 'rejected' WHERE id = ?",
                                      (selected_spot_id,))
                            conn.commit(); conn.close()
                            st.success("✅ Archived")
                            st.session_state.spotlight_id = None
                            time.sleep(0.5); st.rerun()
                    with btn_col3:
                        if ev_score >= 3.5:
                            if st.button("👍 Save Interest", type="secondary",
                                         key=f"save_int_{selected_spot_id}"):
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute("UPDATE jobs SET status = 'interested' WHERE id = ?",
                                          (selected_spot_id,))
                                conn.commit(); conn.close()
                                st.success("✅ Marked as interested"); st.rerun()

                else:
                    st.info("This job hasn't been deep-evaluated yet.")

                    pre_col1, pre_col2 = st.columns(2)
                    with pre_col1:
                        if st.button("🗂️ Archive", type="secondary",
                                     key=f"archive_unevaluated_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("UPDATE jobs SET status = 'rejected' WHERE id = ?",
                                      (selected_spot_id,))
                            conn.commit(); conn.close()
                            st.success("✅ Archived")
                            st.session_state.spotlight_id = None
                            time.sleep(0.5); st.rerun()
                    with pre_col2:
                        if st.button("👍 Save Interest", type="secondary",
                                     key=f"save_int_unevaluated_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("UPDATE jobs SET status = 'interested' WHERE id = ?",
                                      (selected_spot_id,))
                            conn.commit(); conn.close()
                            st.success("✅ Marked as interested"); st.rerun()

                    st.divider()

                    # Model selector + Deep Evaluate
                    model_col, btn_col, note_col = st.columns([1.5, 1, 2])
                    with model_col:
                        _eval_opts = _build_eval_model_options(gemini_ok, claude_ok, ollama_ok)
                        _pref_label = _best_eval_label(gemini_ok, claude_ok, ollama_ok)
                        _default_idx = _eval_opts.index(_pref_label) if _pref_label in _eval_opts else 0
                        eval_model_label = st.selectbox(
                            "🧠 Evaluator", _eval_opts, index=_default_idx,
                            key=f"eval_model_{selected_spot_id}",
                        )
                    with btn_col:
                        evaluate_btn = st.button(
                            "🚀 Deep Evaluate", type="primary", width="stretch",
                            key=f"eval_{selected_spot_id}",
                        )
                    with note_col:
                        hints = {
                            "⚡ Gemini 2.5 Flash (fast · free tier)": "Fast · good quality · ~15s",
                            "🧠 Gemini 2.5 Pro (best · free tier)": "Highest Gemini quality · ~30s",
                            "☁️ Claude Haiku (accurate · ~$0.01)": "Precise instruction-following · ~20s",
                            "☁️ Claude Sonnet (nuanced · ~$0.05)": "Most nuanced analysis · ~30s",
                            "🖥️ Llama 3.1 8B (local · free)": "Local · free · may hallucinate",
                            "🖥️ Mistral 7B (local · free)": "Local · free · weakest instruction-following",
                        }
                        st.caption(hints.get(eval_model_label, ""))

                    if evaluate_btn:
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute("SELECT company, description FROM jobs WHERE id = ?",
                                  (selected_spot_id,))
                        job_fetch = c.fetchone()
                        conn.close()

                        company_name = job_fetch[0] if job_fetch else "Unknown Company"
                        job_desc_raw  = job_fetch[1] if job_fetch else ""
                        desc_for_eval = (
                            f"COMPANY: {company_name}\n\n{job_desc_raw}" if job_desc_raw else ""
                        )

                        if not desc_for_eval or len(desc_for_eval.strip()) < 80:
                            st.warning("⚠️ Description too short. Paste the full JD below first.")
                        else:
                            with st.spinner(f"Running A–G evaluation using {eval_model_label}…"):
                                eval_result = _run_eval_with_label(
                                    eval_model_label, desc_for_eval, ollama_ok
                                )
                            if eval_result["status"] == "success":
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute(
                                    "INSERT INTO evaluations "
                                    "(job_id, score, blocks, legitimacy, archetype, summary, model) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (selected_spot_id, eval_result["score"],
                                     eval_result["evaluation"], eval_result["legitimacy"],
                                     eval_result["archetype"],
                                     f"{eval_result['company']} - {eval_result['role']}",
                                     eval_result["model"]),
                                )
                                conn.commit(); conn.close()
                                st.session_state.eval_results[selected_spot_id] = eval_result
                                st.rerun()
                            else:
                                st.error(f"❌ {eval_result.get('error')}")

                # ── Paste / Update JD expander ────────────────────────────────
                with st.expander("📎 Paste or update job description", expanded=False):
                    new_jd = st.text_area(
                        "Job Description", value=spot_desc or "", height=260,
                        key=f"jd_area_{selected_spot_id}",
                    )
                    djd_c1, djd_c2, djd_c3 = st.columns(3)
                    with djd_c1:
                        if st.button("💾 Save JD", width="stretch",
                                     key=f"save_jd_{selected_spot_id}"):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute("UPDATE jobs SET description = ? WHERE id = ?",
                                      (new_jd, selected_spot_id))
                            conn.commit(); conn.close()
                            st.success("✅ Description saved."); st.rerun()

                    # Best-model selector for Save & Evaluate
                    _se_opts = _build_eval_model_options(gemini_ok, claude_ok, ollama_ok)
                    _se_pref = _best_eval_label(gemini_ok, claude_ok, ollama_ok)
                    _se_idx  = _se_opts.index(_se_pref) if _se_pref in _se_opts else 0

                    with djd_c2:
                        se_model = st.selectbox(
                            "Model", _se_opts, index=_se_idx,
                            key=f"se_model_{selected_spot_id}",
                            label_visibility="collapsed",
                        )
                    with djd_c3:
                        if st.button("💾 Save & Evaluate", width="stretch", type="primary",
                                     key=f"save_eval_jd_{selected_spot_id}"):
                            if not new_jd or len(new_jd.strip()) < 80:
                                st.warning("⚠️ Description too short to evaluate.")
                            elif se_model.startswith("⚠️"):
                                st.error("❌ No evaluation model available.")
                            else:
                                conn = get_connection()
                                c = conn.cursor()
                                c.execute("UPDATE jobs SET description = ? WHERE id = ?",
                                          (new_jd, selected_spot_id))
                                c.execute("DELETE FROM evaluations WHERE job_id = ?",
                                          (selected_spot_id,))
                                conn.commit(); conn.close()
                                if selected_spot_id in st.session_state.eval_results:
                                    del st.session_state.eval_results[selected_spot_id]

                                conn = get_connection()
                                c = conn.cursor()
                                c.execute("SELECT company FROM jobs WHERE id = ?",
                                          (selected_spot_id,))
                                co_row = c.fetchone()
                                conn.close()
                                full_jd = f"COMPANY: {co_row[0] if co_row else ''}\n\n{new_jd}"

                                with st.spinner(f"Evaluating with {se_model}…"):
                                    eval_result = _run_eval_with_label(
                                        se_model, full_jd, ollama_ok
                                    )
                                if eval_result["status"] == "success":
                                    conn = get_connection()
                                    c = conn.cursor()
                                    c.execute(
                                        "INSERT INTO evaluations "
                                        "(job_id, score, blocks, legitimacy, archetype, summary, model) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                        (selected_spot_id, eval_result["score"],
                                         eval_result["evaluation"], eval_result["legitimacy"],
                                         eval_result["archetype"],
                                         f"{eval_result['company']} - {eval_result['role']}",
                                         eval_result["model"]),
                                    )
                                    conn.commit(); conn.close()
                                    st.success(f"✅ Saved & evaluated — score: {eval_result['score']:.1f}/5")
                                    st.rerun()
                                else:
                                    st.error(f"❌ {eval_result.get('error')}")

        # ── Add new job expander ──────────────────────────────────────────────
        with st.expander("➕ Add & evaluate a new job (paste JD)", expanded=False):
            st.caption("Add a job that isn't in the database yet.")
            nj_c1, nj_c2 = st.columns(2)
            with nj_c1:
                nj_company  = st.text_input("Company Name", placeholder="e.g., DBS Bank", key="nj_company")
                nj_title    = st.text_input("Job Title", placeholder="e.g., Senior Product Manager", key="nj_title")
                nj_url      = st.text_input("Job URL (optional)", placeholder="https://…", key="nj_url")
            with nj_c2:
                nj_salary   = st.text_input("Salary (optional)", value="SGD 140k+", key="nj_salary")
                nj_location = st.text_input("Location", value="Singapore", key="nj_location")
            nj_jd = st.text_area(
                "Full Job Description", placeholder="Paste the complete JD here…",
                height=240, key="nj_jd",
            )

            # Model selector — best available, no hardcoded Ollama
            _nj_opts = _build_eval_model_options(gemini_ok, claude_ok, ollama_ok)
            _nj_pref = _best_eval_label(gemini_ok, claude_ok, ollama_ok)
            _nj_idx  = _nj_opts.index(_nj_pref) if _nj_pref in _nj_opts else 0
            nj_col1, nj_col2 = st.columns([2, 1])
            with nj_col1:
                nj_model = st.selectbox(
                    "Evaluate with", _nj_opts, index=_nj_idx, key="nj_model",
                )
            with nj_col2:
                nj_eval_btn = st.button(
                    "🚀 Save & Evaluate", type="primary", width="stretch", key="nj_eval_btn",
                )

            if nj_eval_btn:
                if not nj_jd.strip():
                    st.error("Please paste a job description.")
                elif nj_model.startswith("⚠️"):
                    st.error("❌ No evaluation model available. Add a Gemini or Claude API key.")
                else:
                    with st.spinner(f"Running full A–G evaluation with {nj_model}…"):
                        nj_result = _run_eval_with_label(nj_model, nj_jd, ollama_ok)
                    if nj_result["status"] == "success":
                        conn = get_connection()
                        c = conn.cursor()
                        nj_id = None
                        is_duplicate = False
                        if nj_url and nj_url.strip():
                            c.execute("SELECT id FROM jobs WHERE url = ?", (nj_url.strip(),))
                            existing = c.fetchone()
                            if existing:
                                st.warning(
                                    f"⚠️ Job with this URL already exists (ID: {existing[0]})."
                                )
                                conn.close()
                                is_duplicate = True
                        if not is_duplicate:
                            c.execute(
                                "INSERT INTO jobs (company, title, salary, location, url, description, source) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (nj_company or "Unknown", nj_title or "Unknown",
                                 nj_salary or None, nj_location or None,
                                 nj_url or None, nj_jd, "manual_paste"),
                            )
                            nj_id = c.lastrowid
                            c.execute(
                                "INSERT INTO evaluations "
                                "(job_id, score, blocks, legitimacy, archetype, summary, model) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (nj_id, nj_result["score"], nj_result["evaluation"],
                                 nj_result["legitimacy"], nj_result["archetype"],
                                 f"{nj_result['company']} - {nj_result['role']}",
                                 nj_result["model"]),
                            )
                            conn.commit(); conn.close()
                            st.success(f"✅ Saved! Score: {nj_result['score']:.1f}/5.0")
                            st.session_state.spotlight_id = nj_id
                            st.rerun()
                    else:
                        st.error(f"❌ {nj_result.get('error', 'Unknown error')}")

        # ── Manage Jobs expander ──────────────────────────────────────────────
        with st.expander("🗑️ Manage Jobs", expanded=False):
            st.caption("Bulk operations — remove jobs from the database.")
            mgr_a, mgr_b, mgr_c = st.columns(3)

            with mgr_a:
                if st.button("🧹 Clear Unevaluated", width="stretch",
                             key="mgr_clear_uneval"):
                    conn2 = get_connection()
                    c2 = conn2.cursor()
                    c2.execute("DELETE FROM jobs WHERE id NOT IN (SELECT job_id FROM evaluations)")
                    n = c2.rowcount
                    conn2.commit(); conn2.close()
                    st.session_state.spotlight_id = None
                    st.success(f"✅ Removed {n} unevaluated jobs"); st.rerun()

            with mgr_b:
                source_list = sorted({r[7] for r in rows if r[7]})
                if source_list:
                    src_to_clear = st.selectbox("Clear by source", source_list,
                                                 key="mgr_clear_src_select")
                    if st.button(f"🗑️ Clear '{src_to_clear}'", width="stretch",
                                 key="mgr_clear_src_btn"):
                        conn2 = get_connection()
                        c2 = conn2.cursor()
                        c2.execute(
                            "DELETE FROM evaluations WHERE job_id IN "
                            "(SELECT id FROM jobs WHERE source = ?)", (src_to_clear,),
                        )
                        c2.execute("DELETE FROM jobs WHERE source = ?", (src_to_clear,))
                        n = c2.rowcount
                        conn2.commit(); conn2.close()
                        st.session_state.spotlight_id = None
                        st.success(f"✅ Removed {n} jobs from '{src_to_clear}'"); st.rerun()
                else:
                    st.info("No jobs to clear by source.")

            with mgr_c:
                st.warning("⚠️ Removes everything")
                if st.button("💣 Clear ALL Jobs", width="stretch",
                             type="secondary", key="mgr_clear_all"):
                    conn2 = get_connection()
                    c2 = conn2.cursor()
                    c2.execute("DELETE FROM evaluations")
                    c2.execute("DELETE FROM applications")
                    c2.execute("DELETE FROM jobs")
                    conn2.commit(); conn2.close()
                    st.session_state.spotlight_id = None
                    st.session_state.eval_results = {}
                    st.session_state.last_scan_ids = set()
                    st.success("✅ All jobs cleared."); st.rerun()

        # ── Archived Jobs ─────────────────────────────────────────────────────
        if archived_rows:
            st.divider()
            with st.expander(f"🗂️ Archived Jobs ({len(archived_rows)})", expanded=False):
                st.caption("Jobs marked as not a fit. Un-archive to reconsider.")
                archived_display = []
                for r in archived_rows:
                    jid, company, title, salary, location, ev_score, ev_status, source, qs_score, qs_reason, desc, status, notes = r
                    archived_display.append({
                        "ID": jid, "Company": company, "Title": title,
                        "Score": f"{ev_score:.1f}" if ev_score > 0 else "—",
                        "Notes": notes or "—",
                    })
                st.dataframe(pd.DataFrame(archived_display), width="stretch",
                             hide_index=True, key="arch_df")
                arch_id_col, arch_btn_col = st.columns([2, 1])
                with arch_id_col:
                    arch_id = st.number_input("Job ID to restore", min_value=1, step=1,
                                               key="arch_id_input")
                with arch_btn_col:
                    if st.button("↩️ Un-Archive", key="unarch_btn"):
                        if arch_id in [r[0] for r in archived_rows]:
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "UPDATE jobs SET status = 'discovered', notes = NULL WHERE id = ?",
                                (arch_id,),
                            )
                            conn.commit(); conn.close()
                            st.success(f"✅ Job {arch_id} restored"); st.rerun()
                        else:
                            st.error(f"Job {arch_id} not found in archive")

# ============================================================================
# PAGE: COMPARE
# ============================================================================
elif page == "⚖️ Compare":
    st.header("⚖️ Compare Evaluated Jobs")
    st.caption("Select 2–4 evaluated jobs to compare side-by-side")

    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        SELECT j.id, j.company, j.title, e.score, e.legitimacy, e.archetype, e.blocks
        FROM evaluations e
        JOIN jobs j ON e.job_id = j.id
        ORDER BY e.score DESC
    ''')
    cmp_evaluated = c.fetchall()
    conn.close()

    if not cmp_evaluated:
        st.info("No evaluated jobs yet — evaluate some jobs in the Pipeline first.")
    else:
        cmp_options = {
            f"{j[1]} — {j[2]} (Score: {j[3]:.1f})": j[0]
            for j in cmp_evaluated
        }
        cmp_selected = st.multiselect(
            "Choose 2–4 jobs to compare",
            list(cmp_options.keys()),
            max_selections=4,
        )

        if len(cmp_selected) >= 2:
            cmp_ids = [cmp_options[s] for s in cmp_selected]

            conn = get_connection()
            c = conn.cursor()
            comparison_data = []
            for cmp_id in cmp_ids:
                c.execute('''
                    SELECT j.company, j.title, j.salary, j.location,
                           e.score, e.legitimacy, e.archetype, e.model
                    FROM jobs j JOIN evaluations e ON j.id = e.job_id
                    WHERE j.id = ?
                ''', (cmp_id,))
                row = c.fetchone()
                if row:
                    legit_emoji = {"Legitimate": "✅", "Verify": "⚠️",
                                   "Fraudulent": "🚩"}.get(row[5], "❓")
                    comparison_data.append({
                        "Company":     row[0],
                        "Title":       row[1],
                        "Salary":      row[2] or "N/A",
                        "Location":    row[3] or "N/A",
                        "Score":       row[4],
                        "Legitimacy":  f"{legit_emoji} {row[5] or '—'}",
                        "Archetype":   row[6] or "—",
                        "Evaluated by": row[7] or "—",
                    })
            conn.close()

            st.dataframe(pd.DataFrame(comparison_data), width="stretch", hide_index=True)

            st.subheader("Detailed Evaluation Blocks")
            for i, cmp_id in enumerate(cmp_ids, 1):
                cmp_job = next((j for j in cmp_evaluated if j[0] == cmp_id), None)
                if cmp_job:
                    with st.expander(f"{i}. {cmp_job[1]} — {cmp_job[2]} · Score: {cmp_job[3]:.1f}"):
                        display_cmp = re.sub(
                            r'^#{1,3} ', '#### ',
                            cmp_job[6] or "No evaluation content stored.",
                            flags=re.MULTILINE,
                        )
                        st.markdown(display_cmp)
        else:
            st.info(f"Select at least 2 jobs to compare (currently selected: {len(cmp_selected)})")

# ============================================================================
# PAGE: SETTINGS
# ============================================================================
elif page == "⚙️ Settings":
    st.header("⚙️ Settings")

    # ── Temporary secrets debug (remove once keys confirmed working) ──────────
    with st.expander("🔑 Debug: Secrets Check", expanded=True):
        try:
            all_keys = list(st.secrets.keys())
            st.write("Keys found in st.secrets:", all_keys)
            for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "app_password", "CAREER_OPS_PROFILE"):
                if k in st.secrets:
                    val = str(st.secrets[k])
                    masked = val[:6] + "..." if len(val) > 6 else "(set but short)"
                    st.success(f"✅ `{k}` found — `{masked}`")
                else:
                    st.error(f"❌ `{k}` NOT found in secrets")
        except Exception as e:
            st.error(f"Could not read st.secrets: {e}")
        st.caption("Remove this expander once keys are confirmed.")
    # ── End debug ─────────────────────────────────────────────────────────────

    tab_salary, tab_search, tab_ai, tab_health, tab_data = st.tabs([
        "💰 Salary",
        "🔍 Search Defaults",
        "🤖 AI Preferences",
        "🔧 Scraper Health",
        "🗄️ Data",
    ])

    # ── TAB 1: Salary Targets ─────────────────────────────────────────────────
    with tab_salary:
        st.subheader("💰 Salary Expectations")
        st.caption("Used in job evaluations to assess compensation fit (Block D)")

        current_salary = float(get_setting("current_salary", 150000))
        target_min     = float(get_setting("target_salary_min", 180000))
        target_max     = float(get_setting("target_salary_max", 250000))

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Current Annual Compensation (SGD)**")
            new_current = st.number_input(
                "Current salary (base + bonus)", min_value=0, max_value=1_000_000,
                value=int(current_salary), step=10_000, key="current_salary_input",
                help="Used as baseline for comparing job offers",
            )
        with col2:
            st.markdown("**Target Salary Range (SGD/year)**")
            tmin_col, tmax_col = st.columns(2)
            with tmin_col:
                new_target_min = st.number_input(
                    "Min target", min_value=0, max_value=1_000_000,
                    value=int(target_min), step=10_000, key="target_min_input",
                )
            with tmax_col:
                new_target_max = st.number_input(
                    "Max target", min_value=0, max_value=1_000_000,
                    value=int(target_max), step=10_000, key="target_max_input",
                )

        st.markdown("---")
        st.markdown(f"""
**Your Salary Profile:**
- **Current:** SGD {new_current:,}/year
- **Target range:** SGD {new_target_min:,}–{new_target_max:,}/year
- **Red flag if below:** SGD {new_current:,} (your current)
- **Upside if above:** SGD {new_target_max:,}+
        """)

        if st.button("💾 Save Salary Settings", type="primary", width="stretch",
                     key="save_salary_btn"):
            save_setting("current_salary",    new_current)
            save_setting("target_salary_min", new_target_min)
            save_setting("target_salary_max", new_target_max)
            st.success("✅ Salary settings saved — evaluations will use these targets.")
            time.sleep(1); st.rerun()

    # ── TAB 2: Search Defaults ────────────────────────────────────────────────
    with tab_search:
        st.subheader("🔍 Search Defaults")
        st.caption(
            "These values pre-populate the **Run New Scan** panel in the Pipeline. "
            "You can always override them per-scan."
        )

        _cur_kw      = get_setting("default_keyword",  "Product Manager")
        _cur_sal_min = int(get_setting("default_sal_min", "8000"))
        _cur_sal_max = int(get_setting("default_sal_max", "20000"))

        sd_col1, sd_col2 = st.columns(2)
        with sd_col1:
            new_kw = st.text_input(
                "Default keyword",
                value=_cur_kw,
                help="The keyword that fills the search box when you open the scan panel",
            )
        with sd_col2:
            st.markdown("**Default monthly salary filter (SGD)**")
            sd_sc1, sd_sc2 = st.columns(2)
            with sd_sc1:
                new_sal_min = st.number_input(
                    "Min", min_value=0, max_value=50_000,
                    value=_cur_sal_min, step=500, key="def_sal_min",
                )
            with sd_sc2:
                new_sal_max = st.number_input(
                    "Max", min_value=0, max_value=50_000,
                    value=_cur_sal_max, step=500, key="def_sal_max",
                )
            st.caption(f"≈ SGD {new_sal_min*12:,}–{new_sal_max*12:,}/yr")

        if st.button("💾 Save Search Defaults", type="primary", width="stretch",
                     key="save_search_btn"):
            save_setting("default_keyword",  new_kw)
            save_setting("default_sal_min",  new_sal_min)
            save_setting("default_sal_max",  new_sal_max)
            st.success("✅ Search defaults saved.")
            time.sleep(1); st.rerun()

    # ── TAB 3: AI Preferences ─────────────────────────────────────────────────
    with tab_ai:
        st.subheader("🤖 AI Model Preferences")
        st.caption(
            "Set your preferred model for evaluations. "
            "This pre-selects the model in all evaluation dropdowns."
        )

        # Status summary
        ai_rows = []
        ai_rows.append({
            "Model":    "Gemini (Google)",
            "Status":   "🟢 Ready" if gemini_ok else "🔘 No API key",
            "Cost":     "Free tier (generous quota)",
            "Quality":  "⭐⭐⭐⭐",
            "Speed":    "Fast",
        })
        ai_rows.append({
            "Model":    "Claude (Anthropic)",
            "Status":   "🟢 Ready" if claude_ok else "🔘 No API key",
            "Cost":     "~$0.01–$0.05 per eval",
            "Quality":  "⭐⭐⭐⭐⭐",
            "Speed":    "Medium",
        })
        ai_rows.append({
            "Model":    "Ollama (local)",
            "Status":   "🟢 Running" if ollama_ok else "⚫ Not running (optional)",
            "Cost":     "Free — runs on your Mac",
            "Quality":  "⭐⭐",
            "Speed":    "Slow",
        })
        st.dataframe(pd.DataFrame(ai_rows), width="stretch", hide_index=True)

        st.markdown("---")
        _cur_pref = get_setting("default_evaluator", "gemini")
        pref_options = ["gemini", "claude", "ollama"]
        pref_labels  = ["⚡ Gemini (recommended — free tier)",
                        "☁️ Claude (most accurate)",
                        "🖥️ Ollama (local · optional)"]
        _pref_idx = pref_options.index(_cur_pref) if _cur_pref in pref_options else 0

        new_pref = st.radio(
            "Preferred evaluator",
            pref_labels,
            index=_pref_idx,
            help="Pipeline evaluation dropdowns will default to this model",
        )
        pref_key = pref_options[pref_labels.index(new_pref)]

        if not gemini_ok and pref_key == "gemini":
            st.warning("⚠️ Gemini API key not set. Add GEMINI_API_KEY to .env")
        if not claude_ok and pref_key == "claude":
            st.warning("⚠️ Claude API key not set. Add ANTHROPIC_API_KEY to .env")
        if not ollama_ok and pref_key == "ollama":
            st.warning("⚠️ Ollama is not running. Start with: ollama serve")

        st.markdown("**API Key Setup** — add these to your `.env` file:")
        st.code("GEMINI_API_KEY=your_key_here\nANTHROPIC_API_KEY=your_key_here", language="bash")

        if st.button("💾 Save AI Preference", type="primary", width="stretch",
                     key="save_ai_btn"):
            save_setting("default_evaluator", pref_key)
            st.success(f"✅ Default evaluator set to: {pref_key}")
            time.sleep(1); st.rerun()

    # ── TAB 4: Scraper Health Check ───────────────────────────────────────────
    with tab_health:
        st.subheader("🔧 Scraper Health Check")
        st.caption(
            "Test each source with a simple keyword to confirm it's returning results. "
            "Run monthly or when results feel sparse."
        )

        TEST_KEYWORD = "manager"
        AUTH_BLOCKED = {
            "HRP Portal": "careers.hrp.gov.sg — requires SAML2 login, cannot scrape headlessly"
        }
        ALL_BOARD_SOURCES   = ["MyCareersFuture", "Indeed", "JobStreet", "LinkedIn", "Glints"]
        ALL_COMPANY_SOURCES = {
            "DBS":       "direct Workday",
            "Grab":      "direct portal",
            "GovTech":   "Careers@Gov",
            "HTX":       "Careers@Gov",
            "MAS":       "MCF",
            "Anthropic": "Greenhouse API",
            "Airwallex": "Ashby API",
        }

        st.markdown("**📰 Job Boards**")
        hc_b_all, hc_b_none = st.columns([1, 5])
        with hc_b_all:
            if st.button("All", key="hc_boards_all", use_container_width=True):
                for b in ALL_BOARD_SOURCES:
                    st.session_state[f"hc_board_{b}"] = True
        with hc_b_none:
            if st.button("None", key="hc_boards_none", use_container_width=True):
                for b in ALL_BOARD_SOURCES:
                    st.session_state[f"hc_board_{b}"] = False

        hc_board_cols = st.columns(len(ALL_BOARD_SOURCES))
        selected_hc_boards = []
        for i, b in enumerate(ALL_BOARD_SOURCES):
            with hc_board_cols[i]:
                if st.checkbox(b, value=False, key=f"hc_board_{b}"):
                    selected_hc_boards.append(b)

        st.markdown("**🏢 Company Sources**")
        hc_c_all, hc_c_none = st.columns([1, 5])
        with hc_c_all:
            if st.button("All", key="hc_cos_all", use_container_width=True):
                for co in ALL_COMPANY_SOURCES:
                    st.session_state[f"hc_co_{co}"] = True
        with hc_c_none:
            if st.button("None", key="hc_cos_none", use_container_width=True):
                for co in ALL_COMPANY_SOURCES:
                    st.session_state[f"hc_co_{co}"] = False

        hc_co_cols = st.columns(len(ALL_COMPANY_SOURCES))
        selected_hc_companies = []
        for i, (co, method) in enumerate(ALL_COMPANY_SOURCES.items()):
            with hc_co_cols[i]:
                if st.checkbox(f"{co}\n({method})", value=False, key=f"hc_co_{co}"):
                    selected_hc_companies.append(co)

        st.markdown("**🔒 Auth-blocked (cannot test headlessly)**")
        for name, reason in AUTH_BLOCKED.items():
            st.caption(f"🔒 {name}: {reason}")

        total_selected = len(selected_hc_boards) + len(selected_hc_companies)
        if total_selected > 0:
            st.info(
                f'Testing **{total_selected}** source(s) with keyword **"{TEST_KEYWORD}"**'
            )

        if st.button("🔧 Run Health Check", type="primary", width="stretch",
                     disabled=(total_selected == 0), key="run_hc_btn"):
            results = []
            prog_hc = st.progress(0, text="Starting health check…")
            total_steps = total_selected

            for idx, source in enumerate(selected_hc_boards):
                prog_hc.progress(idx / total_steps,
                                 text=f"Testing {source}…")
                try:
                    sc_result = scrape_jobs(TEST_KEYWORD, [source], num_results=3)
                    count = sc_result.get("count", 0)
                    if count > 0:
                        status = f"✅ OK ({count} results)"
                    else:
                        status = "⚠️ 0 results"
                except Exception as e:
                    status = f"❌ Error: {str(e)[:60]}"
                results.append({"Source": source, "Type": "Job Board",
                                 "Method": "Playwright", "Status": status})

            for idx, co in enumerate(selected_hc_companies):
                prog_hc.progress((len(selected_hc_boards) + idx) / total_steps,
                                 text=f"Testing {co}…")
                try:
                    sc_result = scrape_jobs(TEST_KEYWORD, [], num_results=3,
                                            companies=[co])
                    count = sc_result.get("count", 0)
                    if count > 0:
                        status = f"✅ OK ({count} results)"
                    else:
                        status = "⚠️ 0 results"
                except Exception as e:
                    status = f"❌ Error: {str(e)[:60]}"
                results.append({"Source": co, "Type": "Company Direct",
                                 "Method": ALL_COMPANY_SOURCES.get(co, "—"),
                                 "Status": status})

            prog_hc.progress(1.0, text="✅ Health check complete!")
            st.markdown("---")
            st.subheader("📊 Results")
            st.dataframe(pd.DataFrame(results), width="stretch", hide_index=True)

            ok_count   = sum(1 for r in results if r["Status"].startswith("✅"))
            warn_count = sum(1 for r in results if r["Status"].startswith("⚠️"))
            err_count  = sum(1 for r in results if r["Status"].startswith("❌"))
            st.caption(
                f"✅ {ok_count} OK · ⚠️ {warn_count} zero results · ❌ {err_count} errors"
            )
            if warn_count > 0 or err_count > 0:
                st.warning(
                    "⚠️ Some sources need attention — could be bot-detection, "
                    "site changes, or no matching jobs for the test keyword."
                )

    # ── TAB 5: Data Management ────────────────────────────────────────────────
    with tab_data:
        st.subheader("🗄️ Data Management")

        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM jobs")
        n_jobs = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM evaluations")
        n_evals = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM applications")
        n_apps = c.fetchone()[0]
        c.execute("SELECT source, COUNT(*) as cnt FROM jobs GROUP BY source ORDER BY cnt DESC")
        source_counts = c.fetchall()
        conn.close()

        dm_c1, dm_c2, dm_c3 = st.columns(3)
        dm_c1.metric("Total Jobs",       n_jobs)
        dm_c2.metric("Evaluations",      n_evals)
        dm_c3.metric("Applications",     n_apps)

        if source_counts:
            st.markdown("**Jobs by source:**")
            src_df = pd.DataFrame(source_counts, columns=["Source", "Count"])
            st.dataframe(src_df, width="stretch", hide_index=True)

        st.markdown("---")
        st.subheader("📥 Export")

        conn = get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT j.id, j.company, j.title, j.salary, j.location, j.source,
                   j.url, j.status, j.created_at,
                   COALESCE(e.score, '') as score,
                   COALESCE(e.legitimacy, '') as legitimacy,
                   COALESCE(e.archetype, '') as archetype,
                   COALESCE(a.status, '') as app_status,
                   COALESCE(a.applied_at, '') as applied_date
            FROM jobs j
            LEFT JOIN evaluations e ON j.id = e.job_id
            LEFT JOIN applications a ON j.id = a.job_id
            ORDER BY j.created_at DESC
        ''')
        export_rows = c.fetchall()
        conn.close()

        if export_rows:
            export_df = pd.DataFrame(export_rows, columns=[
                "ID", "Company", "Title", "Salary", "Location", "Source",
                "URL", "Status", "Created", "Score", "Legitimacy", "Archetype",
                "Application Status", "Applied Date",
            ])
            csv = export_df.to_csv(index=False)
            st.download_button(
                "⬇️ Download all jobs as CSV",
                data=csv,
                file_name=f"career_ops_export_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                width="stretch",
            )

        st.markdown("---")
        st.subheader("🗑️ Bulk Delete")
        st.caption("These actions are permanent and cannot be undone.")

        bd_c1, bd_c2 = st.columns(2)
        with bd_c1:
            if st.button("🧹 Delete Unevaluated Jobs", width="stretch",
                         key="bulk_del_uneval"):
                conn2 = get_connection()
                c2 = conn2.cursor()
                c2.execute("DELETE FROM jobs WHERE id NOT IN (SELECT job_id FROM evaluations)")
                n = c2.rowcount
                conn2.commit(); conn2.close()
                st.success(f"✅ Deleted {n} unevaluated jobs"); st.rerun()
        with bd_c2:
            if st.button("💣 Delete ALL Data", width="stretch", type="secondary",
                         key="bulk_del_all"):
                conn2 = get_connection()
                c2 = conn2.cursor()
                c2.execute("DELETE FROM evaluations")
                c2.execute("DELETE FROM applications")
                c2.execute("DELETE FROM jobs")
                conn2.commit(); conn2.close()
                st.session_state.spotlight_id  = None
                st.session_state.eval_results  = {}
                st.session_state.last_scan_ids = set()
                st.success("✅ All data cleared."); st.rerun()

# ============================================================================
# FOOTER
# ============================================================================
st.divider()
st.caption(f"Career-Ops · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
