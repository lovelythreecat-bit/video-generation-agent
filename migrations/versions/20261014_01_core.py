"""Initial eight core business tables."""

from alembic import op
from video_agent.db_schema import metadata

revision = "20261014_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Fail on pre-existing conflicting tables instead of silently adopting data.
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    metadata.drop_all(op.get_bind(), checkfirst=False)
