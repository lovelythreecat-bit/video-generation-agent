from pathlib import Path

import pytest

from tests.recovery_support import confirm_and_finish, digest, drain, generation_dir, make_agent, open_plan, require_ffmpeg
from video_agent.errors import VideoAgentError
from video_agent.media import concat_clips
import video_agent.media as media


def test_compose_retry_reuses_the_same_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    from tests.recovery_support import CountingMusic, CountingVoice

    video = agent.video
    agent.voice = CountingVoice(agent.voice)
    agent.music = CountingMusic(agent.music)
    submits = {"count": 0}
    original = video.submit_shot

    def count_submit(request):
        submits["count"] += 1
        return original(request)

    video.submit_shot = count_submit
    task, _image = open_plan(agent)

    def fail_once(clips, dest):
        raise VideoAgentError("MEDIA_EXPORT_FAILED", "拼接失败", stage="composing", retryable=True)

    monkeypatch.setattr(media, "concat_clips", fail_once)
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    failed = drain(agent, task.task_id)
    monkeypatch.setattr(media, "concat_clips", concat_clips)
    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.code == "MEDIA_EXPORT_FAILED"
    assert failed.failure.retryable is True
    generation_id = failed.generation_id
    root = generation_dir(agent, task.task_id, generation_id)
    voice = digest(root / "voice.m4a")
    shot = digest(root / "shots" / "s1.mp4")
    voice_calls = agent.voice.calls
    music_calls = agent.music.calls
    submit_count = submits["count"]
    agent.retry_generation(generation_id)
    again = drain(agent, task.task_id)
    assert again.status == "succeeded"
    assert again.generation_id == generation_id
    assert digest(root / "voice.m4a") == voice
    assert digest(root / "shots" / "s1.mp4") == shot
    assert agent.voice.calls == voice_calls
    assert agent.music.calls == music_calls
    assert submits["count"] == submit_count


def test_first_frame_failure_cannot_reach_the_video_model(tmp_path: Path):
    agent = make_agent(tmp_path)
    submits = {"count": 0}
    original = agent.video.submit_shot

    def count_submit(request):
        submits["count"] += 1
        return original(request)

    agent.video.submit_shot = count_submit
    task, _image = open_plan(agent, first_frame=False)
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    failed = drain(agent, task.task_id)
    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.code == "FIRST_FRAME_NOT_IMPLEMENTED"
    assert failed.failure.retryable is False
    with pytest.raises(VideoAgentError) as caught:
        agent.retry_generation(failed.generation_id)
    assert caught.value.code == "STATE_CONFLICT"
    assert submits["count"] == 0
