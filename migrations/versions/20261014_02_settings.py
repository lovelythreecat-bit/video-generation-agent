"""Encrypted model profile revisions, role bindings and public settings."""
from alembic import op
from sqlalchemy import text

from video_agent.config_store import metadata, seed_default_settings

revision = '20261014_02'
down_revision = '20261014_01'
branch_labels = None
depends_on = None


class _SeedConnection:
    def __init__(self, connection): self.connection = connection
    def execute(self, sql, parameters=None):
        return self.connection.execute(text(sql), parameters or {}).mappings()


def upgrade():
    connection = op.get_bind()
    metadata.create_all(connection, checkfirst=True)
    seed_default_settings(_SeedConnection(connection))


def downgrade():
    metadata.drop_all(op.get_bind(), checkfirst=True)
