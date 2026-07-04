import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "prompts.db"
LOG_PATH = DATA_DIR / "capture.log"

# data/ holds the full local capture history of every Claude Code session
# (prompts.db, logs, insights, VAPID keys) -- potentially sensitive. Every
# file or directory this process creates (DATA_DIR.mkdir(), open(..., "w"/"a"),
# sqlite3.connect() on a new db, etc.) should default to owner-only, not
# whatever umask the invoking shell/launchd happens to have. Setting this here
# means it's in effect before any submodule (db.py, capture.py, push_notify.py,
# migrations/_runner.py, ...) does its own first mkdir/open, since importing
# any of them imports this package first.
os.umask(0o077)
