import os
import io
import base64
import time
import sqlite3
import logging
import asyncio
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
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False


# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gurenko-bot")


# -------------------- ENV HELPERS --------------------
def getenv_any(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def getenv_int(*keys: str, default: int = 0) -> int:
    v = getenv_any(*keys, default="")
    if v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


# -------------------- CONFIG --------------------
TELEGRAM_TOKEN = getenv_any("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_API_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set (add TELEGRAM_TOKEN in Render env)")

# Webhook base url (Render): WEBHOOK_URL preferred, fallback to WEBHOOK_BASE/RENDER_EXTERNAL_URL
WEBHOOK_BASE = getenv_any("WEBHOOK_URL", "WEBHOOK_BASE", "RENDER_EXTERNAL_URL", default="")
WEBHOOK_PATH = "/webhook"

REQUIRED_CHANNEL = getenv_any("REQUIRED_CHANNEL", "TG_CHANNEL", default="@gurenko_kristina_ai")
CHANNEL_INVITE_URL = getenv_any("CHANNEL_INVITE_URL", default="https://t.me/gurenko_kristina_ai")

INSTAGRAM_URL = getenv_any("INSTAGRAM_URL", default="https://www.instagram.com/gurenko_kristina/")

ADMIN_USER_ID = getenv_int("ADMIN_USER_ID", default=0)

# Limits
FREE_DAILY_LIMIT = getenv_int("FREE_DAILY_LIMIT", "GEN_FREE_DAILY", default=1)
VIP_DAILY_LIMIT = getenv_int("VIP_DAILY_LIMIT", default=30)
VIP_DURATION_DAYS = getenv_int("VIP_DURATION_DAYS", "VIP_DAYS", default=30)

STRICT_CHANNEL_CHECK = getenv_int("STRICT_CHANNEL_CHECK", default=1)  # 1=strict, 0=allow if check fails

# OpenAI
OPENAI_API_KEY = getenv_any("OPENAI_API_KEY", default="")
OPENAI_IMAGE_MODEL = getenv_any("OPENAI_IMAGE_MODEL", "IMAGE_MODEL", default="gpt-image-1")
OPENAI_VIDEO_MODEL = getenv_any("OPENAI_VIDEO_MODEL", default="sora-2")
OPENAI_TEXT_MODEL = getenv_any("OPENAI_TEXT_MODEL", "OPENAI_MODEL", default="gpt-4o-mini")

DB_PATH = getenv_any("DB_PATH", default="bot.db")


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
        conn.commit()

def now_utc():
    return datetime.utcnow()

def today_str():
    return date.today().isoformat()

def ensure_user(u):
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
                (u.id, u.username or "", u.first_name or "", now_utc().isoformat()),
            )
        else:
            conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (u.username or "", u.first_name or "", u.id),
            )
        conn.commit()

def get_user(user_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def set_referred(user_id: int, inviter_id: int):
    with db() as conn:
        me = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not me:
            return
        if me["referred_by"]:
            return
        if inviter_id == user_id:
            return

        conn.execute("UPDATE users SET referred_by=? WHERE user_id=?", (inviter_id, user_id))
        conn.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (inviter_id,))
        conn.execute("UPDATE users SET bonus_credits = bonus_credits + 1 WHERE user_id=?", (inviter_id,))
        conn.commit()

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
        return True, f"🎁 У тебя есть бонус: {bonus} доп. генераций."

    limit = VIP_DAILY_LIMIT if vip else FREE_DAILY_LIMIT
    used = int(row["used_count"] or 0)
    if used >= limit:
        if vip:
            return False, f"Лимит на сегодня исчерпан: {used}/{limit} (VIP)."
        return False, f"Лимит на сегодня исчерпан: {used}/{limit}. Завтра будет доступно снова."
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


# -------------------- GATES --------------------
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
        return False if STRICT_CHANNEL_CHECK else True

