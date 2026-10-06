"""External feedback and scoped directory administration."""

from aiogram import F, Router
from aiogram.types import Message

from app.bot.feedback import buttons
from app.core.logging import Event, log_event
from app.core.permissions import AccessDenied, InputError
from app.services.feedback_service import KINDS
from app.services.identity_service import valid_name


async def people_page(message, service, actor, page=0, search="", management=False):
    people = await service.people(actor, page, search, management=management)
    action = "manage" if management else "list"
    rows = [
        [
            (
                ("🟢 " if p["is_active"] else "⏸ ")
                + p["display_name"]
                + (f" — {p['department']}" if p["department"] else ""),
                "person" if management else "target",
                [p["id"], p["display_name"], bool(p["is_active"])],
            )
        ]
        for p in people[:10]
    ]
    if page:
        rows.append([("قبلی", action, [page - 1, search])])
    if len(people) > 10:
        rows.append([("بعدی", action, [page + 1, search])])
    rows.append(
        [
            (
                "➕ افزودن فرد" if management else "➕ پیشنهاد فرد جدید",
                "add_person" if management else "propose",
                None,
            )
        ]
    )
    if not management:
        rows.append([("🔎 جستجوی فرد", "search", None)])
    rows.append([("بازگشت", "menu", None)])
    await message.answer(
        "افراد خارج از گروه:" if people else "فردی یافت نشد.",
        reply_markup=await buttons(service, actor, rows),
    )


async def menu(message, service, actor_id):
    actor = await service.identity.authenticate(actor_id)
    rows = [[("👥 فهرست افراد خارجی", "list", [0, ""])]]
    if actor.allows("external", "read_feedback"):
        rows.append([("📥 صندوق بازخورد خارجی", "inbox", 0)])
    if actor.allows("external", "manage_external_people"):
        rows += [
            [("⚙️ مدیریت افراد خارجی", "manage", [0, ""])],
            [("📋 پیشنهادهای افراد جدید", "proposals", 0)],
        ]
    await message.answer(
        "بخش خارج از گروه\nبازخورد در صندوق مدیران این بخش ثبت می‌شود؛ "
        "مستقیماً به فرد خارجی پیام نمی‌فرستیم.",
        reply_markup=await buttons(service, actor_id, rows),
    )


async def handle_external_text(message, service):
    from app.bot.conversations import conversation_text

    if await conversation_text(message, service):
        return True
    actor = message.from_user.id
    draft = await service.draft(actor)
    if draft is None:
        return False
    if message.text is None:
        raise InputError("فقط متن بفرستید؛ برای لغو /cancel")
    action = draft["action"]
    if action == "search":
        search = message.text.strip()
        if len(search) > 80:
            raise InputError("جستجو حداکثر ۸۰ نویسه است.")
        await service.cancel(actor)
        await people_page(message, service, actor, search=search)
        return True
    if action in {"add_person", "edit_person", "propose"}:
        if action in {"add_person", "edit_person"}:
            await service.identity.require(actor, "external", "manage_external_people")
        parts = message.text.split("|", 1)
        name = valid_name(parts[0])
        department = valid_name(parts[1]) if len(parts) == 2 else None
        await service.cancel(actor)
        await message.answer(
            f"نام: {name}\nبخش: {department or 'ثبت نشده'}\nآیا تأیید می‌کنید؟",
            reply_markup=await buttons(
                service,
                actor,
                [
                    [
                        (
                            "✅ تأیید",
                            {
                                "add_person": "save_person",
                                "edit_person": "save_edit",
                                "propose": "save_proposal",
                            }[action],
                            [draft["payload"], name, department]
                            if action == "edit_person"
                            else [name, department],
                        )
                    ],
                    [("❌ لغو", "menu", None)],
                ],
            ),
        )
        return True
    data, token = await service.preview(actor, message.text)
    from aiogram.types import InlineKeyboardButton

    markup = await buttons(service, actor, [[("❌ لغو", "menu", None)]])
    markup.inline_keyboard.insert(0, [InlineKeyboardButton(text="✅ ارسال", callback_data=token)])
    await message.answer(
        f"درباره: {data['recipient_name']}\nنوع: {KINDS[data['kind']]}\n\n{data['body']}\n\n"
        "این متن را مدیران بخش خارجی می‌بینند. نام و نشانه‌های شناسایی خود را بررسی کنید.",
        parse_mode=None,
        reply_markup=markup,
    )
    return True


