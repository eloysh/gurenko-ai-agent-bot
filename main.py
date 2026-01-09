import os
import sqlite3
import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

from fastapi import FastAPI, Request
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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

from openai import OpenAI

# ============================
# CONFIG (env vars)
# ============================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
TG_CHANNEL = os.getenv("TG_CHANNEL", "@gurenko_kristina_ai")
TZ_NAME = os.getenv("TZ", "Asia/Tokyo")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "3"))
VIP_DAYS = int(os.getenv("VIP_DAYS", "30"))
VIP_PRICE_STARS = int(os.getenv("VIP_PRICE_STARS", "299"))
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")

tz = ZoneInfo(TZ_NAME)

SYSTEM_PROMPT = """Ты — AI-агент Кристины.
Тема: нейросети для реалистичных фото/видео (Sora/HeyGen/Meta AI), промты, сценарии Reels.
Отвечай коротко, по шагам, без воды.
Если нужно — дай 1-2 примера промтов.
Если вопрос про Reels — начинай с 'Хук/первые 2 секунды/формат/текст на экране'.
"""

# ============================
# PROMPT OF THE DAY (7 days loop)
# ============================
DAILY_PACK = [
    {
        "title": "День 1 — Анти-кукла (реалистичная кожа)",
        "prompt": "Ultra-realistic close-up portrait, natural skin texture with pores and micro-details, subtle imperfections, realistic highlights, no beauty retouch. Identity locked to reference 1:1 (do not change facial structure). Soft cinematic lighting, 50mm, shallow DOF, 8K.",
        "negative": "no smoothing, no wax skin, no doll face, no plastic skin, no enlarged eyes, no AI glamour, no face morph",
        "tip": "Свет у окна + не завышай sharpness/clarity (иначе пластик).",
    },
    {
        "title": "День 2 — Sora: видео 10 сек из 1 фото",
        "prompt": "Cinematic 4K video, 9:16, 10 seconds. Identity locked 1:1 to the reference. Subtle head turn 5°, natural blink, micro-expressions, gentle breathing, slight hair movement from soft wind. Film grain, realistic motion blur.",
        "negative": "no face morph, no jitter, no warping, no uncanny smile, no extra fingers, no distorted eyes",
        "tip": "Движение делай микро — так меньше искажений.",
    },
    {
        "title": "День 3 — Дорогой глянец (fashion-editorial)",
        "prompt": "High-end fashion editorial portrait, clean studio background, softbox key light + gentle rim light, crisp detail, natural skin texture, luxury look, neutral grading, 85mm lens, f/2.0, 8K. Identity unchanged 1:1.",
        "negative": "no glossy plastic skin, no overcontrast, no oversharpen, no heavy beauty filter",
        "tip": "Нейтральный цвет + мягкий свет = «дорого».",
    },
    {
        "title": "День 4 — Снег без CGI",
        "prompt": "Ultra realistic winter portrait outdoors, gentle snowfall, snow crystals on hair and jacket, cold breath visible, natural skin texture preserved, cinematic lighting, realistic shadows, 8K. Identity locked 1:1.",
        "negative": "no fake snow overlay, no CGI snow, no blur face, no skin smoothing, no face morph",
        "tip": "Пиши ‘gentle snowfall’, не ‘heavy particles’.",
    },
    {
        "title": "День 5 — Кино-кадр (тёплый интерьер)",
        "prompt": "Cinematic portrait, warm amber practical lights in background (bokeh), soft key light, realistic skin pores, subtle film grain, 35mm lens, f/1.8, 8K, identity unchanged 1:1.",
        "negative": "no orange skin, no harsh HDR, no beauty filter, no wax skin",
        "tip": "Bokeh на фоне делает кадр «как кино».",
    },
    {
        "title": "День 6 — 3 ракурса, одно лицо (1:1)",
        "prompt": "Create three ultra-realistic portraits of the same person with identity preserved 1:1: (1) front, (2) 3/4, (3) profile. Keep facial proportions identical, consistent hairstyle, natural skin texture. Cinematic soft lighting, 8K.",
        "negative": "no identity drift, no different person, no age change, no face morph, no doll face",
        "tip": "Обязательно добавляй ‘same person’ + запрет identity drift.",
    },
    {
        "title": "День 7 — Reels упаковка (под залёт)",
        "prompt": "Сценарий 10 сек: 0–1с «Это 1 промт», 1–3с до/после, 3–6с «убираем куклу (negative)», 6–8с «пиши СНЕГ в бота», 8–10с CTA «подпишись на канал».",
        "negative": "",
        "tip": "Текст на экране крупно (3–5 слов), первые 2 секунды — хук.",
    },
]

