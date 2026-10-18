"""Credential encryption and versioned, confidential work configuration snapshots."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os

from cryptography.fernet import Fernet, InvalidToken

from video_agent.config import DEFAULT_CONFIG_JSON, AppConfig, _is_secret, config_to_dict, validate_config
from video_agent.errors import VideoAgentError


class SecretCipher:
    def __init__(self, secret_key: str | bytes | None = None) -> None:
        self._fernet = None
        self._key = secret_key if secret_key is not None else os.environ.get('VIDEO_AGENT_SECRET_KEY')
        if self._key:
            try:
                self._fernet = Fernet(self._key)
            except (ValueError, TypeError):
                raise VideoAgentError('CONFIGURATION_ERROR', '配置加密主密钥无效') from None

    def __repr__(self) -> str:
        return 'SecretCipher(<redacted>)'

    def encrypt(self, secrets: dict) -> str | None:
        if not secrets:
            return None
        if self._fernet is None:
            raise VideoAgentError('CONFIGURATION_ERROR', '配置加密主密钥缺失')
        raw = json.dumps(secrets, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return self._fernet.encrypt(raw).decode('ascii')

    def fingerprint(self, value: object) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if self._key:
            key = self._key.encode('ascii') if isinstance(self._key, str) else self._key
            return hmac.new(key, raw, hashlib.sha256).hexdigest()
        return hashlib.sha256(raw).hexdigest()

    def decrypt(self, value: str | None) -> dict:
        if value is None:
            return {}
        if not isinstance(value, str) or not value:
            raise VideoAgentError('CONFIGURATION_ERROR', '配置密钥无法解密')
        if self._fernet is None:
            raise VideoAgentError('CONFIGURATION_ERROR', '配置加密主密钥缺失')
        try:
            result = json.loads(self._fernet.decrypt(value.encode('ascii')))
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            raise VideoAgentError('CONFIGURATION_ERROR', '配置密钥无法解密') from None


def _split(value, path=()):
    """Extract nested credentials as paths, so custom adapter extras stay confidential."""
    if isinstance(value, dict):
        public, secrets = {}, []
        for key, child in value.items():
            if _is_secret(str(key)):
                secrets.append([list(path + (str(key),)), child])
            else:
                public[key], found = _split(child, path + (str(key),))
                secrets.extend(found)
        return public, secrets
    if isinstance(value, list):
        public, secrets = [], []
        for index, child in enumerate(value):
            converted, found = _split(child, path + (index,))
            public.append(converted)
            secrets.extend(found)
        return public, secrets
    return copy.deepcopy(value), []


class SnapshotCodec:
    FORMAT_VERSION = 1

    def __init__(self, secret_key: str | bytes | None = None) -> None:
        self.cipher = SecretCipher(secret_key)

    def __repr__(self) -> str:
        return 'SnapshotCodec(<redacted>)'

    def encode(self, config: AppConfig) -> dict:
        public, secrets = _split(config_to_dict(config))
        encrypted = self.cipher.encrypt({'values': secrets} if secrets else {})
        return {'format_version': self.FORMAT_VERSION, 'config': public, 'encrypted_secrets': encrypted}

    def decode(self, snapshot: dict) -> AppConfig:
        try:
            if not isinstance(snapshot, dict) or snapshot.get('format_version') != self.FORMAT_VERSION:
                raise ValueError
            public = copy.deepcopy(snapshot['config'])
            if not isinstance(public, dict):
                raise ValueError
            # Encrypted snapshots must never carry credentials in their public half.
            _, plain_secrets = _split(public)
            if plain_secrets:
                raise ValueError
            secrets = self.cipher.decrypt(snapshot.get('encrypted_secrets'))
            for path, value in secrets.get('values', []):
                target = public
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
            # Migrated old snapshots can have only mode/adapters, with defaults filled explicitly.
            merged = json.loads(DEFAULT_CONFIG_JSON)
            merged.update(public)
            return validate_config(merged)
        except VideoAgentError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise VideoAgentError('CONFIGURATION_ERROR', '工作配置快照无效或版本不支持') from None
