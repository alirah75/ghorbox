"""Bounded per-domain retention transactions; preview does not modify application records."""

from sqlalchemy import delete, func, select, update

from app.services.statistics_service import StatisticsService

RETENTION_DAYS = 180
BATCH_SIZE = 100


class MaintenanceService:
    def __init__(self, feedback):
        self.s = feedback
        self.t = feedback.tables

    async def run(self, apply=False):
        s = self.s
        now, cutoff = int(s.clock()), int(s.clock()) - RETENTION_DAYS * 86400
        c, m, r, o, a, sessions, contributors = (
            self.t[name]
            for name in (
                "conversations",
                "messages",
                "reports",
                "outbox",
                "participant_access",
                "interaction_sessions",
                "weekly_contributors",
            )
        )
        async with s.database.transaction(write=apply) as conn:
            open_reports = (
                select(r.c.id)
                .join(m, m.c.id == r.c.message_id)
                .where(m.c.conversation_id == c.c.id, r.c.status == "open")
                .exists()
            )
            candidates = (
                (
                    await conn.execute(
                        select(c.c.id)
                        .where(c.c.updated_at <= cutoff, ~open_reports)
                        .order_by(c.c.updated_at, c.c.id)
                        .limit(BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )
            expired = (c.c.status == "open") & (c.c.expires_at <= now)
            expired_sessions = sessions.c.expires_at <= now
            lost_leases = (o.c.status == "sending") & (o.c.lease_until <= now)
            result = {
                "conversations_to_expire": await conn.scalar(
                    select(func.count()).select_from(c).where(expired)
                ),
                "conversations_to_purge": len(candidates),
                "sessions_to_remove": await conn.scalar(
                    select(func.count()).select_from(sessions).where(expired_sessions)
                ),
                "ambiguous_deliveries": await conn.scalar(
                    select(func.count()).select_from(o).where(lost_leases)
                ),
                "applied": apply,
            }
            if not apply:
                return result
            await conn.execute(
                update(o)
                .where(lost_leases)
                .values(status="uncertain", lease_until=None, error_code="expired_lease")
            )
            # Preserve last human activity; expiry must not reset the retention clock.
            await conn.execute(
                update(c)
                .where(expired)
                .values(status="expired", updated_at=c.c.updated_at, version=c.c.version + 1)
            )
            inactive = select(c.c.id).where(c.c.status.in_(["closed", "expired", "suspended"]))
            await conn.execute(
                update(o)
                .where(
                    o.c.status == "pending",
                    o.c.message_id.in_(select(m.c.id).where(m.c.conversation_id.in_(inactive))),
                )
                .values(status="suspended", error_code="conversation_inactive", lease_until=None)
            )
            if candidates:
                message_ids = select(m.c.id).where(m.c.conversation_id.in_(candidates))
                await conn.execute(delete(r).where(r.c.message_id.in_(message_ids)))
                await conn.execute(delete(o).where(o.c.message_id.in_(message_ids)))
                await conn.execute(delete(a).where(a.c.conversation_id.in_(candidates)))
                await conn.execute(delete(m).where(m.c.conversation_id.in_(candidates)))
                await conn.execute(delete(c).where(c.c.id.in_(candidates)))
            await conn.execute(delete(sessions).where(expired_sessions))
            # A conservative eight-day margin handles local week boundaries and timezone offsets.
            await conn.execute(
                delete(contributors).where(contributors.c.period_start < cutoff - 8 * 86400)
            )
            if "external_person_proposals" in self.t:
                p = self.t["external_person_proposals"]
                old = (
                    select(p.c.id)
                    .where(p.c.status != "pending", p.c.updated_at <= cutoff)
                    .limit(BATCH_SIZE)
                )
                await conn.execute(delete(p).where(p.c.id.in_(old)))
            await StatisticsService(s).reconcile(conn)
            return result