def get_daily_item():
    today = datetime.now(tz).date()
    idx = today.toordinal() % len(DAILY_PACK)
    return DAILY_PACK[idx]

# ============================
# DB (SQLite)
# ============================
DB_PATH = "data.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        mode TEXT DEFAULT 'menu',
        used_today INTEGER DEFAULT 0,
        last_reset TEXT,
        vip_until TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL,
        telegram_payment_charge_id TEXT,
        payload TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def upsert_user(tg_id: int, username: str | None):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute(
            "INSERT INTO users (tg_id, username, last_reset) VALUES (?, ?, ?)",
            (tg_id, username or "", date.today().isoformat())
        )
    else:
        cur.execute(
            "UPDATE users SET username=? WHERE tg_id=?",
            (username or "", tg_id)
        )
    conn.commit()
    conn.close()

def get_user(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row

def set_mode(tg_id: int, mode: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET mode=? WHERE tg_id=?", (mode, tg_id))
    conn.commit()
    conn.close()

def is_vip(row) -> bool:
    if not row:
        return False
    vu = row["vip_until"]
    if not vu:
        return False
    try:
        return datetime.fromisoformat(vu).replace(tzinfo=tz) > datetime.now(tz)
    except Exception:
        return False

def reset_if_needed(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT used_today, last_reset FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return
    last_reset = r["last_reset"]
    today = datetime.now(tz).date().isoformat()
    if last_reset != today:
        cur.execute(
            "UPDATE users SET used_today=0, last_reset=? WHERE tg_id=?",
            (today, tg_id)
        )
        conn.commit()
    conn.close()

def inc_usage(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET used_today = used_today + 1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

def set_vip(tg_id: int, days: int):
    until = (datetime.now(tz) + timedelta(days=days)).isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET vip_until=? WHERE tg_id=?", (until, tg_id))
    conn.commit()
    conn.close()

def seed_prompts_if_empty():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM prompts")
    c = cur.fetchone()["c"]
    if c == 0:
        samples = [
            ("Оживление фото", "Лицо 1:1 (без куклы)", "УЛЬТРА-реалистично, натуральная текстура кожи, без beauty-фильтров. Сохранить личность 1:1: не менять форму лица/глаз/носа/губ, не взрослить. Мягкий ключевой свет + лёгкий контровой, реалистичная оптика 50mm, shallow DOF. Негатив: no face morph, no wax skin, no over-smoothing."),
            ("Sora", "Видео из 1 фото (10 сек)", "Cinematic 4K, 9:16, 10s. Subtle head turn 5°, natural blink, micro-expressions, breathing. Identity locked to reference. Soft film grain, realistic motion blur, no distortion."),
            ("HeyGen", "Говорящая голова (15 сек)", "Friendly confident tone, slight smile. Clean studio lighting, natural skin texture, no over-sharpen. Script: 1 хук + 1 польза + CTA в Telegram."),
            ("Suno", "Вирусный хук (12–18 сек)", "Modern pop/edm hook, 124 bpm, punchy drums, catchy topline, Russian lyrics, 1 hook line repeated. No kids choir."),
            ("Reels-хуки", "3 хука на выбор", "1) 'Смотри, это сделано из 1 фото…' 2) 'Почему у всех лицо кукла — и как исправить' 3) 'Хочешь промт? Напиши ПРОМТ'"),
        ]
        cur.executemany("INSERT INTO prompts(category,title,body) VALUES (?,?,?)", samples)
        conn.commit()
    conn.close()

def list_categories():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT category FROM prompts ORDER BY category")
    cats = [r["category"] for r in cur.fetchall()]
    conn.close()
    return cats

def list_prompts(category: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,title FROM prompts WHERE category=? ORDER BY id", (category,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_prompt(pid: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM prompts WHERE id=?", (pid,))
    r = cur.fetchone()
    conn.close()
    return r

def log_payment(tg_id: int, charge_id: str, payload: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments(tg_id, telegram_payment_charge_id, payload, created_at) VALUES (?,?,?,?)",
        (tg_id, charge_id, payload, datetime.now(tz).isoformat())
    )
    conn.commit()
    conn.close()

# ============================
# OpenAI
# ============================
oai = OpenAI(api_key=OPENAI_API_KEY, timeout=30, max_retries=2)

async def ask_openai(question: str) -> str:
    def _call():
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            temperature=0.7,
        )

    try:
        resp = await asyncio.to_thread(_call)
        text = resp.choices[0].message.content or ""
        return text.strip() or "Пустой ответ. Попробуй переформулировать запрос."
    except Exception as e:
        print("OpenAI error:", repr(e))
        return "⚠️ Сейчас не получилось получить ответ от GPT. Попробуй ещё раз через минуту."

# ============================
# Telegram UI
# ============================
def kb_subscribe():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton("👀 Показать пример результата", callback_data="sample")],
        [InlineKeyboardButton("📌 Что умеет бот", callback_data="about")],
    ])

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Промт дня", callback_data="daily")],
        [InlineKeyboardButton("🎬 База промтов", callback_data="prompts")],
        [InlineKeyboardButton("🧠 Задать вопрос AI-агенту", callback_data="ask")],
        [InlineKeyboardButton("📣 Поделиться ботом", callback_data="share")],
        [InlineKeyboardButton("⭐ VIP без лимитов", callback_data="vip")],
    ])

