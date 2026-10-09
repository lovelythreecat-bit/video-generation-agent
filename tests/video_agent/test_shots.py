import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from video_agent.clock import FrozenClock
from video_agent.fake_planner import FakePlanner
from video_agent.models import CreateTaskRequest, PlanDocument, ProductInfo, SharedVisual, ShotPlan, VideoOptions
from video_agent.ports import PlanRequest, PlanResult, QueryResult, ReviseResult, ShotRequest, SubmitResult, VideoCapabilities
from video_agent.service import VideoAgent


def _need_ffmpeg() -> None:
    if __import__("shutil").which("ffmpeg") is None or __import__("shutil").which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "va"
    home.mkdir(parents=True)
    payload = {
        "mode": "fake",
        "duration_tiers": [5, 10, 15, 30],
        "default_duration": 10,
        "max_images": 12,
        "max_file_bytes": 20971520,
        "video_bitrate": 8000000,
        "audio_bitrate": 192000,
        "lease_seconds": 15,
        "resolutions": {
            "9:16": [180, 320],
            "16:9": [320, 180],
            "1:1": [64, 64],
            "4:5": [1080, 1350],
            "3:4": [1080, 1440],
            "4:3": [1440, 1080],
            "3:2": [1620, 1080],
            "2:3": [1080, 1620],
        },
        "adapters": {
            "planner": {"adapter": "fake"},
            "video": {"adapter": "fake"},
            "voice": {"adapter": "fake"},
            "music": {"adapter": "fake"},
            "visual": {"adapter": "none"},
        },
    }
    (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return home


def _png() -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (18, 32), (220, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


def _request(home: Path) -> CreateTaskRequest:
    return CreateTaskRequest(
        product_info=ProductInfo(name="杯子", selling_points=["耐热"]),
        images=[],
        user_prompt="放在桌上",
        video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=False),
    )


class RecordingVideo:
    def __init__(self, home: Path, extra_frames: int = 0, pending_queries: int = 0) -> None:
        self.home = home
        self.extra_frames = extra_frames
        self.pending_queries = pending_queries
        self.queries = 0
        self.seen: list[tuple] = []
        self._requests: dict[str, ShotRequest] = {}

    def capabilities(self) -> VideoCapabilities:
        return VideoCapabilities(
            native_aspects=["9:16"],
            max_segment_seconds=30,
            supports_first_frame=True,
            supports_reference_images=True,
            supports_tail_frame=True,
        )

    def submit_shot(self, request: ShotRequest) -> SubmitResult:
        connection = sqlite3.connect(self.home / "agent.sqlite")
        row = connection.execute(
            """SELECT provider_idempotency_key, submission_state
               FROM generation_shots WHERE generation_id=? AND shot_id=?""",
            (request.generation_id, request.shot_id),
        ).fetchone()
        self.seen.append((request.shot_id, row))
        external = f"ext-{request.shot_id}"
        self._requests[external] = request
        return SubmitResult(external_task_id=external, uncertain=False)

    def query(self, *, external_task_id: str | None, provider_idempotency_key: str) -> QueryResult:
        del external_task_id, provider_idempotency_key
        self.queries += 1
        if self.queries <= self.pending_queries:
            return QueryResult(state="pending")
        return QueryResult(state="succeeded")

    def download(self, external_task_id: str) -> bytes:
        _need_ffmpeg()
        from video_agent.media import fit_image, still_clip
        import tempfile

        request = self._requests[external_task_id]
        frames = max(1, request.frame_count + self.extra_frames)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "frame.png"
            fitted = root / "fitted.png"
            clip = root / "clip.mp4"
            Image.open(request.first_frame_path).save(image)
            fit_image(image, request.fit, request.width, request.height, fitted)
            still_clip(fitted, frames, request.width, request.height, clip, video_bitrate=500_000)
            return clip.read_bytes()


class TwoShotPlanner:
    def capabilities_note(self) -> str:
        return "two shots"

    def plan(self, request: PlanRequest) -> PlanResult:
        total = request.duration_seconds * 30
        cut = total // 2
        document = PlanDocument(
            creative="展示杯子",
            audience="商品观众",
            selling_points=list(request.product_info.selling_points),
            shared_visual=SharedVisual(
                product_reference="红色杯子",
                scene="桌面",
                lighting="均匀",
                palette="红",
                style="写实",
            ),
            shots=[
                ShotPlan(
                    shot_id="s1",
                    content="前段",
                    action="静止",
                    shot_size="中景",
                    camera="固定",
                    scene_id="scene-1",
                    start_frame=0,
                    end_frame=cut,
                ),
                ShotPlan(
                    shot_id="s2",
                    content="后段",
                    action="静止",
                    shot_size="中景",
                    camera="固定",
                    scene_id="scene-1",
                    start_frame=cut,
                    end_frame=total,
                ),
            ],
            duration_seconds=request.duration_seconds,
            aspect_ratio=request.aspect_ratio,
            fit="native" if request.aspect_ratio == "9:16" else "pad",
            subtitles=False,
            voiceover=False,
            music=False,
            first_frame_source="selected",
            first_frame_asset_id=request.first_frame_asset_id,
            model_capabilities="two shots",
        )
        return PlanResult(document=document)

    def revise(self, current: PlanDocument, instruction: str, request: PlanRequest) -> ReviseResult:
        del current, instruction, request
        return ReviseResult()


def _confirmed(tmp_path: Path, video: RecordingVideo, planner=None):
    home = _home(tmp_path)
    video.home = home
    agent = VideoAgent(
        home=home,
        clock=FrozenClock(datetime(2026, 10, 9, tzinfo=timezone.utc)),
        video=video,
        planner=planner if planner is not None else FakePlanner(),
    )
    asset_id = agent.register_asset(filename="p.png", kind="image", data=_png())
    created = agent.create_task(
        CreateTaskRequest(
            product_info=ProductInfo(name="杯子", selling_points=["耐热"]),
            images=[asset_id],
            first_frame_asset_id=asset_id,
            user_prompt="放在桌上",
            video_options=VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=False),
        )
    )
    for _ in range(10):
        view = agent.get_task(created.task_id)
        if view.status == "awaiting_confirmation":
            break
        agent.tick()
    view = agent.get_task(created.task_id)
    assert view.head_plan_version is not None
    agent.confirm(created.task_id, view.head_plan_version)
    confirmed = agent.get_task(created.task_id)
    assert confirmed.generation_id is not None
    return agent, home, confirmed


