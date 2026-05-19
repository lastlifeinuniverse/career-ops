"""
profile_loader.py — loads a named profile YAML from config/profiles/
Active profile is selected via:
  1. CAREER_OPS_PROFILE env var (for Streamlit Cloud deployments)
  2. Streamlit session_state (set by sidebar selector in app.py)
  3. Falls back to the user's own config/profile.yml as "default"
"""
import os
from pathlib import Path
import yaml

PROFILES_DIR = Path(__file__).parent / "config" / "profiles"
OWN_PROFILE_PATH = Path(__file__).parent / "config" / "profile.yml"

# Built-in profile list — add new ones here as you onboard more friends
AVAILABLE_PROFILES = {
    "default":   {"display": "Yingkai (Me)",   "emoji": "🎯", "path": OWN_PROFILE_PATH},
    "kai_hiong": {"display": "Kai Hiong",       "emoji": "🏦", "path": PROFILES_DIR / "kai_hiong.yml"},
    "zy":        {"display": "Zong Yan (ZY)",   "emoji": "🎨", "path": PROFILES_DIR / "zy.yml"},
}


def load_profile(name: str) -> dict:
    """Load a profile YAML by name. Falls back to default if not found."""
    meta = AVAILABLE_PROFILES.get(name, AVAILABLE_PROFILES["default"])
    path = meta["path"]
    if not path.exists():
        return {}
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def get_env_profile() -> str:
    """Return the profile name from env var, or 'default'."""
    return os.environ.get("CAREER_OPS_PROFILE", "default")


def profile_db_path(name: str) -> Path:
    """Return the SQLite path for a given profile (isolated per user)."""
    base = Path(__file__).parent
    if name == "default":
        return base / "career_ops.db"
    return base / f"career_ops_{name}.db"


def get_classifier(profile: dict) -> dict:
    """Extract classifier keyword lists from profile dict."""
    c = profile.get("classifier", {})
    return {
        "block":    [kw.lower() for kw in c.get("block", [])],
        "core":     [kw.lower() for kw in c.get("core", [])],
        "adjacent": [kw.lower() for kw in c.get("adjacent", [])],
    }


def get_defaults(profile: dict) -> dict:
    """Extract scan defaults from profile dict."""
    d = profile.get("defaults", {})
    comp = profile.get("compensation", {})
    return {
        "keyword":       d.get("keyword", "Product Manager"),
        "salary_min":    d.get("salary_min", 8000),
        "salary_max":    d.get("salary_max", 20000),
        "quick_terms":   d.get("quick_search_terms", []),
        "current_comp":  comp.get("current", 150000),
        "target_min":    comp.get("target_min", 180000),
        "target_max":    comp.get("target_max", 250000),
    }


def get_eval_persona(profile: dict) -> str:
    """Return the eval persona block for use in AI prompts."""
    return profile.get("eval_persona", "")


def get_quick_screen_persona(profile: dict) -> str:
    """Return the quick-screen persona for use in AI prompts."""
    return profile.get("quick_screen_persona", "")
