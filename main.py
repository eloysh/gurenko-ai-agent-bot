import os
import re
import time
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

from openai import AsyncOpenAI

from db import init_db, upsert_user, get_user, add_prompt, list_prompts, count_prompts, get_prompt, toggle_favorite, is_favorite, set_vip, add_referral, inc_referrals_count, add_credits
from prompt_parser import extract_candidates, guess_category


# -------------------- Logging --------------------
logger = logging.getLogger("gurenko-bot")
logging.basicConfig(level=logging.INFO)


# -------------------- ENV --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # @channel or -100...
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "").strip()
DISCUSSION_CHAT_ID = os.getenv("DISCUSSION_CHAT_ID", "").strip()  # optional (int as str)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini").strip()
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1").strip()

PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "").strip()  # for Stars can be empty string in some setups
VIP_STARS_PRICE = int(os.getenv("VIP_STARS_PRICE", "299").strip())
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0").strip() or 0)

INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/gurenko_kristina").strip()

TIMEZONE = os.getenv("TIMEZONE", "Asia/Vladivostok").strip()

if not TELEGRAM_BOT_TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN is empty. Bot will not work.")

# SQLite init
init_db()

# OpenAI client (optional)
openai_client: Optional[AsyncOpenAI] = None
if OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# -------------------- Helpers --------------------
def _is_discussion(chat_id: int) -> bool:
    if not DISCUSSION_CHAT_ID:
        return False
    try:
        return int(DISCUSSION_CHAT_ID) == int(chat_id)
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _vip_until_iso(days: int = 30) -> str:
    return (datetime.utcnow() + timedelta(days=days)).isoformat()


async def safe_edit(query, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, parse_mode: Optional[str] = ParseMode.HTML) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=True)
    except BadRequest as e:
        msg = str(e)
        if "Message is not modified" in msg:
            # ignore
            return
        logger.warning("safe_edit BadRequest: %s", msg)
    except TelegramError as e:
        logger.warning("safe_edit TelegramError: %s", str(e))


async def safe_send(bot, chat_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, parse_mode: Optional[str] = ParseMode.HTML) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=True)
    except TelegramError as e:
        logger.warning("safe_send TelegramError: %s", str(e))


def subscribe_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📌 Подписаться на канал", url=REQUIRED_CHANNEL_URL or f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")],
    ]
    return InlineKeyboardMarkup(buttons)


def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎬 Sora / Видео-промты", callback_data="m:sora"),
         InlineKeyboardButton("🧍 HeyGen / Оживление", callback_data="m:heygen")],
        [InlineKeyboardButton("🖼 Meta AI / Фото-стили", callback_data="m:meta"),
         InlineKeyboardButton("🪝 Reels Hooks", callback_data="m:hooks")],
        [InlineKeyboardButton("📚 База промтов", callback_data="m:prompts"),
         InlineKeyboardButton("🧠 AI помощник", callback_data="m:ai")],
        [InlineKeyboardButton("⭐ VIP (299 Stars)", callback_data="m:vip"),
         InlineKeyboardButton("🎁 Рефералка", callback_data="m:ref")],
        [InlineKeyboardButton("📸 Instagram", callback_data="m:ig"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="m:settings")],
    ]
    return InlineKeyboardMarkup(rows)


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="m:home")]])


def prompts_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎬 Sora", callback_data="p:list:sora:0"),
         InlineKeyboardButton("🧍 HeyGen", callback_data="p:list:heygen:0")],
        [InlineKeyboardButton("🖼 Meta", callback_data="p:list:meta:0"),
         InlineKeyboardButton("🪝 Hooks", callback_data="p:list:hooks:0")],
        [InlineKeyboardButton("🆕 Новые", callback_data="p:new:0"),
         InlineKeyboardButton("🔎 Поиск", callback_data="p:search")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="m:home")],
    ]
    return InlineKeyboardMarkup(rows)


