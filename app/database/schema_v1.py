"""Frozen initial schema. Future changes must use a new Alembic revision."""

import time
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)


def base_columns():
    return [
        Column("id", Text, primary_key=True, default=lambda: uuid4().hex),
        Column(
            "created_at",
            Integer,
            nullable=False,
            default=lambda: int(time.time()),
            server_default=text("(unixepoch())"),
        ),
        Column(
            "updated_at",
            Integer,
            nullable=False,
            default=lambda: int(time.time()),
            onupdate=lambda: int(time.time()),
            server_default=text("(unixepoch())"),
        ),
    ]


def col(name, type_=Text, *, nullable=False, default=None, fk=None):
    args = [ForeignKey(fk, ondelete="RESTRICT")] if fk else []
    return Column(name, type_, *args, nullable=nullable, server_default=default)


def enum(name, values):
    # Values originate from the static schema, never from user input.
    return CheckConstraint(f"{name} IN ({','.join(repr(v) for v in values)})")


def schema(kind: str) -> MetaData:
    if kind not in {"identity", "internal", "external"}:
        raise ValueError("Unknown database kind")
    metadata = MetaData()

    def table(name, *columns):
        return Table(name, metadata, *base_columns(), *columns)

    table(
        "database_identity",
        col("kind"),
        col("instance_id"),
        UniqueConstraint("kind"),
        CheckConstraint("id = 'singleton'"),
        enum("kind", [kind]),
    )
    if kind == "identity":
        users = table(
            "users",
            col("telegram_id", Integer),
            col("display_name"),
            col("is_active", Integer, default="1"),
            col("started_at", Integer, nullable=True),
            col("delivery_state", default="unknown"),
            UniqueConstraint("telegram_id"),
            CheckConstraint("telegram_id > 0 AND telegram_id < 4503599627370496"),
            CheckConstraint("is_active IN (0,1)"),
            enum("delivery_state", ["unknown", "reachable", "blocked"]),
        )
        Index("ix_users_active_name", users.c.is_active, users.c.display_name)
        table(
            "admins",
            col("user_id", fk="users.id"),
            col("is_active", Integer, default="1"),
            UniqueConstraint("user_id"),
            CheckConstraint("is_active IN (0,1)"),
        )
        table(
            "admin_grants",
            col("admin_id", fk="admins.id"),
            col("scope"),
            col("permission"),
            UniqueConstraint("admin_id", "scope", "permission"),
            enum("scope", ["internal", "external", "system"]),
            enum(
                "permission",
                [
                    "read_feedback",
                    "manage_reports",
                    "view_statistics",
                    "manage_users",
                    "manage_external_people",
                    "manage_admins",
                ],
            ),
        )
        audit = table(
            "admin_audit",
            col("actor_admin_id", fk="admins.id"),
            col("action"),
            col("target_ref", nullable=True),
            col("outcome"),
        )
        Index("ix_admin_audit_created", audit.c.created_at)
        table("bootstrap_state", CheckConstraint("id = 'singleton'"))
        return metadata

    table(
        "key_bindings",
        col("key_version"),
        col("content_check", LargeBinary),
        col("routing_check", LargeBinary),
        col("index_check", LargeBinary),
        UniqueConstraint("key_version"),
    )
    conversations = table(
        "conversations",
        col("kind"),
        col("subject_cipher", LargeBinary),
        col("route_cipher", LargeBinary),
        col("status", default="open"),
        col("expires_at", Integer),
        col("version", Integer, default="1"),
        enum("kind", ["criticism", "suggestion"]),
        enum("status", ["open", "closed", "suspended", "expired"]),
        CheckConstraint("version > 0"),
    )
    Index("ix_conversations_status_expiry", conversations.c.status, conversations.c.expires_at)
    access = table(
        "participant_access",
        col("conversation_id", fk="conversations.id"),
        col("access_tag", LargeBinary),
        col("permissions_cipher", LargeBinary),
        UniqueConstraint("conversation_id", "access_tag"),
    )
    Index("ix_participant_tag", access.c.access_tag)
    messages = table(
        "messages",
        col("conversation_id", fk="conversations.id"),
        col("sequence", Integer),
        col("body_cipher", LargeBinary),
        col("direction_cipher", LargeBinary),
        col("message_type"),
        col("accepted_day"),
        UniqueConstraint("conversation_id", "sequence"),
        CheckConstraint("sequence > 0"),
        enum("message_type", ["initial", "reply"]),
        CheckConstraint(
            "(sequence = 1 AND message_type = 'initial') OR "
            "(sequence > 1 AND message_type = 'reply')"
        ),
    )
    Index("ix_messages_day", messages.c.accepted_day)
    reports = table(
        "reports",
        col("message_id", fk="messages.id"),
        col("reporter_tag", LargeBinary),
        col("reason"),
        col("status", default="open"),
        col("resolution", nullable=True),
        UniqueConstraint("message_id", "reporter_tag"),
        enum("reason", ["insult", "threat", "inappropriate", "spam", "other"]),
        enum("status", ["open", "accepted", "dismissed"]),
    )
    Index("ix_reports_status", reports.c.status)
    table(
        "weekly_reports",
        col("period_start", Integer),
        col("period_end", Integer),
        col("policy_version", Integer),
        col("payload_cipher", LargeBinary),
        col("status", default="prepared"),
        UniqueConstraint("period_start", "period_end", "policy_version"),
        CheckConstraint("period_end > period_start"),
        enum("status", ["prepared", "queued", "complete"]),
    )
    outbox = table(
        "outbox",
        col("message_id", nullable=True, fk="messages.id"),
        col("weekly_report_id", nullable=True, fk="weekly_reports.id"),
        col("destination_cipher", LargeBinary),
        col("dedupe_key"),
        col("status", default="pending"),
        col("attempts", Integer, default="0"),
        col("next_attempt_at", Integer),
        col("lease_until", Integer, nullable=True),
        col("error_code", nullable=True),
        UniqueConstraint("dedupe_key"),
        CheckConstraint("(message_id IS NOT NULL) != (weekly_report_id IS NOT NULL)"),
        CheckConstraint("attempts >= 0"),
        enum("status", ["pending", "sending", "sent", "failed", "suspended", "uncertain"]),
    )
    Index("ix_outbox_due", outbox.c.status, outbox.c.next_attempt_at)
    sessions = table(
        "interaction_sessions",
        col("token_hash", LargeBinary),
        col("owner_tag", LargeBinary),
        col("state_cipher", LargeBinary),
        col("expires_at", Integer),
        col("consumed_at", Integer, nullable=True),
        UniqueConstraint("token_hash"),
    )
    Index("ix_sessions_expiry", sessions.c.expires_at)
    table(
        "weekly_contributors",
        col("period_start", Integer),
        col("contributor_tag", LargeBinary),
        UniqueConstraint("period_start", "contributor_tag"),
    )
    if kind == "external":
        people = table(
            "external_people",
            col("display_name"),
            col("department", nullable=True),
            col("is_active", Integer, default="1"),
            CheckConstraint("is_active IN (0,1)"),
        )
        Index("ix_external_people_active_name", people.c.is_active, people.c.display_name)
        proposals = table(
            "external_person_proposals",
            col("name"),
            col("department", nullable=True),
            col("status", default="pending"),
            col("notification_route_cipher", LargeBinary, nullable=True),
            enum("status", ["pending", "accepted", "dismissed"]),
        )
        Index("ix_proposals_status", proposals.c.status)
    return metadata
