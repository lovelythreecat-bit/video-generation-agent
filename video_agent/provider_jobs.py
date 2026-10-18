"""Durable provider submission evidence; never stores credentials or request bodies."""
from dataclasses import dataclass, field
import json

import sqlalchemy as sa

from video_agent.db_schema import key_type, text_type
from video_agent.errors import VideoAgentError

metadata = sa.MetaData()
provider_jobs = sa.Table('provider_jobs', metadata,
    sa.Column('adapter', key_type(64), primary_key=True),
    sa.Column('key', key_type(), primary_key=True),
    sa.Column('external_task_id', key_type(255)),
    sa.Column('submission_state', key_type(32), nullable=False),
    sa.Column('request_metadata_json', text_type(), nullable=False),
    sa.Column('download_metadata_json', text_type(), nullable=False),
    mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin')
sa.Index('ix_provider_external', provider_jobs.c.adapter, provider_jobs.c.external_task_id)
worker_heartbeats = sa.Table('worker_heartbeats', metadata,
    sa.Column('singleton_id', sa.Integer(), primary_key=True),
    sa.Column('worker_owner', key_type(), unique=True), sa.Column('lease_until', key_type()),
    sa.Column('last_seen_at', key_type()),
    sa.Column('stopping', sa.Integer(), nullable=False, server_default=sa.text('0')),
    mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin')


@dataclass(frozen=True)
class ProviderJob:
    adapter: str
    key: str
    external_task_id: str | None = None
    submission_state: str = 'intended'
    request_metadata: dict = field(default_factory=dict)
    download_metadata: dict = field(default_factory=dict)


def _job(row):
    return None if row is None else ProviderJob(adapter=row['adapter'], key=row['key'],
        external_task_id=row['external_task_id'], submission_state=row['submission_state'],
        request_metadata=json.loads(row['request_metadata_json']), download_metadata=json.loads(row['download_metadata_json']))


class ProviderJobStore:
    def __init__(self, database):
        self.database = database

    def get(self, adapter: str, key: str) -> ProviderJob | None:
        with self.database.transaction(write=False) as conn:
            return _job(conn.execute('SELECT * FROM provider_jobs WHERE adapter=:adapter AND `key`=:key', {'adapter': adapter, 'key': key}).fetchone())

    def get_by_external_id(self, adapter: str, external_id: str) -> ProviderJob | None:
        with self.database.transaction(write=False) as conn:
            return _job(conn.execute('SELECT * FROM provider_jobs WHERE adapter=:adapter AND external_task_id=:external_id ORDER BY `key` LIMIT 1', {'adapter': adapter, 'external_id': external_id}).fetchone())

    def _values(self, job):
        # A strict allowlist prevents accidental persistence of auth headers, prompts,
        # data URLs or signed download URLs as adapters evolve.
        request = {k: v for k, v in job.request_metadata.items() if k in {'model', 'frame_count', 'aspect_ratio', 'with_audio'} and isinstance(v, (str, int, bool))}
        download = {k: int(v) for k, v in job.download_metadata.items() if k in {'width', 'height'} and isinstance(v, int) and not isinstance(v, bool)}
        if job.adapter == 'fake' and isinstance(job.request_metadata.get('fake_request'), dict):
            from video_agent.ports import ShotRequest
            fake = ShotRequest.model_validate(job.request_metadata['fake_request']).model_dump(mode='json')
            # The fake renderer needs structural media data only.
            fake['prompt'] = ''
            fake['shared_visual'] = dict(product_reference='', scene='', lighting='', palette='', style='')
            request['fake_request'] = fake
        return {'adapter': job.adapter, 'key': job.key, 'external_task_id': job.external_task_id,
            'state': job.submission_state, 'request': json.dumps(request, ensure_ascii=False), 'download': json.dumps(download)}

    def reserve(self, job: ProviderJob) -> bool:
        """Only the transaction that inserts (or safely replaces absent) may POST."""
        values = self._values(job)
        try:
            with self.database.transaction() as conn:
                conn.execute('INSERT INTO provider_jobs (adapter, `key`, external_task_id, submission_state, request_metadata_json, download_metadata_json) VALUES (:adapter, :key, :external_task_id, :state, :request, :download)', values)
            return True
        except sa.exc.IntegrityError:
            with self.database.transaction() as conn:
                result = conn.execute("UPDATE provider_jobs SET submission_state=:state, external_task_id=:external_task_id, request_metadata_json=:request, download_metadata_json=:download WHERE adapter=:adapter AND `key`=:key AND submission_state='absent'", values)
                return result.rowcount == 1

    def save(self, job: ProviderJob) -> None:
        values = self._values(job)
        with self.database.transaction() as conn:
            columns = '(adapter, `key`, external_task_id, submission_state, request_metadata_json, download_metadata_json)'
            insert = f'INSERT INTO provider_jobs {columns} VALUES (:adapter, :key, :external_task_id, :state, :request, :download)'
            if conn.dialect_name == 'mysql':
                sql = insert + ' ON DUPLICATE KEY UPDATE external_task_id=VALUES(external_task_id), submission_state=VALUES(submission_state), request_metadata_json=VALUES(request_metadata_json), download_metadata_json=VALUES(download_metadata_json)'
            else:
                sql = insert + ' ON CONFLICT(adapter, `key`) DO UPDATE SET external_task_id=excluded.external_task_id, submission_state=excluded.submission_state, request_metadata_json=excluded.request_metadata_json, download_metadata_json=excluded.download_metadata_json'
            conn.execute(sql, values)


def ensure_worker_schema(database):
    """Only explicit isolated SQLite fixtures create schema outside Alembic."""
    if database.dialect_name != 'sqlite':
        raise ValueError('Production schema is managed by Alembic')
    metadata.create_all(database.engine)
    with database.transaction() as conn:
        conn.execute('INSERT INTO worker_heartbeats (singleton_id) VALUES (1) ON CONFLICT(singleton_id) DO NOTHING')


class DurableFakeVideo:
    """The local fake keeps its requests in MySQL so process restarts are real."""
    def __init__(self, job_store):
        from video_agent.fake_video import FakeVideo
        self.inner = FakeVideo()
        self.job_store = job_store

    def capabilities(self):
        return self.inner.capabilities()

    def submit_shot(self, request):
        import uuid
        from video_agent.ports import SubmitResult
        job = self.job_store.get('fake', request.provider_idempotency_key)
        if job is not None and job.external_task_id:
            return SubmitResult(external_task_id=job.external_task_id)
        external_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'video-agent-fake:' + request.provider_idempotency_key))
        self.job_store.save(ProviderJob(adapter='fake', key=request.provider_idempotency_key, external_task_id=external_id,
            submission_state='accepted', request_metadata={'fake_request': request.model_dump(mode='json')},
            download_metadata={'width': request.width, 'height': request.height}))
        return SubmitResult(external_task_id=external_id)

    def query(self, *, external_task_id, provider_idempotency_key):
        from video_agent.ports import QueryResult
        job = self.job_store.get('fake', provider_idempotency_key)
        return QueryResult(state='succeeded' if job is not None and job.external_task_id else 'absent')

    def download(self, external_task_id):
        from video_agent.ports import ShotRequest
        job = self.job_store.get_by_external_id('fake', external_task_id)
        if job is None:
            job = self.job_store.get('fake', external_task_id)
        if job is None or 'fake_request' not in job.request_metadata:
            raise VideoAgentError('PROVIDER_REJECTED', '本地替身任务不存在', retryable=True)
        self.inner._by_external[external_task_id] = ShotRequest.model_validate(job.request_metadata['fake_request'])
        return self.inner.download(external_task_id)


