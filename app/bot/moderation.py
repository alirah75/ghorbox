"""Report, closure and scope-bound administrative review screens."""

from app.bot.feedback import buttons
from app.core.permissions import AccessDenied
from app.services.conversation_service import ConversationService
from app.services.feedback_service import KINDS
from app.services.moderation_service import REASONS, ModerationService

LABELS = {"open": "در انتظار بررسی", "accepted": "پذیرفته‌شده", "dismissed": "ردشده"}


async def moderation_callback(message, service, actor_id, token, action, payload):
    if not action.startswith("mod_"):
        return False
    moderation = ModerationService(service)
    actor = await service.identity.authenticate(actor_id)
    if action.endswith("_save"):
        result = await moderation.execute(actor_id, token)
        text = "✅ گزارش ثبت شد؛ ثبت گزارش به‌تنهایی گفتگو را نمی‌بندد."
        if result in {"mod_close_save", "mod_admin_close_save"}:
            text = "✅ گفتگو بسته شد. تاریخچه باقی می‌ماند و پاسخ جدید پذیرفته نمی‌شود."
        elif result == "mod_resolve_save":
            text = "✅ نتیجه بررسی ثبت شد."
        rows = (
            [[("بازگشت به بررسی گزارش‌ها", "mod_reports", ["open", 0])]]
            if result == "mod_resolve_save"
            else []
        )
        await message.answer(
            text + "\nبرای منو /start",
            reply_markup=await buttons(service, actor_id, rows) if rows else None,
        )
        return True
    await service.take(actor_id, token)
    await service.cancel(actor_id)
    if action == "mod_menu":
        rows = []
        if actor.allows(service.scope.value, "read_feedback"):
            rows.append([("📥 بازخوردهای اولیه", "mod_feedback", 0)])
        if actor.allows(service.scope.value, "manage_reports"):
            rows.append([("🚩 بررسی گزارش‌ها", "mod_reports", ["open", 0])])
        if not rows:
            raise AccessDenied()
        await message.answer(
            "مدیریت حوزه " + ("داخلی" if service.scope.value == "internal" else "خارجی"),
            reply_markup=await buttons(service, actor_id, rows),
        )
    elif action == "mod_reports":
        status, page = payload
        items = await moderation.reports(actor_id, status, page)
        rows = [
            [(f"گزارش {page * 10 + i + 1} — {REASONS[r['reason']]}", "mod_report", r["id"])]
            for i, r in enumerate(items[:10])
        ]
        if page:
            rows.append([("قبلی", "mod_reports", [status, page - 1])])
        if len(items) > 10:
            rows.append([("بعدی", "mod_reports", [status, page + 1])])
        rows += [
            [(label, "mod_reports", [key, 0])] for key, label in LABELS.items() if key != status
        ]
        rows.append([("بازگشت", "mod_menu", None)])
        await message.answer(
            "گزارش‌ها — " + LABELS[status] + ("" if items else "\nگزارشی وجود ندارد."),
            reply_markup=await buttons(service, actor_id, rows),
        )
    elif action == "mod_report":
        item = await moderation.report_detail(actor_id, payload)
        rows = []
        if item["status"] == "open":
            rows.append(
                [("پذیرش و بستن گفتگو", "mod_resolve_confirm", {"id": payload, "accept": True})]
            )
            rows.append([("رد گزارش", "mod_resolve_confirm", {"id": payload, "accept": False})])
        rows.append([("بازگشت", "mod_reports", [item["status"], 0])])
        await message.answer(
            f"🚩 دلیل: {REASONS[item['reason']]}\n"
            f"وضعیت: {LABELS[item['status']]}\n\n{item['body']}",
            parse_mode=None,
            protect_content=True,
            reply_markup=await buttons(service, actor_id, rows),
        )
    elif action == "mod_resolve_confirm":
        await moderation.report_detail(actor_id, payload["id"])
        text = (
            "گزارش پذیرفته و گفتگو بسته شود؟ ارسال‌های در انتظار متوقف می‌شوند."
            if payload["accept"]
            else "گزارش رد شود؟ وضعیت گفتگو تغییر نمی‌کند."
        )
        await message.answer(
            text,
            reply_markup=await buttons(
                service,
                actor_id,
                [
                    [("✅ تأیید تصمیم", "mod_resolve_save", payload)],
                    [("لغو", "mod_report", payload["id"])],
                ],
            ),
        )
    elif action == "mod_report_choose":
        view = await ConversationService(service).thread(actor_id, **payload)
        data = {
            "cid": payload["cid"],
            "mid": view["message_id"],
            "manager": payload.get("manager", False),
        }
        await message.answer(
            "چرا این پیام را گزارش می‌کنید؟",
            reply_markup=await buttons(
                service,
                actor_id,
                [
                    [(label, "mod_report_confirm", {**data, "reason": key})]
                    for key, label in REASONS.items()
                ],
            ),
        )
    elif action == "mod_report_confirm":
        await message.answer(
            f"ثبت گزارش با دلیل «{REASONS[payload['reason']]}» را تأیید می‌کنید؟",
            reply_markup=await buttons(
                service,
                actor_id,
                [[("✅ ثبت گزارش", "mod_report_save", payload)], [("لغو", "history_menu", None)]],
            ),
        )
    elif action == "mod_close_confirm":
        await ConversationService(service).thread(actor_id, **payload)
        if payload.get("manager"):
            actor.require(service.scope.value, "manage_reports")
        await message.answer(
            "گفتگو برای هر دو طرف بسته شود؟ تاریخچه باقی می‌ماند؛ "
            "پاسخ و بازگشایی در این نسخه ممکن نیست.",
            reply_markup=await buttons(
                service,
                actor_id,
                [[("✅ بستن گفتگو", "mod_close_save", payload)], [("لغو", "thread", payload)]],
            ),
        )
    elif action == "mod_feedback":
        items = await moderation.feedback_list(actor_id, payload)
        rows = [
            [(f"{KINDS[r['kind']]} — {r['subject']}", "mod_initial", r["id"])] for r in items[:10]
        ]
        if payload:
            rows.append([("قبلی", "mod_feedback", payload - 1)])
        if len(items) > 10:
            rows.append([("بعدی", "mod_feedback", payload + 1)])
        rows.append([("بازگشت", "mod_menu", None)])
        await message.answer(
            "بازخوردهای اولیه:" if items else "بازخوردی ثبت نشده است.",
            reply_markup=await buttons(service, actor_id, rows),
        )
    elif action in {"mod_initial", "mod_admin_close_confirm"}:
        item = await moderation.initial_feedback(actor_id, payload)
        if action == "mod_admin_close_confirm":
            actor.require(service.scope.value, "manage_reports")
            await message.answer(
                "بستن این گفتگو و توقف ارسال‌های در انتظار را تأیید می‌کنید؟",
                reply_markup=await buttons(
                    service,
                    actor_id,
                    [
                        [("✅ بستن گفتگو", "mod_admin_close_save", {"cid": payload})],
                        [("لغو", "mod_initial", payload)],
                    ],
                ),
            )
        else:
            rows = [[("بازگشت", "mod_feedback", 0)]]
            if item["status"] != "closed" and actor.allows(service.scope.value, "manage_reports"):
                rows.insert(0, [("🔒 بستن گفتگو", "mod_admin_close_confirm", payload)])
            await message.answer(
                f"درباره: {item['subject']}\nنوع: {KINDS[item['kind']]}\n\n{item['body']}"
                + ("\nگفتگو بسته است." if item["status"] == "closed" else ""),
                parse_mode=None,
                protect_content=True,
                reply_markup=await buttons(service, actor_id, rows),
            )
    else:
        raise AccessDenied()
    return True
