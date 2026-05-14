from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "prompts.db"
LOG_PATH = DATA_DIR / "capture.log"