def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🖼 Сгенерировать фото"), KeyboardButton("🎬 Сгенерировать видео")],
            [KeyboardButton("🤖 ИИ помощник"), KeyboardButton("🎁 Промт дня")],
            [KeyboardButton("📆 Челлендж 30 дней"), KeyboardButton("🎁 Пригласить друга")],
            [KeyboardButton("⭐️ VIP / Подписка"), KeyboardButton("✅ Проверить Instagram")],
            [KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )

def share_keyboard(user_id: int):
    bot_un = BOT_USERNAME or "your_bot_username"
    deep = f"https://t.me/{bot_un}?start=ref_{user_id}"
    share_url = f"https://t.me/share/url?url={quote(deep)}&text={quote('Смотри, бот Кристины с промтами и генерацией фото/видео 👇')}"
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
        [InlineKeyboardButton("✅ Я подписался — отправить заявку", callback_data="ig_request")]
    ])

async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    u = update.effective_user
    ensure_user(u)

    ok = await is_subscribed_to_channel(context.bot, u.id)
    if not ok:
        await update.effective_message.reply_text(
            "🔒 Чтобы пользоваться ботом, нужно быть подписанным на мой Telegram-канал.\n\n"
            "Нажми «Подписаться», потом «Я подписался — проверить».",
            reply_markup=channel_gate_keyboard()
        )
        return False

    row = get_user(u.id)
    if int(row["ig_verified"] or 0) != 1:
        await update.effective_message.reply_text(
            "🔒 Ещё один шаг: подтверждение подписки на Instagram.\n\n"
            "Instagram не даёт надёжную авто-проверку подписки через бота.\n"
            "Поэтому ты отправляешь заявку, а я подтверждаю — и бот открывается полностью.\n\n"
            "Нажми кнопку ниже 👇",
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
        return None, "OpenAI API не настроен (нет OPENAI_API_KEY)."

    try:
        res = client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024"
        )
        data0 = res.data[0]
        b64 = getattr(data0, "b64_json", None) or (data0.get("b64_json") if isinstance(data0, dict) else None)
        if not b64:
            return None, "Не пришли данные изображения (b64_json пуст)."
        return base64.b64decode(b64), None
    except Exception as e:
        return None, f"Не удалось сгенерировать фото: {e}"

def openai_text_answer(user_text: str) -> tuple[str | None, str | None]:
    client = get_openai_client()
    if not client:
        return None, "OpenAI API не настроен (нет OPENAI_API_KEY)."
    try:
        # modern Responses API (simple)
        resp = client.responses.create(
            model=OPENAI_TEXT_MODEL,
            input=user_text
        )
        text = getattr(resp, "output_text", None)
        if not text:
            # fallback
            text = str(resp)
        return text, None
    except Exception as e:
        return None, f"Не удалось получить ответ ИИ: {e}"

async def sora_video_worker(chat_id: int, user_id: int, prompt: str, row_snapshot):
    """
    Background worker: creates video job, polls status, downloads bytes, sends to user.
    Consumes generation only on success.
    """
    client = get_openai_client()
    if not client:
        await tg_app.bot.send_message(chat_id, "OpenAI API не настроен (нет OPENAI_API_KEY).")
        return

    try:
        video = await asyncio.to_thread(
            client.videos.create,
            model=OPENAI_VIDEO_MODEL,
            prompt=prompt,
        )
        vid = video.id

        # Poll
        for _ in range(120):  # ~4 min if 2s sleep
            v = await asyncio.to_thread(client.videos.retrieve, vid)
            status = getattr(v, "status", None) or (v.get("status") if isinstance(v, dict) else None)
            if status == "succeeded":
                break
            if status == "failed":
                await tg_app.bot.send_message(chat_id, "❌ Видео не удалось сгенерировать (status=failed).")
                return
            await asyncio.sleep(2)

        # Download bytes
        resp = await asyncio.to_thread(client.videos.download_content, video_id=vid)
        content = await asyncio.to_thread(resp.read)

        # Send to telegram
        bio = io.BytesIO(content)
        bio.name = "video.mp4"
        consume_generation(row_snapshot)
        await tg_app.bot.send_video(chat_id, video=bio, caption="Готово ✅", supports_streaming=True, reply_markup=main_menu())
    except Exception as e:
        await tg_app.bot.send_message(chat_id, f"❌ Ошибка генерации видео: {e}")


