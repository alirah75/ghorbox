"""Internal feedback UX backed by encrypted, persistent callback state."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.logging import Event, log_event
from app.core.permissions import AccessDenied, InputError


async def feedback_home_button(service, actor_id):
    return InlineKeyboardButton(
        text="🏢 همکاران داخل گروه", callback_data=await service.issue(actor_id, "list", [0, ""])
    )


async def buttons(service, actor_id, rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label, callback_data=await service.issue(actor_id, action, payload)
                )
                for label, action, payload in row
            ]
            for row in rows
        ]
    )


async def colleagues_page(message, service, actor_id, page=0, search=""):
    colleagues = await service.colleagues(actor_id, page, search)
    rows = [
        [(item["display_name"], "target", [item["id"], item["display_name"]])]
        for item in colleagues[:10]
    ]
    pages = []
    if page:
        pages.append(("قبلی", "list", [page - 1, search]))
    if len(colleagues) > 10:
        pages.append(("بعدی", "list", [page + 1, search]))
    if pages:
        rows.append(pages)
    rows += [
        [("🔎 جستجوی همکار", "search", None)],
        [("🏠 منوی اصلی", "home", None)],
    ]
    await message.answer(
        "همکار موردنظر را انتخاب کنید:" if colleagues else "همکاری یافت نشد.",
        reply_markup=await buttons(service, actor_id, rows),
    )


async def handle_feedback_text(message, service):
    from app.bot.conversations import conversation_text

    if await conversation_text(message, service):
        return True
    actor_id = message.from_user.id
    draft = await service.draft(actor_id)
    if draft is None:
        return False
    if message.text is None:
        raise InputError("فقط متن پذیرفته می‌شود؛ عکس، فایل و صدا مجاز نیست.")
    if draft["action"] == "search":
        search = message.text.strip()
        if len(search) > 80:
            raise InputError("عبارت جستجو بیش از حد طولانی است.")
        await service.cancel(actor_id)
        await colleagues_page(message, service, actor_id, search=search)
        return True
    receipt = await service.submit_body(actor_id, message.text)
    await message.answer(
        "✅ بازخورد ثبت شد و در صف تحویل قرار گرفت.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📬 وضعیت تحویل", callback_data=receipt)]]
        ),
    )
    return True


def create_feedback_router(service, cancel_admin, external=None, show_home=None):
    router = Router(name="internal_feedback")
    router.callback_query.filter(F.message.chat.type == "private")

    @router.callback_query(F.data.startswith("f:"))
    async def callback(query: CallbackQuery):
        actor_id = query.from_user.id
        try:
            action, payload = await service.peek(actor_id, query.data)
        except (InputError, AccessDenied) as error:
            await query.answer(
                str(error) if isinstance(error, InputError) else "دسترسی فعال نیست.",
                show_alert=True,
            )
            return
        await query.answer()
        if not isinstance(query.message, Message):
            return
        message = query.message
        try:
            cancel_admin(actor_id)
            if external is not None:
                await external.cancel(actor_id)
            from app.bot.conversations import conversation_callback

            if await conversation_callback(message, service, actor_id, query.data, action, payload):
                return
            if action == "confirm":
                receipt = await service.submit(actor_id, query.data)
                await message.answer(
                    "✅ بازخورد ثبت شد و در صف تحویل قرار گرفت.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="📬 وضعیت تحویل", callback_data=receipt)]
                        ]
                    ),
                )
                return
            if action == "receipt":
                status, token = await service.delivery_status(actor_id, query.data)
                labels = {
                    "pending": "در صف تحویل",
                    "sending": "در حال ارسال",
                    "sent": "به تلگرام گیرنده تحویل شد (به معنی خوانده‌شدن نیست).",
                    "failed": "تحویل ناموفق بود.",
                    "suspended": "تحویل فعلاً متوقف است.",
                    "uncertain": (
                        "نتیجه تحویل به علت اختلال ارتباط مشخص نیست؛ "
                        "برای جلوگیری از تکرار، ارسال خودکار تکرار نشد."
                    ),
                }
                await message.answer(
                    labels[status],
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="بررسی دوباره", callback_data=token)]
                        ]
                    ),
                )
                return
            action, payload = await service.take(actor_id, query.data)
            await service.cancel(actor_id)
            if action == "list":
                await colleagues_page(message, service, actor_id, *payload)
            elif action == "target":
                await message.answer(
                    f"برای {payload[1]} چه بازخوردی دارید؟",
                    reply_markup=await buttons(
                        service,
                        actor_id,
                        [
                            [
                                ("🔴 انتقاد", "kind", [payload[0], "criticism"]),
                                ("💡 پیشنهاد", "kind", [payload[0], "suggestion"]),
                            ],
                            [("🏠 منوی اصلی", "home", None)],
                        ],
                    ),
                )
            elif action == "kind":
                await service.begin(actor_id, *payload)
                await message.answer(
                    "متن بازخورد را بنویسید (۵ تا ۳۰۰۰ واحد متنی).\nبرای لغو /cancel",
                    reply_markup=await buttons(
                        service, actor_id, [[("🏠 منوی اصلی", "home", None)]]
                    ),
                )
            elif action == "search":
                await service.begin_search(actor_id)
                await message.answer("بخشی از نام همکار را بنویسید. برای لغو /cancel")
            elif action == "cancel":
                await message.answer("عملیات لغو شد. برای منو /start را بفرستید.")
            elif action == "home" and show_home is not None:
                await show_home(message, actor_id)
            else:
                raise InputError("دکمه نامعتبر است.")
        except (InputError, AccessDenied) as error:
            await message.answer(
                str(error) if isinstance(error, InputError) else "دسترسی فعال نیست."
            )
        except Exception:
            log_event(Event.UPDATE_FAILED)
            await message.answer("خطایی رخ داد؛ وضعیت را از تاریخچه یا /start بررسی کنید.")

    return router
