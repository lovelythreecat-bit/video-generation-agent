import subprocess
import threading

from video_agent.config import RATIO_PAIRS, SettingsPatch
from video_agent.media import probe


def test_http_fake_generation_to_download(api, auth, png):
    # Small real encoding keeps this contract test fast; it is not an MP4 stub.
    current = api.settings.read_settings()
    resolutions = {ratio: (width * 20, height * 20) for ratio, (width, height) in RATIO_PAIRS.items()}
    api.settings.save_settings(SettingsPatch(default_duration=5, resolutions=resolutions, video_bitrate=200000, audio_bitrate=64000), expected_version=current.configuration_revision)
    stop = threading.Event()
    def work():
        while not stop.is_set():
            if not api.agent.tick().did_work:
                stop.wait(0.01)
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        asset = api.client.post("/api/v1/assets", headers=auth, data={"kind": "image"}, files={"file": ("cup.png", png, "image/png")})
        assert asset.status_code == 201, asset.text
        created = api.client.post("/api/v1/tasks", headers=auth, json={"images": [asset.json()["asset_id"]], "first_frame_asset_id": asset.json()["asset_id"], "product_info": {"name": "测试杯", "selling_points": ["轻便"]}, "user_prompt": "商品展示", "video_options": {"duration_seconds": 5, "subtitles": False, "voiceover": False, "music": False}})
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]
        task = None
        for _ in range(500):
            response = api.client.get(f"/api/v1/tasks/{task_id}", headers=auth)
            assert response.status_code == 200
            task = response.json()
            if task["head_plan_version"] is not None:
                break
            assert task["status"] != "failed", task
            stop.wait(0.01)
        assert task["head_plan_version"] is not None
        confirmed = api.client.post(f"/api/v1/tasks/{task_id}/confirm", headers=auth, json={"plan_version": task["head_plan_version"]})
        assert confirmed.status_code == 202, confirmed.text
        generation_id = confirmed.json()["generation_id"]
        for _ in range(3000):
            generation = api.client.get(f"/api/v1/generations/{generation_id}", headers=auth).json()
            assert generation["status"] != "failed", generation
            if generation["status"] == "succeeded":
                break
            stop.wait(0.01)
        assert generation["status"] == "succeeded", generation
        artifacts = api.client.get(f"/api/v1/tasks/{task_id}/artifacts", headers=auth).json()
        assert len(artifacts) == 1
        artifact = artifacts[0]
        content_url = f"/api/v1/artifacts/{artifact['artifact_id']}/content"
        full = api.client.get(content_url, headers=auth)
        assert full.status_code == 200
        dest = api.runtime.home / "downloaded.mp4"
        dest.write_bytes(full.content)
        inspected = probe(dest)
        assert inspected.width == 180 and inspected.height == 320
        assert abs(inspected.frame_count - 150) <= 1
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(dest), "-f", "null", "-"], check=True, capture_output=True, timeout=30)
        partial = api.client.get(content_url, headers={**auth, "Range": "bytes=0-127"})
        assert partial.status_code == 206 and partial.content == full.content[:128]
        assert "path" not in str(artifact)
        if api.app.state.production:
            assert not (api.runtime.home / "agent.sqlite").exists()
            assert not (api.runtime.home / "config.json").exists()
            assert not (api.runtime.home / "provider-state").exists()
            with api.agent.db.transaction(write=False) as connection:
                assert connection.execute("SELECT COUNT(*) AS n FROM provider_jobs").fetchone()["n"] > 0
    finally:
        stop.set()
        worker.join(timeout=30)
