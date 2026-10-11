"""Scope-bound initial-feedback screens."""

from app.bot.feedback import buttons
from app.core.permissions import MANAGE_ADMINS, AccessDenied
from app.core.security import Scope
from app.services.feedback_service import KINDS
from app.services.moderation_service import ModerationService


async def moderation_callback(message, service, actor_id, token, action, payload):
    if not action.startswith("mod_"):
        return False
    moderation = ModerationService(service)
    actor = await service.identity.authenticate(actor_id)
    await service.take(actor_id, token)
    await service.cancel(actor_id)
    if action == "mod_menu":
        if not actor.allows(service.scope.value, "read_feedback"):
            raise AccessDenied()
        title = "بازخوردهای اولیه حوزه خارجی"
        if service.scope == Scope.INTERNAL:
            title = (
                "همه بازخوردهای اولیه داخلی"
                if actor.allows(*MANAGE_ADMINS)
                else "بازخوردهای اولیه گروه شما"
            )
        await message.answer(
            title,
            reply_markup=await buttons(
                service,
                actor_id,
                [
                    [("📥 بازخوردهای اولیه", "mod_feedback", 0)],
                    [("🏠 منوی اصلی", "home", None)],
                ],
            ),
        )
    elif action == "mod_feedback":
        items = await moderation.feedback_list(actor_id, payload)
        rows = []
        for item in items[:10]:
            label = f"{KINDS[item['kind']]} — {item['subject']}"
            if item["group"]:
                label += f" — {item['group']}"
            rows.append([(label, "mod_initial", item["id"])])
        if payload:
            rows.append([("قبلی", "mod_feedback", payload - 1)])
        if len(items) > 10:
            rows.append([("بعدی", "mod_feedback", payload + 1)])
        rows.append([("بازگشت", "mod_menu", None)])
        await message.answer(
            "بازخوردهای اولیه:" if items else "بازخوردی ثبت نشده است.",
            reply_markup=await buttons(service, actor_id, rows),
        )
    elif action == "mod_initial":
        item = await moderation.initial_feedback(actor_id, payload)
        group = f"گروه: {item['group']}\n" if item["group"] else ""
        await message.answer(
            f"درباره: {item['subject']}\n{group}نوع: {KINDS[item['kind']]}\n\n{item['body']}"
            + ("\nاین گفت‌وگو از نسخه قبلی بسته شده است." if item["status"] == "closed" else ""),
            parse_mode=None,
            protect_content=True,
            reply_markup=await buttons(service, actor_id, [[("بازگشت", "mod_feedback", 0)]]),
        )
    else:
        raise AccessDenied()
    return True
