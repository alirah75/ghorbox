"""Read-only access to the same frozen release used for automated weekly delivery."""

from app.bot.feedback import buttons
from app.services.statistics_service import StatisticsService


async def statistics_callback(message, service, actor, token, action, payload):
    if action not in {"stats_archive", "stats_view"}:
        return False
    await service.identity.require(actor, service.scope.value, "view_statistics")
    await service.take(actor, token)
    await service.cancel(actor)
    statistics = StatisticsService(service)
    if action == "stats_archive":
        page = payload
        reports = await statistics.archive(actor, page)
        rows = [
            [("هفته شروع‌شده در " + item["start"], "stats_view", {"id": item["id"], "page": 0})]
            for item in reports[:10]
        ]
        if page:
            rows.append([("قبلی", "stats_archive", page - 1)])
        if len(reports) > 10:
            rows.append([("بعدی", "stats_archive", page + 1)])
        await message.answer(
            "گزارش‌های هفتگی منتشرشده:"
            if reports
            else "هنوز گزارشی آماده نشده است. زمان‌بندی هر دقیقه بررسی می‌شود؛ برای منو /start",
            reply_markup=await buttons(service, actor, rows) if rows else None,
        )
    else:
        result = await statistics.view(actor, payload["id"], payload["page"])
        rows = []
        if payload["page"]:
            rows.append([("گیرندگان قبلی", "stats_view", {**payload, "page": payload["page"] - 1})])
        if result["more"]:
            rows.append([("گیرندگان بعدی", "stats_view", {**payload, "page": payload["page"] + 1})])
        rows.append([("آرشیو گزارش‌ها", "stats_archive", 0)])
        await message.answer(
            result["text"],
            parse_mode=None,
            protect_content=True,
            reply_markup=await buttons(service, actor, rows),
        )
    return True
