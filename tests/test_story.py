"""剧情图与引擎测试:保证"剧情收得住、分支真的不同、结局可达"。"""
import pytest

from app.story import InvalidChoice, Story
from app.story.validate import validate_story


@pytest.fixture(scope="module")
def story() -> Story:
    return Story.load()


def play(story: Story, choices: list[str]):
    state = story.new_game()
    for choice_id in choices:
        story.choose(state, choice_id)
    return state


# 每个结局至少一条"黄金路径"
ROUTES = {
    "truth": ["start", "cut_song", "to_archive", "archive_back", "riverside", "riverside_go", "play_tape"],
    "late": ["start", "cut_song", "to_archive", "archive_back", "mountain", "mountain_go", "play_tape"],
    "switch": ["start", "go_live", "to_volunteer", "volunteer_back", "mountain", "mountain_go", "report_evac"],
    "gate": ["start", "cut_song", "to_bureau", "bureau_back", "mountain", "mountain_go", "grab_key"],
}


def test_graph_is_closed_and_fully_reachable(story):
    report = validate_story(story)
    assert report.ok, report.errors
    assert report.reachable_nodes == set(story.nodes)
    assert report.reachable_endings == {e.id for e in story.endings}


def test_story_has_enough_endings_and_forks(story):
    assert len(story.endings) >= 3
    kinds = {e.kind for e in story.endings}
    assert {"good", "bad"} <= kinds
    forks = [n for n in story.nodes.values() if len([c for c in n.choices]) >= 2]
    assert len(forks) >= 4  # 至少 2 个关键岔路,这里有 4 个


@pytest.mark.parametrize("ending_id,choices", ROUTES.items())
def test_each_ending_is_reachable_by_its_route(story, ending_id, choices):
    state = play(story, choices)
    node = story.current(state)
    assert node.is_ending
    assert node.ending.id == ending_id


def test_different_choices_lead_to_different_endings(story):
    endings = {story.current(play(story, c)).ending.id for c in ROUTES.values()}
    assert len(endings) == len(ROUTES)


def test_first_fork_leads_to_different_scenes(story):
    a = play(story, ["start", "cut_song"])
    b = play(story, ["start", "go_live"])
    assert a.node_id != b.node_id
    assert a.variables["route"] != b.variables["route"]


def test_locked_choice_is_rejected_and_state_untouched(story):
    # 听众接力路线不会拿到磁带,所以 station 里的 play_tape 应被锁定
    state = play(story, ["start", "go_live", "to_volunteer", "volunteer_back", "mountain", "mountain_go"])
    assert story.current(state).id == "station"
    before = (state.node_id, dict(state.variables), len(state.history))
    with pytest.raises(InvalidChoice):
        story.choose(state, "play_tape")  # 没有磁带
    assert (state.node_id, state.variables, len(state.history)) == before


def test_unknown_choice_and_choice_after_ending_rejected(story):
    state = story.new_game()
    with pytest.raises(InvalidChoice):
        story.choose(state, "no_such_choice")
    ended = play(story, ROUTES["gate"])
    with pytest.raises(InvalidChoice):
        story.choose(ended, "start")


def test_choice_views_mark_locked_choices(story):
    state = play(story, ["start", "go_live", "to_volunteer", "volunteer_back", "riverside", "riverside_go"])
    views = {v.id: v for v in story.choice_views(state)}
    assert views["play_tape"].locked and views["play_tape"].lock_hint
    assert not views["report_evac"].locked
    assert not views["grab_key"].locked


def test_confession_gives_tape_on_soothe_route(story):
    state = play(story, ["start", "go_live", "to_soothe", "soothe_back"])
    assert story.current(state).id == "handoff"
    assert state.variables["tape"] is True and state.variables["confessed"] is True
    text = "".join(story.current(state).render_text(state.variables))
    assert "手帕" in text  # 陈岚的坦白变体被渲染


def test_archive_route_uses_other_handoff_variant(story):
    state = play(story, ["start", "cut_song", "to_archive", "archive_back"])
    text = "".join(story.current(state).render_text(state.variables))
    assert "我一次都没睡踏实过" in text
    assert "手帕" not in text


def test_ending_text_depends_on_state(story):
    # 同一个结局("迟到的真话"),文本随 evac 状态变化
    unprepared = play(story, ROUTES["late"])
    prepared = play(story, ["start", "go_live", "to_soothe", "soothe_back", "riverside", "riverside_go", "honest"])
    assert story.current(prepared).ending.id == "late"
    t_unprepared = "".join(story.current(unprepared).render_text(unprepared.variables))
    t_prepared = "".join(story.current(prepared).render_text(prepared.variables))
    assert "水来得比人快" in t_unprepared
    assert "水来得比人快" not in t_prepared
    assert prepared.variables["evac"] is True


def test_history_records_memos(story):
    state = play(story, ROUTES["truth"])
    memos = [h["memo"] for h in state.history if h["memo"]]
    assert len(memos) >= 4
    assert state.step == len(ROUTES["truth"])


def test_every_node_has_image_key_and_beats(story):
    for node in story.nodes.values():
        assert node.image
        assert node.beats, f"{node.id} needs beats for AI narration constraints"