def _step(home: Path, generation_id: str) -> tuple:
    connection = sqlite3.connect(home / "agent.sqlite")
    return connection.execute(
        "SELECT step, done, next_attempt_at FROM work_items WHERE generation_id=?",
        (generation_id,),
    ).fetchone()


def test_submit_writes_intended_before_the_provider_returns(tmp_path: Path):
    _need_ffmpeg()
    video = RecordingVideo(Path("."))
    agent, home, confirmed = _confirmed(tmp_path, video)
    agent.tick()
    agent.tick()
    assert video.seen
    shot_id, row = video.seen[0]
    assert shot_id == "s1"
    assert row == (f"{confirmed.generation_id}:s1", "intended")
    connection = sqlite3.connect(home / "agent.sqlite")
    stored = connection.execute(
        """SELECT submission_state, external_task_id, status
           FROM generation_shots WHERE generation_id=? AND shot_id='s1'""",
        (confirmed.generation_id,),
    ).fetchone()
    assert stored == ("accepted", "ext-s1", "submitted")
    assert _step(home, confirmed.generation_id)[0] == "import_shot"


def test_import_trims_long_clips_and_stops_before_audio(tmp_path: Path):
    _need_ffmpeg()
    from video_agent.media import probe

    video = RecordingVideo(Path("."), extra_frames=4)
    agent, home, confirmed = _confirmed(tmp_path, video)
    for _ in range(6):
        if _step(home, confirmed.generation_id)[0] == "audio":
            break
        agent.tick()
    assert _step(home, confirmed.generation_id)[0] == "audio"
    clip = home / "tasks" / confirmed.task_id / "generations" / confirmed.generation_id / "shots" / "s1.mp4"
    assert probe(clip).frame_count == 150
    connection = sqlite3.connect(home / "agent.sqlite")
    status = connection.execute(
        "SELECT status, auto_attempts_used FROM generations WHERE generation_id=?",
        (confirmed.generation_id,),
    ).fetchone()
    assert status == ("running", 0)