def prompt_item_kb(prompt_id: int, fav: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(("⭐ В избранное" if not fav else "✅ В избранном"), callback_data=f"p:fav:{prompt_id}"),
         InlineKeyboardButton("📤 Поделиться красиво", callback_data=f"p:share:{prompt_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="m:prompts"),
         InlineKeyboardButton("🏠 Меню", callback_data="m:home")],
    ]
    return InlineKeyboardMarkup(rows)


def pagination_kb(category: str, page: int, total_pages: int, query: Optional[str] = None) -> InlineKeyboardMarkup:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️", callback_data=f"p:list:{category}:{page-1}" + (f":q:{query}" if query else "")))
    row.append(InlineKeyboardButton(f"{page+1}/{max(total_pages,1)}", callback_data="noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("➡️", callback_data=f"p:list:{category}:{page+1}" + (f":q:{query}" if query else "")))
    rows = [row, [InlineKeyboardButton("⬅️ Категории", callback_data="m:prompts"), InlineKeyboardButton("🏠 Меню", callback_data="m:home")]]
    return InlineKeyboardMarkup(rows)


def instagram_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔗 Открыть мой Instagram", url=INSTAGRAM_URL)],
        [InlineKeyboardButton("✍️ Написать пост", callback_data="ig:post"),
         InlineKeyboardButton("🏷 Хештеги", callback_data="ig:tags")],
        [InlineKeyboardButton("🎬 Сценарий Reels", callback_data="ig:reels"),
         InlineKeyboardButton("⬅️ В меню", callback_data="m:home")],
    ]
    return InlineKeyboardMarkup(rows)


def settings_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🧠 Стиль: Коротко+структурно", callback_data="set:style:short"),
         InlineKeyboardButton("🧠 Стиль: Подробно", callback_data="set:style:long")],
        [InlineKeyboardButton("🧹 Сбросить режимы", callback_data="set:reset")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="m:home")],
    ]
    return InlineKeyboardMarkup(rows)


def is_owner(user_id: int) -> bool:
    return OWNER_USER_ID and user_id == OWNER_USER_ID


# -------------------- Subscription Guard (ОБЯЗАТЕЛЬНО) --------------------
# кеш проверок чтобы не долбить getChatMember каждую секунду
SUB_CACHE: Dict[int, Tuple[bool, float]] = {}  # user_id -> (ok, ts)
SUB_TTL = 60.0  # seconds


