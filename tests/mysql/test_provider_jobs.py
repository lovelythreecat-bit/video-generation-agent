from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from PIL import Image

from video_agent.errors import VideoAgentError
from video_agent.ports import ShotRequest, SharedVisual


@pytest.fixture
def store(mysql_database):
    from video_agent.provider_jobs import ProviderJobStore, metadata
    metadata.create_all(mysql_database.engine)
    with mysql_database.transaction() as conn:
        conn.execute('DELETE FROM provider_jobs')
    return ProviderJobStore(mysql_database)


def _request(tmp_path):
    image = tmp_path / 'first.png'
    Image.new('RGB', (10, 10)).save(image)
    return ShotRequest(generation_id='g', shot_id='s', provider_idempotency_key='g:s', prompt='display',
        shared_visual=SharedVisual(product_reference='red', scene='desk', lighting='day', palette='warm', style='real'),
        first_frame_path=image, reference_paths=[], tail_frame_path=None, frame_count=150, aspect_ratio='9:16', fit='native', width=72, height=128)


def test_provider_record_survives_restart(store):
    from video_agent.provider_jobs import ProviderJob, ProviderJobStore
    store.save(ProviderJob(adapter='wan', key='g:s', external_task_id='remote', submission_state='accepted', download_metadata={'width': 72, 'height': 128}))
    restored = ProviderJobStore(store.database).get('wan', 'g:s')
    assert restored.external_task_id == 'remote'
    assert restored.download_metadata == {'width': 72, 'height': 128}
    assert store.get_by_external_id('wan', 'remote') == restored


@pytest.mark.parametrize('adapter', ['seedance', 'wan'])
def test_uncertain_submission_never_blindly_resubmits(store, tmp_path, adapter):
    from video_agent.seedance_video import SeedanceVideo
    from video_agent.wan_video import WanVideo
    calls = []
    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        raise VideoAgentError('PROVIDER_SUBMISSION_UNCERTAIN', 'network')
    constructor = SeedanceVideo if adapter == 'seedance' else WanVideo
    extra = {} if adapter == 'seedance' else {'max_segment_seconds': '10'}
    def build():
        return constructor(base_url='https://example.invalid', api_key='secret', model='wan2.6' if adapter == 'wan' else 'seedance', job_store=store, transport=transport, **extra)
    with pytest.raises(VideoAgentError):
        build().submit_shot(_request(tmp_path))
    assert store.get(adapter, 'g:s').submission_state == 'uncertain'
    with pytest.raises(VideoAgentError) as caught:
        build().submit_shot(_request(tmp_path))
    assert caught.value.code == 'PROVIDER_SUBMISSION_UNCERTAIN'
    assert build().query(external_task_id=None, provider_idempotency_key='g:s').state == 'pending'
    assert len(calls) == 1


def test_response_save_failure_does_not_repeat_post(store, tmp_path, monkeypatch):
    from video_agent.seedance_video import SeedanceVideo
    posts = []
    monkeypatch.setattr('video_agent.seedance_video.request_json', lambda *a, **kw: posts.append(a[0]) or {'id': 'remote-1'})
    save = store.save
    def broken(job):
        if job.submission_state == 'accepted':
            raise RuntimeError('simulated DB outage')
        save(job)
    monkeypatch.setattr(store, 'save', broken)
    def build():
        return SeedanceVideo(base_url='https://example.invalid', api_key='secret', model='m', job_store=store)
    with pytest.raises(VideoAgentError) as caught:
        build().submit_shot(_request(tmp_path))
    assert caught.value.code == 'PROVIDER_SUBMISSION_UNCERTAIN'
    monkeypatch.setattr(store, 'save', save)
    with pytest.raises(VideoAgentError):
        build().submit_shot(_request(tmp_path))
    assert posts == ['POST']


def test_concurrent_submissions_reserve_one_post(store, tmp_path, monkeypatch):
    from video_agent.seedance_video import SeedanceVideo
    request = _request(tmp_path)
    barrier = threading.Barrier(2)
    posts = []
    get = store.get
    def racing_get(adapter, key):
        job = get(adapter, key)
        if job is None:
            barrier.wait(timeout=5)
        return job
    monkeypatch.setattr(store, 'get', racing_get)
    monkeypatch.setattr('video_agent.seedance_video.request_json', lambda *a, **kw: posts.append(a[0]) or {'id': 'remote'})
    def submit(_):
        provider = SeedanceVideo(base_url='https://example.invalid', api_key='secret', model='m', job_store=store)
        try:
            return provider.submit_shot(request)
        except VideoAgentError as exc:
            assert exc.code == 'PROVIDER_SUBMISSION_UNCERTAIN'
            return None
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(submit, range(2)))
    assert posts == ['POST']


def test_wan_download_restores_original_dimensions(store, monkeypatch):
    from video_agent.provider_jobs import ProviderJob, ProviderJobStore
    from video_agent.wan_video import WanVideo
    store.save(ProviderJob(adapter='wan', key='old-key', external_task_id='old-job', submission_state='accepted',
        request_metadata={'model': 'wan2.6'}, download_metadata={'width': 72, 'height': 128}))
    sizes = []
    monkeypatch.setattr('video_agent.wan_video.request_json', lambda *a, **kw: {'output': {'video_url': 'https://video.invalid/file'}})
    monkeypatch.setattr('video_agent.wan_video.request_bytes', lambda *a, **kw: b'video')
    monkeypatch.setattr('video_agent.wan_video._scale_mp4', lambda data, w, h: sizes.append((w, h)) or data)
    provider = WanVideo(base_url='https://api.invalid', api_key='test', model='wan3.0', max_segment_seconds='15', job_store=ProviderJobStore(store.database))
    assert provider.download('old-job') == b'video'
    assert sizes == [(72, 128)]


def test_provider_metadata_never_persists_headers_or_urls(store):
    from video_agent.provider_jobs import ProviderJob
    store.save(ProviderJob(adapter='seedance', key='safe-key',
        request_metadata={'model': 'model', 'api_key': 'test-private-key', 'headers': {'Authorization': 'test-private-header'}},
        download_metadata={'width': 72, 'height': 128, 'url': 'https://example.invalid/?key=test-private-url'}))
    with store.database.transaction(write=False) as conn:
        row = conn.execute("SELECT * FROM provider_jobs WHERE `key`='safe-key'").fetchone()
    assert 'test-private' not in str(row)
