"""Add manager-owned internal groups and one-group-per-user membership."""

import sqlalchemy as sa
from alembic import context, op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _base_columns():
    return [
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.Integer(), server_default=sa.text("(unixepoch())"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.Integer(), server_default=sa.text("(unixepoch())"), nullable=False
        ),
    ]


def upgrade():
    if context.config.attributes["kind"] != "identity":
        return
    op.create_table(
        "internal_groups",
        *_base_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("manager_admin_id", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("is_active IN (0,1)"),
        sa.ForeignKeyConstraint(["manager_admin_id"], ["admins.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manager_admin_id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_internal_groups_active_name", "internal_groups", ["is_active", "name"])
    op.create_table(
        "internal_group_memberships",
        *_base_columns(),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("assigned_by_admin_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["assigned_by_admin_id"], ["admins.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["internal_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index(
        "ix_internal_group_memberships_group", "internal_group_memberships", ["group_id"]
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup instead.")
