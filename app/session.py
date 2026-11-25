"""内存会话存储(带 TTL)。单机小游戏足够;重启后旧会话失效,前端会优雅地重新开始。"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .story.engine import GameState


@dataclass
class Session:
    id: str
    state: GameState
    touched: float
    last_act: float = 0.0


class SessionStore:
    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.monotonic, max_sessions: int = 2000):
        self._ttl = ttl_seconds
        self._clock = clock
        self._max = max_sessions
        self._sessions: dict[str, Session] = {}

    def now(self) -> float:
        return self._clock()

    def create(self, state: GameState) -> Session:
        self._evict()
        session = Session(id=uuid.uuid4().hex, state=state, touched=self._clock(), last_act=-1e9)
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if self._clock() - session.touched > self._ttl:
            del self._sessions[session_id]
            return None
        session.touched = self._clock()
        return session

    def __len__(self) -> int:
        return len(self._sessions)

    def _evict(self) -> None:
        """过期清理;超过容量时丢弃最久未活动的会话,避免内存无限增长。"""
        now = self._clock()
        for sid in [s for s, v in self._sessions.items() if now - v.touched > self._ttl]:
            del self._sessions[sid]
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions.values(), key=lambda s: s.touched)
            del self._sessions[oldest.id]
