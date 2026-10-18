"""Atomic settings/profile management and encrypted configuration snapshots."""
from __future__ import annotations

import hmac
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from video_agent.config import (
    ADAPTER_ROLES, PUBLIC_SETTING_FIELDS, AppConfig, SettingsPatch, _is_secret, validate_config,
)
from video_agent.config_store import (
    BindingTarget, BindingsPatch, BindingsView, ModelProfileCreate, ModelProfileUpdate,
    ProfilePage, PublicModelProfile, SettingsView, ensure_settings_schema,
)
from video_agent.errors import VideoAgentError
from video_agent.secrets import SnapshotCodec


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _error(code: str, message: str):
    raise VideoAgentError(code, message)


def _no_secret_parameters(value) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_secret(str(key)):
                _error('VALIDATION_ERROR', '敏感凭据必须通过 secrets 字段提供')
            _no_secret_parameters(child)
    elif isinstance(value, list):
        for child in value:
            _no_secret_parameters(child)


class SettingsService:
    def __init__(self, database, secret_key: str | bytes | None = None) -> None:
        self.database = database
        self.codec = SnapshotCodec(secret_key)
        if getattr(database, 'dialect_name', None) == 'sqlite':
            ensure_settings_schema(database)

    @contextmanager
    def _connection(self, conn=None, *, write=False):
        if conn is not None:
            yield conn
        else:
            with self.database.transaction(write=write) as connection:
                yield connection

    def _settings_row(self, conn, *, lock=False):
        sql = 'SELECT * FROM app_settings WHERE singleton_id=1'
        if lock:
            sql = conn.for_update(sql)
        row = conn.execute(sql).fetchone()
        if row is None:
            _error('CONFIGURATION_ERROR', '数据库配置尚未初始化')
        return row

    def _expect(self, row, expected):
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            _error('VALIDATION_ERROR', '配置版本必须是正整数')
        if expected != row['configuration_revision']:
            _error('CONFLICT', '配置版本已变化，请重新读取')

    def _bump(self, conn, current):
        conn.execute('UPDATE app_settings SET configuration_revision=:version WHERE singleton_id=1', {'version': current + 1})

    def _profile_row(self, conn, profile_id):
        row = conn.execute('SELECT * FROM model_profiles WHERE profile_id=:id AND deleted_at IS NULL', {'id': profile_id}).fetchone()
        if row is None:
            _error('NOT_FOUND', '模型配置不存在')
        return row

    def _public(self, row) -> PublicModelProfile:
        body = json.loads(row['body_json'])
        presence = body.get('secret_presence', {})
        return PublicModelProfile(
            profile_id=row['profile_id'], name=body['name'], role=row['role'], adapter=row['adapter'],
            base_url=body.get('base_url'), model=body.get('model'), parameters=body.get('parameters', {}),
            enabled=bool(row['enabled']), version=row['version'], has_api_key=bool(presence.get('api_key')),
            secret_presence=presence, created_at=row['created_at'], updated_at=row['updated_at'],
        )

    def _validate_profile(self, body):
        from video_agent.factory import adapter_roles
        roles = adapter_roles(body['adapter'])
        if body['role'] not in roles:
            _error('VALIDATION_ERROR', '适配器与模型角色不兼容')
        _no_secret_parameters(body.get('parameters', {}))
        if any(key in body.get('parameters', {}) for key in ('adapter', 'base_url', 'model')):
            _error('VALIDATION_ERROR', '适配器保留参数必须通过对应字段提供')
        if any(not isinstance(key, str) or not _is_secret(key) for key in body.get('secrets', {})):
            _error('VALIDATION_ERROR', 'secrets 只能包含敏感凭据字段')
        address = body.get('base_url')
        if address:
            try:
                url = urlsplit(address)
                if url.username or url.password or any(_is_secret(key) for key in parse_qs(url.query)):
                    _error('VALIDATION_ERROR', '接口地址不得包含敏感凭据')
            except ValueError:
                _error('VALIDATION_ERROR', '接口地址格式无效')

    def _write_profile(self, conn, profile_id, body, *, version, created_at, deleted_at=None):
        self._validate_profile(body)
        secrets = {key: value for key, value in body.pop('secrets', {}).items() if value != ''}
        body['secret_presence'] = {key: True for key in secrets}
        encrypted = self.codec.cipher.encrypt(secrets)
        stamp = _now()
        parameters = dict(id=profile_id, role=body['role'], adapter=body['adapter'], enabled=int(body['enabled']),
                          version=version, body=_json(body), encrypted=encrypted, created=created_at,
                          updated=stamp, deleted=deleted_at)
        if version == 1:
            conn.execute('INSERT INTO model_profiles(profile_id,role,adapter,enabled,version,body_json,encrypted_secrets,created_at,updated_at,deleted_at) VALUES (:id,:role,:adapter,:enabled,:version,:body,:encrypted,:created,:updated,:deleted)', parameters)
        else:
            conn.execute('UPDATE model_profiles SET role=:role,adapter=:adapter,enabled=:enabled,version=:version,body_json=:body,encrypted_secrets=:encrypted,updated_at=:updated,deleted_at=:deleted WHERE profile_id=:id', parameters)
        conn.execute('INSERT INTO model_profile_revisions(profile_id,version,body_json,encrypted_secrets,created_at) VALUES (:id,:version,:body,:encrypted,:updated)', parameters)
        return self._profile_row(conn, profile_id) if deleted_at is None else None

    def read_effective_config(self, *, conn=None) -> AppConfig:
        with self._connection(conn, write=True) as connection:
            settings = self._settings_row(connection, lock=True)
            body = json.loads(settings['settings_json'])
            adapters = {}
            for binding in connection.execute('SELECT * FROM adapter_bindings').fetchall():
                role = binding['role']
                if binding['profile_id']:
                    profile = self._profile_row(connection, binding['profile_id'])
                    if not profile['enabled'] or profile['role'] != role or profile['adapter'] != binding['adapter']:
                        _error('CONFIGURATION_ERROR', '绑定的模型配置不可用')
                    public = json.loads(profile['body_json'])
                    fields = dict(public.get('parameters', {}))
                    fields.update({name: public[name] for name in ('base_url', 'model') if public.get(name) is not None})
                    fields.update(self.codec.cipher.decrypt(profile['encrypted_secrets']))
                    adapters[role] = {'adapter': profile['adapter'], **fields}
                else:
                    adapters[role] = {'adapter': binding['adapter']}
            body['adapters'] = adapters
            return validate_config(body)

    def snapshot(self, *, conn=None) -> dict:
        with self._connection(conn, write=True) as connection:
            # Serialize with configuration mutations, including all profile versions and bindings.
            row = self._settings_row(connection, lock=True)
            snapshot = self.codec.encode(self.read_effective_config(conn=connection))
            snapshot['configuration_revision'] = row['configuration_revision']
            return snapshot

    def read_settings(self) -> SettingsView:
        with self.database.transaction(write=False) as conn:
            row = self._settings_row(conn)
            return SettingsView(configuration_revision=row['configuration_revision'], **json.loads(row['settings_json']))

    def read_bindings(self) -> BindingsView:
        with self.database.transaction() as conn:
            self._settings_row(conn, lock=True)
            return self._bindings(conn)

    def _bindings(self, conn):
        row = self._settings_row(conn)
        values = {item['role']: BindingTarget(adapter=item['adapter'], profile_id=item['profile_id'])
                  for item in conn.execute('SELECT * FROM adapter_bindings').fetchall()}
        return BindingsView(configuration_revision=row['configuration_revision'], **values)

    def _set_binding(self, conn, role, target):
        if target.profile_id:
            row = self._profile_row(conn, target.profile_id)
            if not row['enabled']:
                _error('CONFLICT', '不能绑定已停用模型配置')
            if row['role'] != role or (target.adapter is not None and target.adapter != row['adapter']):
                _error('VALIDATION_ERROR', '适配器与绑定角色不兼容')
            adapter = row['adapter']
        else:
            adapter = target.adapter
            if adapter not in ('fake', 'none') or (role in ('planner', 'video') and adapter == 'none'):
                _error('VALIDATION_ERROR', '真实适配器必须绑定模型配置')
        conn.execute('UPDATE adapter_bindings SET adapter=:adapter,profile_id=:id WHERE role=:role',
                     {'adapter': adapter, 'id': target.profile_id, 'role': role})

    def set_bindings(self, patch: BindingsPatch, *, expected_version: int) -> BindingsView:
        with self.database.transaction() as conn:
            row = self._settings_row(conn, lock=True)
            self._expect(row, expected_version)
            for role in ADAPTER_ROLES:
                target = getattr(patch, role)
                if target is not None:
                    self._set_binding(conn, role, target)
            self.read_effective_config(conn=conn)
            self._bump(conn, row['configuration_revision'])
            return self._bindings(conn)

    def save_settings(self, patch: SettingsPatch, *, expected_version: int) -> AppConfig:
        with self.database.transaction() as conn:
            row = self._settings_row(conn, lock=True)
            self._expect(row, expected_version)
            body = json.loads(row['settings_json'])
            for name in PUBLIC_SETTING_FIELDS:
                value = getattr(patch, name)
                if value is not None:
                    body[name] = value
            conn.execute('UPDATE app_settings SET settings_json=:body WHERE singleton_id=1', {'body': _json(body)})
            # Desktop-compatible adapter form updates share the very same transaction and validation.
            for role in ADAPTER_ROLES:
                value = getattr(patch, role)
                if value is None:
                    continue
                if value.adapter in ('fake', 'none'):
                    self._set_binding(conn, role, BindingTarget(adapter=value.adapter))
                    continue
                binding = conn.execute('SELECT * FROM adapter_bindings WHERE role=:role', {'role': role}).fetchone()
                current = self._profile_row(conn, binding['profile_id']) if binding['profile_id'] else None
                same = current is not None and current['adapter'] == value.adapter
                data = json.loads(current['body_json']) if same else {'name': role, 'role': role, 'adapter': value.adapter, 'parameters': {}, 'enabled': True}
                credentials = self.codec.cipher.decrypt(current['encrypted_secrets']) if same else {}
                for key, field_value in value.fields.items():
                    if _is_secret(key):
                        if field_value != '':
                            credentials[key] = field_value
                    elif key in ('base_url', 'model'):
                        data[key] = field_value
                    else:
                        data['parameters'][key] = field_value
                data['secrets'] = credentials
                profile_id = current['profile_id'] if same else str(uuid.uuid4())
                updated = self._write_profile(conn, profile_id, data, version=current['version'] + 1 if same else 1,
                                              created_at=current['created_at'] if same else _now())
                self._set_binding(conn, role, BindingTarget(profile_id=updated['profile_id']))
            result = self.read_effective_config(conn=conn)
            self._bump(conn, row['configuration_revision'])
            return result

    def create_profile(self, data: ModelProfileCreate, *, idempotency_key: str | None = None) -> PublicModelProfile:
        with self.database.transaction() as conn:
            row = self._settings_row(conn, lock=True)
            request_hash = self.codec.cipher.fingerprint(data.model_dump())
            if idempotency_key is not None:
                if re.fullmatch(r'[\x21-\x7e]{1,128}', idempotency_key) is None:
                    _error('VALIDATION_ERROR', '幂等键必须为 1 到 128 个可见 ASCII 字符')
                previous = conn.execute('SELECT * FROM idempotency WHERE method=:method AND idempotency_key=:key',
                                        {'method': 'create_profile', 'key': idempotency_key}).fetchone()
                if previous:
                    if not hmac.compare_digest(previous['request_hash'], request_hash):
                        _error('CONFLICT', '幂等键对应的请求不一致')
                    return PublicModelProfile.model_validate_json(previous['response_json'])
            profile_id = str(uuid.uuid4())
            written = self._write_profile(conn, profile_id, data.model_dump(), version=1, created_at=_now())
            result = self._public(written)
            self._bump(conn, row['configuration_revision'])
            if idempotency_key is not None:
                conn.execute('INSERT INTO idempotency(method,idempotency_key,request_hash,task_id,response_json,created_at) VALUES (:method,:key,:hash,NULL,:response,:created)',
                             {'method': 'create_profile', 'key': idempotency_key, 'hash': request_hash,
                              'response': result.model_dump_json(), 'created': _now()})
            return result

    def get_profile(self, profile_id: str) -> PublicModelProfile:
        with self.database.transaction(write=False) as conn:
            return self._public(self._profile_row(conn, profile_id))

    def list_profiles(self, *, limit: int, offset: int) -> ProfilePage:
        if isinstance(limit, bool) or isinstance(offset, bool) or not 1 <= limit <= 100 or offset < 0:
            _error('VALIDATION_ERROR', '分页参数无效')
        with self.database.transaction(write=False) as conn:
            count = conn.execute('SELECT COUNT(*) AS total FROM model_profiles WHERE deleted_at IS NULL').fetchone()['total']
            rows = conn.execute('SELECT * FROM model_profiles WHERE deleted_at IS NULL ORDER BY updated_at DESC,profile_id DESC LIMIT :limit OFFSET :offset', {'limit': limit, 'offset': offset}).fetchall()
            return ProfilePage(items=[self._public(row) for row in rows], total=count, limit=limit, offset=offset)

    def update_profile(self, profile_id: str, patch: ModelProfileUpdate) -> PublicModelProfile:
        with self.database.transaction() as conn:
            settings = self._settings_row(conn, lock=True)
            row = self._profile_row(conn, profile_id)
            if patch.expected_version != row['version']:
                _error('CONFLICT', '模型配置版本已变化，请重新读取')
            if set(patch.clear_secrets) & set(patch.secrets):
                _error('VALIDATION_ERROR', '同一凭据不能同时更新和清除')
            if any(not _is_secret(key) for key in patch.clear_secrets):
                _error('VALIDATION_ERROR', 'clear_secrets 只能包含敏感凭据字段')
            bound = conn.execute('SELECT role FROM adapter_bindings WHERE profile_id=:id', {'id': profile_id}).fetchone()
            body = json.loads(row['body_json'])
            body.pop('secret_presence', None)
            for name in ('name', 'role', 'adapter', 'base_url', 'model', 'parameters', 'enabled'):
                if name in patch.model_fields_set:
                    value = getattr(patch, name)
                    if value is None and name not in ('base_url', 'model'):
                        _error('VALIDATION_ERROR', '模型配置字段不能设为空')
                    body[name] = value
            if bound and (not body['enabled'] or body['role'] != row['role']):
                _error('CONFLICT', '绑定中的模型配置不能停用或改变角色')
            secrets = self.codec.cipher.decrypt(row['encrypted_secrets'])
            for key in patch.clear_secrets:
                secrets.pop(key, None)
            for key, value in patch.secrets.items():
                if value != '':
                    secrets[key] = value
            body['secrets'] = secrets
            updated = self._write_profile(conn, profile_id, body, version=row['version'] + 1, created_at=row['created_at'])
            if bound:
                conn.execute('UPDATE adapter_bindings SET adapter=:adapter WHERE profile_id=:id',
                             {'adapter': updated['adapter'], 'id': profile_id})
            self.read_effective_config(conn=conn)
            self._bump(conn, settings['configuration_revision'])
            return self._public(updated)

    def delete_profile(self, profile_id: str, *, expected_version: int) -> None:
        with self.database.transaction() as conn:
            settings = self._settings_row(conn, lock=True)
            row = self._profile_row(conn, profile_id)
            if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
                _error('VALIDATION_ERROR', '模型配置版本必须是正整数')
            if expected_version != row['version']:
                _error('CONFLICT', '模型配置版本已变化，请重新读取')
            if conn.execute('SELECT role FROM adapter_bindings WHERE profile_id=:id', {'id': profile_id}).fetchone():
                _error('CONFLICT', '绑定中的模型配置不能删除')
            body = json.loads(row['body_json'])
            body['secrets'] = self.codec.cipher.decrypt(row['encrypted_secrets'])
            self._write_profile(conn, profile_id, body, version=row['version'] + 1, created_at=row['created_at'], deleted_at=_now())
            self._bump(conn, settings['configuration_revision'])