def create_external_router(service, cancel_admin, internal=None):
    router = Router(name="external")
    router.callback_query.filter(F.message.chat.type == "private")

    @router.callback_query(F.data.startswith("e:"))
    async def callback(query):
        actor = query.from_user.id
        try:
            action, payload = await service.peek(actor, query.data)
            await query.answer()
            if not isinstance(query.message, Message):
                return
            message = query.message
            cancel_admin(actor)
            if internal is not None:
                await internal.cancel(actor)
            from app.bot.conversations import conversation_callback

            if await conversation_callback(message, service, actor, query.data, action, payload):
                return
            if action == "confirm":
                await service.submit(actor, query.data)
                await message.answer(
                    "✅ بازخورد در صندوق مدیران بخش خارجی ثبت شد. این به معنی خوانده‌شدن نیست."
                )
                return
            # Keep the token until a mutation succeeds, so a transient failure can be retried.
            if action == "save_person":
                await service.save_person(actor, *payload)
            elif action == "save_edit":
                await service.edit_person(actor, *payload)
            elif action == "save_proposal":
                await service.propose(actor, *payload)
            elif action == "active":
                await service.set_active(actor, *payload)
            elif action == "review":
                await service.review(actor, *payload)
            await service.take(actor, query.data)
            await service.cancel(actor)
            if action in {"save_person", "save_edit", "save_proposal", "active", "review"}:
                await message.answer(
                    "✅ پیشنهاد برای بررسی ثبت شد."
                    if action == "save_proposal"
                    else "✅ تغییرات ذخیره شد."
                )
                await menu(message, service, actor)
            elif action == "menu":
                await menu(message, service, actor)
            elif action in {"list", "manage"}:
                await people_page(message, service, actor, *payload, management=action == "manage")
            elif action == "target":
                await message.answer(
                    f"نوع بازخورد درباره {payload[1]}:",
                    reply_markup=await buttons(
                        service,
                        actor,
                        [
                            [
                                ("🔴 انتقاد", "kind", [payload[0], "criticism"]),
                                ("💡 پیشنهاد", "kind", [payload[0], "suggestion"]),
                            ]
                        ],
                    ),
                )
            elif action == "kind":
                await service.begin(actor, *payload)
                await message.answer(
                    "متن بازخورد را بنویسید (۵ تا ۳۰۰۰ واحد متنی). برای لغو /cancel"
                )
            elif action in {"search", "propose", "add_person", "edit_person"}:
                await service.form(actor, action, payload)
                await message.answer(
                    "بخشی از نام را بنویسید. برای لغو /cancel"
                    if action == "search"
                    else (
                        "نام فرد را بنویسید. در صورت تمایل بخش را پس از | اضافه کنید؛ "
                        "مثال: علی احمدی | تأمین‌کننده\nبرای لغو /cancel"
                    )
                )
            elif action == "person":
                await service.identity.require(actor, "external", "manage_external_people")
                await message.answer(
                    f"مدیریت {payload[1]}: با انتخاب دکمه وضعیت تغییر می‌کند.",
                    reply_markup=await buttons(
                        service,
                        actor,
                        [
                            [
                                (
                                    "غیرفعال کردن" if payload[2] else "فعال کردن",
                                    "active",
                                    [payload[0], not payload[2]],
                                )
                            ],
                            [("✏️ اصلاح نام و بخش", "edit_person", payload[0])],
                            [("لغو", "menu", None)],
                        ],
                    ),
                )
            elif action == "proposals":
                items = await service.proposals(actor, payload)
                rows = [
                    [
                        (
                            p["name"] + (f" — {p['department']}" if p["department"] else ""),
                            "proposal",
                            [p["id"], p["name"]],
                        )
                    ]
                    for p in items[:10]
                ]
                if payload:
                    rows.append([("قبلی", "proposals", payload - 1)])
                if len(items) > 10:
                    rows.append([("بعدی", "proposals", payload + 1)])
                rows.append([("بازگشت", "menu", None)])
                await message.answer(
                    "پیشنهادهای در انتظار بررسی:" if items else "پیشنهاد در انتظار بررسی نداریم.",
                    reply_markup=await buttons(service, actor, rows),
                )
            elif action == "proposal":
                await service.identity.require(actor, "external", "manage_external_people")
                await message.answer(
                    f"بررسی پیشنهاد: {payload[1]}",
                    reply_markup=await buttons(
                        service,
                        actor,
                        [
                            [
                                ("✅ پذیرش و افزودن", "review", [payload[0], True]),
                                ("❌ رد پیشنهاد", "review", [payload[0], False]),
                            ],
                            [("بازگشت", "proposals", 0)],
                        ],
                    ),
                )
            elif action == "inbox":
                items = await service.inbox(actor, payload)
                rows = [
                    [(f"{KINDS[p['kind']]} درباره {p['subject']}", "read", p["id"])]
                    for p in items[:10]
                ]
                if payload:
                    rows.append([("قبلی", "inbox", payload - 1)])
                if len(items) > 10:
                    rows.append([("بعدی", "inbox", payload + 1)])
                rows.append([("بازگشت", "menu", None)])
                await message.answer(
                    "صندوق بازخورد خارجی:" if items else "بازخوردی ثبت نشده است.",
                    reply_markup=await buttons(service, actor, rows),
                )
            elif action == "read":
                from app.bot.conversations import show_thread

                data = await service.read_feedback(actor, payload)
                await show_thread(
                    message,
                    service,
                    actor,
                    {"cid": data["conversation_id"], "page": 0, "manager": True},
                )
            else:
                raise InputError("دکمه نامعتبر است.")
        except (InputError, AccessDenied) as error:
            if isinstance(query.message, Message):
                await query.message.answer(
                    str(error) if isinstance(error, InputError) else "دسترسی به این بخش فعال نیست."
                )
        except Exception:
            log_event(Event.UPDATE_FAILED)
            if isinstance(query.message, Message):
                await query.message.answer("عملیات کامل نشد؛ وضعیت را از /start بررسی کنید.")

    return router