# -------------------- HANDLERS --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    u = update.effective_user
    ensure_user(u)

    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                inviter = int(arg.replace("ref_", "").strip())
                set_referred(u.id, inviter)
            except Exception:
                pass

    if BOT_USERNAME is None:
        me = await context.bot.get_me()
        BOT_USERNAME = me.username

    await update.message.reply_text(
        "Привет! Я — бот Кристины 🤍\n\n"
        "Что умею:\n"
        "• 🖼 Генерация фото по описанию\n"
        "• 🎬 Генерация видео (Sora), если доступна в твоём API\n"
        "• 🤖 ИИ помощник (подскажет промты, идеи, сценарии)\n"
        "• 🎁 Промт дня и 📆 челлендж\n"
        "• 🎁 Рефералка: приглашай друзей и получай бонус-генерации\n\n"
        f"Лимит: бесплатно — {FREE_DAILY_LIMIT}/день. VIP — до {VIP_DAILY_LIMIT}/день на {VIP_DURATION_DAYS} дней.\n\n"
        "Выбирай кнопку в меню 👇",
        reply_markup=main_menu()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    row = get_user(update.effective_user.id)
    _, msg = can_use_generation(row)
    await update.effective_message.reply_text(
        "ℹ️ Помощь\n\n"
        "🖼 Сгенерировать фото → напиши текст-описание\n"
        "🎬 Сгенерировать видео → напиши описание (если Sora доступна)\n"
        "🤖 ИИ помощник → спроси про промты/идеи/сценарии\n"
        "🎁 Пригласить друга → ссылка + бонусы\n\n"
        f"VIP: {'активен ✅' if is_vip(row) else 'нет ❌'}\n"
        f"VIP до: {vip_until_text(row)}\n"
        f"{msg}",
        reply_markup=main_menu()
    )

async def set_mode_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    context.user_data["mode"] = "image"
    await update.effective_message.reply_text(
        "🖼 Напиши описание для генерации фото.\n\n"
        "Пример: «ультра-реалистичный зимний fashion-портрет, мягкий свет, 8K…»"
    )

async def set_mode_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    context.user_data["mode"] = "video"
    await update.effective_message.reply_text(
        "🎬 Напиши описание для генерации видео.\n\n"
        "⚠️ Видео работает только если у твоего API есть доступ к sora-2."
    )

async def set_mode_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    context.user_data["mode"] = "ai"
    await update.effective_message.reply_text(
        "🤖 Я ИИ помощник. Напиши, что нужно:\n"
        "— промт под твой стиль\n"
        "— сценарий Reels\n"
        "— улучшение описания/хуков\n"
        "— идеи для видео/фото"
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    u = update.effective_user
    ensure_user(u)

    if txt == "✅ Проверить Instagram":
        await update.effective_message.reply_text(
            "Подпишись на Instagram и нажми кнопку ниже, чтобы отправить заявку.\n"
            "В заявке укажи свой @ник и (по возможности) скрин подписки.\n\n"
            f"Instagram: {INSTAGRAM_URL}",
            reply_markup=instagram_gate_keyboard(),
        )
        return

    if txt == "🎁 Пригласить друга":
        if not await require_access(update, context):
            return
        deep = f"https://t.me/{BOT_USERNAME}?start=ref_{u.id}"
        await update.effective_message.reply_text(
            "Вот твоя ссылка-приглашение (и кнопка для шаринга):\n\n"
            f"{deep}",
            reply_markup=share_keyboard(u.id),
        )
        return

    if txt == "ℹ️ Помощь":
        await help_cmd(update, context)
        return

    if txt == "⭐️ VIP / Подписка":
        if not await require_access(update, context):
            return
        await update.effective_message.reply_text(
            "⭐️ VIP / Подписка\n\n"
            f"VIP даёт до {VIP_DAILY_LIMIT} генераций в день на {VIP_DURATION_DAYS} дней.\n"
            "Пока выдача VIP вручную (я отмечаю VIP в базе).\n\n"
            "Если хочешь — добавим оплату звёздами/ЮKassa отдельным шагом.",
            reply_markup=main_menu()
        )
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
        tasks = [
            "День 1: Сделай 3 варианта одного портрета (разный свет).",
            "День 2: Сделай один и тот же кадр в 3 ракурсах (close/mid/full).",
            "День 3: Отработай кожу: поры/текстура/без пластика.",
            "День 4: Снег/частицы: реалистичный snowfall и bokeh.",
            "День 5: Outfit-замена без изменения лица.",
        ]
        day_idx = int(time.time() // 86400) % len(tasks)
        await update.effective_message.reply_text(
            f"📆 Челлендж:\n\n{tasks[day_idx]}\n\nХочешь — добавлю все 30 дней и прогресс ✅",
            reply_markup=main_menu()
        )
        return

    if txt == "🤖 ИИ помощник":
        await set_mode_ai(update, context)
        return

    # If awaiting IG info
    if context.user_data.get("await_ig_info"):
        # store IG handle text
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, txt, "text info received", now_utc().isoformat())
            )
            conn.commit()

        context.user_data["await_ig_info"] = False
        await update.effective_message.reply_text(
            "✅ Принято! Я проверю и открою доступ.\n\n"
            "Если нужно быстрее — напиши мне в личку: “проверь IG в боте”.",
            reply_markup=main_menu()
        )

        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка: user_id={u.id}, username=@{u.username}\n"
                    f"Текст: {txt}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    # Free-form actions require access
    if not await require_access(update, context):
        return

    mode = context.user_data.get("mode")

    if mode == "ai":
        await update.effective_message.reply_text("⏳ Думаю…")
        text, err = await asyncio.to_thread(openai_text_answer, txt)
        if err:
            await update.effective_message.reply_text(err, reply_markup=main_menu())
        else:
            await update.effective_message.reply_text(text, reply_markup=main_menu())
        context.user_data["mode"] = None
        return

    if mode in ("image", "video") and txt:
        row = get_user(u.id)
        ok, msg = can_use_generation(row)
        if not ok:
            await update.effective_message.reply_text("⛔️ " + msg, reply_markup=main_menu())
            context.user_data["mode"] = None
            return

        if mode == "image":
            await update.effective_message.reply_text("⏳ Генерирую фото…")
            img, err = await asyncio.to_thread(openai_generate_image, txt)
            if err:
                await update.effective_message.reply_text(err, reply_markup=main_menu())
            else:
                consume_generation(row)
                await update.effective_message.reply_photo(photo=img, caption="Готово ✅", reply_markup=main_menu())
            context.user_data["mode"] = None
            return

        if mode == "video":
            await update.effective_message.reply_text("⏳ Запускаю генерацию видео… (это может занять 1–3 минуты)")
            # run in background to avoid webhook timeouts
            asyncio.create_task(sora_video_worker(update.effective_chat.id, u.id, txt, row))
            context.user_data["mode"] = None
            return

    await update.effective_message.reply_text(
        "Напиши, что хочешь сделать, или выбери кнопку из меню 👇",
        reply_markup=main_menu()
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    u = update.effective_user
    ensure_user(u)

    if query.data == "check_channel":
        ok = await is_subscribed_to_channel(context.bot, u.id)
        if ok:
            await query.edit_message_text("✅ Канал подтверждён! Теперь подтвердим Instagram 👇",
                                          reply_markup=instagram_gate_keyboard())
        else:
            await query.edit_message_text(
                "Пока не вижу подписку 😔\n\nПодпишись и нажми «проверить» ещё раз.",
                reply_markup=channel_gate_keyboard()
            )
        return

    if query.data == "ig_request":
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, "", "requested via button", now_utc().isoformat())
            )
            conn.commit()

        await query.edit_message_text(
            "✅ Заявка создана.\n\n"
            "Отправь мне одним сообщением:\n"
            "1) твой @ник в Instagram\n"
            "2) (желательно) скрин, где видно что ты подписан(а)\n\n"
            "После подтверждения бот откроется полностью."
        )
        context.user_data["await_ig_info"] = True
        return

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)

    if context.user_data.get("await_ig_info"):
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, "", "photo proof received", now_utc().isoformat())
            )
            conn.commit()

        context.user_data["await_ig_info"] = False

        await update.effective_message.reply_text(
            "✅ Принято! Я подтвержу и открою доступ.\n\n"
            "Если нужно быстрее — напиши мне в личку: “проверь IG в боте”.",
            reply_markup=main_menu()
        )

        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка (фото): user_id={u.id}, username=@{u.username}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    if not await require_access(update, context):
        return

    await update.effective_message.reply_text(
        "Фото получил ✅\n"
        "Сейчас генерация работает по тексту. Если хочешь режим “по фото” — скажи, добавлю.",
        reply_markup=main_menu()
    )

