"""Shared private history/reply screens for both isolated scopes."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.feedback import buttons
from app.core.permissions import InputError
from app.core.security import Scope
from app.services.conversation_service import ConversationService
from app.services.feedback_service import KINDS

STATUSES = {
    "pending": "در صف",
    "sending": "در حال ارسال",
    "sent": "تحویل تلگرام؛ نه تأیید خواندن",
    "failed": "تحویل ناموفق",
    "suspended": "تحویل متوقف",
    "uncertain": "نتیجه تحویل نامشخص",
}


async def show_thread(message, service, actor, payload):
    view = await ConversationService(service).thread(actor, **payload)
    rows = []
    if view["page"]:
        rows.append([("پیام قبلی", "thread", {**payload, "page": view["page"] - 1})])
    if view["page"] + 1 < view["total"]:
        rows.append([("پیام بعدی", "thread", {**payload, "page": view["page"] + 1})])
    if view["can_reply"]:
        if service.scope == Scope.INTERNAL:
            rows.append([("💬 ورود به گفتگوی ناشناس", "chat_open", {"cid": payload["cid"]})])
        else:
            rows.append(
                [
                    (
                        "↩️ پاسخ به گفتگو",
                        "reply",
                        {"cid": payload["cid"], "manager": payload.get("manager", False)},
                    )
                ]
            )
    rows.append([("🔄 تازه‌سازی", "thread", payload)])
    if payload.get("manager"):
        rows.append([("بازگشت به صندوق", "inbox", 0)])
    else:
        rows.append([("تاریخچه من", "history_menu", None)])
    status = f"\nوضعیت ارسال: {STATUSES[view['delivery']]}" if view["delivery"] else ""
    department = f"بخش: {view['department']}\n" if view["department"] else ""
    await message.answer(
        f"گفتگو درباره: {view['subject']}\n{department}نوع: {KINDS[view['kind']]}\n"
        f"پیام {view['page'] + 1} از {view['total']} — {view['speaker']}\n\n{view['body']}"
        + status
        + ("" if view["can_reply"] else "\nاین گفتگو قابل پاسخ نیست."),
        parse_mode=None,
        protect_content=True,
        reply_markup=await buttons(service, actor, rows),
    )


async def conversation_callback(message, service, actor, token, action, payload):
    from app.bot.moderation import moderation_callback
    from app.bot.statistics import statistics_callback

    if await statistics_callback(message, service, actor, token, action, payload):
        return True

    if await moderation_callback(message, service, actor, token, action, payload):
        return True
    if action not in {
        "history_menu",
        "history",
        "thread",
        "reply",
        "reply_confirm",
        "chat_open",
        "chat_list",
    }:
        return False
    conversations = ConversationService(service)
    if action == "chat_open":
        await service.take(actor, token)
        await service.cancel(actor)
        await conversations.activate_chat(actor, payload["cid"])
        await message.answer(
            "💬 گفتگوی ناشناس فعال شد. پیام‌ها را عادی بفرستید؛ نیازی به Reply نیست.\n"
            "برای خروج /cancel یا /start را بفرستید."
        )
        return True
    if action == "reply_confirm":
        result = await conversations.submit_reply(actor, token)
        await message.answer(
            "✅ پاسخ ثبت شد و در صف تحویل قرار گرفت."
            if result.pop("queued")
            else "✅ پاسخ در صندوق مدیران بخش خارجی ثبت شد."
        )
        await show_thread(message, service, actor, result)
        return True
    await service.take(actor, token)
    await service.cancel(actor)
    if action == "chat_list":
        page = payload
        items = await conversations.active_chats(actor, page)
        rows = [
            [(f"{KINDS[item['kind']]} — {item['label']}", "chat_open", {"cid": item["id"]})]
            for item in items[:10]
        ]
        pages = []
        if page:
            pages.append(("قبلی", "chat_list", page - 1))
        if len(items) > 10:
            pages.append(("بعدی", "chat_list", page + 1))
        if pages:
            rows.append(pages)
        rows.append([("📚 تاریخچه گفتگوها", "history_menu", None)])
        await message.answer(
            "گفتگوی فعال را انتخاب کنید؛ گفتگوهای دیگر باز می‌مانند."
            if items
            else "گفتگوی ناشناس بازی ندارید.",
            reply_markup=await buttons(service, actor, rows),
        )
    elif action == "history_menu":
        await message.answer(
            "تاریخچه گفتگوهای من",
            reply_markup=await buttons(
                service,
                actor,
                [
                    [("💬 گفتگوهای فعال", "chat_list", 0)],
                    [("📤 بازخوردهای ارسالی من", "history", ["sent", 0])],
                    [("📩 گفتگوهای آغازشده توسط دیگران", "history", ["received", 0])],
                ],
            ),
        )
    elif action == "history":
        folder, page = payload
        items = await conversations.history(actor, folder, page)
        rows = [
            [(f"{KINDS[r['kind']]} — {r['subject']}", "thread", {"cid": r["id"], "page": 0})]
            for r in items[:10]
        ]
        if page:
            rows.append([("قبلی", "history", [folder, page - 1])])
        if len(items) > 10:
            rows.append([("بعدی", "history", [folder, page + 1])])
        rows.append([("بازگشت", "history_menu", None)])
        await message.answer(
            "گفتگوها:" if items else "گفتگویی در این بخش ندارید.",
            reply_markup=await buttons(service, actor, rows),
        )
    elif action == "thread":
        await show_thread(message, service, actor, payload)
    elif action == "reply":
        await conversations.begin_reply(actor, **payload)
        await message.answer("متن پاسخ را بنویسید؛ ۵ تا ۳۰۰۰ واحد متنی. برای لغو /cancel")
    return True


async def conversation_text(message, service):
    draft = await service.draft(message.from_user.id)
    if draft is None:
        return False
    if draft["action"] == "chat_active":
        if message.text is None:
            raise InputError("فقط متن پذیرفته می‌شود؛ برای خروج /cancel")
        await ConversationService(service).send_active(message.from_user.id, message.text)
        await message.answer("✅ پیام ناشناس ارسال شد. گفتگو فعال است؛ برای خروج /cancel")
        return True
    if draft["action"] != "reply":
        return False
    if message.text is None:
        raise InputError("فقط متن برای پاسخ پذیرفته می‌شود؛ برای لغو /cancel")
    actor = message.from_user.id
    data, token = await ConversationService(service).preview_reply(actor, message.text)
    await message.answer(
        ("پاسخ از طرف مدیریت بخش خارجی (واسطه)" if data["manager"] else "پیش‌نمایش پاسخ")
        + f"\n\n{data['body']}\n\nنشانه‌های شناسایی خود را بررسی کنید. ارسال شود؟",
        parse_mode=None,
        protect_content=True,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ ارسال پاسخ", callback_data=token)],
                [
                    InlineKeyboardButton(
                        text="❌ لغو", callback_data=await service.issue(actor, "history_menu")
                    )
                ],
            ]
        ),
    )
    return True
