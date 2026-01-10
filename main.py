import os
import base64
import sqlite3
import logging
import time
import asyncio
from datetime import datetime, timedelta, date
from urllib.parse import quote
from typing import Optional, Tuple

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


# -------------------- LOGGING --------------------
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
    for k in keys:
        v = os.getenv(k)
        if v is None:
            continue
        s = str(v).strip()
        if s == "":
            continue
        try:
            return int(s)
        except Exception:
            pass
    return default

def env_bool(*keys: str, default: bool = False) -> bool:
    for k in keys:
        v = os.getenv(k)
        if v is None:
            continue
        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    return default


# -------------------- CONFIG --------------------
TELEGRAM_TOKEN = env_str("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", default="")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_BOT_TOKEN is not set")

# Webhook base url (Render): accept multiple keys
WEBHOOK_BASE = env_str("WEBHOOK_URL", "WEBHOOK_BASE", "RENDER_EXTERNAL_URL", default="").rstrip("/")
WEBHOOK_PATH = "/webhook"

# Channel gating
REQUIRED_CHANNEL = env_str("REQUIRED_CHANNEL", "TG_CHANNEL", default="@gurenko_kristina_ai")
CHANNEL_INVITE_URL = env_str("CHANNEL_INVITE_URL", default="https://t.me/gurenko_kristina_ai")
STRICT_CHANNEL_CHECK = env_bool("STRICT_CHANNEL_CHECK", default=True)

# Instagram gating (manual / pseudo-auto)
INSTAGRAM_URL = env_str("INSTAGRAM_URL", default="https://www.instagram.com/gurenko_kristina/")
AUTO_IG_VERIFY = env_bool("AUTO_IG_VERIFY", default=False)  # если 1 — открывает доступ после отправки @ника/скрина (НЕ реальная авто-проверка!)

# Admin
ADMIN_USER_ID = env_int("ADMIN_USER_ID", default=0)

# Limits (поддержка твоих старых ключей)
FREE_DAILY_LIMIT = env_int("FREE_DAILY_LIMIT", "GEN_FREE_DAILY", default=1)
VIP_DAILY_LIMIT = env_int("VIP_DAILY_LIMIT", "DAILY_LIMIT", default=30)
VIP_DURATION_DAYS = env_int("VIP_DURATION_DAYS", "VIP_DAYS", default=30)

# Stars price
VIP_PRICE_STARS = env_int("VIP_PRICE_STARS", default=299)

# OpenAI
OPENAI_API_KEY = env_str("OPENAI_API_KEY", default="")
OPENAI_IMAGE_MODEL = env_str("OPENAI_IMAGE_MODEL", "IMAGE_MODEL", default="gpt-image-1")
OPENAI_MODEL = env_str("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_VIDEO_MODEL = env_str("OPENAI_VIDEO_MODEL", default="sora-2")

# DB
DB_PATH = env_str("DB_PATH", default="bot.db")


# -------------------- APP/STATE --------------------
app = FastAPI()
tg_app: Optional[Application] = None
BOT_USERNAME: Optional[str] = None


# -------------------- DB --------------------
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

def now_utc() -> datetime:
    return datetime.utcnow()

def today_str() -> str:
    return date.today().isoformat()

def ensure_user(u):
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at, used_date, used_count) VALUES (?, ?, ?, ?, ?, ?)",
                (u.id, u.username or "", u.first_name or "", now_utc().isoformat(), today_str(), 0),
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
    if inviter_id == user_id:
        return
    with db() as conn:
        me = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not me:
            return
        if me["referred_by"]:
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

def vip_until_text(row) -> str:
    if not row or not row["vip_until"]:
        return "нет"
    return str(row["vip_until"]).replace("T", " ")

def reset_daily_if_needed(row):
    if not row:
        return
    td = today_str()
    if row["used_date"] != td:
        with db() as conn:
            conn.execute("UPDATE users SET used_date=?, used_count=0 WHERE user_id=?", (td, row["user_id"]))
            conn.commit()

def can_use_generation(row) -> Tuple[bool, str]:
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


