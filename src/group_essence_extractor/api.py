from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import get_settings
from .db import EssenceRepository
from .ingest import ingest_all


settings = get_settings()
repo = EssenceRepository(settings.db_path)
repo.init_db()
app = FastAPI(title="Group Essence Extractor", version="0.1.0")


class SearchQuery(BaseModel):
    sender_time: str = ""
    essence_time: str = ""
    sender: str = ""
    sender_qq: str = ""
    operator: str = ""
    operator_qq: str = ""
    content: str = ""
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class SearchRequest(BaseModel):
    request_id: str = ""
    query: SearchQuery


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/ingest")
def trigger_ingest() -> dict[str, Any]:
    stat = ingest_all(settings, repo)
    return {"status": "ok", "data": stat}


@app.post("/api/v1/search")
def remote_search(req: SearchRequest) -> dict[str, Any]:
    q = req.query
    items = repo.search(
        sender_time=q.sender_time,
        essence_time=q.essence_time,
        sender=q.sender,
        sender_qq=q.sender_qq,
        operator=q.operator,
        operator_qq=q.operator_qq,
        content=q.content,
        limit=q.limit,
        offset=q.offset,
    )
    return {
        "request_id": req.request_id,
        "status": "ok",
        "count": len(items),
        "items": items,
    }
