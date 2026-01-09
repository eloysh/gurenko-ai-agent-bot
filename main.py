import os
import io
import base64
import sqlite3
import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

from fastapi import FastAPI, Request, Response
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

# Текстовая модель (ответы агента)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Модели генерации
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
OPENAI_VIDEO_MODEL = os.getenv("OPENAI_VIDEO_MODEL", "sora")  # см. доступность в аккаунте

TG_CHANNEL = os.getenv("TG_CHANNEL", "@gurenko_kristina_ai")
TZ_NAME = os.getenv("TZ", "Asia/Tokyo")

# Лимиты вопросов
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "3"))

# Лимиты генераций (фото/видео)
GEN_FREE_DAILY = int(os.getenv("GEN_FREE_DAILY", "1"))   # 1 в день бесплатно (фото ИЛИ видео)
GEN_VIP_DAILY = int(os.getenv("GEN_VIP_DAILY", "9999"))  # VIP лимит (или оставь 9999)

# VIP
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
Если вопрос про Reels — начинай с 'Хук / первые 2 секунды / формат / текст на экране / CTA'.
"""

AUDIT_PROMPT = """Ты — эксперт по Reels/Shorts.
Пользователь даёт ссылку/описание ролика и нишу: нейросети, оживление фото/видео, промты.
Нужно:
1) Сильный хук на 1–2 секунды (3 варианта)
2) Текст на экране (коротко)
3) Монтаж: кадры/темп/длина
4) CTA (комментарий/директ/телеграм)
5) 5 хештегов без спама
Пиши по делу, без морали и воды.
"""

# ============================
# PROMPT OF DAY + CHALLENGE 30
# ============================
PROMPTS_OF_DAY = [
    "Ультра-реализм, натуральная кожа, без куклы: мягкий ключевой свет + лёгкий контровой, 50mm, shallow DOF. Негатив: no wax skin, no over-smoothing, no face morph.",
    "Видео из 1 фото (identity lock): микродвижения, естественное моргание, дыхание, лёгкий поворот головы 5°, реалистичный motion blur.",
    "Тренд: ‘глянец’ — fashion-editorial, чистый фон, студийный свет, high-end ретушь без пластика, текстура кожи сохранена.",
    "Ночной город + снег: кинематографичный контраст, отражения, влажный асфальт, лёгкая плёнка, без пересветов.",
    "‘Сделай как у Кристины’: хук + промт + настройки + подпись + 5 тегов (всё в одном сообщении).",
]

CHALLENGE_30 = [
    ("День 1 — Реалистичная кожа", "Сделай портрет без ‘куклы’: текстура кожи, поры, естественные тени.", "Результат: 1 фото до/после + промт."),
    ("День 2 — Свет", "Сравни 3 схемы света: мягкий, контровой, ‘окно’ (тепло).", "Результат: 3 варианта одного кадра."),
    ("День 3 — Камера/оптика", "Сделай 35mm vs 50mm vs 85mm (ощущение лица).", "Результат: 3 кадра + вывод."),
    ("День 4 — Анти-искажения", "Собери свой негатив-промт (anti-wax, anti-face-morph).", "Результат: шаблон негатива."),
    ("День 5 — Full body пропорции", "Сделай полный рост без ‘ломаных’ рук/ног.", "Результат: 1 удачный шаблон."),
    ("День 6 — Стиль ‘Зима-глянец’", "Снежный fashion-editorial без перебора фильтров.", "Результат: 1 обложка."),
    ("День 7 — Видео 4 сек", "Сделай видео из 1 фото: моргание/дыхание/микромимика.", "Результат: 1 короткий клип."),
    ("День 8 — Текст на экране", "Напиши 5 коротких фраз-хуков под твой стиль.", "Результат: список из 5."),
    ("День 9 — Монтаж", "Собери ролик: 0–2с хук, 2–6с процесс, 6–9с результат, 9–12с CTA.", "Результат: структура."),
    ("День 10 — Сторителлинг", "Сделай ролик ‘до → проблема → после’.", "Результат: сценарий 10–12с."),
    ("День 11 — Тёплый интерьер", "Тёплый свет, уют, натуральная кожа, реализм.", "Результат: 1 фото."),
    ("День 12 — Ночь/неон", "Ночь, неон, контраст, без шума/грязи.", "Результат: 1 фото."),
    ("День 13 — Мимика", "Сделай 3 эмоции без ‘чужого лица’.", "Результат: 3 кадра."),
    ("День 14 — Пакет промтов", "Собери 5 промтов под разные локации.", "Результат: пакет 5."),
    ("День 15 — Видео 8 сек", "Видео дольше: плавный поворот + шаг + взгляд.", "Результат: 1 видео."),
    ("День 16 — Говорящая голова", "Скрипт: 1 хук + 1 польза + CTA в Telegram.", "Результат: текст 15с."),
    ("День 17 — ‘Почему не залетело’", "Разбор 1 твоего ролика: хук/темп/CTA.", "Результат: чеклист правок."),
    ("День 18 — 5 CTA", "Сделай 5 CTA: коммент/директ/телега/сохранить/поделиться.", "Результат: 5 фраз."),
    ("День 19 — Обложки", "Сделай 3 обложки под один ролик.", "Результат: 3 варианта."),
    ("День 20 — 10 хуков", "Сгенерируй 10 хуков под нейросети/оживление.", "Результат: список 10."),
    ("День 21 — Сериал контента", "Придумай рубрику на 7 дней (одна тема).", "Результат: план 7 роликов."),
    ("День 22 — База промтов", "Добавь 5 промтов в свою базу (категории).", "Результат: 5 карточек."),
    ("День 23 — Видео ‘глянец’", "Сделай fashion-video: плавные движения, свет, кожа.", "Результат: 1 видео."),
    ("День 24 — Видео ‘улица’", "Улица/ветер/движение волос, без артефактов.", "Результат: 1 видео."),
    ("День 25 — Трендовый звук", "Подбери 1 звук и сделай ролик под него.", "Результат: сценарий под звук."),
    ("День 26 — Тест длин", "7с vs 12с vs 20с (что лучше).", "Результат: вывод."),
    ("День 27 — Пакет ‘Зима’", "Собери 10 зимних промтов (разные сцены).", "Результат: пакет 10."),
    ("День 28 — ‘Сделай как у меня’", "Сделай шаблон: хук→промт→настройки→теги.", "Результат: 1 шаблон."),
    ("День 29 — Продающее описание", "Описание профиля + оффер 1 строкой.", "Результат: 3 варианта."),
    ("День 30 — Итог", "Собери лучший ролик месяца + CTA в бот.", "Результат: готовый текст поста."),
]

def prompt_of_day_text() -> str:
    idx = date.today().toordinal() % len(PROMPTS_OF_DAY)
    return PROMPTS_OF_DAY[idx]

# ============================
# DB (SQLite)
# ============================
DB_PATH = "data.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_column(cur, table: str, column: str, ddl: str):
    cur.execute(f"PRAGMA table_info({table})")
    cols = {r[1] for r in cur.fetchall()}
    if column not in cols:
        cur.execute(ddl)

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

        gen_used_today INTEGER DEFAULT 0,
        gen_last_reset TEXT,

        vip_until TEXT,

        challenge_day INTEGER DEFAULT 0,
        challenge_last_date TEXT
    )
    """)
    # миграции (если у тебя старая таблица)
    ensure_column(cur, "users", "gen_used_today", "ALTER TABLE users ADD COLUMN gen_used_today INTEGER DEFAULT 0")
    ensure_column(cur, "users", "gen_last_reset", "ALTER TABLE users ADD COLUMN gen_last_reset TEXT")
    ensure_column(cur, "users", "challenge_day", "ALTER TABLE users ADD COLUMN challenge_day INTEGER DEFAULT 0")
    ensure_column(cur, "users", "challenge_last_date", "ALTER TABLE users ADD COLUMN challenge_last_date TEXT")

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