# -------------------- ADMIN --------------------
async def ig_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /ig_ok <user_id>")
        return
    uid = int(context.args[0])
    with db() as conn:
        conn.execute("UPDATE users SET ig_verified=1 WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM ig_requests WHERE user_id=?", (uid,))
        conn.commit()
    await update.message.reply_text(f"✅ IG подтвержден для {uid}")
    try:
        await context.bot.send_message(uid, "✅ Instagram подтвержден! Доступ открыт 🎉", reply_markup=main_menu())
    except Exception:
        pass

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
    try:
        await context.bot.send_message(uid, "❌ Не получилось подтвердить Instagram. Пришли заявку ещё раз.")
    except Exception:
        pass

async def vip_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    if len(context.args) < 1:
        await update.message.reply_text("Формат: /vip_add <user_id> [days]")
        return
    uid = int(context.args[0])
    days = int(context.args[1]) if len(context.args) > 1 else VIP_DURATION_DAYS
    until = now_utc() + timedelta(days=days)
    with db() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until.isoformat(), uid))
        conn.commit()
    await update.message.reply_text(f"⭐️ VIP выдан для {uid} до {until.isoformat()}")
    try:
        await context.bot.send_message(uid, f"⭐️ VIP активирован до {until.isoformat()} 🎉", reply_markup=main_menu())
    except Exception:
        pass