async def is_subscribed(bot, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        # если канал не задан — считаем что доступ открыт (но ты просила обязательно: поэтому лучше всегда задавай REQUIRED_CHANNEL)
        return True

    cached = SUB_CACHE.get(user_id)
    if cached and (time.time() - cached[1] < SUB_TTL):
        return cached[0]

    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        status = member.status  # string or ChatMemberStatus
        allowed = {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,  # важно: в PTB нет CREATOR, есть OWNER
        }
        ok = status in allowed or str(status) in {"member", "administrator", "creator", "owner"}
        SUB_CACHE[user_id] = (ok, time.time())
        return ok
    except TelegramError as e:
        logger.warning("channel check failed: %s", str(e))
        SUB_CACHE[user_id] = (False, time.time())
        return False


async def guard_or_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    True если можно продолжать, False если показали экран подписки и дальше не идём.
    """
    # не мешаем сбору промтов из канала
    if update.channel_post is not None:
        return True

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False

    if is_owner(user.id):
        return True

    ok = await is_subscribed(context.bot, user.id)
    if ok:
        return True

    text = (
        "🔒 <b>Доступ открывается после подписки на канал</b>\n\n"
        "1) Нажми «📌 Подписаться»\n"
        "2) Вернись сюда и нажми «✅ Я подписался»\n\n"
        "После этого откроется меню и все функции."
    )

    # если это callback — редактируем текущий экран
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query, text=text, reply_markup=subscribe_kb())
    else:
        await safe_send(context.bot, chat_id=chat.id, text=text, reply_markup=subscribe_kb())
    return False


# -------------------- OpenAI Helpers --------------------
SYSTEM_BASE = (
    "Ты — AI-ассистент Кристины. Делаешь промты/сценарии/тексты для соцсетей и нейросетей. "
    "Пиши по-русски. Отвечай так, чтобы человек мог СРАЗУ скопировать и использовать. "
    "Структура: короткий заголовок, затем пункты, затем готовый промт/текст в блоке."
)

def user_style(context: ContextTypes.DEFAULT_TYPE) -> str:
    style = context.user_data.get("style", "short")
    if style == "long":
        return "Ответ может быть подробнее, с примерами и вариантами."
    return "Ответ короткий, структурный, без воды, максимум пользы."


async def openai_text(context: ContextTypes.DEFAULT_TYPE, user_text: str) -> Optional[str]:
    if not openai_client:
        return None
    try:
        prompt = f"{SYSTEM_BASE}\n{user_style(context)}"
        # Responses API
        resp = await openai_client.responses.create(
            model=OPENAI_TEXT_MODEL,
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
        )
        # openai responses: output_text convenience may exist
        out_text = getattr(resp, "output_text", None)
        if out_text:
            return out_text.strip()

        # fallback parse
        try:
            # resp.output is list of content blocks
            parts = []
            for item in resp.output:
                for c in item.content:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        parts.append(getattr(c, "text", ""))
            joined = "\n".join([p for p in parts if p]).strip()
            return joined or None
        except Exception:
            return None
    except Exception as e:
        msg = str(e)
        logger.warning("OpenAI text error: %s", msg)
        # 401 unauthorized etc
        return f"⚠️ AI временно недоступен (ошибка доступа к OpenAI: {msg}). Проверь OPENAI_API_KEY на Render."


async def openai_image(prompt: str) -> Tuple[Optional[str], Optional[str]]:
    """
    returns (image_url, error_message)
    """
    if not openai_client:
        return None, "⚠️ Генерация картинок выключена: не задан OPENAI_API_KEY."
    try:
        img = await openai_client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
        )
        # typical: img.data[0].url
        url = None
        if hasattr(img, "data") and img.data:
            url = getattr(img.data[0], "url", None)
        if not url:
            return None, "⚠️ Не удалось получить ссылку на изображение."
        return url, None
    except Exception as e:
        msg = str(e)
        logger.warning("OpenAI image error: %s", msg)
        return None, f"⚠️ Картинка не сгенерировалась (OpenAI ошибка: {msg}). Проверь OPENAI_API_KEY и доступ к модели."


# -------------------- UI Text --------------------
def start_text() -> str:
    return (
        "✨ <b>Gurenko AI Agent</b>\n\n"
        "Я помогу тебе делать <b>вау-контент</b>:\n"
        "• промты для Sora / HeyGen / Meta AI\n"
        "• сценарии Reels + хуки\n"
        "• готовые посты и хештеги для Instagram\n"
        "• база промтов + избранное\n"
        "• VIP за ⭐ Telegram Stars\n\n"
        "Нажми кнопку — и погнали 🚀"
    )


def vip_text(user: Optional[Dict[str, Any]]) -> str:
    is_v = bool(user and user.get("is_vip"))
    until = (user.get("vip_until") if user else None) or "—"
    return (
        "⭐ <b>VIP доступ</b>\n\n"
        "VIP даёт:\n"
        "✅ закрытые промты и подборки\n"
        "✅ больше генераций/шаблонов\n"
        "✅ новые промты быстрее всех\n\n"
        f"Твой статус: <b>{'VIP ✅' if is_v else 'Обычный'}</b>\n"
        f"VIP до: <b>{until}</b>\n"
    )


def ref_text(bot_username: str, user_id: int, count: int, credits: int) -> str:
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    return (
        "🎁 <b>Реферальная ссылка</b>\n\n"
        "Приглашай друзей — получай бонусы.\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{count}</b>\n"
        f"Бонус-кредиты: <b>{credits}</b>\n\n"
        "Хочешь — я сделаю тебе красивый текст-приглашение под Reels/Stories."
    )


# -------------------- Handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # register user + referral
    referred_by = None
    if context.args and len(context.args) >= 1:
        arg = context.args[0].strip()
        if arg.startswith("ref_"):
            try:
                referred_by = int(arg.replace("ref_", "").strip())
            except Exception:
                referred_by = None

    upsert_user(user.id, user.username, user.first_name, referred_by=referred_by)

    # handle referral bonus only if new referral
    if referred_by and referred_by != user.id:
        inserted = add_referral(referred_by, user.id)
        if inserted:
            inc_referrals_count(referred_by)
            add_credits(referred_by, 5)

    # subscription gate
    ok = await guard_or_subscribe(update, context)
    if not ok:
        return

    await safe_send(context.bot, chat_id=chat.id, text=start_text(), reply_markup=main_menu_kb())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok = await guard_or_subscribe(update, context)
    if not ok:
        return
    await safe_send(context.bot, chat_id=update.effective_chat.id, text="🏠 <b>Главное меню</b>", reply_markup=main_menu_kb())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    # subscription check ALWAYS
    ok = await guard_or_subscribe(update, context)
    if not ok:
        return

    data = query.data or ""
    await query.answer()

    # noop
    if data == "noop":
        return

    # after subscribe button
    if data == "check_sub":
        user = update.effective_user
        if not user:
            return
        sub_ok = await is_subscribed(context.bot, user.id)
        if not sub_ok:
            await safe_edit(query, "❌ Пока не вижу подписку. Подпишись и нажми ещё раз ✅", reply_markup=subscribe_kb())
            return
        await safe_edit(query, start_text(), reply_markup=main_menu_kb())
        return

    # main menu routes
    if data.startswith("m:"):
        section = data.split(":", 1)[1]
        context.user_data.pop("mode", None)

        if section == "home":
            await safe_edit(query, "🏠 <b>Главное меню</b>\nВыбирай раздел 👇", reply_markup=main_menu_kb())
            return

        if section in ("sora", "heygen", "meta", "hooks"):
            text = (
                f"✨ <b>{section.upper()}</b>\n\n"
                "Скажи, что хочешь получить — и я сделаю готовый промт.\n\n"
                "Пример запроса:\n"
                f"• «Сделай {section}-промт для зимнего fashion-editorial, 9:16, ультра-реализм»\n\n"
                "Пиши прямо сюда сообщением 👇"
            )
            context.user_data["mode"] = f"gen:{section}"
            await safe_edit(query, text, reply_markup=back_menu_kb())
            return

        if section == "prompts":
            await safe_edit(query, "📚 <b>База промтов</b>\nВыбери категорию:", reply_markup=prompts_menu_kb())
            return

        if section == "ai":
            context.user_data["mode"] = "ai"
            await safe_edit(query, "🧠 <b>AI помощник</b>\n\nПиши вопрос/задачу одним сообщением — я отвечу.\n\nЧтобы выйти: нажми «⬅️ В меню».", reply_markup=back_menu_kb())
            return

        if section == "vip":
            user = get_user(update.effective_user.id)
            rows = [
                [InlineKeyboardButton(f"⭐ Купить VIP за {VIP_STARS_PRICE} Stars", callback_data="vip:buy")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="m:home")],
            ]
            await safe_edit(query, vip_text(user), reply_markup=InlineKeyboardMarkup(rows))
            return

        if section == "ref":
            user = get_user(update.effective_user.id) or {}
            bot_username = (await context.bot.get_me()).username
            txt = ref_text(bot_username, update.effective_user.id, int(user.get("referrals_count") or 0), int(user.get("credits") or 0))
            rows = [
                [InlineKeyboardButton("✍️ Сделай текст-приглашение", callback_data="ref:copytext")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="m:home")],
            ]
            await safe_edit(query, txt, reply_markup=InlineKeyboardMarkup(rows))
            return

        if section == "ig":
            await safe_edit(query, "📸 <b>Instagram-помощник</b>\nВыбирай, что сделать:", reply_markup=instagram_kb())
            return

        if section == "settings":
            await safe_edit(query, "⚙️ <b>Настройки</b>\nВыбери стиль ответов:", reply_markup=settings_kb())
            return

    # prompts flows
    if data.startswith("p:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "new":
            # latest prompts (all)
            page = int(parts[2]) if len(parts) > 2 else 0
            limit = 6
            offset = page * limit
            items = list_prompts(category="all", query=None, limit=limit, offset=offset)
            total = count_prompts(category="all", query=None)
            total_pages = (total + limit - 1) // limit

            if not items:
                await safe_edit(query, "Пока нет промтов. Добавь пост в канал — бот соберёт ✨", reply_markup=prompts_menu_kb())
                return

            text = "🆕 <b>Новые промты</b>\n\n"
            for it in items:
                pid = it["id"]
                title = it["title"] or "Без названия"
                cat = it["category"] or "all"
                text += f"• <b>{title}</b> <i>({cat})</i> — /p{pid}\n"

            # show list with buttons as well
            rows = []
            for it in items:
                pid = it["id"]
                title = (it["title"] or "Промт")[:28]
                rows.append([InlineKeyboardButton(f"📄 {title}", callback_data=f"p:open:{pid}")])

            rows.append([InlineKeyboardButton("⬅️", callback_data=f"p:new:{max(page-1,0)}"),
                         InlineKeyboardButton(f"{page+1}/{max(total_pages,1)}", callback_data="noop"),
                         InlineKeyboardButton("➡️", callback_data=f"p:new:{min(page+1, max(total_pages-1,0))}")])
            rows.append([InlineKeyboardButton("⬅️ Категории", callback_data="m:prompts"), InlineKeyboardButton("🏠 Меню", callback_data="m:home")])

            await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(rows))
            return

        if action == "search":
            context.user_data["mode"] = "search_prompts"
            await safe_edit(query, "🔎 <b>Поиск по базе</b>\n\nНапиши ключевое слово одним сообщением (например: «снегурочка», «8K кожа», «Sora 9:16»).", reply_markup=back_menu_kb())
            return

        if action == "list":
            # p:list:category:page[:q:query]
            category = parts[2] if len(parts) > 2 else "all"
            page = int(parts[3]) if len(parts) > 3 else 0
            query_txt = None
            if ":q:" in data:
                query_txt = data.split(":q:", 1)[1]

            limit = 6
            offset = page * limit
            items = list_prompts(category=category, query=query_txt, limit=limit, offset=offset)
            total = count_prompts(category=category, query=query_txt)
            total_pages = (total + limit - 1) // limit

            if not items:
                await safe_edit(query, "Пока пусто. Скоро подтяну новые промты из канала ✨", reply_markup=prompts_menu_kb())
                return

            header = f"📚 <b>Промты — {category.upper()}</b>\n"
            if query_txt:
                header += f"🔎 Поиск: <i>{query_txt}</i>\n"
            header += "\nВыбери промт 👇\n\n"

            rows = []
            for it in items:
                pid = it["id"]
                title = (it["title"] or "Промт")[:32]
                rows.append([InlineKeyboardButton(f"📄 {title}", callback_data=f"p:open:{pid}")])

            rows.append(list(pagination_kb(category, page, total_pages, query_txt).inline_keyboard[0]))
            rows.append(list(pagination_kb(category, page, total_pages, query_txt).inline_keyboard[1]))

            await safe_edit(query, header, reply_markup=InlineKeyboardMarkup(rows))
            return

        if action == "open":
            pid = int(parts[2])
            p = get_prompt(pid)
            if not p:
                await safe_edit(query, "Не нашла этот промт 🥲", reply_markup=prompts_menu_kb())
                return
            fav = is_favorite(update.effective_user.id, pid)
            body = p["body"] or ""
            title = p["title"] or "Промт"
            cat = p["category"] or "all"
            source = p["source"] or "—"
            text = (
                f"📄 <b>{title}</b>\n"
                f"<i>{cat}</i> • source: <i>{source}</i>\n\n"
                f"<code>{body}</code>"
            )
            await safe_edit(query, text, reply_markup=prompt_item_kb(pid, fav))
            return

        if action == "fav":
            pid = int(parts[2])
            added = toggle_favorite(update.effective_user.id, pid)
            p = get_prompt(pid)
            if not p:
                await safe_edit(query, "Не нашла этот промт 🥲", reply_markup=prompts_menu_kb())
                return
            title = p["title"] or "Промт"
            cat = p["category"] or "all"
            source = p["source"] or "—"
            body = p["body"] or ""
            fav = is_favorite(update.effective_user.id, pid)
            text = (
                f"{'✅ Добавила в избранное!' if added else '🗑 Убрала из избранного.'}\n\n"
                f"📄 <b>{title}</b>\n"
                f"<i>{cat}</i> • source: <i>{source}</i>\n\n"
                f"<code>{body}</code>"
            )
            await safe_edit(query, text, reply_markup=prompt_item_kb(pid, fav))
            return

        if action == "share":
            pid = int(parts[2])
            p = get_prompt(pid)
            if not p:
                await safe_edit(query, "Не нашла этот промт 🥲", reply_markup=prompts_menu_kb())
                return
            title = p["title"] or "Промт"
            cat = p["category"] or "all"
            body = p["body"] or ""
            share = (
                "🔥 <b>ПРОМТ ДНЯ</b>\n"
                f"Категория: <b>{cat.upper()}</b>\n\n"
                f"✅ <b>{title}</b>\n\n"
                f"<code>{body}</code>\n\n"
                f"✨ Больше промтов: {REQUIRED_CHANNEL_URL or ''}"
            )
            await safe_edit(query, share, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"p:open:{pid}")]]))
            return

    # Instagram flows
    if data.startswith("ig:"):
        action = data.split(":", 1)[1]
        context.user_data["mode"] = f"ig:{action}"
        if action == "post":
            await safe_edit(query, "📸 <b>Пост для Instagram</b>\n\nНапиши тему/что на видео/что хочешь донести. Я сделаю готовый текст + CTA + 3 варианта тональности.", reply_markup=back_menu_kb())
            return
        if action == "tags":
            await safe_edit(query, "🏷 <b>Хештеги</b>\n\nНапиши: ниша + город/страна + формат (Reels/пост). Я дам 3 набора: мягкие / средние / агрессивные.", reply_markup=back_menu_kb())
            return
        if action == "reels":
            await safe_edit(query, "🎬 <b>Сценарий Reels</b>\n\nНапиши: что в кадре + цель (просмотры/подписка/переход в TG). Я дам: хук, сценарий 0–3с/3–10с/10–25с, титры, CTA.", reply_markup=back_menu_kb())
            return

    # referral helper
    if data == "ref:copytext":
        context.user_data["mode"] = "ref_invite_text"
        await safe_edit(query, "🎁 Напиши: кому и про что приглашение (Reels/Stories/пост). Я сделаю текст-приглашение под твою реф-ссылку.", reply_markup=back_menu_kb())
        return

    # settings
    if data.startswith("set:"):
        _, what, val = data.split(":")
        if what == "style":
            context.user_data["style"] = val
            await safe_edit(query, f"✅ Стиль установлен: <b>{'Коротко+структурно' if val=='short' else 'Подробно'}</b>", reply_markup=settings_kb())
            return
        if what == "reset":
            context.user_data.pop("mode", None)
            await safe_edit(query, "✅ Сбросила режимы. Возвращаю в меню 👇", reply_markup=main_menu_kb())
            return

    # VIP purchase
    if data == "vip:buy":
        user = get_user(update.effective_user.id) or {}
        # send invoice
        try:
            prices = [LabeledPrice(label=f"VIP на 30 дней", amount=VIP_STARS_PRICE)]
            await context.bot.send_invoice(
                chat_id=update.effective_chat.id,
                title="VIP доступ",
                description="VIP на 30 дней + закрытые промты и больше генераций.",
                payload=f"vip_{update.effective_user.id}_{int(time.time())}",
                provider_token=PROVIDER_TOKEN,  # Stars иногда допускает пустую строку
                currency="XTR",
                prices=prices,
            )
        except TelegramError as e:
            await safe_send(context.bot, update.effective_chat.id, f"⚠️ Не получилось выставить счёт: {e}")
        return


async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if not query:
        return
    await query.answer(ok=True)


async def on_success_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # user paid
    ok = await guard_or_subscribe(update, context)
    if not ok:
        return
    user = update.effective_user
    if not user:
        return

    until = _vip_until_iso(30)
    set_vip(user.id, True, until)

    await safe_send(
        context.bot,
        update.effective_chat.id,
        f"✅ <b>VIP активирован!</b>\n\nТеперь у тебя VIP до: <b>{until}</b>\n\nОткрываю меню 👇",
        reply_markup=main_menu_kb(),
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # subscription gate ALWAYS
    ok = await guard_or_subscribe(update, context)
    if not ok:
        return

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    txt = (update.message.text or "").strip() if update.message else ""
    if not txt:
        return

    upsert_user(user.id, user.username, user.first_name)

    mode = context.user_data.get("mode")

    # search prompts
    if mode == "search_prompts":
        q = txt[:80]
        limit = 6
        items = list_prompts(category="all", query=q, limit=limit, offset=0)
        if not items:
            await safe_send(context.bot, chat.id, "Ничего не нашла по запросу 😿 Попробуй другое слово.", reply_markup=back_menu_kb())
            return

        text = f"🔎 <b>Результаты по запросу:</b> <i>{q}</i>\n\nВыбери промт 👇"
        rows = []
        for it in items:
            pid = it["id"]
            title = (it["title"] or "Промт")[:32]
            rows.append([InlineKeyboardButton(f"📄 {title}", callback_data=f"p:open:{pid}")])
        rows.append([InlineKeyboardButton("⬅️ В меню базы", callback_data="m:prompts"), InlineKeyboardButton("🏠 Меню", callback_data="m:home")])
        await safe_send(context.bot, chat.id, text, reply_markup=InlineKeyboardMarkup(rows))
        return

    # referral invite text
    if mode == "ref_invite_text":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{user.id}"
        req = f"Сделай текст приглашения. Контекст: {txt}. Вставь ссылку: {link}. Дай 3 варианта: мягкий/энергичный/агрессивный CTA."
        ans = await openai_text(context, req)
        await safe_send(context.bot, chat.id, ans or "⚠️ AI недоступен.", reply_markup=back_menu_kb())
        return

    # Instagram modes
    if isinstance(mode, str) and mode.startswith("ig:"):
        sub = mode.split(":", 1)[1]
        if sub == "post":
            req = f"Сделай Instagram пост. Тема/контент: {txt}. Дай 3 варианта тональности + короткий заголовок + CTA в Telegram."
        elif sub == "tags":
            req = f"Подбери хештеги для Instagram. Запрос: {txt}. Дай 3 набора: мягкие/средние/агрессивные. 10-15 штук в каждом."
        else:
            req = f"Сделай сценарий Reels. Запрос: {txt}. Дай: хук, сценарий по секундам, титры, озвучка, CTA в Telegram."
        ans = await openai_text(context, req)
        await safe_send(context.bot, chat.id, ans or "⚠️ AI недоступен.", reply_markup=back_menu_kb())
        return

    # generator modes
    if isinstance(mode, str) and mode.startswith("gen:"):
        section = mode.split(":", 1)[1]
        req = (
            f"Сгенерируй один лучший промт для {section}. "
            f"Пожелания пользователя: {txt}. "
            "Сделай: 1) короткое пояснение (1-2 строки), 2) ГОТОВЫЙ ПРОМТ в код-блоке."
        )
        ans = await openai_text(context, req)
        await safe_send(context.bot, chat.id, ans or "⚠️ AI недоступен.", reply_markup=back_menu_kb())
        return

    # AI chat mode
    if mode == "ai":
        ans = await openai_text(context, txt)
        await safe_send(context.bot, chat.id, ans or "⚠️ AI недоступен. Проверь OPENAI_API_KEY на Render.", reply_markup=back_menu_kb())
        return

    # default: show menu hint
    await safe_send(context.bot, chat.id, "Выбери раздел в меню 👇", reply_markup=main_menu_kb())


# -------------------- Channel / Discussion prompt collector --------------------
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Если бот админ в канале: парсим channel_post и сохраняем промты.
    """
    post = update.channel_post
    if not post:
        return

    text = (post.text or post.caption or "").strip()
    if not text:
        return

    cands = extract_candidates(text)
    if not cands:
        return

    category = guess_category(text)
    inserted_any = False
    for c in cands:
        title = f"Из канала • {category.upper()}"
        ok = add_prompt(category=category, title=title, body=c, source="channel")
        inserted_any = inserted_any or ok

    if inserted_any:
        logger.info("Saved prompts from channel_post (%s) count=%d", category, len(cands))


async def on_discussion_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
    if not _is_discussion(msg.chat_id):
        return

    text = msg.text.strip()
    cands = extract_candidates(text)
    if not cands:
        return

    category = guess_category(text)
    inserted_any = False
    for c in cands:
        title = f"Из комментов • {category.upper()}"
        ok = add_prompt(category=category, title=title, body=c, source="discussion")
        inserted_any = inserted_any or ok

    if inserted_any:
        logger.info("Saved prompts from discussion (%s) count=%d", category, len(cands))


# -------------------- Error Handler --------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await safe_send(context.bot, update.effective_chat.id, "⚠️ Упс, что-то пошло не так. Я уже чинюсь. Попробуй ещё раз.", reply_markup=main_menu_kb())
    except Exception:
        pass


# -------------------- App / Webhook --------------------
tg_app: Optional[Application] = None


def build_telegram_app() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_menu))

    application.add_handler(CallbackQueryHandler(on_callback))

    # payments
    application.add_handler(PreCheckoutQueryHandler(on_precheckout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_success_payment))

    # channel posts collector
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))

    # discussion collector (only if DISCUSSION_CHAT_ID provided, but handler is safe anyway)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_discussion_message), group=0)

    # main message handler (DM / group messages)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_message), group=1)

    application.add_error_handler(on_error)

    return application


