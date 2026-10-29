"""Settings come from environment variables (the cloud routine's environment) with an optional local .env as fallback.
Only names with these prefixes are read, so nothing else in the environment is picked up."""
import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
PREFIXES = ("STG_", "KG_", "GIT_", "SHEET_", "JINSTAGE_")


def load():
    cfg = dict(dotenv_values(ROOT / ".env")) if (ROOT / ".env").exists() else {}
    cfg.update({k: v for k, v in os.environ.items() if k.startswith(PREFIXES)})
    return cfg
