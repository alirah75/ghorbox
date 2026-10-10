"""Current database schema assembled from the frozen initial schema and later revisions."""

from sqlalchemy import CheckConstraint, Index, Integer, Table, UniqueConstraint

from app.database.schema_v1 import base_columns, col
from app.database.schema_v1 import schema as schema_v1


def schema(kind: str):
    metadata = schema_v1(kind)
    if kind != "identity":
        return metadata

    groups = Table(
        "internal_groups",
        metadata,
        *base_columns(),
        col("name"),
        col("manager_admin_id", fk="admins.id"),
        col("is_active", Integer, default="1"),
        UniqueConstraint("name"),
        UniqueConstraint("manager_admin_id"),
        CheckConstraint("is_active IN (0,1)"),
    )
    Index("ix_internal_groups_active_name", groups.c.is_active, groups.c.name)
    memberships = Table(
        "internal_group_memberships",
        metadata,
        *base_columns(),
        col("group_id", fk="internal_groups.id"),
        col("user_id", fk="users.id"),
        col("assigned_by_admin_id", fk="admins.id"),
        UniqueConstraint("user_id"),
        UniqueConstraint("group_id", "user_id"),
    )
    Index("ix_internal_group_memberships_group", memberships.c.group_id)
    return metadata
