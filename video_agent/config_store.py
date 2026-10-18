"""Configuration DTOs and schema; credentials never appear in public resource models."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT

from video_agent.config import DEFAULT_CONFIG_JSON, PUBLIC_SETTING_FIELDS

Role = Literal['planner', 'video', 'voice', 'music', 'visual']


class DTO(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class ModelProfileCreate(DTO):
    name: str = Field(min_length=1, max_length=200)
    role: Role
    adapter: str = Field(min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=200)
    parameters: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    enabled: bool = True


class ModelProfileUpdate(DTO):
    expected_version: int = Field(ge=1, strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    adapter: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=200)
    parameters: dict[str, JsonValue] | None = Field(default=None, repr=False)
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    clear_secrets: list[str] = Field(default_factory=list)
    enabled: bool | None = None


class PublicModelProfile(DTO):
    profile_id: str
    name: str
    role: Role
    adapter: str
    base_url: str | None
    model: str | None
    parameters: dict[str, JsonValue]
    enabled: bool
    version: int
    has_api_key: bool
    secret_presence: dict[str, bool]
    created_at: str
    updated_at: str


class ProfilePage(DTO):
    items: list[PublicModelProfile]
    limit: int
    offset: int
    total: int


class BindingTarget(DTO):
    adapter: str | None = None
    profile_id: str | None = None


class BindingsPatch(DTO):
    planner: BindingTarget | None = None
    video: BindingTarget | None = None
    voice: BindingTarget | None = None
    music: BindingTarget | None = None
    visual: BindingTarget | None = None


class BindingsView(DTO):
    configuration_revision: int
    planner: BindingTarget
    video: BindingTarget
    voice: BindingTarget
    music: BindingTarget
    visual: BindingTarget


class SettingsView(DTO):
    configuration_revision: int
    mode: Literal['fake', 'real']
    duration_tiers: list[int]
    default_duration: int
    max_images: int
    max_file_bytes: int
    video_bitrate: int
    audio_bitrate: int
    lease_seconds: int
    resolutions: dict[str, tuple[int, int]]


metadata = MetaData()
_TEXT = Text().with_variant(LONGTEXT(), 'mysql')
_ID = String(128, collation='utf8mb4_bin').with_variant(String(128), 'sqlite')
_OPTIONS = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_bin'}

model_profiles = Table('model_profiles', metadata,
    Column('profile_id', _ID, primary_key=True),
    Column('role', String(32), nullable=False),
    Column('adapter', String(80), nullable=False),
    Column('enabled', Integer, nullable=False),
    Column('version', Integer, nullable=False),
    Column('body_json', _TEXT, nullable=False),
    Column('encrypted_secrets', _TEXT),
    Column('created_at', String(40), nullable=False),
    Column('updated_at', String(40), nullable=False),
    Column('deleted_at', String(40)), **_OPTIONS)

model_profile_revisions = Table('model_profile_revisions', metadata,
    Column('profile_id', _ID, primary_key=True),
    Column('version', Integer, primary_key=True),
    Column('body_json', _TEXT, nullable=False),
    Column('encrypted_secrets', _TEXT),
    Column('created_at', String(40), nullable=False), **_OPTIONS)

adapter_bindings = Table('adapter_bindings', metadata,
    Column('role', String(32), primary_key=True),
    Column('adapter', String(80), nullable=False),
    Column('profile_id', _ID), **_OPTIONS)

app_settings = Table('app_settings', metadata,
    Column('singleton_id', Integer, primary_key=True),
    Column('settings_json', _TEXT, nullable=False),
    Column('configuration_revision', Integer, nullable=False), **_OPTIONS)


def seed_default_settings(conn) -> None:
    """Seed an empty database once; never reset a configured database on later upgrades."""
    if conn.execute('SELECT singleton_id FROM app_settings WHERE singleton_id=1').fetchone():
        return
    data = json.loads(DEFAULT_CONFIG_JSON)
    conn.execute('INSERT INTO app_settings(singleton_id,settings_json,configuration_revision) VALUES (1,:settings,1)',
                 {'settings': json.dumps({key: data[key] for key in PUBLIC_SETTING_FIELDS}, ensure_ascii=False)})
    for role, adapter in data['adapters'].items():
        conn.execute('INSERT INTO adapter_bindings(role,adapter,profile_id) VALUES (:role,:adapter,NULL)',
                     {'role': role, 'adapter': adapter['adapter']})


def ensure_settings_schema(database) -> None:
    """Explicit schema bootstrap for isolated test databases and migration tooling."""
    metadata.create_all(database.engine)
    with database.transaction() as conn:
        seed_default_settings(conn)
