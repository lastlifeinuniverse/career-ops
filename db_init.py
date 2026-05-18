import sqlite3
from pathlib import Path
import json

DB_PATH = Path(__file__).parent / "career_ops.db"

def init_db():
    """Initialize SQLite database with required tables."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Jobs table (from scans)
    c.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            salary TEXT,
            location TEXT,
            url TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            status TEXT DEFAULT 'discovered'
        )
    ''')

    # Evaluations table (from Ollama)
    c.execute('''
        CREATE TABLE IF NOT EXISTS evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            score REAL,
            blocks TEXT,
            legitimacy TEXT,
            archetype TEXT,
            summary TEXT,
            model TEXT DEFAULT 'mistral',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )
    ''')

    # Applications table (tracking)
    c.execute('''
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            eval_id INTEGER,
            status TEXT DEFAULT 'interested',
            applied_date DATE,
            response_date DATE,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(id),
            FOREIGN KEY(eval_id) REFERENCES evaluations(id)
        )
    ''')

    # User settings / profile
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Add missing columns if they don't exist yet (backward compatibility)
    migrations = [
        ("jobs", "quick_score", "REAL"),
        ("jobs", "quick_reason", "TEXT"),
        ("applications", "applied_at", "TIMESTAMP"),
    ]
    for table, col, definition in migrations:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    conn.close()

def get_connection():
    """Get database connection."""
    return sqlite3.connect(DB_PATH)

def dict_from_row(row, columns):
    """Convert sqlite3 row to dict."""
    return {col: row[i] for i, col in enumerate(columns)}
