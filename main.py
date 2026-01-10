import os
import re
import json
import time
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# OpenAI SDK
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gurenko-bot")

# -----------------------------
# ENV (accept aliases)
# -----------------------------
def getenv_any(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

TELEGRAM_TOKEN = getenv_any("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", default="")
WEBHOOK_BASE = getenv_any("WEBHOOK_BASE", "WEBHOOK_URL", "RENDER_EXTERNAL_URL", default="")
REQUIRED_CHANNEL = getenv_any("REQUIRED_CHANNEL", "TG_CHANNEL", default="@gurenko_kristina_ai")
CHANNEL_INVITE_URL = getenv_any("CHANNEL_INVITE_URL", default="https://t.me/gurenko_kristina_ai")

INSTAGRAM_URL = getenv_any("INSTAGRAM_URL", default="https://www.instagram.com/gurenko_kristina/")
AUTO_IG_VERIFY = getenv_any("AUTO_IG_VERIFY", default="0") == "1"
STRICT_CHANNEL_CHECK = getenv_any("STRICT_CHANNEL_CHECK", default="1") == "1"

TZ_NAME = getenv_any("TZ", default="Asia/Tokyo")
TZ = ZoneInfo(TZ_NAME)

ADMIN_USER_ID = int(getenv_any("ADMIN_USER_ID", default="0") or 0)

DAILY_LIMIT = int(getenv_any("DAILY_LIMIT", default="3") or 3)
GEN_FREE_DAILY = int(getenv_any("GEN_FREE_DAILY", default="1") or 1)

VIP_DAYS = int(getenv_any("VIP_DAYS", default="30") or 30)
VIP_PRICE_STARS = int(getenv_any("VIP_PRICE_STARS", default="299") or 299)

OPENAI_API_KEY = getenv_any("OPENAI_API_KEY", default="")
OPENAI_MODEL = getenv_any("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_IMAGE_MODEL = getenv_any("OPENAI_IMAGE_MODEL", "IMAGE_MODEL", default="gpt-image-1")

DB_PATH = getenv_any("DB_PATH", default="bot.db")

if not TELEGRAM_TOKEN:
    log.warning("TELEGRAM_BOT_TOKEN is empty. Bot will not work until set.")
if not WEBHOOK_BASE:
    log.warning("WEBHOOK_BASE is empty. Webhook setup will fail on startup.")


# -----------------------------
# DB (SQLite)
# -----------------------------
_db_lock = asyncio.Lock()

def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _db_init():
    conn = _db_connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        created_at TEXT,
        referred_by INTEGER,
        ref_rewarded INTEGER DEFAULT 0,

        ig_handle TEXT,
        ig_verified INTEGER DEFAULT 0,
        ig_verified_at TEXT,

        vip_until TEXT,
        bonus_credits INTEGER DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage (
        user_id INTEGER,
        day TEXT,
        used INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, day)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ig_requests (
        user_id INTEGER PRIMARY KEY,
        handle TEXT,
        last_file_id TEXT,
        status TEXT,
        created_at TEXT
    );
    """)

    conn.commit()
    conn.close()

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")

def today_key() -> str:
    return date.today().isoformat()

async def db_exec(query: str, params: Tuple = ()) -> None:
    async with _db_lock:
        conn = _db_connect()
        conn.execute(query, params)
        conn.commit()
        conn.close()

async def db_one(query: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
    async with _db_lock:
        conn = _db_connect()
        cur = conn.execute(query, params)
        row = cur.fetchone()
        conn.close()
        return row

async def db_ensure_user(user_id: int) -> None:
    row = await db_one("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if row:
        return
    await db_exec(
        "INSERT INTO users(user_id, created_at) VALUES(?, ?)",
        (user_id, now_iso())
    )

async def db_set_referred_by(user_id: int, ref_id: int) -> None:
    await db_ensure_user(user_id)
    row = await db_one("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
    if row and row["referred_by"]:
        return
    if ref_id == user_id:
        return
    await db_exec("UPDATE users SET referred_by=? WHERE user_id=?", (ref_id, user_id))

async def db_get_user(user_id: int) -> sqlite3.Row:
    await db_ensure_user(user_id)
    row = await db_one("SELECT * FROM users WHERE user_id=?", (user_id,))
    return row

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone(TZ)
    except Exception:
        return None

async def db_is_vip(user_id: int) -> bool:
    u = await db_get_user(user_id)
    dt = _parse_dt(u["vip_until"])
    return bool(dt and dt > datetime.now(TZ))

async def db_add_vip_days(user_id: int, days: int) -> None:
    u = await db_get_user(user_id)
    current = _parse_dt(u["vip_until"])
    base = current if current and current > datetime.now(TZ) else datetime.now(TZ)
    new_dt = base + timedelta(days=days)
    await db_exec("UPDATE users SET vip_until=? WHERE user_id=?", (new_dt.isoformat(timespec="seconds"), user_id))

async def db_get_usage(user_id: int) -> int:
    row = await db_one("SELECT used FROM usage WHERE user_id=? AND day=?", (user_id, today_key()))
    return int(row["used"]) if row else 0

async def db_inc_usage(user_id: int) -> None:
    used = await db_get_usage(user_id)
    if used == 0:
        await db_exec("INSERT OR REPLACE INTO usage(user_id, day, used) VALUES(?, ?, ?)", (user_id, today_key(), 1))
    else:
        await db_exec("UPDATE usage SET used=used+1 WHERE user_id=? AND day=?", (user_id, today_key()))

async def db_add_bonus_credits(user_id: int, n: int) -> None:
    await db_exec("UPDATE users SET bonus_credits=bonus_credits+? WHERE user_id=?", (n, user_id))

async def db_use_bonus_credit_if_any(user_id: int) -> bool:
    u = await db_get_user(user_id)
    if int(u["bonus_credits"]) > 0:
        await db_exec("UPDATE users SET bonus_credits=bonus_credits-1 WHERE user_id=?", (user_id,))
        return True
    return False

async def db_set_ig_info(user_id: int, handle: Optional[str], file_id: Optional[str], verified: bool) -> None:
    await db_ensure_user(user_id)
    if handle:
        await db_exec("UPDATE users SET ig_handle=? WHERE user_id=?", (handle, user_id))
        await db_exec("""
            INSERT INTO ig_requests(user_id, handle, last_file_id, status, created_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET handle=excluded.handle, last_file_id=excluded.last_file_id, status=excluded.status
        """, (user_id, handle, file_id or "", "received", now_iso()))
    else:
        # update only file_id
        await db_exec("""
            INSERT INTO ig_requests(user_id, handle, last_file_id, status, created_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_file_id=excluded.last_file_id, status=excluded.status
        """, (user_id, "", file_id or "", "received", now_iso()))

    if verified:
        await db_exec("UPDATE users SET ig_verified=1, ig_verified_at=? WHERE user_id=?", (now_iso(), user_id))
        await db_exec("UPDATE ig_requests SET status='approved' WHERE user_id=?", (user_id,))

async def db_set_ig_verified(user_id: int, verified: bool) -> None:
    if verified:
        await db_exec("UPDATE users SET ig_verified=1, ig_verified_at=? WHERE user_id=?", (now_iso(), user_id))
        await db_exec("UPDATE ig_requests SET status='approved' WHERE user_id=?", (user_id,))
    else:
        await db_exec("UPDATE users SET ig_verified=0 WHERE user_id=?", (user_id,))
        await db_exec("UPDATE ig_requests SET status='rejected' WHERE user_id=?", (user_id,))


async def maybe_reward_referral(user_id: int) -> None:
    """
    Reward referrer once, when user is fully unlocked (channel+IG).
    """
    u = await db_get_user(user_id)
    if int(u["ref_rewarded"]) == 1:
        return
    ref_id = u["referred_by"]
    if not ref_id:
        return

    # reward: +3 bonus generations
    await db_add_bonus_credits(int(ref_id), 3)
    await db_exec("UPDATE users SET ref_rewarded=1 WHERE user_id=?", (user_id,))


# -----------------------------
# Telegram UI
# -----------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 ИИ помощник", callback_data="ai_mode")],
        [InlineKeyboardButton("🖼 Сгенерировать фото", callback_data="gen_image")],
        [InlineKeyboardButton("⭐️ VIP / Подписка", callback_data="vip")],
        [InlineKeyboardButton("🎁 Пригласить друга (+бонус)", callback_data="ref")],
        [InlineKeyboardButton("📌 Инструкция / Канал", url=CHANNEL_INVITE_URL)],
    ])

def gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я подписалась на канал", callback_data="check_channel")],
        [InlineKeyboardButton("🔗 Открыть канал", url=CHANNEL_INVITE_URL)],
        [InlineKeyboardButton("✅ Подтвердить Instagram", callback_data="ig_start")],
        [InlineKeyboardButton("🔗 Открыть Instagram", url=INSTAGRAM_URL)],
    ])

def vip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ Купить VIP на {VIP_DAYS} дней — {VIP_PRICE_STARS} Stars", callback_data="buy_vip")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
    ])

def ig_admin_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"ig_approve:{user_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"ig_reject:{user_id}"),
        ]
    ])

async def safe_edit(query, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            # ignore
            return
        raise

# -----------------------------
# Channel check (FIXED)
# -----------------------------
async def is_subscribed_to_channel(bot, user_id: int) -> Tuple[bool, str]:
    """
    Returns (ok, reason). Robust across PTB versions.
    """
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        status = str(member.status).lower()  # 'member', 'administrator', 'creator'/'owner', 'restricted', 'left', 'kicked'
        if status in ("left", "kicked"):
            return False, f"status={status}"
        return True, f"status={status}"
    except Exception as e:
        log.warning("channel check failed: %s", e)
        if STRICT_CHANNEL_CHECK:
            return False, f"error={type(e).__name__}"
        # if not strict -> allow
        return True, "check_error_but_allowed"

async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    user_id = user.id
    await db_ensure_user(user_id)

    ok, _reason = await is_subscribed_to_channel(context.bot, user_id)
    u = await db_get_user(user_id)
    ig_ok = int(u["ig_verified"]) == 1

    if ok and ig_ok:
        # reward referral when first fully unlocked
        await maybe_reward_referral(user_id)
        return True

    txt = (
        "🔒 <b>Доступ закрыт</b>\n\n"
        "Чтобы открыть меню:\n"
        "1) Подпишись на Telegram-канал\n"
        f"2) Подтверди Instagram (ник + скрин)\n\n"
        f"Канал: {REQUIRED_CHANNEL}\n"
        f"Instagram: {INSTAGRAM_URL}\n"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query, txt, gate_kb())
    else:
        await update.effective_message.reply_text(txt, reply_markup=gate_kb(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    return False

# -----------------------------
# OpenAI helpers
# -----------------------------
def openai_client() -> Optional[OpenAI]:
    if not OPENAI_API_KEY:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)

async def openai_text(prompt: str) -> str:
    client = openai_client()
    if not client:
        return "OpenAI ключ не настроен. Добавь OPENAI_API_KEY в Render."
    def _run():
        r = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": "Ты полезный ИИ-помощник Кристины. Отвечай по делу, давай пошаговые инструкции, промты и идеи для контента."},
                {"role": "user", "content": prompt},
            ],
        )
        # responses api returns output_text convenience
        return getattr(r, "output_text", None) or (r.output[0].content[0].text if r.output else "…")
    return await asyncio.to_thread(_run)

async def openai_image(prompt: str) -> Tuple[bool, str]:
    client = openai_client()
    if not client:
        return False, "OpenAI ключ не настроен (OPENAI_API_KEY пустой)."
    def _run():
        img = client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
        )
        # return b64_json or url depending; here assume base64
        b64 = img.data[0].b64_json
        return b64
    try:
        b64 = await asyncio.to_thread(_run)
        return True, b64
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

# -----------------------------
# Handlers
# -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await db_ensure_user(user.id)

    # referral param
    if context.args:
        m = re.match(r"^ref_(\d+)$", context.args[0])
        if m:
            await db_set_referred_by(user.id, int(m.group(1)))

    # Show gate or menu
    if not await require_access(update, context):
        return

    await update.effective_message.reply_text(
        "✨ <b>Привет!</b>\n\nМеню открыто. Выбирай действие 👇",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML
    )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    await update.effective_message.reply_text("Меню 👇", reply_markup=main_menu_kb())

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await update.effective_message.reply_text(f"Твой numeric id: <code>{user.id}</code>", parse_mode=ParseMode.HTML)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mode", None)
    await update.effective_message.reply_text("Ок, вышли из режима. Меню 👇", reply_markup=main_menu_kb())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    await db_ensure_user(user_id)

    data = query.data or ""

    # IG admin actions
    if data.startswith("ig_approve:") or data.startswith("ig_reject:"):
        if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
            await query.answer("Нет доступа.", show_alert=True)
            return
        target = int(data.split(":")[1])
        if data.startswith("ig_approve:"):
            await db_set_ig_verified(target, True)
            await context.bot.send_message(chat_id=target, text="✅ Instagram подтверждён! Меню открыто 👇", reply_markup=main_menu_kb())
            await safe_edit(query, "✅ Одобрено.")
        else:
            await db_set_ig_verified(target, False)
            await context.bot.send_message(chat_id=target, text="❌ Instagram не подтверждён. Пришли ник и скрин ещё раз через меню.")
            await safe_edit(query, "❌ Отклонено.")
        return

    if data in ("menu",):
        if not await require_access(update, context):
            return
        await safe_edit(query, "Меню 👇", main_menu_kb())
        return

    if data == "check_channel":
        # just re-check and show correct screen
        if not await require_access(update, context):
            return
        await safe_edit(query, "✅ Канал ок. Меню 👇", main_menu_kb())
        return

    if data == "ig_start":
        # ask for IG
        await safe_edit(
            query,
            "Отправь <b>ник Instagram</b> (например: <code>gurenko_kristina</code>) и/или <b>скрин подписки</b>.\n\n"
            "После этого я открою меню (если включён AUTO_IG_VERIFY) или отправлю админу на подтверждение.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]])
        )
        context.user_data["await_ig"] = True
        return

    if data == "ai_mode":
        if not await require_access(update, context):
            return
        context.user_data["mode"] = "ai"
        await safe_edit(
            query,
            "🤖 <b>ИИ помощник включён</b>\n\nПиши вопрос сообщением — отвечу.\n\n"
            "Команды: /reset — выйти в меню.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="menu")]])
        )
        return

    if data == "gen_image":
        if not await require_access(update, context):
            return
        context.user_data["mode"] = "image"
        await safe_edit(
            query,
            "🖼 Напиши, какое фото сделать (1 сообщением).\n\n"
            "Пример: <i>«Ультра-реалистичный зимний fashion-портрет, 8K, мягкий свет, снежинки на волосах…»</i>\n\n"
            "Команды: /reset — выйти в меню.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="menu")]])
        )
        return

    if data == "vip":
        if not await require_access(update, context):
            return
        await safe_edit(query, "⭐️ <b>VIP</b>\n\nVIP снимает лимиты и даёт приоритет.", vip_kb())
        return

    if data == "buy_vip":
        if not await require_access(update, context):
            return

        # Telegram Stars invoice: currency XTR, provider_token can be empty string for Stars
        prices = [LabeledPrice(label=f"VIP на {VIP_DAYS} дней", amount=VIP_PRICE_STARS)]
        payload = f"vip:{user_id}:{int(time.time())}"

        try:
            await context.bot.send_invoice(
                chat_id=user_id,
                title=f"VIP на {VIP_DAYS} дней",
                description="Открывает VIP-доступ в боте.",
                payload=payload,
                provider_token="",      # Stars
                currency="XTR",
                prices=prices,
            )
        except Exception as e:
            await query.answer("Не удалось выставить счёт. Проверь оплату Stars в боте.", show_alert=True)
            log.exception("send_invoice failed: %s", e)
        return

    if data == "ref":
        if not await require_access(update, context):
            return
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        await safe_edit(
            query,
            "🎁 <b>Бонус за друга</b>\n\n"
            "Отправь другу эту ссылку. Когда он подпишется на канал и подтвердит Instagram — тебе начислится <b>+3</b> генерации.\n\n"
            f"<code>{link}</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="menu")]])
        )
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await db_ensure_user(user.id)

    # If waiting IG info
    if context.user_data.get("await_ig"):
        txt = (update.effective_message.text or "").strip()
        handle = None
        if txt:
            handle = txt.lstrip("@").strip()
        verified = AUTO_IG_VERIFY

        await db_set_ig_info(user.id, handle, None, verified=verified)

        # notify admin if not auto
        if not AUTO_IG_VERIFY and ADMIN_USER_ID:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"🧾 <b>IG запрос</b>\nuser_id: <code>{user.id}</code>\nhandle: <code>{handle or ''}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=ig_admin_kb(user.id),
            )

        context.user_data["await_ig"] = False

        # show menu or gate
        if await require_access(update, context):
            await update.effective_message.reply_text("✅ Принято. Меню 👇", reply_markup=main_menu_kb())
        else:
            await update.effective_message.reply_text("✅ Принято. Теперь подпишись на канал и/или дождись подтверждения.", reply_markup=gate_kb())
        return

    # Mode handlers
    mode = context.user_data.get("mode")

    if mode == "ai":
        if not await require_access(update, context):
            return
        q = update.effective_message.text or ""
        msg = await update.effective_message.reply_text("Думаю…")
        ans = await openai_text(q)
        try:
            await msg.edit_text(ans)
        except Exception:
            await update.effective_message.reply_text(ans)
        return

    if mode == "image":
        if not await require_access(update, context):
            return

        is_vip = await db_is_vip(user.id)
        used = await db_get_usage(user.id)

        # allow bonus credits first
        if not is_vip:
            used_bonus = await db_use_bonus_credit_if_any(user.id)
            if not used_bonus:
                if used >= DAILY_LIMIT:
                    await update.effective_message.reply_text(
                        f"Лимит на сегодня исчерпан: {DAILY_LIMIT}/{DAILY_LIMIT}.\n"
                        "Можно купить VIP ⭐️ или пригласить друга 🎁",
                        reply_markup=main_menu_kb()
                    )
                    return
                await db_inc_usage(user.id)

        prompt = update.effective_message.text or ""
        wait = await update.effective_message.reply_text("🖼 Генерирую…")

        ok, result = await openai_image(prompt)
        if not ok:
            await wait.edit_text(
                "Не удалось сгенерировать фото.\n\n"
                f"Ошибка: <code>{result}</code>\n\n"
                "Проверь:\n"
                "1) OPENAI_API_KEY (активен)\n"
                "2) есть доступ к images/model\n"
                f"3) OPENAI_IMAGE_MODEL={OPENAI_IMAGE_MODEL}",
                parse_mode=ParseMode.HTML
            )
            return

        # send base64 as photo
        import base64
        data = base64.b64decode(result)
        await context.bot.send_photo(chat_id=user.id, photo=data, caption="Готово ✅")
        try:
            await wait.delete()
        except Exception:
            pass
        return

    # default
    if not await require_access(update, context):
        return
    await update.effective_message.reply_text("Выбери действие 👇", reply_markup=main_menu_kb())


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await db_ensure_user(user.id)

    # IG screenshot support
    if context.user_data.get("await_ig"):
        photos = update.effective_message.photo or []
        file_id = photos[-1].file_id if photos else None
        verified = AUTO_IG_VERIFY
        await db_set_ig_info(user.id, handle=None, file_id=file_id, verified=verified)

        if not AUTO_IG_VERIFY and ADMIN_USER_ID:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"🧾 <b>IG запрос (скрин)</b>\nuser_id: <code>{user.id}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=ig_admin_kb(user.id),
            )

        context.user_data["await_ig"] = False

        if await require_access(update, context):
            await update.effective_message.reply_text("✅ Скрин принят. Меню 👇", reply_markup=main_menu_kb())
        else:
            await update.effective_message.reply_text("✅ Скрин принят. Подпишись на канал и/или дождись подтверждения.", reply_markup=gate_kb())
        return

    # ignore other photos
    if not await require_access(update, context):
        return
    await update.effective_message.reply_text("Фото получено. Если это скрин IG — нажми «Подтвердить Instagram» в меню.", reply_markup=main_menu_kb())


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        await query.answer(ok=True)
    except Exception:
        pass

async def on_success_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    await db_add_vip_days(user.id, VIP_DAYS)
    await msg.reply_text(f"✅ Оплата прошла! VIP активирован на {VIP_DAYS} дней ⭐️", reply_markup=main_menu_kb())

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error: %s", context.error)

# -----------------------------
# FastAPI webhook app
# -----------------------------
app = FastAPI()
tg_app: Optional[Application] = None

@app.get("/")
async def root():
    return {"ok": True}

@app.head("/")
async def root_head():
    return

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def startup():
    global tg_app
    _db_init()

    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("menu", cmd_menu))
    tg_app.add_handler(CommandHandler("myid", cmd_myid))
    tg_app.add_handler(CommandHandler("reset", cmd_reset))

    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(PreCheckoutQueryHandler(precheckout))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_success_payment))

    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    tg_app.add_error_handler(on_error)

    await tg_app.initialize()
    await tg_app.start()

    # set webhook
    if WEBHOOK_BASE:
        url = f"{WEBHOOK_BASE.rstrip('/')}/webhook"
        await tg_app.bot.set_webhook(url=url)
        me = await tg_app.bot.get_me()
        log.info("Bot username: %s", me.username)
        log.info("Webhook set: %s", url)

@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        await tg_app.stop()
        await tg_app.shutdown()