def kb_back_main():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="menu")]])

def kb_categories():
    cats = list_categories()
    rows = [[InlineKeyboardButton(c, callback_data=f"cat:{c}")] for c in cats]
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)

def kb_prompt_list(category: str):
    items = list_prompts(category)
    rows = [[InlineKeyboardButton(r["title"], callback_data=f"p:{r['id']}")] for r in items]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="prompts")])
    return InlineKeyboardMarkup(rows)

def kb_vip_buy():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ Купить VIP на {VIP_DAYS} дней — {VIP_PRICE_STARS} Stars", callback_data="buy_vip")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

# ============================
# Helpers
# ============================
async def is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=TG_CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except BadRequest:
        return False
    except Exception:
        return False

async def require_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    ok = await is_subscribed(update, context)
    if ok:
        return True
    if update.message:
        await update.message.reply_text(
            f"Для доступа подпишись на канал {TG_CHANNEL} и нажми «Проверить подписку».",
            reply_markup=kb_subscribe()
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            f"Для доступа подпишись на канал {TG_CHANNEL} и нажми «Проверить подписку».",
            reply_markup=kb_subscribe()
        )
    return False

# ============================
# Commands
# ============================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    text = (
        "Привет! Я AI-бот Кристины 🤍\n\n"
        "Здесь — промты и гайды по нейросетям (Sora/HeyGen/Meta AI) + ответы как ChatGPT.\n"
        "🎁 Есть «Промт дня».\n\n"
        f"✅ Чтобы открыть доступ — подпишись на канал: {TG_CHANNEL}\n"
        "Нажми «Проверить подписку»."
    )
    await update.message.reply_text(text, reply_markup=kb_subscribe())

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    set_mode(u.id, "menu")
    await update.message.reply_text("Меню:", reply_markup=kb_main())

async def prompts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    await update.message.reply_text("Выбери категорию промтов:", reply_markup=kb_categories())

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    set_mode(u.id, "ask")
    await update.message.reply_text(
        f"Ок ✅ Напиши свой вопрос одним сообщением.\n\nЛимит бесплатно: {DAILY_LIMIT}/день (VIP — без лимитов).",
        reply_markup=kb_back_main()
    )

async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    if not await require_sub(update, context):
        return
    await update.message.reply_text(
        f"VIP снимает лимиты и открывает быстрые шаблоны.\n"
        f"Срок: {VIP_DAYS} дней\n"
        f"Цена: {VIP_PRICE_STARS} Stars",
        reply_markup=kb_vip_buy()
    )

async def paysupport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Поддержка по оплатам ⭐\n"
        "Если платеж прошёл, но VIP не включился — напиши сюда:\n"
        "• свой @username\n"
        "• время оплаты\n"
        "• скрин чека Stars\n\n"
        "Мы проверим и включим доступ.",
        reply_markup=kb_main()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — запуск и доступ\n"
        "/menu — меню\n"
        "/prompts — база промтов\n"
        "/ask — задать вопрос\n"
        "/vip — VIP без лимитов\n"
        "/paysupport — поддержка по оплатам",
        reply_markup=kb_main()
    )

