import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _req(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env var {name} (see .env.example)")
    return v


@dataclass(frozen=True)
class Config:
    sheet_id: str
    sheet_tab_gid: str
    google_sa_json: str
    git_remote: str          # https://github.com/rohanabalagalla/JinStage.git
    git_branch: str
    git_token: str
    stg_url: str
    stg_db: str
    stg_login: str
    stg_secret: str
    kg_url: str
    kg_db: str
    kg_login: str
    kg_secret: str
    kill_file: Path
    max_attempts: int = 3


def load() -> Config:
    return Config(
        sheet_id=_req("SHEET_ID"), sheet_tab_gid=os.environ.get("SHEET_TAB_GID", "0"),
        google_sa_json=_req("GOOGLE_SA_JSON"),
        git_remote=_req("GIT_REMOTE"), git_branch=os.environ.get("GIT_BRANCH", "Upgrade_Testing"),
        git_token=_req("GIT_TOKEN"),
        stg_url=_req("STG_URL"), stg_db=_req("STG_DB"), stg_login=_req("STG_LOGIN"), stg_secret=_req("STG_SECRET"),
        kg_url=_req("KG_URL"), kg_db=_req("KG_DB"), kg_login=_req("KG_LOGIN"), kg_secret=_req("KG_SECRET"),
        kill_file=ROOT / "state" / "STOP",
    )