# -------------------- FASTAPI ROUTES --------------------
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


# -------------------- STARTUP/SHUTDOWN --------------------
@app.on_event("startup")
async def on_startup():
    global tg_app, BOT_USERNAME

    init_db()

    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("help", help_cmd))

    tg_app.add_handler(CommandHandler("ig_ok", ig_ok))
    tg_app.add_handler(CommandHandler("ig_no", ig_no))
    tg_app.add_handler(CommandHandler("vip_add", vip_add))

    tg_app.add_handler(MessageHandler(filters.Regex(r"^🖼 Сгенерировать фото$"), set_mode_image))
    tg_app.add_handler(MessageHandler(filters.Regex(r"^🎬 Сгенерировать видео$"), set_mode_video))
    tg_app.add_handler(MessageHandler(filters.Regex(r"^🤖 ИИ помощник$"), set_mode_ai))

    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await tg_app.initialize()
    await tg_app.start()

    me = await tg_app.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot username: %s", BOT_USERNAME)

    if WEBHOOK_BASE:
        url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH
        await tg_app.bot.set_webhook(url)
        log.info("Webhook set: %s", url)
    else:
        log.warning("WEBHOOK_URL/WEBHOOK_BASE/RENDER_EXTERNAL_URL not set. Webhook NOT configured.")

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