def upsert_user(tg_id: int, username: Optional[str]):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
    exists = cur.fetchone() is not None
    today = datetime.now(tz).date().isoformat()
    if not exists:
        cur.execute(
            "INSERT INTO users (tg_id, username, last_reset, gen_last_reset) VALUES (?, ?, ?, ?)",
            (tg_id, username or "", today, today)
        )
    else:
        cur.execute("UPDATE users SET username=? WHERE tg_id=?", (username or "", tg_id))
        # заполним reset поля если пустые
        cur.execute("UPDATE users SET last_reset=COALESCE(last_reset, ?), gen_last_reset=COALESCE(gen_last_reset, ?) WHERE tg_id=?",
                    (today, today, tg_id))
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

def reset_ask_if_needed(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT used_today, last_reset FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return
    today = datetime.now(tz).date().isoformat()
    if r["last_reset"] != today:
        cur.execute("UPDATE users SET used_today=0, last_reset=? WHERE tg_id=?", (today, tg_id))
        conn.commit()
    conn.close()

def reset_gen_if_needed(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT gen_used_today, gen_last_reset FROM users WHERE tg_id=?", (tg_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return
    today = datetime.now(tz).date().isoformat()
    if r["gen_last_reset"] != today:
        cur.execute("UPDATE users SET gen_used_today=0, gen_last_reset=? WHERE tg_id=?", (today, tg_id))
        conn.commit()
    conn.close()

def inc_ask(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET used_today = used_today + 1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

def inc_gen(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET gen_used_today = gen_used_today + 1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    conn.close()

def set_vip(tg_id: int, days: int):
    until = (datetime.now(tz) + timedelta(days=days)).isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET vip_until=? WHERE tg_id=?", (until, tg_id))
    conn.commit()
    conn.close()

def set_challenge_start(tg_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET challenge_day=1, challenge_last_date=? WHERE tg_id=?",
                (datetime.now(tz).date().isoformat(), tg_id))
    conn.commit()
    conn.close()

def advance_challenge(tg_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    row = get_user(tg_id)
    day = int(row["challenge_day"] or 0)
    next_day = min(day + 1, 30)
    cur.execute("UPDATE users SET challenge_day=?, challenge_last_date=? WHERE tg_id=?",
                (next_day, datetime.now(tz).date().isoformat(), tg_id))
    conn.commit()
    conn.close()
    return next_day

def log_payment(tg_id: int, charge_id: str, payload: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments(tg_id, telegram_payment_charge_id, payload, created_at) VALUES (?,?,?,?)",
        (tg_id, charge_id, payload, datetime.now(tz).isoformat())
    )
    conn.commit()
    conn.close()

def seed_prompts_if_empty():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM prompts")
    c = cur.fetchone()["c"]
    if c == 0:
        samples = [
            ("Оживление фото", "Лицо 1:1 (без куклы)", "УЛЬТРА-реалистично, натуральная текстура кожи, без beauty-фильтров. Сохранить личность 1:1: не менять форму лица/глаз/носа/губ, не взрослить. Свет: мягкий ключ + лёгкий контровой, оптика 50mm, shallow DOF. Негатив: no face morph, no wax skin, no over-smoothing."),
            ("Видео (Sora)", "Видео 4 сек из 1 фото", "4s, vertical 1080x1920. Identity locked. Subtle head turn 5°, natural blink, micro-expressions, breathing. Realistic motion blur, no distortions."),
            ("HeyGen", "Говорящая голова (15 сек)", "Тон: дружелюбно-уверенно, лёгкая улыбка. Скрипт: 1 хук + 1 польза + CTA в Telegram."),
            ("Reels", "Хук + сценарий", "Хук 1–2с → процесс 2–6с → результат 6–9с → CTA 9–12с. Текст на экране: 5–7 слов."),
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

# ============================
# OpenAI client
# ============================
oai = OpenAI(api_key=OPENAI_API_KEY)

async def ask_openai(question: str, system: str = SYSTEM_PROMPT) -> str:
    def _call():
        return oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            temperature=0.7,
        )
    try:
        resp = await asyncio.to_thread(_call)
        text = resp.choices[0].message.content or ""
        return text.strip() or "Пустой ответ. Попробуй переформулировать запрос."
    except Exception as e:
        return f"⚠️ Ошибка GPT: {type(e).__name__}. Проверь Render → Logs."

async def gen_image(prompt: str) -> Optional[bytes]:
    def _call():
        # Images API
        res = oai.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
        )
        b64 = res.data[0].b64_json
        return base64.b64decode(b64)
    try:
        return await asyncio.to_thread(_call)
    except Exception:
        return None

async def gen_video(prompt: str) -> Optional[bytes]:
    def _call():
        # Video API (Sora)
        # seconds обычно: 4/8/12; size например 1080x1920
        v = oai.videos.create(
            model=OPENAI_VIDEO_MODEL,
            prompt=prompt,
            seconds=4,
            size="1080x1920",
        )
        content = oai.videos.content(v.id)
        # content — бинарь mp4
        return content
    try:
        return await asyncio.to_thread(_call)
    except Exception:
        return None

# ============================
# Telegram UI (keyboards)
# ============================
def kb_subscribe():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton("📌 Что умеет бот", callback_data="about")],
    ])

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Сгенерировать ФОТО (1/день)", callback_data="gen_photo"),
         InlineKeyboardButton("🎥 Сгенерировать ВИДЕО (1/день)", callback_data="gen_video")],
        [InlineKeyboardButton("🎁 Промт дня", callback_data="prompt_day"),
         InlineKeyboardButton("🔥 Челлендж 30 дней", callback_data="challenge")],
        [InlineKeyboardButton("📉 Разбор Reels ‘почему не залетело’", callback_data="audit")],
        [InlineKeyboardButton("🎬 База промтов", callback_data="prompts")],
        [InlineKeyboardButton("🧠 Задать вопрос AI-агенту", callback_data="ask"),
         InlineKeyboardButton("⭐ VIP без лимитов", callback_data="vip")],
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

