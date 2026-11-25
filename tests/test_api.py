"""API 端到端测试:用 TestClient 走完整流程(无需网络、无需 Key)。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.client import LLMUnavailable
from app.main import create_app
from app.story import Story
from tests.stubs import StubLLM
from tests.test_story import ROUTES


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        data = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if data:
            events.append(json.loads(data[0]))
    return events


@pytest.fixture()
def offline_client():
    """没有 API Key:验证"零配置也能通关"。"""
    app = create_app(settings=Settings(api_key=""))
    with TestClient(app) as client:
        yield client


def new_game(client: TestClient) -> tuple[str, dict]:
    data = client.post("/api/session").json()
    return data["session_id"], data["scene"]


def choose(client: TestClient, sid: str, choice_id: str) -> dict:
    resp = client.post("/api/choose", json={"session_id": sid, "choice_id": choice_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["scene"]


def test_index_and_health(offline_client):
    assert offline_client.get("/").status_code == 200
    health = offline_client.get("/healthz").json()
    assert health["status"] == "ok" and health["ai_enabled"] is False


def test_meta_exposes_endings_and_ai_flag(offline_client):
    meta = offline_client.get("/api/meta").json()
    assert meta["ai_enabled"] is False
    assert {e["id"] for e in meta["endings"]} == {"truth", "late", "switch", "gate"}
    assert meta["title"]


@pytest.mark.parametrize("ending_id,route", ROUTES.items())
def test_full_playthrough_reaches_each_ending_without_ai(offline_client, ending_id, route):
    sid, scene = new_game(offline_client)
    assert scene["node_id"] == "prologue" and scene["paragraphs"] and scene["choices"]
    for choice_id in route:
        scene = choose(offline_client, sid, choice_id)
    assert scene["ending"]["id"] == ending_id
    assert scene["choices"] == []
    assert scene["recap"], "结局页需要回顾玩家的选择"

    # 旁白 / 后日谈在没有 Key 时降级为预写文本,且流以 done 结尾
    for path in (f"/api/narrate/{sid}", f"/api/epilogue/{sid}"):
        events = parse_sse(offline_client.get(path).text)
        assert events[0]["type"] == "replace" and events[0]["reason"] == "ai_disabled"
        assert events[-1] == {"type": "done", "source": "authored", "model": "", "reason": "ai_disabled"}
    narration = parse_sse(offline_client.get(f"/api/narrate/{sid}").text)[0]["paragraphs"]
    assert narration == scene["paragraphs"]


def test_error_handling(offline_client):
    assert offline_client.post("/api/choose", json={"session_id": "nope", "choice_id": "x"}).status_code == 404
    assert offline_client.get("/api/narrate/nope").status_code == 404
    assert offline_client.get("/api/session/nope").status_code == 404

    sid, _ = new_game(offline_client)
    bad = offline_client.post("/api/choose", json={"session_id": sid, "choice_id": "no_such"})
    assert bad.status_code == 400
    # 非结局场景请求后日谈 → 400
    assert offline_client.get(f"/api/epilogue/{sid}").status_code == 400


def test_locked_choice_is_rejected_over_http(offline_client):
    sid, _ = new_game(offline_client)
    for c in ["start", "go_live", "to_volunteer", "volunteer_back", "mountain", "mountain_go"]:
        scene = choose(offline_client, sid, c)
    locked = {c["id"]: c for c in scene["choices"]}["play_tape"]
    assert locked["locked"] and locked["lock_hint"]
    resp = offline_client.post("/api/choose", json={"session_id": sid, "choice_id": "play_tape"})
    assert resp.status_code == 400
    # 状态没有被破坏,仍可继续
    assert choose(offline_client, sid, "report_evac")["ending"]["id"] == "switch"


def test_resume_session_returns_current_scene(offline_client):
    sid, _ = new_game(offline_client)
    choose(offline_client, sid, "start")
    scene = offline_client.get(f"/api/session/{sid}").json()["scene"]
    assert scene["node_id"] == "studio"


def test_every_scene_image_is_served_or_gracefully_missing(offline_client):
    sid, scene = new_game(offline_client)
    image = scene["image"]
    if image:  # 图片已生成时必须可访问;未生成时前端使用渐变占位
        assert offline_client.get(image).status_code == 200


# ---------------------------------------------------------------- 开启 AI 的路径(用替身)


def ai_client(stub: StubLLM) -> TestClient:
    return TestClient(create_app(settings=Settings(api_key="k"), llm=stub))


def test_narrate_streams_ai_text_over_sse():
    # 一份"合格"的 AI 输出:保留了原稿的全部事实(这里直接用原稿分块模拟)
    authored = Story.load().node("prologue").render_text({})
    text = "\n\n".join(authored)
    stub = StubLLM(chunks=[text[i : i + 15] for i in range(0, len(text), 15)])
    with ai_client(stub) as client:
        sid, _ = new_game(client)
        resp = client.get(f"/api/narrate/{sid}")
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(resp.text)
        assert all(e["type"] == "delta" for e in events[:-1])
        assert "".join(e["text"] for e in events[:-1]) == text
        assert events[-1]["type"] == "done" and events[-1]["source"] == "ai"


def test_narrate_rejects_ai_text_that_drops_story_facts():
    """守卫生效:AI 写得再漂亮,丢了关键事实就整体替换为预写版本。"""
    stub = StubLLM(chunks=["夜很深,风很大,窗外有一只猫经过,屋里没有人说话。" * 8])
    with ai_client(stub) as client:
        sid, scene = new_game(client)
        events = parse_sse(client.get(f"/api/narrate/{sid}").text)
        assert events[-2]["type"] == "replace" and events[-2]["reason"] == "missing_facts"
        assert events[-2]["paragraphs"] == scene["paragraphs"]


def test_narrate_degrades_when_ai_is_down():
    with ai_client(StubLLM(before=LLMUnavailable("down"))) as client:
        sid, scene = new_game(client)
        events = parse_sse(client.get(f"/api/narrate/{sid}").text)
        assert events[0]["type"] == "replace" and events[0]["reason"] == "llm_unavailable"
        assert events[0]["paragraphs"] == scene["paragraphs"]


def test_free_text_action_advances_story_when_matched():
    stub = StubLLM(complete_text=json.dumps({"choice_id": "go_live", "reply": "你按下了接听键。"}))
    with ai_client(stub) as client:
        sid, _ = new_game(client)
        choose(client, sid, "start")
        res = client.post("/api/act", json={"session_id": sid, "text": "我直接接电话"}).json()
        assert res["matched"] and res["scene"]["node_id"] == "live"
        # 紧接着再次提交会被限流,保护额度
        again = client.post("/api/act", json={"session_id": sid, "text": "再来"})
        assert again.status_code == 429


def test_free_text_action_unmatched_keeps_scene():
    stub = StubLLM(complete_text=json.dumps({"choice_id": None, "reply": "你想了想,眼下做不到。"}))
    with ai_client(stub) as client:
        sid, _ = new_game(client)
        choose(client, sid, "start")
        res = client.post("/api/act", json={"session_id": sid, "text": "我飞走"}).json()
        assert res["matched"] is False and "scene" not in res
        assert client.get(f"/api/session/{sid}").json()["scene"]["node_id"] == "studio"


def test_free_text_validation():
    with ai_client(StubLLM()) as client:
        sid, _ = new_game(client)
        too_long = client.post("/api/act", json={"session_id": sid, "text": "x" * 500})
        assert too_long.status_code == 422
