"""Portable core schema. Production DDL is applied by Alembic, never at startup."""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

metadata = sa.MetaData()


def text_type():
    return sa.Text().with_variant(LONGTEXT(), "mysql")


def key_type(length=128):
    return sa.String(length).with_variant(sa.String(length, collation="utf8mb4_bin"), "mysql")


def column(name, *, required=False, key=False, length=128, integer=False, default=None):
    kind = sa.Integer() if integer else key_type(length) if key else text_type()
    return sa.Column(name, kind, nullable=not required, server_default=sa.text(str(default)) if default is not None else None)


def table(name, *columns, constraints=()):
    return sa.Table(name, metadata, *columns, *constraints, mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_bin")


assets = table("assets",
    sa.Column("asset_id", key_type(), primary_key=True),
    column("kind", required=True), column("filename", required=True), column("media_type", required=True),
    column("byte_size", integer=True, required=True), column("relative_path", required=True), column("created_at", key=True, required=True),
)
tasks = table("tasks", sa.Column("task_id", key_type(), primary_key=True),
    column("status", key=True, required=True), column("stage"), column("stage_detail"), column("failure_scope"),
    column("failure_code"), column("failure_message"), column("failure_stage"), column("failure_retryable", integer=True),
    column("failure_details_json"), column("questions_json", required=True),
    column("external_user_id", key=True, length=255), column("external_request_id", key=True, length=255),
    column("head_plan_version", integer=True), column("confirmed_plan_version", integer=True), column("generation_id", key=True),
    column("product_info_json", required=True), column("user_prompt", required=True), column("video_options_json", required=True),
    column("audio_options_json", required=True), column("image_asset_ids_json", required=True), column("first_frame_asset_id", key=True),
    column("pending_instruction"), column("planning_basis_json", required=True),
    column("completed_shots", integer=True), column("total_shots", integer=True),
    column("created_at", key=True, required=True), column("updated_at", key=True, required=True),
)
plan_versions = table("plan_versions",
    sa.Column("task_id", key_type(), primary_key=True), sa.Column("plan_version", sa.Integer(), primary_key=True),
    column("document_json", required=True), column("abandoned", integer=True, required=True, default=0), column("created_at", key=True, required=True),
)
generations = table("generations", sa.Column("generation_id", key_type(), primary_key=True),
    column("task_id", key=True, required=True), column("plan_version", integer=True, required=True), column("kind", required=True),
    column("shot_ids_json", required=True), column("base_artifact_id", key=True), column("status", key=True, required=True),
    column("stage", required=True), column("stage_detail"), column("completed_shots", integer=True), column("total_shots", integer=True),
    column("snapshot_json", required=True), column("copied_config_json", required=True), column("failure_code"), column("failure_message"),
    column("failure_stage"), column("failure_retryable", integer=True), column("failure_details_json"),
    column("auto_attempts_used", integer=True, required=True, default=0), column("next_attempt_at", key=True),
    column("created_at", key=True, required=True), column("updated_at", key=True, required=True),
)
generation_shots = table("generation_shots", sa.Column("generation_id", key_type(), primary_key=True), sa.Column("shot_id", key_type(), primary_key=True),
    column("ordinal", integer=True, required=True), column("start_frame", integer=True, required=True), column("end_frame", integer=True, required=True),
    column("status", required=True), column("provider_idempotency_key", key=True), column("external_task_id", key=True, length=255),
    column("submission_state"), column("clip_path"),
)
artifacts = table("artifacts", sa.Column("artifact_id", key_type(), primary_key=True), column("task_id", key=True, required=True),
    column("generation_id", key=True, required=True), column("plan_version", integer=True, required=True), column("version", integer=True, required=True),
    column("duration_seconds", integer=True, required=True), column("aspect_ratio", required=True), column("width", integer=True, required=True),
    column("height", integer=True, required=True), column("relative_path", required=True), column("quality_status", required=True),
    column("quality_json", required=True), column("created_at", key=True, required=True), constraints=(sa.UniqueConstraint("task_id", "version"),),
)
work_items = table("work_items", sa.Column("work_id", key_type(), primary_key=True), column("task_id", key=True, required=True),
    column("kind", required=True), column("generation_id", key=True), column("ready_at", key=True, required=True),
    column("lease_owner", key=True), column("lease_until", key=True), column("copied_config_json", required=True), column("step", required=True),
    column("next_attempt_at", key=True), column("done", integer=True, required=True, default=0),
)
idempotency = table("idempotency", sa.Column("method", key_type(64), primary_key=True), sa.Column("idempotency_key", key_type(), primary_key=True),
    column("request_hash", key=True, length=64, required=True), column("task_id", key=True),
    column("response_json"), column("created_at", key=True, required=True),
)
sa.Index("ix_tasks_updated", tasks.c.updated_at, tasks.c.task_id)
sa.Index("ix_tasks_external_user", tasks.c.external_user_id)
sa.Index("ix_tasks_external_request", tasks.c.external_request_id)
sa.Index("ix_generations_task", generations.c.task_id, generations.c.status)
sa.Index("ix_work_ready", work_items.c.done, work_items.c.ready_at, work_items.c.lease_until)