# ============================
# Callbacks + Payments
# ============================
async def cbq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    upsert_user(u.id, u.username)

    data = query.data

    # доступны даже без подписки
    if data == "about":
        await query.edit_message_text(
            "Я умею:\n"
            "• Проверять подписку на канал\n"
            "• Давать «Промт дня»\n"
            "• Выдавать базу промтов по кнопкам\n"
            "• Отвечать как AI-агент (с лимитом)\n"
            "• VIP без лимитов через Telegram Stars",
            reply_markup=kb_subscribe()
        )
        return

    if data == "sample":
        await query.edit_message_text(
            "👀 Пример результата (как выглядит ответ подписчикам):\n\n"
            "<b>PROMPT:</b>\n"
            "<code>Ультра-реалистичный портрет, натуральная текстура кожи (видны поры/микродетали), "
            "без пластика и сглаживания. Личность 1:1, не менять форму лица/глаз/носа/губ. "
            "Свет: мягкий key + лёгкий rim, 50mm, f/1.8, 8K.</code>\n\n"
            "<b>NEGATIVE:</b>\n"
            "<code>no face morph, no wax skin, no over-smoothing, no doll face, no beauty filter.</code>\n\n"
            f"✅ Чтобы открыть всё меню и «Промт дня» — подпишись на {TG_CHANNEL} и нажми «Проверить подписку».",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_subscribe()
        )
        return

    if data == "check_sub":
        ok = await is_subscribed(update, context)
        if ok:
            set_mode(u.id, "menu")
            await query.edit_message_text("Доступ открыт ✅ Выбирай:", reply_markup=kb_main())
        else:
            await query.edit_message_text(
                "Пока не вижу подписку 😕\n\n"
                f"1) Подпишись на {TG_CHANNEL}\n"
                "2) Вернись и нажми «Проверить подписку»\n\n"
                "⚠️ Если подписка есть, но не проходит — добавь бота админом в канал.",
                reply_markup=kb_subscribe()
            )
        return

    # gate: subscription required for everything else
    if not await require_sub(update, context):
        return

    if data == "menu":
        set_mode(u.id, "menu")
        await query.edit_message_text("Меню:", reply_markup=kb_main())
        return

    if data == "daily":
        item = get_daily_item()
        text = f"<b>{item['title']}</b>\n\n<b>PROMPT:</b>\n<code>{item['prompt']}</code>"
        if item["negative"]:
            text += f"\n\n<b>NEGATIVE:</b>\n<code>{item['negative']}</code>"
        text += f"\n\n<b>Подсказка:</b> {item['tip']}\n\n🔑 Хочешь секретный промт? Напиши мне: <b>СНЕГ</b>"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back_main())
        return

    if data == "share":
        share_text = "Я пользуюсь AI-ботом Кристины: промты Sora/HeyGen/Meta AI + Промт дня 🤍"
        bot_link = "https://t.me/gurenko_ai_agent_bot"
        share_link = f"https://t.me/share/url?url={quote(bot_link)}&text={quote(share_text)}"
        await query.edit_message_text(
            "📣 Поделиться ботом:\nНажми кнопку ниже и отправь друзьям.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Поделиться", url=share_link)],
                [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
            ])
        )
        return

    if data == "prompts":
        await query.edit_message_text("Выбери категорию промтов:", reply_markup=kb_categories())
        return

    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        await query.edit_message_text(f"Категория: {cat}", reply_markup=kb_prompt_list(cat))
        return

    if data.startswith("p:"):
        pid = int(data.split(":", 1)[1])
        p = get_prompt(pid)
        if not p:
            await query.edit_message_text("Промт не найден.", reply_markup=kb_back_main())
            return
        await query.edit_message_text(
            f"<b>{p['title']}</b>\n\n<code>{p['body']}</code>",
            reply_markup=kb_back_main(),
            parse_mode=ParseMode.HTML
        )
        return

    if data == "ask":
        set_mode(u.id, "ask")
        await query.edit_message_text(
            f"Ок ✅ Напиши свой вопрос одним сообщением.\n\nЛимит бесплатно: {DAILY_LIMIT}/день (VIP — без лимитов).",
            reply_markup=kb_back_main()
        )
        return

    if data == "vip":
        await query.edit_message_text(
            f"VIP снимает лимиты и открывает быстрые шаблоны.\n"
            f"Срок: {VIP_DAYS} дней\n"
            f"Цена: {VIP_PRICE_STARS} Stars",
            reply_markup=kb_vip_buy()
        )
        return

    if data == "buy_vip":
        payload = f"vip_{u.id}_{int(datetime.now(tz).timestamp())}"
        prices = [LabeledPrice(label=f"VIP {VIP_DAYS} дней", amount=VIP_PRICE_STARS)]
        await context.bot.send_invoice(
            chat_id=u.id,
            title="VIP-доступ",
            description=f"VIP на {VIP_DAYS} дней: без лимитов + премиум промты",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
        )
        return

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    await q.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    sp = update.message.successful_payment
    log_payment(u.id, sp.telegram_payment_charge_id, sp.invoice_payload)
    set_vip(u.id, VIP_DAYS)
    await update.message.reply_text(
        f"Оплата прошла ✅ VIP активирован на {VIP_DAYS} дней!\n\n"
        "Можешь задавать вопросы без лимитов.",
        reply_markup=kb_main()
    )

