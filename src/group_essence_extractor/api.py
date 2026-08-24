from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import EssenceRepository
from .ingest import ingest_all


class SearchQuery(BaseModel):
    sender_time: str = ""
    essence_time: str = ""
    sender: str = ""
    sender_qq: str = ""
    operator: str = ""
    operator_qq: str = ""
    content: str = ""
    group_id: str = ""
    source: str = ""
    content_type: str = ""
    sender_time_from: str = ""
    sender_time_to: str = ""
    essence_time_from: str = ""
    essence_time_to: str = ""
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class SearchRequest(BaseModel):
    request_id: str = ""
    query: SearchQuery


def create_app(
    settings: Settings | None = None,
    repository: EssenceRepository | None = None,
) -> FastAPI:
    current_settings = settings or get_settings()
    current_repo = repository or EssenceRepository(current_settings.db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        current_repo.init_db()
        yield

    application = FastAPI(
        title="Group Essence Extractor",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = current_settings
    application.state.repository = current_repo

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/v1/ingest")
    def trigger_ingest(request: Request) -> dict[str, Any]:
        stat = ingest_all(request.app.state.settings, request.app.state.repository)
        return {"status": "ok", "data": stat}

    @application.post("/api/v1/search")
    def remote_search(req: SearchRequest, request: Request) -> dict[str, Any]:
        q = req.query
        page = request.app.state.repository.search_page(
            sender_time=q.sender_time,
            essence_time=q.essence_time,
            sender=q.sender,
            sender_qq=q.sender_qq,
            operator=q.operator,
            operator_qq=q.operator_qq,
            content=q.content,
            group_id=q.group_id,
            source=q.source,
            content_type=q.content_type,
            sender_time_from=q.sender_time_from,
            sender_time_to=q.sender_time_to,
            essence_time_from=q.essence_time_from,
            essence_time_to=q.essence_time_to,
            limit=q.limit,
            offset=q.offset,
        )
        return {
            "request_id": req.request_id,
            "status": "ok",
            **page.as_dict(),
        }

    return application


app = create_app()
