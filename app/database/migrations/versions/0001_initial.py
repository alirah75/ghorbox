"""Initial isolated stores; this revision and schema_v1 are immutable."""

from uuid import uuid4

from alembic import context, op

from app.database.schema_v1 import schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    kind = context.config.attributes["kind"]
    metadata = schema(kind)
    connection = op.get_bind()
    metadata.create_all(connection)
    connection.execute(
        metadata.tables["database_identity"]
        .insert()
        .values(id="singleton", kind=kind, instance_id=uuid4().hex)
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup instead.")
