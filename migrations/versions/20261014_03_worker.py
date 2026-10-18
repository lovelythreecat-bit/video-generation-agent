"""Durable provider submissions and singleton worker lease."""
from alembic import op
from sqlalchemy import text
from video_agent.provider_jobs import metadata

revision = '20261014_03'
down_revision = '20261014_02'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    metadata.create_all(connection, checkfirst=True)
    connection.execute(text('INSERT INTO worker_heartbeats (singleton_id, worker_owner, lease_until, last_seen_at, stopping) VALUES (1, NULL, NULL, NULL, 0)'))


def downgrade():
    metadata.drop_all(op.get_bind(), checkfirst=True)
