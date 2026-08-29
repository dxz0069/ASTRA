from fastapi import APIRouter

from astra.server.db import get_conn
from astra.server.models import Settings

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=Settings)
def get_settings():
    with get_conn() as conn:
        row = conn.execute("SELECT step_timeout, decide_timeout FROM settings WHERE rowid = 1").fetchone()
        return Settings(step_timeout=row["step_timeout"], decide_timeout=row["decide_timeout"])


@router.put("/settings", response_model=Settings)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute(
            "UPDATE settings SET step_timeout = ?, decide_timeout = ? WHERE rowid = 1",
            (body.step_timeout, body.decide_timeout),
        )
        return body