async def set_webhook(application: Application) -> None:
    if not WEBHOOK_BASE_URL:
        logger.warning("WEBHOOK_BASE_URL empty, skipping setWebhook.")
        return

    url = f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"
    allowed_updates = [
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "callback_query",
        "pre_checkout_query",
    ]
    try:
        await application.bot.set_webhook(url=url, allowed_updates=allowed_updates)
        logger.info("Webhook set: %s", url)
    except TelegramError as e:
        logger.warning("set_webhook failed: %s", str(e))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global tg_app

    tg_app = build_telegram_app()
    await tg_app.initialize()
    await tg_app.start()
    await set_webhook(tg_app)

    me = await tg_app.bot.get_me()
    logger.info("Bot username: %s", me.username)

    yield

    # shutdown gracefully
    try:
        await tg_app.stop()
    finally:
        await tg_app.shutdown()
        tg_app = None


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=PlainTextResponse)
async def root_get():
    return "OK"


@app.head("/", response_class=PlainTextResponse)
async def root_head():
    return "OK"


@app.get("/health")
async def health():
    return {"ok": True, "ts": _now_iso()}


@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    global tg_app
    if tg_app is None:
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    try:
        await tg_app.process_update(update)
    except Exception as e:
        logger.exception("process_update failed: %s", str(e))
    return {"ok": True}
