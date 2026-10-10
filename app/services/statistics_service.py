"""Frozen weekly releases: scoped aggregates, small-cohort and complementary suppression."""

from collections import defaultdict
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert

from app.core.permissions import AccessDenied, InputError
from app.core.security import Purpose, Scope
from app.database.identity_repository import IdentityRepository

POLICY_VERSION = 1
MIN_COHORT = 5


def bucket(number):
    if number < MIN_COHORT:
        return None
    lower = number // MIN_COHORT * MIN_COHORT
    return f"{lower}–{lower + MIN_COHORT - 1}"


def latest_period(now, zone):
    """Most recent week whose Saturday 09:00 local release time has passed."""
    date = datetime.fromtimestamp(now, zone)
    end = (date - timedelta(days=(date.weekday() - 5) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if date < end.replace(hour=9):
        end -= timedelta(days=7)
    return int((end - timedelta(days=7)).timestamp()), int(end.timestamp())


def render_report(payload, page=None):
    scope = "داخلی" if payload["scope"] == "internal" else "خارجی"
    text = (
        f"📊 گزارش هفتگی حوزه {scope}\nاز {payload['start']} تا پیش از {payload['end']}"
        "\nمرز هفته: شنبه ساعت ۰۰:۰۰؛ تاریخ میلادی محلی\n"
    )
    if payload["hidden"]:
        return text + "\nآمار این هفته برای حفاظت از حریم خصوصی منتشر نمی‌شود."
    labels = {
        "initial": "بازخورد اولیه",
        "criticism": "انتقاد",
        "suggestion": "پیشنهاد",
        "contributors": "مشارکت‌کنندگان اولیه",
        "replies": "پاسخ‌ها",
        "active_members": "اعضای فعال هنگام تهیه",
    }
    text += "\nاعداد به‌صورت بازه‌اند؛ «پنهان» به معنی صفر نیست."
    for key, value in payload["counts"].items():
        text += f"\n{labels[key]}: {value or 'پنهان'}"
    if payload["recipients"] is None:
        text += "\nتفکیک گیرندگان: برای حفظ حریم خصوصی پنهان است."
    elif page is None:
        text += "\nتفکیک مجاز گیرندگان در پنل آمار قابل مشاهده است."
    else:
        text += "\n\nدریافت بازخورد اولیه:"
        for item in payload["recipients"][page * 10 : page * 10 + 10]:
            text += f"\n{item['name']}: {item['count']}"
    return text


class StatisticsService:
    def __init__(self, feedback):
        self.s = feedback
        self.t = feedback.tables

    async def _snapshot(self, conn, ic, start, end):
        s = self.s
        first = datetime.fromtimestamp(start, s.zone).date().isoformat()
        last = datetime.fromtimestamp(end, s.zone).date().isoformat()
        payload = {
            "policy": POLICY_VERSION,
            "scope": s.scope.value,
            "start": first,
            "end": last,
            "hidden": True,
            "counts": {},
            "recipients": None,
        }
        contributors = self.t["weekly_contributors"]
        cohort = await conn.scalar(
            select(func.count())
            .select_from(contributors)
            .where(contributors.c.period_start == start)
        )
        if cohort < MIN_COHORT:
            return payload
        m, c = self.t["messages"], self.t["conversations"]
        rows = (
            await conn.execute(
                select(c.c.id, c.c.kind, c.c.subject_cipher, c.c.route_cipher)
                .join(m, m.c.conversation_id == c.c.id)
                .where(
                    m.c.message_type == "initial",
                    m.c.accepted_day >= first,
                    m.c.accepted_day < last,
                )
            )
        ).mappings()
        kinds = {"criticism": set(), "suggestion": set()}
        kind_counts = {"criticism": 0, "suggestion": 0}
        receivers = defaultdict(lambda: {"names": set(), "cohort": set(), "count": 0})
        seen = set()
        total = 0
        for row in rows:
            # Routing IDs are used only inside this private aggregator; no ID or route is released.
            route = s.unseal(row["route_cipher"], row["id"], "route")
            tag = s.cipher.tag("statistics-cohort", str(start), route["sender"])
            subject = s.unseal(row["subject_cipher"], row["id"], "subject", Purpose.CONTENT)
            seen.add(tag)
            kinds[row["kind"]].add(tag)
            kind_counts[row["kind"]] += 1
            receiver = receivers[subject["id"]]
            receiver["names"].add(subject["name"])
            receiver["cohort"].add(tag)
            receiver["count"] += 1
            total += 1
        # Fail closed on retention/import inconsistencies; do not trust only the index.
        if len(seen) < MIN_COHORT:
            return payload
        counts = {
            "initial": bucket(total),
            "contributors": bucket(len(seen)),
            "criticism": None,
            "suggestion": None,
        }
        if all(len(group) >= MIN_COHORT for group in kinds.values()):
            counts.update({key: bucket(value) for key, value in kind_counts.items()})
        replies = await conn.scalar(
            select(func.count())
            .select_from(m)
            .where(m.c.message_type == "reply", m.c.accepted_day >= first, m.c.accepted_day < last)
        )
        counts.update(replies=bucket(replies))
        if s.scope == Scope.INTERNAL:
            users = s.identity.tables["users"]
            counts["active_members"] = bucket(
                await ic.scalar(
                    select(func.count()).select_from(users).where(users.c.is_active == 1)
                )
            )
        payload.update(hidden=False, counts=counts)
        # Do not expose the remaining small group by subtracting displayed recipients from a total.
        if receivers and all(len(item["cohort"]) >= MIN_COHORT for item in receivers.values()):
            payload["recipients"] = sorted(
                [
                    {"name": sorted(item["names"])[0], "count": bucket(item["count"])}
                    for item in receivers.values()
                ],
                key=lambda item: item["name"],
            )
        return payload

    def dedupe(self, report_id, user_id):
        return self.s.cipher.tag("weekly-delivery", report_id, user_id).hex()

    async def _queue(self, conn, ic, report_id):
        s = self.s
        u, a, g = (s.identity.tables[name] for name in ("users", "admins", "admin_grants"))
        users = (
            await ic.execute(
                select(u.c.id)
                .join(a, a.c.user_id == u.c.id)
                .join(g, g.c.admin_id == a.c.id)
                .where(
                    u.c.is_active == 1,
                    a.c.is_active == 1,
                    u.c.started_at.is_not(None),
                    u.c.delivery_state != "blocked",
                    g.c.scope == s.scope.value,
                    g.c.permission == "view_statistics",
                )
                .distinct()
            )
        ).scalars()
        inserted = 0
        for user_id in users:
            job = uuid4().hex
            result = await conn.execute(
                insert(self.t["outbox"])
                .values(
                    id=job,
                    weekly_report_id=report_id,
                    destination_cipher=s.seal(user_id, job, "destination"),
                    dedupe_key=self.dedupe(report_id, user_id),
                    next_attempt_at=int(s.clock()),
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
            )
            inserted += result.rowcount
        if inserted:
            w = self.t["weekly_reports"]
            await conn.execute(update(w).where(w.c.id == report_id).values(status="queued"))

    async def tick(self):
        """Trusted scheduler; no arbitrary-period or rebuild endpoint is exposed."""
        s = self.s
        from app.services.backup_service import recovery_floor

        floor = recovery_floor(s.database.path.parent)
        latest_start, latest_end = latest_period(s.clock(), s.zone)
        if latest_end <= floor:
            return
        async with s.identity.database.transaction(write=True) as ic:
            async with s.database.transaction(write=True) as conn:
                w = self.t["weekly_reports"]
                last_end = await conn.scalar(select(func.max(w.c.period_end)))
                if floor:
                    last_end = max(last_end or 0, floor)
                periods = []
                if last_end is None:
                    periods.append((latest_start, latest_end))
                elif last_end < latest_end:
                    boundary = datetime.fromtimestamp(last_end, s.zone)
                    for _ in range(4):
                        end = boundary + timedelta(days=7)
                        if int(end.timestamp()) > latest_end:
                            break
                        periods.append((int(boundary.timestamp()), int(end.timestamp())))
                        boundary = end
                for start, end in periods:
                    existing = await conn.scalar(
                        select(w.c.id).where(w.c.period_start == start, w.c.period_end == end)
                    )
                    if existing is not None:
                        continue
                    report_id = uuid4().hex
                    payload = await self._snapshot(conn, ic, start, end)
                    await conn.execute(
                        w.insert().values(
                            id=report_id,
                            period_start=start,
                            period_end=end,
                            policy_version=POLICY_VERSION,
                            payload_cipher=s.seal(
                                payload, report_id, "weekly_report", Purpose.CONTENT
                            ),
                        )
                    )
                    await self._queue(conn, ic, report_id)
                # Retry eligibility only for the latest release, avoiding floods of old reports.
                current = await conn.scalar(select(w.c.id).where(w.c.period_end == latest_end))
                if current:
                    await self._queue(conn, ic, current)
                await self.reconcile(conn)

    async def reconcile(self, conn):
        w, o = self.t["weekly_reports"], self.t["outbox"]
        unfinished = (
            select(o.c.id)
            .where(o.c.weekly_report_id == w.c.id, o.c.status.in_(["pending", "sending"]))
            .exists()
        )
        await conn.execute(
            update(w).where(w.c.status == "queued", ~unfinished).values(status="complete")
        )

    async def archive(self, actor_id, page=0):
        await self.s.identity.require(actor_id, self.s.scope.value, "view_statistics")
        if type(page) is not int or not 0 <= page <= 10000:
            raise InputError("صفحه نامعتبر است.")
        w = self.t["weekly_reports"]
        async with self.s.database.transaction() as conn:
            rows = (
                await conn.execute(
                    select(w.c.id, w.c.period_start, w.c.period_end)
                    .order_by(w.c.period_end.desc())
                    .offset(page * 10)
                    .limit(11)
                )
            ).mappings()
            return [
                {
                    "id": r["id"],
                    "start": datetime.fromtimestamp(r["period_start"], self.s.zone)
                    .date()
                    .isoformat(),
                }
                for r in rows
            ]

    async def view(self, actor_id, report_id, page=0):
        await self.s.identity.require(actor_id, self.s.scope.value, "view_statistics")
        if type(page) is not int or not 0 <= page <= 10000:
            raise InputError("صفحه نامعتبر است.")
        w = self.t["weekly_reports"]
        async with self.s.database.transaction() as conn:
            row = (
                (await conn.execute(select(w.c.id, w.c.payload_cipher).where(w.c.id == report_id)))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise InputError("گزارش یافت نشد.")
            payload = self.s.unseal(
                row["payload_cipher"], row["id"], "weekly_report", Purpose.CONTENT
            )
            return {
                "text": render_report(payload, page),
                "more": len(payload["recipients"] or []) > page * 10 + 10,
            }

    async def prepare_delivery(self, job):
        s = self.s
        user_id = s.unseal(job["destination_cipher"], job["id"], "destination")
        if job["dedupe_key"] != self.dedupe(job["weekly_report_id"], user_id):
            raise ValueError("Invalid weekly binding")
        async with s.identity.database.transaction() as ic:
            u = s.identity.tables["users"]
            row = (
                await ic.execute(
                    select(u.c.telegram_id).where(
                        u.c.id == user_id,
                        u.c.is_active == 1,
                        u.c.started_at.is_not(None),
                        u.c.delivery_state != "blocked",
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            try:
                actor = await IdentityRepository(ic).authenticate(row.telegram_id)
                actor.require(s.scope.value, "view_statistics")
            except AccessDenied:
                return None
            async with s.database.transaction() as conn:
                w = self.t["weekly_reports"]
                report = (
                    (
                        await conn.execute(
                            select(w.c.id, w.c.payload_cipher).where(
                                w.c.id == job["weekly_report_id"]
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                payload = s.unseal(
                    report["payload_cipher"], report["id"], "weekly_report", Purpose.CONTENT
                )
        return row.telegram_id, render_report(payload), user_id