def kb_challenge_controls():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово → следующий день", callback_data="challenge_done")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
    ])

# ============================
# Helpers
# ============================
async def safe_edit(query, text: str, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        # фикс “Message is not modified”
        if "Message is not modified" in str(e):
            return
        raise

async def is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=TG_CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

async def require_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    ok = await is_subscribed(update, context)
    if ok:
        return True
    text = f"Для доступа подпишись на канал {TG_CHANNEL} и нажми «Проверить подписку»."
    if update.message:
        await update.message.reply_text(text, reply_markup=kb_subscribe())
    elif update.callback_query:
        await safe_edit(update.callback_query, text, reply_markup=kb_subscribe())
    return False

def gen_limit_for_user(row) -> int:
    return GEN_VIP_DAILY if is_vip(row) else GEN_FREE_DAILY

# ============================
# Commands
# ============================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username)
    text = (
        "Привет! Я AI-бот Кристины 🤍\n\n"
        "Я умею:\n"
        "• Генерировать ФОТО и ВИДЕО (Sora) с лимитами\n"
        "• Давать ‘Промт дня’\n"
        "• Вести челлендж 30 дней\n"
        "• Разбирать Reels ‘почему не залетело’\n"
        "• Давать базу промтов и отвечать как AI-агент\n\n"
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

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — запуск\n"
        "/menu — меню\n"
        "/prompts — база промтов\n"
        "/ask — задать вопрос\n"
        "/vip — VIP\n",
        reply_markup=kb_main()
    )

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
        f"VIP снимает лимиты и открывает максимум функций.\n"
        f"Срок: {VIP_DAYS} дней\n"
        f"Цена: {VIP_PRICE_STARS} Stars",
        reply_markup=kb_vip_buy()
    )