class DurableSubmission:
    """Shared crash boundary for adapters whose API cannot look up a client key."""
    def _durable_before(self, request):
        if self.job_store is None:
            return None
        job = self.job_store.get(self.adapter_name, request.provider_idempotency_key)
        if job is not None:
            if job.external_task_id:
                return job.external_task_id
            if job.submission_state in {'intended', 'uncertain', 'accepted'}:
                raise VideoAgentError('PROVIDER_SUBMISSION_UNCERTAIN', '外部提交结果不确定，等待原任务确认', retryable=True)
        reserved = self.job_store.reserve(ProviderJob(adapter=self.adapter_name, key=request.provider_idempotency_key,
            submission_state='intended', request_metadata={'model': self.model, 'frame_count': request.frame_count,
                'aspect_ratio': request.aspect_ratio, 'with_audio': request.with_audio},
            download_metadata={'width': request.width, 'height': request.height}))
        if not reserved:
            job = self.job_store.get(self.adapter_name, request.provider_idempotency_key)
            if job is not None and job.external_task_id:
                return job.external_task_id
            raise VideoAgentError('PROVIDER_SUBMISSION_UNCERTAIN', '外部提交已由其他执行者登记', retryable=True)
        return None

    def _durable_uncertain(self, key):
        if self.job_store is None:
            return
        from dataclasses import replace
        try:
            job = self.job_store.get(self.adapter_name, key)
            if job is not None:
                self.job_store.save(replace(job, submission_state='uncertain'))
        except Exception:
            # The already committed intended record is sufficient evidence to
            # prevent another POST when the database returns.
            pass

    def _durable_accepted(self, key, external_id):
        if self.job_store is None:
            return
        from dataclasses import replace
        try:
            job = self.job_store.get(self.adapter_name, key)
            if job is None:
                raise RuntimeError('Missing submission evidence')
            self.job_store.save(replace(job, external_task_id=external_id, submission_state='accepted'))
        except Exception:
            raise VideoAgentError('PROVIDER_SUBMISSION_UNCERTAIN', '外部任务已响应，任务号持久化未确认', retryable=True) from None

    def _durable_lookup(self, key):
        return None if self.job_store is None else self.job_store.get(self.adapter_name, key)

    def _durable_absent(self, key):
        if self.job_store is None:
            return
        from dataclasses import replace
        job = self.job_store.get(self.adapter_name, key)
        if job is not None:
            self.job_store.save(replace(job, submission_state='absent', external_task_id=None))
