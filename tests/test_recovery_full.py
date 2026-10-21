from pathlib import Path

from tests.recovery_support import (
    confirm_and_finish,
    drain,
    make_agent,
    open_plan,
    read_rows,
    require_ffmpeg,
)
from video_agent.models import RegenerateRequest


def test_full_regenerate_rebuilds_shots_without_independent_audio(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    from tests.recovery_support import CountingMusic, CountingVoice

    agent.voice = CountingVoice(agent.voice)
    agent.music = CountingMusic(agent.music)
    task, _image = open_plan(agent)
    finished = confirm_and_finish(agent, task.task_id)
    assert finished.status == "succeeded"
    assert agent.voice.calls == 0
    assert agent.music.calls == 0
    old = agent.list_artifacts(task.task_id)[0]
    old_path = agent.artifact_path(old.artifact_id)
    agent.regenerate(task.task_id, RegenerateRequest(kind="full"))
    again = drain(agent, task.task_id)
    assert again.status == "succeeded"
    assert again.generation_id != old.generation_id
    view = agent.get_generation(again.generation_id)
    assert view.kind == "full"
    assert view.shot_ids
    assert agent.voice.calls == 0
    assert agent.music.calls == 0
    artifacts = agent.list_artifacts(task.task_id)
    assert [item.version for item in artifacts] == [2, 1]
    assert old_path.is_file()
    assert agent.artifact_path(artifacts[0].artifact_id).is_file()
    assert old_path != agent.artifact_path(artifacts[0].artifact_id)
    rows = read_rows(
        agent.home,
        "SELECT status FROM generation_shots WHERE generation_id=?",
        (again.generation_id,),
    )
    assert rows
    assert [row["status"] for row in rows] == ["imported"]