# ============================
# Background generation tasks
# ============================
async def _send_image_task(chat_id: int, prompt: str, context: ContextTypes.DEFAULT_TYPE):
    img = await gen_image(prompt)
    if not img:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Не удалось сгенерировать фото. Проверь модель/доступ/лимиты API.", reply_markup=kb_main())
        return
    bio = io.BytesIO(img)
    bio.name = "image.png"
    await context.bot.send_photo(chat_id=chat_id, photo=bio, caption="Готово 🤍", reply_markup=kb_main())

async def _send_video_task(chat_id: int, prompt: str, context: ContextTypes.DEFAULT_TYPE):
    vid = await gen_video(prompt)
    if not vid:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Не удалось сгенерировать видео. Проверь модель Sora/доступ/лимиты API.", reply_markup=kb_main())
        return
    bio = io.BytesIO(vid)
    bio.name = "video.mp4"
    await context.bot.send_video(chat_id=chat_id, video=bio, caption="Готово 🤍", reply_markup=kb_main())

# ============================
# Callbacks + Payments
# ============================
async def cbq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    upsert_user(u.id, u.username)
    data = query.data

    if data == "about":
        await safe_edit(query,
            "Я умею:\n"
            "• Генерировать фото/видео (с лимитами)\n"
            "• Промт дня\n"
            "• Челлендж 30 дней\n"
            "• Разбор Reels\n"
            "• База промтов\n"
            "• AI-ответы как ChatGPT\n\n"
            "⚠️ Для проверки подписки бот должен быть админом канала.",
            reply_markup=kb_subscribe()
        )
        return

    if data == "check_sub":
        ok = await is_subscribed(update, context)
        if ok:
            set_mode(u.id, "menu")
            await safe_edit(query, "Доступ открыт ✅ Выбирай:", reply_markup=kb_main())
        else:
            await safe_edit(query,
                "Пока не вижу подписку 😕\n\n"
                f"1) Подпишись на {TG_CHANNEL}\n"
                "2) Вернись и нажми «Проверить подписку»\n\n"
                "⚠️ Если подписка есть, но не проходит — добавь бота админом в канал.",
                reply_markup=kb_subscribe()
            )
        return

    # gate
    if not await require_sub(update, context):
        return

    if data == "menu":
        set_mode(u.id, "menu")
        await safe_edit(query, "Меню:", reply_markup=kb_main())
        return

    if data == "prompts":
        await safe_edit(query, "Выбери категорию промтов:", reply_markup=kb_categories())
        return

    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        await safe_edit(query, f"Категория: {cat}", reply_markup=kb_prompt_list(cat))
        return

    if data.startswith("p:"):
        pid = int(data.split(":", 1)[1])
        p = get_prompt(pid)
        if not p:
            await safe_edit(query, "Промт не найден.", reply_markup=kb_back_main())
            return
        await safe_edit(query,
            f"<b>{p['title']}</b>\n\n<code>{p['body']}</code>",
            reply_markup=kb_back_main(),
            parse_mode=ParseMode.HTML
        )
        return

    if data == "ask":
        set_mode(u.id, "ask")
        await safe_edit(query,
            f"Ок ✅ Напиши свой вопрос одним сообщением.\n\nЛимит бесплатно: {DAILY_LIMIT}/день (VIP — без лимитов).",
            reply_markup=kb_back_main()
        )
        return

    if data == "audit":
        set_mode(u.id, "audit")
        await safe_edit(query,
            "Скинь ссылку на Reels (или коротко опиши ролик).\n\nЯ разберу: хук, текст на экране, монтаж, CTA и теги.",
            reply_markup=kb_back_main()
        )
        return

    if data == "prompt_day":
        txt = prompt_of_day_text()
        await safe_edit(query,
            f"🎁 <b>Промт дня</b>\n\n<code>{txt}</code>\n\nХочешь — нажми меню и попроси адаптацию под твой стиль.",
            reply_markup=kb_back_main(),
            parse_mode=ParseMode.HTML
        )
        return

    if data == "challenge":
        row = get_user(u.id)
        day = int(row["challenge_day"] or 0)
        if day == 0:
            set_challenge_start(u.id)
            day = 1
        title, task, deliver = CHALLENGE_30[day-1]
        await safe_edit(query,
            f"🔥 <b>{title}</b>\n\n• Задание: {task}\n• Что прислать себе: {deliver}\n\nНажми «Готово», когда сделаешь.",
            reply_markup=kb_challenge_controls(),
            parse_mode=ParseMode.HTML
        )
        return

    if data == "challenge_done":
        row = get_user(u.id)
        day = int(row["challenge_day"] or 0)
        if day <= 0:
            set_challenge_start(u.id)
            day = 1
        if day >= 30:
            await safe_edit(query,
                "🏁 Челлендж завершён! Хочешь — начнём заново или соберём твой ‘пакет лучших промтов’.",
                reply_markup=kb_back_main()
            )
            return
        next_day = advance_challenge(u.id)
        title, task, deliver = CHALLENGE_30[next_day-1]
        await safe_edit(query,
            f"🔥 <b>{title}</b>\n\n• Задание: {task}\n• Что прислать себе: {deliver}\n\nНажми «Готово», когда сделаешь.",
            reply_markup=kb_challenge_controls(),
            parse_mode=ParseMode.HTML
        )
        return

    if data == "gen_photo":
        set_mode(u.id, "gen_photo")
        await safe_edit(query,
            "🖼️ Ок! Напиши одним сообщением, что генерируем.\n\n"
            "Пример: ‘ультра-реалистичный портрет, тёплый свет, текстура кожи, без куклы’.",
            reply_markup=kb_back_main()
        )
        return

    if data == "gen_video":
        set_mode(u.id, "gen_video")
        await safe_edit(query,
            "🎥 Ок! Напиши одним сообщением, что должно быть в видео.\n\n"
            "Пример: ‘девушка в зимнем образе, лёгкий поворот головы, моргание, снег, реализм, без искажений’.",
            reply_markup=kb_back_main()
        )
        return

    if data == "vip":
        await safe_edit(query,
            f"VIP снимает лимиты.\nСрок: {VIP_DAYS} дней\nЦена: {VIP_PRICE_STARS} Stars",
            reply_markup=kb_vip_buy()
        )
        return

    if data == "buy_vip":
        payload = f"vip_{u.id}_{int(datetime.now(tz).timestamp())}"
        prices = [LabeledPrice(label=f"VIP {VIP_DAYS} дней", amount=VIP_PRICE_STARS)]
        await context.bot.send_invoice(
            chat_id=u.id,
            title="VIP-доступ",
            description=f"VIP на {VIP_DAYS} дней: без лимитов + максимум функций",
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
        f"Оплата прошла ✅ VIP активирован на {VIP_DAYS} дней!",
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

    row = get_user(u.id)
    mode = row["mode"] if row else "menu"

    text = (update.message.text or "").strip()
    if not text:
        return

    # --- ASK GPT ---
    if mode == "ask":
        reset_ask_if_needed(u.id)
        row = get_user(u.id)
        vip = is_vip(row)
        used = int(row["used_today"])

        if (not vip) and used >= DAILY_LIMIT:
            await update.message.reply_text(
                f"Лимит {DAILY_LIMIT}/день исчерпан 😕\n\n⭐ Хочешь без лимитов? Подключи VIP.",
                reply_markup=kb_vip_buy()
            )
            return

        await update.message.reply_text("Думаю… 🤍")
        answer = await ask_openai(text, system=SYSTEM_PROMPT)
        if not vip:
            inc_ask(u.id)
        await update.message.reply_text(answer, reply_markup=kb_main())
        return

    # --- AUDIT REELS ---
    if mode == "audit":
        await update.message.reply_text("Разбираю… 🤍")
        answer = await ask_openai(text, system=AUDIT_PROMPT)
        await update.message.reply_text(answer, reply_markup=kb_main())
        set_mode(u.id, "menu")
        return

    # --- GENERATION LIMIT ---
    if mode in ("gen_photo", "gen_video"):
        reset_gen_if_needed(u.id)
        row = get_user(u.id)
        vip = is_vip(row)
        used = int(row["gen_used_today"])
        limit = gen_limit_for_user(row)

        if used >= limit:
            await update.message.reply_text(
                f"Лимит генераций на сегодня исчерпан 😕\n\n"
                f"Бесплатно: {GEN_FREE_DAILY}/день (фото или видео)\n"
                f"VIP: больше лимит/безлимит",
                reply_markup=kb_vip_buy()
            )
            return

        # Увеличим счётчик сразу (чтобы не спамили кнопкой)
        inc_gen(u.id)

        if mode == "gen_photo":
            await update.message.reply_text("Запускаю генерацию фото… 🤍")
            # background
            context.application.create_task(_send_image_task(u.id, text, context))
            set_mode(u.id, "menu")
            return

        if mode == "gen_video":
            await update.message.reply_text("Запускаю генерацию видео… 🤍")
            context.application.create_task(_send_video_task(u.id, text, context))
            set_mode(u.id, "menu")
            return

    # default fallback
    await update.message.reply_text("Выбери действие в меню:", reply_markup=kb_main())

# ============================
# FastAPI + Webhook
# ============================
app = FastAPI()
application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("menu", menu_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("prompts", prompts_cmd))
application.add_handler(CommandHandler("ask", ask_cmd))
application.add_handler(CommandHandler("vip", vip_cmd))

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

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.get("/webhook")
async def webhook_info():
    return {"ok": True, "note": "Webhook accepts POST from Telegram only."}
