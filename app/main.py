"""FastAPI 应用:会话、选择、自由行动、旁白/后日谈的 SSE 流,以及静态前端。"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import SCENE_DIR, STATIC_DIR, Settings, load_settings
from .llm.client import LLMClient
from .llm.director import Director
from .session import Session, SessionStore
from .story.engine import GameState, InvalidChoice, Story

log = logging.getLogger("film_game")

ACT_MIN_INTERVAL = 1.0  # 自由行动最小间隔(秒),保护模型额度


class ChooseRequest(BaseModel):
    session_id: str
    choice_id: str = Field(max_length=64)


class ActRequest(BaseModel):
    session_id: str
    text: str = Field(max_length=200)


def find_scene_image(key: str) -> str | None:
    for ext in ("png", "jpg", "jpeg", "webp"):
        if (SCENE_DIR / f"{key}.{ext}").exists():
            return f"/static/assets/scenes/{key}.{ext}"
    return None


def scene_payload(story: Story, state: GameState) -> dict[str, Any]:
    node = story.current(state)
    payload: dict[str, Any] = {
        "node_id": node.id,
        "title": node.title,
        "time": node.time,
        "place": node.place,
        "mood": node.mood,
        "image": find_scene_image(node.image),
        "paragraphs": node.render_text(state.variables),
        "choices": [
            {"id": c.id, "label": c.label, "locked": c.locked, "lock_hint": c.lock_hint}
            for c in story.choice_views(state)
        ],
        "step": state.step,
        "ending": None,
        "recap": [],
    }
    if node.ending:
        payload["ending"] = {
            "id": node.ending.id,
            "name": node.ending.name,
            "kind": node.ending.kind,
            "subtitle": node.ending.subtitle,
        }
        payload["recap"] = [h["memo"] for h in state.history if h["memo"]]
    return payload


def sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def sse_response(events: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        async for event in events:
            yield sse(event)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    story: Story | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    story = story or Story.load()
    llm = llm or LLMClient(settings)
    director = Director(story, llm, settings)
    sessions = SessionStore(settings.session_ttl_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("AI %s, models=%s", "enabled" if llm.enabled else "disabled (authored text only)", settings.llm_models)
        yield
        await llm.aclose()

    app = FastAPI(title=story.title, lifespan=lifespan)

    def need_session(session_id: str) -> Session:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found or expired")
        return session

    # ------------------------------------------------------------------ API
    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "title": story.title,
            "tagline": story.meta["tagline"],
            "estimated_minutes": story.meta["estimated_minutes"],
            "ai_enabled": llm.enabled,
            "endings": [
                {"id": e.id, "name": e.name, "kind": e.kind, "subtitle": e.subtitle} for e in story.endings
            ],
        }

    @app.post("/api/session")
    def new_session() -> dict[str, Any]:
        session = sessions.create(story.new_game())
        return {"session_id": session.id, "scene": scene_payload(story, session.state)}

    @app.get("/api/session/{session_id}")
    def resume_session(session_id: str) -> dict[str, Any]:
        session = need_session(session_id)
        return {"session_id": session.id, "scene": scene_payload(story, session.state)}

    @app.post("/api/choose")
    def choose(req: ChooseRequest) -> dict[str, Any]:
        session = need_session(req.session_id)
        try:
            story.choose(session.state, req.choice_id)
        except InvalidChoice as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"scene": scene_payload(story, session.state)}

    @app.post("/api/act")
    async def act(req: ActRequest) -> dict[str, Any]:
        """自由行动:LLM 把玩家输入映射到已有选项。映射成功则直接推进剧情。"""
        session = need_session(req.session_id)
        now = sessions.now()
        if now - session.last_act < ACT_MIN_INTERVAL:
            raise HTTPException(status_code=429, detail="too many requests")
        session.last_act = now

        node_before = session.state.node_id
        result = await director.interpret(session.state, req.text)
        if session.state.node_id != node_before:
            raise HTTPException(status_code=409, detail="scene changed while interpreting")
        response: dict[str, Any] = dict(result)
        if result["matched"]:
            try:
                story.choose(session.state, result["choice_id"])
            except InvalidChoice as exc:  # 防御:解析器已校验过,理论上不会发生
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            response["scene"] = scene_payload(story, session.state)
        return response

    @app.get("/api/narrate/{session_id}")
    async def narrate(session_id: str) -> StreamingResponse:
        session = need_session(session_id)
        return sse_response(director.narrate(session.state.clone()))

    @app.get("/api/epilogue/{session_id}")
    async def epilogue(session_id: str) -> StreamingResponse:
        session = need_session(session_id)
        if not story.current(session.state).is_ending:
            raise HTTPException(status_code=400, detail="story has not ended")
        return sse_response(director.epilogue(session.state.clone()))

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "ai_enabled": llm.enabled, "sessions": len(sessions)}

    # ------------------------------------------------------------------ 静态前端
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