# -------------------- UI --------------------
def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🤖 AI помощник")],
            [KeyboardButton("🖼 Сгенерировать фото"), KeyboardButton("🎬 Сгенерировать видео")],
            [KeyboardButton("🎁 Промт дня"), KeyboardButton("📆 Челлендж 30 дней")],
            [KeyboardButton("🎁 Пригласить друга"), KeyboardButton("⭐️ VIP / Подписка")],
            [KeyboardButton("✅ Проверить Instagram"), KeyboardButton("ℹ️ Помощь")],
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
        [InlineKeyboardButton("✅ Я подписался — отправить заявку", callback_data="ig_request")]
    ])

def vip_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐️ Купить VIP за {VIP_PRICE_STARS} Stars", callback_data="buy_vip")]
    ])


# -------------------- ACCESS GATES --------------------
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
        # Если STRICT_CHANNEL_CHECK выключен — пропускаем даже при ошибке доступа
        return (not STRICT_CHANNEL_CHECK)

async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    True -> можно продолжать
    False -> показываем гейты и стопаем действие
    """
    u = update.effective_user
    ensure_user(u)

    # 1) Channel gate
    ok = await is_subscribed_to_channel(context.bot, u.id)
    if not ok:
        text = (
            "🔒 Чтобы пользоваться ботом, нужно быть подписанным на мой Telegram-канал.\n\n"
            "Нажми «Подписаться», потом «Я подписался — проверить»."
        )
        await update.effective_message.reply_text(text, reply_markup=channel_gate_keyboard())
        return False

    # 2) Instagram gate (manual)
    row = get_user(u.id)
    if int(row["ig_verified"] or 0) != 1:
        text = (
            "🔒 Ещё один шаг: подтверждение подписки на Instagram.\n\n"
            "⚠️ Instagram не даёт надёжную авто-проверку подписки через Telegram-бота.\n"
            "Поэтому ты отправляешь заявку, а я подтверждаю — и бот открывается полностью.\n\n"
            "Нажми кнопку ниже 👇"
        )
        await update.effective_message.reply_text(text, reply_markup=instagram_gate_keyboard())
        return False

    return True


# -------------------- OPENAI HELPERS --------------------
def get_openai_client():
    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return None
    return OpenAI(api_key=OPENAI_API_KEY)

def openai_generate_image(prompt: str) -> Tuple[Optional[bytes], Optional[str]]:
    client = get_openai_client()
    if not client:
        return None, "OpenAI API не настроен (нет OPENAI_API_KEY)."

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
        img = base64.b64decode(b64)
        return img, None
    except Exception as e:
        s = str(e)
        if "403" in s or "Forbidden" in s:
            return None, (
                "⛔️ OpenAI вернул 403 Forbidden.\n\n"
                "Обычно это значит: нет биллинга/лимитов на API, нет доступа к модели, "
                "или ключ не из того проекта.\n"
                "Проверь Billing/Usage limits в аккаунте OpenAI и что ключ API активный."
            )
        return None, f"Не удалось сгенерировать фото: {e}"

def openai_generate_video_stub(prompt: str) -> Tuple[None, str]:
    return None, (
        "🎬 Видео сейчас недоступно через API в этом деплое.\n\n"
        "Причины обычно такие:\n"
        "• у аккаунта API нет доступа к Sora-модели;\n"
        "• не включён billing/лимиты;\n"
        "• нужна отдельная реализация под video endpoint.\n\n"
        "Если хочешь — включу видео, когда подтвердим доступ Sora в API."
    )

def openai_chat(prompt: str) -> Tuple[Optional[str], Optional[str]]:
    client = get_openai_client()
    if not client:
        return None, "OpenAI API не настроен (нет OPENAI_API_KEY)."

    system = (
        "Ты — AI-помощник Кристины. Помогаешь делать промты для Sora/Meta AI/HeyGen/Suno, "
        "подбирать стили, сценарии Reels, улучшать реализм (кожа, свет, текстуры). "
        "Отвечай по-русски, структурно и по делу."
    )

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        text = getattr(resp, "output_text", None)
        return (text or "Ответ пустой. Попробуй иначе сформулировать."), None
    except Exception as e:
        s = str(e)
        if "403" in s or "Forbidden" in s:
            return None, (
                "⛔️ OpenAI вернул 403 Forbidden.\n"
                "Проверь, что в проекте включён billing/лимиты и ключ API действителен."
            )
        return None, f"AI-помощник не отвечает. Ошибка: {e}"


# -------------------- PAYMENTS (STARS) --------------------
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    if not q.invoice_payload.startswith("vip:"):
        await q.answer(ok=False, error_message="Неверный платёж. Попробуй ещё раз.")
        return
    await q.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    until = now_utc() + timedelta(days=VIP_DURATION_DAYS)
    with db() as conn:
        conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (until.isoformat(), uid))
        conn.commit()

    await update.effective_message.reply_text(
        f"⭐️ VIP активирован на {VIP_DURATION_DAYS} дней!\n"
        f"Действует до: {until.isoformat().replace('T',' ')}",
        reply_markup=main_menu()
    )


# -------------------- HANDLERS --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    u = update.effective_user
    ensure_user(u)

    # referral
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

    text = (
        "Привет! Я — бот Кристины 🤍\n\n"
        "Что умею:\n"
        "• 🤖 AI помощник — улучшаю промты/сценарии/стили\n"
        "• 🖼 Генерация фото (OpenAI Images API)\n"
        "• 🎬 Генерация видео (Sora) — если доступна в твоём API\n"
        "• 🎁 «Промт дня» и 📆 челлендж на 30 дней\n"
        "• 🎁 Рефералка: приглашай друзей и получай бонус-генерации\n\n"
        f"Лимит: бесплатно — {FREE_DAILY_LIMIT} генерация/день. VIP — до {VIP_DAILY_LIMIT}/день.\n"
        "Выбирай кнопку в меню 👇"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu())

    # Сразу проверим доступ (как ты просила: проверка канала/IG уже на старте)
    await require_access(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    row = get_user(update.effective_user.id)
    ok, msg = can_use_generation(row)
    text = (
        "ℹ️ Помощь\n\n"
        "🤖 AI помощник — вопросы про промты/стиль/сценарии.\n"
        "🖼 Сгенерировать фото — отправь текст-описание, получишь картинку.\n"
        "🎬 Сгенерировать видео — описание (если Sora доступна).\n"
        "🎁 Пригласить друга — ссылка, за друзей бонусы.\n\n"
        f"VIP: {'активен ✅' if is_vip(row) else 'нет ❌'}\n"
        f"VIP до: {vip_until_text(row)}\n"
        f"{msg}\n"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu())

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
        "⚠️ Видео работает только если у твоего API есть доступ к Sora."
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    u = update.effective_user
    ensure_user(u)

    # 0) Если ждём IG-данные — обработаем их тут (и не пойдём дальше)
    if context.user_data.get("await_ig_info"):
        ig_handle = ""
        # достанем @ник из текста
        for token in txt.replace("\n", " ").split():
            if token.startswith("@") and len(token) >= 2:
                ig_handle = token.strip()
                break

        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, ig_handle, "text proof received", now_utc().isoformat())
            )
            conn.commit()

        # Псевдо-автоверификация (по твоему AUTO_IG_VERIFY=1)
        if AUTO_IG_VERIFY:
            with db() as conn:
                conn.execute("UPDATE users SET ig_verified=1 WHERE user_id=?", (u.id,))
                conn.execute("DELETE FROM ig_requests WHERE user_id=?", (u.id,))
                conn.commit()
            context.user_data["await_ig_info"] = False
            await update.effective_message.reply_text(
                "✅ Принято! Доступ открыт 🎉",
                reply_markup=main_menu()
            )
            return

        context.user_data["await_ig_info"] = False
        await update.effective_message.reply_text(
            "✅ Принято! Я подтвержу и открою доступ.\n\n"
            "Если нужно быстрее — напиши мне в личку: “проверь IG в боте”.",
            reply_markup=main_menu()
        )

        # уведомим админа
        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    ADMIN_USER_ID,
                    f"IG-заявка: user_id={u.id}, username=@{u.username}\n"
                    f"IG: {ig_handle or '(не указан)'}\n"
                    f"Подтверди: /ig_ok {u.id}  |  Отклонить: /ig_no {u.id}"
                )
            except Exception:
                pass
        return

    # 1) Кнопки меню
    if txt == "✅ Проверить Instagram":
        await update.effective_message.reply_text(
            "Подпишись на Instagram и нажми кнопку ниже, чтобы отправить заявку.\n"
            "В заявке укажи свой @ник и (по возможности) прикрепи скрин подписки.\n\n"
            f"Instagram: {INSTAGRAM_URL}",
            reply_markup=instagram_gate_keyboard(),
        )
        return

    if txt == "🎁 Пригласить друга":
        if not await require_access(update, context):
            return
        await update.effective_message.reply_text(
            "Вот твоя ссылка-приглашение. Нажми «Поделиться», чтобы отправить друзьям:",
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
            f"VIP даёт до {VIP_DAILY_LIMIT} генераций в день на {VIP_DURATION_DAYS} дней.\n"
            f"Цена: {VIP_PRICE_STARS} Stars.\n\n"
            f"Твой VIP: {'активен ✅' if is_vip(row) else 'нет ❌'}\n"
            f"VIP до: {vip_until_text(row)}\n\n"
            "Нажми кнопку ниже, чтобы оплатить ⭐️"
        )
        await update.effective_message.reply_text(text, reply_markup=vip_keyboard())
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
        await update.effective_message.reply_text(
            f"🎁 Промт дня:\n\n{prompts[idx]}",
            reply_markup=main_menu()
        )
        return

    if txt == "📆 Челлендж 30 дней":
        if not await require_access(update, context):
            return
        tasks = [
            "День 1: Сделай 3 варианта одного портрета (разный свет).",
            "День 2: Сделай один и тот же кадр в 3 ракурсах (close/mid/full).",
            "День 3: Отработай наполнение кожи: поры/текстура/без пластика.",
            "День 4: Снег/частицы: реалистичный snowfall и bokeh.",
            "День 5: Outfit-замена без изменения лица.",
        ]
        day_idx = int(time.time() // 86400) % len(tasks)
        await update.effective_message.reply_text(
            f"📆 Челлендж:\n\n{tasks[day_idx]}\n\n"
            "Хочешь — добавлю все 30 дней и отмечание прогресса ✅",
            reply_markup=main_menu()
        )
        return

    if txt == "🤖 AI помощник":
        if not await require_access(update, context):
            return
        context.user_data["mode"] = "assistant"
        await update.effective_message.reply_text(
            "🤖 Напиши запрос.\n\n"
            "Примеры:\n"
            "— «Сделай промт для Sora: зимний fashion editorial, лицо 1:1, ультра-реализм»\n"
            "— «Улучши мой промт, чтобы кожа была натуральной без пластика»"
        )
        return

    # 2) Свободный ввод (режимы)
    mode = context.user_data.get("mode")

    if mode == "assistant" and txt:
        if not await require_access(update, context):
            context.user_data["mode"] = None
            return
        context.user_data["mode"] = None
        await update.effective_message.reply_text("🤖 Думаю…")
        ans, err = await asyncio.to_thread(openai_chat, txt)
        if err:
            await update.effective_message.reply_text(err, reply_markup=main_menu())
            return
        for chunk in [ans[i:i+3500] for i in range(0, len(ans), 3500)]:
            await update.effective_message.reply_text(chunk)
        await update.effective_message.reply_text("Готово ✅", reply_markup=main_menu())
        return

    if mode in ("image", "video") and txt:
        if not await require_access(update, context):
            context.user_data["mode"] = None
            return

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
            # (видео лимит пока не списываем — включишь реальную генерацию, тогда списывать)
            await update.effective_message.reply_text(err, reply_markup=main_menu())

        context.user_data["mode"] = None
        return

    # 3) Хинт
    await update.effective_message.reply_text(
        "Выбери кнопку в меню 👇",
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

        context.user_data["await_ig_info"] = True

        # если включена псевдо-автоверификация — можно открыть сразу после заявки (по твоему желанию)
        if AUTO_IG_VERIFY:
            with db() as conn:
                conn.execute("UPDATE users SET ig_verified=1 WHERE user_id=?", (u.id,))
                conn.execute("DELETE FROM ig_requests WHERE user_id=?", (u.id,))
                conn.commit()
            context.user_data["await_ig_info"] = False
            await query.edit_message_text("✅ Принято! Доступ открыт 🎉")
            await context.bot.send_message(u.id, "Меню доступно ✅", reply_markup=main_menu())
            return

        await query.edit_message_text(
            "✅ Заявка создана.\n\n"
            "Отправь одним сообщением:\n"
            "1) твой @ник в Instagram\n"
            "2) (желательно) скрин, где видно что ты подписан(а)\n\n"
            "После подтверждения бот откроется полностью."
        )
        return

    if query.data == "buy_vip":
        if not await require_access(update, context):
            return

        payload = f"vip:{u.id}:{int(time.time())}"
        prices = [LabeledPrice(label=f"VIP на {VIP_DURATION_DAYS} дней", amount=VIP_PRICE_STARS)]

        try:
            await context.bot.send_invoice(
                chat_id=u.id,
                title="VIP-доступ ⭐️",
                description=f"VIP на {VIP_DURATION_DAYS} дней: до {VIP_DAILY_LIMIT} генераций/день.",
                payload=payload,
                currency="XTR",      # Telegram Stars
                prices=prices,
                provider_token="",   # для Stars пусто
            )
        except Exception as e:
            await context.bot.send_message(u.id, f"Не удалось выставить счёт Stars. Ошибка: {e}", reply_markup=main_menu())
        return

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)

    # если ждём IG-доказательство
    if context.user_data.get("await_ig_info"):
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ig_requests (user_id, ig_handle, note, created_at) VALUES (?, ?, ?, ?)",
                (u.id, "", "photo proof received", now_utc().isoformat())
            )
            conn.commit()

        # псевдо-автоверификация по флажку
        if AUTO_IG_VERIFY:
            with db() as conn:
                conn.execute("UPDATE users SET ig_verified=1 WHERE user_id=?", (u.id,))
                conn.execute("DELETE FROM ig_requests WHERE user_id=?", (u.id,))
                conn.commit()
            context.user_data["await_ig_info"] = False
            await update.effective_message.reply_text("✅ Принято! Доступ открыт 🎉", reply_markup=main_menu())
            return

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

    # остальное — только если есть доступ
    if not await require_access(update, context):
        return

    await update.effective_message.reply_text(
        "Фото получил ✅\n"
        "Сейчас бот генерирует по тексту. Если хочешь режим “по фото” — скажи, добавлю.",
        reply_markup=main_menu()
    )

# -------------------- ADMIN COMMANDS --------------------
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
        await context.bot.send_message(uid, "❌ Не получилось подтвердить Instagram. Пришли заявку ещё раз.", reply_markup=main_menu())
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

@app.head("/", response_class=PlainTextResponse)
async def head_root():
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

    # commands
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("help", help_cmd))

    # admin
    tg_app.add_handler(CommandHandler("ig_ok", ig_ok))
    tg_app.add_handler(CommandHandler("ig_no", ig_no))
    tg_app.add_handler(CommandHandler("vip_add", vip_add))

    # menu modes
    tg_app.add_handler(MessageHandler(filters.Regex(r"^🖼 Сгенерировать фото$"), set_mode_image))
    tg_app.add_handler(MessageHandler(filters.Regex(r"^🎬 Сгенерировать видео$"), set_mode_video))

    # callbacks
    tg_app.add_handler(CallbackQueryHandler(on_button))

    # payments (Stars)
    tg_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # content
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
        log.warning("WEBHOOK_BASE/WEBHOOK_URL/RENDER_EXTERNAL_URL not set. Webhook NOT configured.")

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