# ============================
# Message handler
# ============================
async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)

    if not await require_sub(update, context):
        return

    txt = (update.message.text or "").strip()

    # Секретное слово из канала
    if txt.upper().startswith("СНЕГ"):
        if "2" in txt:
            await update.message.reply_text(
                "❄️ СНЕГ 2 — 3 варианта под ракурсы (строго 1:1):\n\n"
                "1) FRONT:\n"
                "<code>Ultra-realistic winter fashion portrait, front view, identity locked 1:1, natural skin pores, soft key+rim, 50mm f/1.8, 8K.</code>\n\n"
                "2) 3/4 (10°):\n"
                "<code>Same person, 3/4 view, slight head turn 10°, micro-expressions, natural skin texture, cinematic light, 8K. Identity unchanged.</code>\n\n"
                "3) PROFILE:\n"
                "<code>Same person, profile view, identical facial proportions, natural skin texture, soft cinematic lighting, 8K. No identity drift.</code>\n\n"
                "Нужно под конкретный инструмент? Напиши: Sora / Meta AI / HeyGen.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main()
            )
        else:
            await update.message.reply_text(
                "🎁 Секретный промт «СНЕГ»:\n\n"
                "<b>PROMPT:</b>\n"
                "<code>Ультра-реалистичный зимний fashion-editorial портрет, натуральная текстура кожи, без пластика. "
                "Сохранить личность 1:1 (не менять форму лица/глаз/носа/губ). Свет: мягкий key + rim, 50mm, f/1.8, 8K.</code>\n\n"
                "<b>NEGATIVE:</b>\n"
                "<code>no face morph, no wax skin, no over-smoothing, no doll face, no beauty filter, no identity drift.</code>\n\n"
                "Хочешь 3 варианта под ракурсы? Напиши: <b>СНЕГ 2</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main()
            )
        return

    row = get_user(u.id)
    mode = row["mode"] if row else "menu"

    # если не в режиме ask — показываем меню (не ломаем UX)
    if mode != "ask":
        await update.message.reply_text("Выбирай в меню 👇", reply_markup=kb_main())
        return

    reset_if_needed(u.id)
    row = get_user(u.id)
    vip = is_vip(row)
    used = int(row["used_today"])

    if (not vip) and used >= DAILY_LIMIT:
        await update.message.reply_text(
            f"Лимит {DAILY_LIMIT}/день исчерпан 😕\n\n"
            "⭐ Хочешь без лимитов? Подключи VIP.",
            reply_markup=kb_vip_buy()
        )
        return

    question = txt
    await update.message.reply_text("Думаю… 🤍")

    answer = await ask_openai(question)
    if not vip:
        inc_usage(u.id)
    await update.message.reply_text(answer, reply_markup=kb_main())

# ============================
# FastAPI + Webhook
# ============================
app = FastAPI()
application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("menu", menu_cmd))
application.add_handler(CommandHandler("prompts", prompts_cmd))
application.add_handler(CommandHandler("ask", ask_cmd))
application.add_handler(CommandHandler("vip", vip_cmd))
application.add_handler(CommandHandler("paysupport", paysupport_cmd))
application.add_handler(CommandHandler("help", help_cmd))

application.add_handler(CallbackQueryHandler(cbq))
application.add_handler(PreCheckoutQueryHandler(precheckout))
application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))

@app.on_event("startup")
async def on_startup():
    init_db()
    seed_prompts_if_empty()
    await application.initialize()
    await application.start()

    if WEBHOOK_BASE:
        webhook_url = f"{WEBHOOK_BASE}/webhook"
        await application.bot.set_webhook(webhook_url)
        print("Webhook set:", webhook_url)
    else:
        print("WEBHOOK_BASE is empty. Set it in hosting env and redeploy to enable webhook.")

@app.on_event("shutdown")
async def on_shutdown():
    await application.stop()
    await application.shutdown()

@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "ok"}