def test_short_clip_stays_running_after_the_first_short_download(tmp_path: Path):
    _need_ffmpeg()
    video = RecordingVideo(Path("."), extra_frames=-5)
    agent, home, confirmed = _confirmed(tmp_path, video)

    def generation_row():
        connection = sqlite3.connect(home / "agent.sqlite")
        return connection.execute(
            "SELECT status, auto_attempts_used, failure_code FROM generations WHERE generation_id=?",
            (confirmed.generation_id,),
        ).fetchone()

    saw_backoff = False
    for _ in range(12):
        status, used, code = generation_row()
        if status == "running" and used >= 1 and code is None:
            saw_backoff = True
            break
        result = agent.tick()
        if not result.did_work:
            agent.clock.advance(1)
    assert saw_backoff
    status, used, code = generation_row()
    assert status == "running"
    assert used >= 1
    assert code is None
    view = agent.get_task(confirmed.task_id)
    assert view.status == "generating"
    assert view.failure is None


def test_pending_query_defers_until_the_lease_expires(tmp_path: Path):
    _need_ffmpeg()
    video = RecordingVideo(Path("."), pending_queries=1)
    agent, home, confirmed = _confirmed(tmp_path, video)
    agent.tick()
    agent.tick()
    agent.tick()
    step, done, next_attempt = _step(home, confirmed.generation_id)
    assert step == "import_shot"
    assert done == 0
    assert next_attempt is not None
    agent.clock.advance(16)
    agent.tick()
    assert _step(home, confirmed.generation_id)[0] == "audio"
    assert video.queries == 2


def test_import_without_external_id_marks_uncertain_before_fail(tmp_path: Path):
    _need_ffmpeg()
    video = RecordingVideo(Path("."))
    agent, home, confirmed = _confirmed(tmp_path, video)
    agent.tick()
    assert _step(home, confirmed.generation_id)[0] == "submit_shot"
    connection = sqlite3.connect(home / "agent.sqlite")
    connection.execute(
        """UPDATE generation_shots
           SET submission_state='intended', external_task_id=NULL
           WHERE generation_id=? AND shot_id='s1'""",
        (confirmed.generation_id,),
    )
    connection.commit()
    for _ in range(5):
        view = agent.get_task(confirmed.task_id)
        if view.status == "failed":
            break
        agent.tick()
    view = agent.get_task(confirmed.task_id)
    assert view.failure is not None
    assert view.failure.code == "PROVIDER_SUBMISSION_UNCERTAIN"
    stored = connection.execute(
        """SELECT submission_state FROM generation_shots
           WHERE generation_id=? AND shot_id='s1'""",
        (confirmed.generation_id,),
    ).fetchone()
    assert stored == ("uncertain",)


def test_two_shots_import_in_order(tmp_path: Path):
    _need_ffmpeg()
    video = RecordingVideo(Path("."))
    agent, home, confirmed = _confirmed(tmp_path, video, planner=TwoShotPlanner())
    for _ in range(10):
        if _step(home, confirmed.generation_id)[0] == "audio":
            break
        agent.tick()
    assert [item[0] for item in video.seen] == ["s1", "s2"]
    connection = sqlite3.connect(home / "agent.sqlite")
    rows = connection.execute(
        """SELECT shot_id, status FROM generation_shots
           WHERE generation_id=? ORDER BY ordinal""",
        (confirmed.generation_id,),
    ).fetchall()
    assert rows == [("s1", "imported"), ("s2", "imported")]
