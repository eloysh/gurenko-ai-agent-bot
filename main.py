import os
import re
import json
import base64
import time
import sqlite3
import logging
from datetime import datetime, timedelta, date
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# Optional OpenAI
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gurenko-bot")


# -------------------- ENV HELPERS --------------------
def env_str(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def env_int(*keys: str, default: int = 0) -> int:
    v = env_str(*keys, default="")
    if v == "":
        return default
    try:
        return int(str(v).strip())
    except Exception:
        return default


def env_bool(*keys: str, default: bool = False) -> bool:
    v = env_str(*keys, default="")
    if v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


# -------------------- CONFIG --------------------
# Telegram
TELEGRAM_TOKEN = env_str("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_TOKEN is not set")

# Render webhook base url
WEBHOOK_BASE = env_str("WEBHOOK_URL", "WEBHOOK_BASE", "RENDER_EXTERNAL_URL").rstrip("/")
WEBHOOK_PATH = "/webhook"

# Gates
REQUIRED_CHANNEL = env_str("REQUIRED_CHANNEL", "TG_CHANNEL", default="@gurenko_kristina_ai")
REQUIRED_CHANNEL = REQUIRED_CHANNEL if REQUIRED_CHANNEL.startswith("@") else "@" + REQUIRED_CHANNEL
STRICT_CHANNEL_CHECK = env_bool("STRICT_CHANNEL_CHECK", default=True)

def channel_to_invite_url(ch: str) -> str:
    name = ch.lstrip("@")
    return f"https://t.me/{name}"

CHANNEL_INVITE_URL = env_str("CHANNEL_INVITE_URL", default=channel_to_invite_url(REQUIRED_CHANNEL))
INSTAGRAM_URL = env_str("INSTAGRAM_URL", default="https://www.instagram.com/gurenko_kristina/")

# Admin
ADMIN_USER_ID = env_int("ADMIN_USER_ID", default=0)

# IG flow
AUTO_IG_VERIFY = env_bool("AUTO_IG_VERIFY", default=False)

# Limits
FREE_DAILY_LIMIT = env_int("FREE_DAILY_LIMIT", "DAILY_LIMIT", default=3)
VIP_DAILY_LIMIT = env_int("VIP_DAILY_LIMIT", default=30)

# Referral bonuses
REF_BONUS_CREDITS = env_int("REF_BONUS_CREDITS", default=1)   # бонус пригласившему
WELCOME_BONUS_CREDITS = env_int("WELCOME_BONUS_CREDITS", default=0)  # бонус новому

# VIP Stars
VIP_DAYS = env_int("VIP_DAYS", default=30)
VIP_PRICE_STARS = env_int("VIP_PRICE_STARS", default=299)

# OpenAI
OPENAI_API_KEY = env_str("OPENAI_API_KEY")
OPENAI_IMAGE_MODEL = env_str("OPENAI_IMAGE_MODEL", default="gpt-image-1")
OPENAI_TEXT_MODEL = env_str("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_VIDEO_MODEL = env_str("OPENAI_VIDEO_MODEL", default="sora-2")

# DB
DB_PATH = env_str("DB_PATH", default="bot.db")


# -------------------- APP/DB --------------------
app = FastAPI()
tg_app: Application | None = None
BOT_USERNAME: str | None = None


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,

            referred_by INTEGER,
            ref_count INTEGER DEFAULT 0,

            ig_verified INTEGER DEFAULT 0,
            ig_handle TEXT DEFAULT '',

            vip_until TEXT,

            used_date TEXT,
            used_count INTEGER DEFAULT 0,

            bonus_credits INTEGER DEFAULT 0,

            created_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS ig_requests (
            user_id INTEGER PRIMARY KEY,
            ig_handle TEXT,
            note TEXT,
            created_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            payload TEXT,
            stars INTEGER,
            created_at TEXT
        )
        """)
        conn.commit()


def now_utc():
    return datetime.utcnow()


def today_str():
    return date.today().isoformat()


def ensure_user(u) -> bool:
    """Returns True if created new user"""
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at, used_date, used_count, bonus_credits) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (u.id, u.username or "", u.first_name or "", now_utc().isoformat(), today_str(), WELCOME_BONUS_CREDITS),
            )
            conn.commit()
            return True
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (u.username or "", u.first_name or "", u.id),
        )
        conn.commit()
    return False


def get_user(user_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def set_referred(user_id: int, inviter_id: int) -> bool:
    """Set referral once. Returns True if applied."""
    with db() as conn:
        me = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not me:
            return False
        if me["referred_by"]:
            return False
        if inviter_id == user_id:
            return False

        conn.execute("UPDATE users SET referred_by=? WHERE user_id=?", (inviter_id, user_id))
        conn.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (inviter_id,))
        conn.execute("UPDATE users SET bonus_credits = bonus_credits + ? WHERE user_id=?", (REF_BONUS_CREDITS, inviter_id))
        conn.commit()
        return True


def is_vip(row) -> bool:
    if not row or not row["vip_until"]:
        return False
    try:
        dt = datetime.fromisoformat(row["vip_until"])
        return dt > now_utc()
    except Exception:
        return False


def vip_until_text(row):
    if not row or not row["vip_until"]:
        return "нет"
    return row["vip_until"].replace("T", " ")


def reset_daily_if_needed(row):
    if not row:
        return
    td = today_str()
    if row["used_date"] != td:
        with db() as conn:
            conn.execute("UPDATE users SET used_date=?, used_count=0 WHERE user_id=?", (td, row["user_id"]))
            conn.commit()


def can_use_generation(row) -> tuple[bool, str]:
    reset_daily_if_needed(row)
    row = get_user(row["user_id"])
    vip = is_vip(row)

    bonus = int(row["bonus_credits"] or 0)
    if bonus > 0:
        return True, f"🎁 Бонус-генерации: {bonus}."

    limit = VIP_DAILY_LIMIT if vip else FREE_DAILY_LIMIT
    used = int(row["used_count"] or 0)
    if used >= limit:
        return False, f"Лимит на сегодня исчерпан: {used}/{limit}."
    return True, f"Осталось на сегодня: {limit - used}."


def consume_generation(row):
    reset_daily_if_needed(row)
    row = get_user(row["user_id"])

    bonus = int(row["bonus_credits"] or 0)
    with db() as conn:
        if bonus > 0:
            conn.execute("UPDATE users SET bonus_credits = bonus_credits - 1 WHERE user_id=?", (row["user_id"],))
        else:
            conn.execute(
                "UPDATE users SET used_count = used_count + 1, used_date=? WHERE user_id=?",
                (today_str(), row["user_id"])
            )
        conn.commit()


async def is_subscribed_to_channel(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return True
        if member.status == ChatMemberStatus.RESTRICTED:
            return True
        return False
    except Exception as e:
        log.warning("channel check failed: %s", e)
        return False


def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🤖 ИИ помощник"), KeyboardButton("🎁 Промт дня")],
            [KeyboardButton("🖼 Сгенерировать фото"), KeyboardButton("🎬 Сгенерировать видео")],
            [KeyboardButton("📆 Челлендж 30 дней"), KeyboardButton("🎁 Пригласить друга")],
            [KeyboardButton("⭐️ VIP / Подписка"), KeyboardButton("✅ Проверить Instagram")],
            [KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )


def share_keyboard(user_id: int):
    global BOT_USERNAME
    bot_un = BOT_USERNAME or "your_bot_username"
    deep = f"https://t.me/{bot_un}?start=ref_{user_id}"
    share_url = f"https://t.me/share/url?url={quote(deep)}&text={quote('Смотри, бот с промтами и генерацией фото/видео 👇')}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться ботом", url=share_url)],
        [InlineKeyboardButton("🔗 Открыть ссылку приглашения", url=deep)],
    ])


def channel_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подписаться на канал", url=CHANNEL_INVITE_URL)],
        [InlineKeyboardButton("🔁 Я подписался — проверить", callback_data="check_channel")]
    ])


def instagram_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Открыть Instagram", url=INSTAGRAM_URL)],
        [InlineKeyboardButton("✅ Я подписался — отправить ник/скрин", callback_data="ig_request")]
    ])


async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    u = update.effective_user
    ensure_user(u)

    ok = await is_subscribed_to_channel(context.bot, u.id)
    if not ok:
        if STRICT_CHANNEL_CHECK:
            await update.effective_message.reply_text(
                "🔒 Чтобы пользоваться ботом, нужно быть подписанным на мой Telegram-канал.\n\n"
                "Нажми «Подписаться», потом «Я подписался — проверить».",
                reply_markup=channel_gate_keyboard()
            )
            return False

    row = get_user(u.id)
    if int(row["ig_verified"] or 0) != 1:
        await update.effective_message.reply_text(
            "🔒 Ещё шаг: подтверждение подписки на Instagram.\n\n"
            "Нажми кнопку и пришли одним сообщением:\n"
            "1) твой @ник\n"
            "2) (желательно) скрин подписки\n\n"
            "Если включён AUTO_IG_VERIFY=1 — бот откроется сразу после ника/скрина.",
            reply_markup=instagram_gate_keyboard()
        )
        return False

    return True


# -------------------- OPENAI --------------------
def get_openai_client():
    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def openai_generate_image(prompt: str) -> tuple[bytes | None, str | None]:
    client = get_openai_client()
    if not client:
        return None, "OpenAI не настроен: добавь OPENAI_API_KEY в Render."

    try:
        res = client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
        )
        data0 = res.data[0]
        b64 = getattr(data0, "b64_json", None) or (data0.get("b64_json") if isinstance(data0, dict) else None)
        if not b64:
            return None, "Не пришли данные изображения (b64_json пуст)."
        return base64.b64decode(b64), None
    except Exception as e:
        msg = str(e)
        if "403" in msg:
            return None, (
                "❌ OpenAI Images: 403 Forbidden.\n\n"
                "Это значит: ключ рабочий, но проекту запрещены картинки (нет доступа/биллинга/лимитов на Images).\n"
                "Решение: создай новый ключ в проекте с включённым billing и доступом к Images.\n"
                f"Модель оставь: {OPENAI_IMAGE_MODEL}."
            )
        return None, f"Не удалось сгенерировать фото: {e}"


def openai_assistant(text: str) -> tuple[str | None, str | None]:
    client = get_openai_client()
    if not client:
        return None, "OpenAI не настроен: добавь OPENAI_API_KEY в Render."

    system = (
        "Ты — ИИ помощник Кристины. Помогаешь с нейросетями (Sora/HeyGen/Suno/Meta AI), "
        "промтами, идеями Reels, упаковкой профиля, воронками и контентом. "
        "Отвечай кратко, по шагам, с готовыми формулировками."
    )

    try:
        resp = client.responses.create(
            model=OPENAI_TEXT_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
        out = getattr(resp, "output_text", None)
        if out:
            return out.strip(), None
        raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
        return json.dumps(raw, ensure_ascii=False)[:3500], None
    except Exception as e:
        return None, f"ИИ помощник сейчас недоступен: {e}"


# Видео — подключим по API, когда подтвердим доступ (у OpenAI это /v1/videos, model=sora-2). 
def openai_generate_video_stub(prompt: str) -> tuple[None, str]:
    return None, (
        "🎬 Видео пока отключено в этом деплое.\n\n"
        "У OpenAI есть официальный Videos API (model=sora-2). "
        "Как только у твоего проекта будет доступ — включим генерацию видео.\n"
    )


# -------------------- VIP STARS --------------------
def vip_invoice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐️ Купить VIP за {VIP_PRICE_STARS} Stars", callback_data="buy_vip")],
    ])


async def send_vip_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    payload = f"vip:{u.id}:{int(time.time())}"
    prices = [LabeledPrice(label=f"VIP на {VIP_DAYS} дней", amount=VIP_PRICE_STARS)]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="VIP подписка",
        description=f"VIP доступ на {VIP_DAYS} дней: до {VIP_DAILY_LIMIT} генераций/день + бонусы.",
        payload=payload,
        provider_token="",     # Stars
        currency="XTR",        # Stars currency
        prices=prices,
        start_parameter="vip",
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    pay = update.effective_message.successful_payment

    with db() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, payload, stars, created_at) VALUES (?, ?, ?, ?)",
            (u.id, pay.invoice_payload, int(pay.total_amount), now_utc().isoformat())
        )
        conn.commit()

    until = now_utc() + timedelta(days=VIP_DAYS)
    with db() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until.isoformat(), u.id))
        conn.commit()

    await update.effective_message.reply_text(
        f"✅ Оплата прошла! VIP активирован до {until.isoformat().replace('T',' ')} 🎉",
        reply_markup=main_menu()
    )


# -------------------- IG HELPERS --------------------
async def save_ig_request(user_id: int, handle: str, note: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
            (user_id, handle or "", note, now_utc().isoformat())
        )
        conn.execute("UPDATE users SET ig_handle=? WHERE user_id=?", (handle or "", user_id))
        conn.commit()


async def approve_ig(user_id: int, context: ContextTypes.DEFAULT_TYPE | None = None):
    with db() as conn:
        conn.execute("UPDATE users SET ig_verified=1 WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM ig_requests WHERE user_id=?", (user_id,))
        conn.commit()
    if context:
        try:
            await context.bot.send_message(user_id, "✅ Instagram подтвержден! Доступ открыт 🎉", reply_markup=main_menu())
        except Exception:
            pass


def normalize_ig_handle(text: str) -> str:
    t = (text or "").strip()
    t = t.replace("instagram.com/", "")
    m = re.search(r"@([A-Za-z0-9._]{2,30})", t)
    if m:
        return "@" + m.group(1)
    m2 = re.search(r"\b([A-Za-z0-9._]{2,30})\b", t)
    if m2:
        return "@" + m2.group(1)
    return ""


# -------------------- MENUS --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    u = update.effective_user
    is_new = ensure_user(u)

    if BOT_USERNAME is None:
        me = await context.bot.get_me()
        BOT_USERNAME = me.username

    # referral
    applied_ref = False
    inviter_id = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                inviter_id = int(arg.replace("ref_", "").strip())
                applied_ref = set_referred(u.id, inviter_id)
            except Exception:
                pass

    if applied_ref and inviter_id:
        try:
            await context.bot.send_message(
                inviter_id,
                f"🎁 Новый друг пришёл по твоей ссылке!\n+{REF_BONUS_CREDITS} бонус-генерац."
            )
        except Exception:
            pass

    ok = await require_access(update, context)
    if not ok:
        return

    row = get_user(u.id)
    _, msg = can_use_generation(row)

    text = (
        "Привет! Я — бот Кристины 🤍\n\n"
        "Что умею:\n"
        "• 🤖 ИИ помощник (идеи, промты, Reels, упаковка)\n"
        "• 🖼 Генерация фото\n"
        "• 🎬 Генерация видео — подключим после доступа\n"
        "• 🎁 «Промт дня» и 📆 челлендж\n"
        "• 🎁 Рефералка: приглашай друзей и получай бонусы\n\n"
        f"{msg}\n\n"
        "Выбирай в меню 👇"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    row = get_user(update.effective_user.id)
    _, msg = can_use_generation(row)
    text = (
        "ℹ️ Помощь\n\n"
        "🤖 ИИ помощник — спроси что угодно про нейросети/контент/промты.\n"
        "🖼 Сгенерировать фото — отправь текст-описание.\n"
        "🎬 Сгенерировать видео — включим после доступа.\n"
        "🎁 Пригласить друга — бонус-генерации.\n\n"
        f"VIP: {'активен ✅' if is_vip(row) else 'нет ❌'}\n"
        f"VIP до: {vip_until_text(row)}\n"
        f"{msg}\n"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    u = update.effective_user
    ensure_user(u)

    if query.data == "check_channel":
        ok = await is_subscribed_to_channel(context.bot, u.id)
        if ok or not STRICT_CHANNEL_CHECK:
            await query.edit_message_text("✅ Канал подтверждён! Теперь подтвердим Instagram 👇", reply_markup=instagram_gate_keyboard())
        else:
            await query.edit_message_text(
                "Пока не вижу подписку 😔\n\nПодпишись и нажми «проверить» ещё раз.",
                reply_markup=channel_gate_keyboard()
            )
        return

    if query.data == "ig_request":
        await save_ig_request(u.id, "", "requested via button")
        context.user_data["await_ig_info"] = True
        await query.edit_message_text(
            "✅ Ок! Теперь пришли одним сообщением:\n"
            "1) твой @ник в Instagram\n"
            "2) (желательно) скрин подписки\n\n"
            "Если AUTO_IG_VERIFY=1 — доступ откроется сразу."
        )
        return

    if query.data == "buy_vip":
        await send_vip_invoice(update, context)
        return


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)

    # IG proof by photo
    if context.user_data.get("await_ig_info"):
        await save_ig_request(u.id, "", "photo proof received")
        context.user_data["await_ig_info"] = False

        if AUTO_IG_VERIFY:
            await approve_ig(u.id, context)
            return

        await update.effective_message.reply_text("✅ Принято! Я подтвержу и открою доступ.")
        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка (фото): user_id={u.id}, tg=@{u.username}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    if not await require_access(update, context):
        return

    await update.effective_message.reply_text("Фото получил ✅", reply_markup=main_menu())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    u = update.effective_user
    ensure_user(u)

    # IMPORTANT FIX: IG proof can be text too
    if context.user_data.get("await_ig_info"):
        handle = normalize_ig_handle(txt)
        await save_ig_request(u.id, handle, note="text proof received")
        context.user_data["await_ig_info"] = False

        if AUTO_IG_VERIFY:
            await approve_ig(u.id, context)
            return

        await update.effective_message.reply_text("✅ Принято! Я подтвержу и открою доступ.")
        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка: user_id={u.id}, tg=@{u.username}, ig={handle or '(не указан)'}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    # Menu actions
    if txt == "✅ Проверить Instagram":
        await update.effective_message.reply_text(
            "Подпишись на Instagram и нажми кнопку ниже, затем пришли ник/скрин.\n\n"
            f"Instagram: {INSTAGRAM_URL}",
            reply_markup=instagram_gate_keyboard(),
        )
        return

    if txt == "🎁 Пригласить друга":
        if not await require_access(update, context):
            return
        row = get_user(u.id)
        await update.effective_message.reply_text(
            f"🎁 Рефералка\n\n"
            f"👥 Приглашено друзей: {int(row['ref_count'] or 0)}\n"
            f"🎁 Бонус-генераций: {int(row['bonus_credits'] or 0)}\n\n"
            "Отправь другу ссылку 👇",
            reply_markup=share_keyboard(u.id),
        )
        return

    if txt == "ℹ️ Помощь":
        await help_cmd(update, context)
        return

    if txt == "⭐️ VIP / Подписка":
        if not await require_access(update, context):
            return
        row = get_user(u.id)
        text = (
            "⭐️ VIP / Подписка\n\n"
            f"VIP даёт до {VIP_DAILY_LIMIT} генераций в день.\n"
            f"Срок VIP: {VIP_DAYS} дней.\n"
            f"Цена: {VIP_PRICE_STARS} ⭐️ Stars.\n\n"
            f"Твой статус: {'VIP ✅' if is_vip(row) else 'Обычный'}\n"
            f"VIP до: {vip_until_text(row)}"
        )
        await update.effective_message.reply_text(text, reply_markup=vip_invoice_keyboard())
        return

    if txt == "🎁 Промт дня":
        if not await require_access(update, context):
            return
        prompts = [
            "Ультра-реалистичный fashion-портрет, морозные ресницы, 85mm, мягкий свет, 8K, кожа детальная.",
            "Кинематографичный зимний кадр, лёгкий снег, объёмный свет, реалистичная текстура ткани, 4K.",
            "Editorial-фото, минимализм, чистый фон, детализация кожи, натуральные поры, high-end retouch.",
            "Reels-стиль: динамичный ракурс, лёгкий motion blur, реализм, естественные цвета, 4K.",
        ]
        idx = int(time.time() // 86400) % len(prompts)
        await update.effective_message.reply_text(f"🎁 Промт дня:\n\n{prompts[idx]}", reply_markup=main_menu())
        return

    if txt == "📆 Челлендж 30 дней":
        if not await require_access(update, context):
            return
        await update.effective_message.reply_text(
            "📆 Челлендж:\n\n"
            "Напиши: «Сделай мне челлендж 30 дней под мой контент» — и ИИ помощник соберёт полный план ✅",
            reply_markup=main_menu()
        )
        return

    # Mode setters
    if txt == "🖼 Сгенерировать фото":
        if not await require_access(update, context):
            return
        context.user_data["mode"] = "image"
        await update.effective_message.reply_text("🖼 Напиши описание для генерации фото.")
        return

    if txt == "🎬 Сгенерировать видео":
        if not await require_access(update, context):
            return
        context.user_data["mode"] = "video"
        await update.effective_message.reply_text("🎬 Напиши описание для видео (пока отключено).")
        return

    if txt == "🤖 ИИ помощник":
        if not await require_access(update, context):
            return
        context.user_data["mode"] = "assistant"
        await update.effective_message.reply_text("🤖 Напиши вопрос (про промты, Reels, Sora/HeyGen/Suno и т.д.)")
        return

    # Free-form
    if not await require_access(update, context):
        return

    mode = context.user_data.get("mode")

    if mode == "assistant" and txt:
        await update.effective_message.reply_text("⏳ Думаю…")
        out, err = openai_assistant(txt)
        await update.effective_message.reply_text(err if err else out, reply_markup=main_menu())
        context.user_data["mode"] = None
        return

    if mode in ("image", "video") and txt:
        row = get_user(u.id)
        ok, msg = can_use_generation(row)
        if not ok:
            await update.effective_message.reply_text("⛔️ " + msg, reply_markup=main_menu())
            context.user_data["mode"] = None
            return

        await update.effective_message.reply_text("⏳ Генерирую…")

        if mode == "image":
            img, err = openai_generate_image(txt)
            if err:
                await update.effective_message.reply_text(err, reply_markup=main_menu())
            else:
                consume_generation(row)
                await update.effective_message.reply_photo(photo=img, caption="Готово ✅", reply_markup=main_menu())
        else:
            _, err = openai_generate_video_stub(txt)
            await update.effective_message.reply_text(err, reply_markup=main_menu())

        context.user_data["mode"] = None
        return

    await update.effective_message.reply_text("Выбери действие в меню 👇", reply_markup=main_menu())


# -------------------- ADMIN --------------------
async def ig_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /ig_ok <user_id>")
        return
    uid = int(context.args[0])
    await approve_ig(uid, context)
    await update.message.reply_text(f"✅ IG подтвержден для {uid}")


async def ig_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /ig_no <user_id>")
        return
    uid = int(context.args[0])
    with db() as conn:
        conn.execute("DELETE FROM ig_requests WHERE user_id=?", (uid,))
        conn.commit()
    await update.message.reply_text(f"❌ IG отклонен для {uid}")


# -------------------- FASTAPI --------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@app.post(WEBHOOK_PATH)
async def webhook(req: Request):
    if tg_app is None:
        return {"ok": False, "error": "bot not ready"}
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


# -------------------- STARTUP --------------------
@app.on_event("startup")
async def on_startup():
    global tg_app, BOT_USERNAME

    init_db()

    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("help", help_cmd))

    tg_app.add_handler(CommandHandler("ig_ok", ig_ok))
    tg_app.add_handler(CommandHandler("ig_no", ig_no))

    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await tg_app.initialize()
    await tg_app.start()

    me = await tg_app.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot username: %s", BOT_USERNAME)

    if WEBHOOK_BASE:
        url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
        await tg_app.bot.set_webhook(url=url)
        log.info("Webhook set: %s", url)
