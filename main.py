import os, re, json, base64, sqlite3, logging, time
from typing import Optional, Tuple, List
from fastapi import FastAPI, Request, HTTPException
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import BadRequest
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gurenko-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")  # https://....onrender.com
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "")  # @channelusername or -100...
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID", "")  # channel id for collecting prompts
DISCUSSION_GROUP_ID = os.getenv("DISCUSSION_GROUP_ID", "")  # group id for comments
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")

DB_PATH = os.getenv("DB_PATH", "bot.db")

OK_STATUSES = {"creator", "administrator", "member"}

client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()
tg_app: Optional[Application] = None


# ---------------- DB ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at INTEGER,
        is_subscribed INTEGER DEFAULT 1,
        vip_until INTEGER DEFAULT 0,
        inviter_id INTEGER DEFAULT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS prompts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_chat_id TEXT,
        source_message_id INTEGER,
        origin TEXT,               -- channel | comment | manual
        title TEXT,
        body TEXT,
        tags TEXT,
        tool TEXT,
        created_at INTEGER
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS favorites(
        user_id INTEGER,
        prompt_id INTEGER,
        created_at INTEGER,
        PRIMARY KEY(user_id, prompt_id)
    )
    """)
    conn.commit()
    conn.close()

def upsert_user(u):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO users(user_id, username, first_name, created_at)
    VALUES(?,?,?,?)
    ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name
    """, (u.id, u.username or "", u.first_name or "", int(time.time())))
    conn.commit()
    conn.close()

def add_prompt(source_chat_id: str, source_message_id: int, origin: str,
               title: str, body: str, tags: List[str], tool: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO prompts(source_chat_id, source_message_id, origin, title, body, tags, tool, created_at)
    VALUES(?,?,?,?,?,?,?,?)
    """, (str(source_chat_id), int(source_message_id), origin, title, body, ",".join(tags), tool, int(time.time())))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid

def latest_prompt() -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM prompts ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row

def list_subscribers() -> List[int]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE is_subscribed=1")
    rows = cur.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]

def set_subscribe(user_id: int, v: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_subscribed=? WHERE user_id=?", (v, user_id))
    conn.commit()
    conn.close()

def set_inviter(user_id: int, inviter_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET inviter_id=? WHERE user_id=? AND inviter_id IS NULL", (inviter_id, user_id))
    conn.commit()
    conn.close()


# ---------------- Utils ----------------
async def is_in_required_channel(bot, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        m = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return getattr(m, "status", None) in OK_STATUSES
    except Exception:
        return False

def looks_like_prompt(text: str) -> bool:
    t = text.lower()
    if "промпт" in t or "prompt" in t:
        return True
    if any(x in t for x in ["#sora", "sora", "heygen", "meta ai", "midjourney", "8k", "ultra realistic"]):
        return len(text) > 120
    if "```" in text:
        return True
    return len(text) > 250

def nice_prompt_card(row) -> str:
    tags = row["tags"] or ""
    tool = row["tool"] or "PROMPT"
    title = row["title"] or "Новый промпт"
    body = row["body"] or ""
    preview = body.strip()
    if len(preview) > 700:
        preview = preview[:700] + "…"
    return (
        f"🔥 <b>{title}</b>\n"
        f"🧩 <b>{tool}</b>\n"
        f"🏷 <i>{tags}</i>\n\n"
        f"<code>{preview}</code>"
    )

async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


# ---------------- OpenAI helpers ----------------
def openai_extract_prompt(raw: str) -> Tuple[str, str, List[str], str]:
    """
    Возвращает: title, clean_prompt, tags[], tool
    Без экзотики: если модель недоступна/ошибка — откатываемся на эвристику.
    """
    try:
        prompt = (
            "Ты редактор базы промптов Кристины. "
            "Вытащи из текста ЧИСТЫЙ промпт (без воды), придумай короткий заголовок (до 6 слов), "
            "выдай 3-6 тегов и название инструмента (Sora/HeyGen/MetaAI/Reels/Photo/Other). "
            "Верни JSON строго с ключами: title, clean_prompt, tags, tool."
        )
        r = client.responses.create(
            model=TEXT_MODEL,
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": raw}
            ]
        )
        txt = r.output_text
        data = json.loads(txt)
        title = str(data.get("title", "Новый промпт")).strip()
        clean = str(data.get("clean_prompt", raw)).strip()
        tags = data.get("tags", [])
        tool = str(data.get("tool", "Other")).strip()
        if not isinstance(tags, list):
            tags = [str(tags)]
        tags = [str(x).strip().lstrip("#") for x in tags if str(x).strip()]
        return title, clean, tags[:8], tool
    except Exception:
        # fallback
        title = "Промпт"
        tool = "Other"
        tags = []
        clean = raw.strip()
        return title, clean, tags, tool

def openai_make_image_b64(prompt: str) -> str:
    # Image generation via Responses API tool (base64)
    r = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        tools=[{"type": "image_generation"}],
    )
    # find base64 image in output
    for out in r.output:
        if out.type == "image_generation_call":
            return out.result  # base64
    raise RuntimeError("No image returned")


# ---------------- UI ----------------
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Промты", callback_data="prompts"),
         InlineKeyboardButton("🧠 Чат-помощник", callback_data="chat")],
        [InlineKeyboardButton("🖼 Сделать фото", callback_data="gen_image"),
         InlineKeyboardButton("🔊 Озвучить", callback_data="tts")],
        [InlineKeyboardButton("⭐ VIP", callback_data="vip"),
         InlineKeyboardButton("🎁 Пригласить", callback_data="share")]
    ])

def kb_need_subscribe():
    btns = []
    if REQUIRED_CHANNEL and str(REQUIRED_CHANNEL).startswith("@"):
        btns.append([InlineKeyboardButton("✅ Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")])
    btns.append([InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub")])
    return InlineKeyboardMarkup(btns)

def kb_prompt_actions(prompt_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 В избранное", callback_data=f"fav:{prompt_id}"),
         InlineKeyboardButton("🖼 Сгенерировать", callback_data=f"img:{prompt_id}")],
        [InlineKeyboardButton("🔊 Озвучить разбор", callback_data=f"tts:{prompt_id}")]
    ])


# ---------------- Handlers ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)

    # referral: /start ref_123
    if context.args and context.args[0].startswith("ref_"):
        try:
            inviter = int(context.args[0].split("_", 1)[1])
            if inviter != u.id:
                set_inviter(u.id, inviter)
        except Exception:
            pass

    ok = await is_in_required_channel(context.bot, u.id)
    if not ok:
        await update.message.reply_text(
            "Чтобы получить базу промптов и уроки — подпишись на канал 👇\n"
            "Потом нажми «Проверить подписку».",
            reply_markup=kb_need_subscribe(),
            parse_mode="HTML"
        )
        return

    text = (
        "👋 <b>Привет! Я — AI-бот Кристины.</b>\n\n"
        "Выбирай, что сделать прямо сейчас:\n"
        "🔥 взять готовый промпт\n"
        "🧠 спросить совет (текст)\n"
        "🖼 сгенерировать фото\n"
        "🔊 получить голосовой мини-гайд\n\n"
        "Жми кнопки ниже 👇"
    )
    await update.message.reply_text(text, reply_markup=kb_main(), parse_mode="HTML")

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"Твой Telegram user_id: {u.id}")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    upsert_user(u)

    # subscription gate
    if query.data != "check_sub":
        ok = await is_in_required_channel(context.bot, u.id)
        if not ok:
            await safe_edit(query,
                "Сначала подпишись на канал 👇\nПотом нажми «Проверить подписку».",
                reply_markup=kb_need_subscribe()
            )
            return

    if query.data == "check_sub":
        ok = await is_in_required_channel(context.bot, u.id)
        if ok:
            await safe_edit(query, "✅ Подписка подтверждена!\nОткрываю меню 👇", reply_markup=kb_main())
        else:
            await query.answer("Пока не вижу подписку. Попробуй ещё раз через 5–10 секунд.", show_alert=True)
        return

    if query.data == "prompts":
        row = latest_prompt()
        if not row:
            await safe_edit(query, "Пока нет промптов в базе. Я начну собирать их из канала автоматически ✅")
            return
        pid = row["id"]
        await safe_edit(query, nice_prompt_card(row), reply_markup=kb_prompt_actions(pid))
        return

    if query.data.startswith("fav:"):
        pid = int(query.data.split(":")[1])
        conn = db()
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO favorites(user_id, prompt_id, created_at) VALUES(?,?,?)",
                    (u.id, pid, int(time.time())))
        conn.commit()
        conn.close()
        await query.answer("📌 Сохранено в избранное")
        return

    if query.data == "share":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{u.id}"
        txt = (
            "🎁 <b>Твоя персональная ссылка</b>\n\n"
            "Отправь друзьям — они получат доступ к базе, а тебе я добавлю бонусы в VIP.\n\n"
            f"<code>{link}</code>"
        )
        await safe_edit(query, txt, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📣 Поделиться", url=f"https://t.me/share/url?url={link}&text=AI%20бот%20с%20промптами%20Кристины")]
        ]))
        return

    if query.data == "gen_image":
        await safe_edit(query,
            "Напиши мне текст промпта для фото (одним сообщением) — я сгенерирую картинку 🖼\n\n"
            "Пример: “ультра-реалистичное зимнее fashion-editorial, 8K, мягкий свет…”"
        )
        context.user_data["awaiting"] = "image_prompt"
        return

    if query.data == "chat":
        await safe_edit(query,
            "Окей, напиши вопрос 👇\n\n"
            "Я отвечу как твой AI-помощник и могу: придумать промпт, хук, сценарий, текст, разбор фото и т.д."
        )
        context.user_data["awaiting"] = "chat"
        return

    if query.data == "tts":
        await safe_edit(query,
            "Напиши текст — я озвучу его голосом 🔊\n"
            "(например: “объясни, как использовать этот промпт в Sora”)"
        )
        context.user_data["awaiting"] = "tts_text"
        return

    if query.data.startswith("img:"):
        pid = int(query.data.split(":")[1])
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT body FROM prompts WHERE id=?", (pid,))
        row = cur.fetchone()
        conn.close()
        if not row:
            await query.answer("Промпт не найден", show_alert=True)
            return
        await safe_edit(query, "🖼 Генерирую картинку… (это может занять немного времени)")
        try:
            b64 = openai_make_image_b64(row["body"])
            img_bytes = base64.b64decode(b64)
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=img_bytes, caption="Готово ✅")
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"Не получилось сгенерировать: {e}")
        return

    if query.data.startswith("tts:"):
        pid = int(query.data.split(":")[1])
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT title, body FROM prompts WHERE id=?", (pid,))
        row = cur.fetchone()
        conn.close()
        if not row:
            await query.answer("Промпт не найден", show_alert=True)
            return
        text = f"{row['title']}. Кратко: как использовать. {row['body'][:700]}"
        await safe_edit(query, "🔊 Озвучиваю…")
        try:
            # Streaming to file (opus) then send voice
            path = "/tmp/voice.ogg"
            with client.audio.speech.with_streaming_response.create(
                model="gpt-4o-mini-tts",
                voice="alloy",
                input=text,
                response_format="opus",
            ) as resp:
                resp.stream_to_file(path)
            with open(path, "rb") as f:
                await context.bot.send_voice(chat_id=query.message.chat_id, voice=f, caption="Готово ✅")
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"Не получилось озвучить: {e}")
        return

    if query.data == "vip":
        # Telegram Stars invoice (currency XTR)
        prices = [LabeledPrice(label="VIP на 30 дней", amount=299)]  # Stars amount is integer
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title="VIP доступ",
            description="VIP: база промптов + генераторы + закрытые кнопки",
            payload=f"vip30:{u.id}",
            provider_token="",       # Stars: empty is ok
            currency="XTR",
            prices=prices,
        )
        await query.answer("Счёт отправлен ✅")
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)

    ok = await is_in_required_channel(context.bot, u.id)
    if not ok:
        await update.message.reply_text(
            "Сначала подпишись на канал 👇\nПотом нажми «Проверить подписку».",
            reply_markup=kb_need_subscribe()
        )
        return

    mode = context.user_data.get("awaiting")
    text = (update.message.text or "").strip()

    if mode == "image_prompt":
        context.user_data["awaiting"] = None
        await update.message.reply_text("🖼 Генерирую…")
        try:
            b64 = openai_make_image_b64(text)
            img_bytes = base64.b64decode(b64)
            await update.message.reply_photo(photo=img_bytes, caption="Готово ✅")
        except Exception as e:
            await update.message.reply_text(f"Не получилось: {e}")
        return

    if mode == "tts_text":
        context.user_data["awaiting"] = None
        await update.message.reply_text("🔊 Озвучиваю…")
        try:
            path = "/tmp/voice.ogg"
            with client.audio.speech.with_streaming_response.create(
                model="gpt-4o-mini-tts",
                voice="alloy",
                input=text,
                response_format="opus",
            ) as resp:
                resp.stream_to_file(path)
            with open(path, "rb") as f:
                await update.message.reply_voice(voice=f, caption="Готово ✅")
        except Exception as e:
            await update.message.reply_text(f"Не получилось озвучить: {e}")
        return

    # default: chat assistant
    await update.message.reply_text("🧠 Думаю…")
    try:
        r = client.responses.create(
            model=TEXT_MODEL,
            input=[
                {"role": "system", "content": "Ты дружелюбный, очень практичный AI-помощник Кристины. Отвечай кратко, шагами, с примерами."},
                {"role": "user", "content": text}
            ]
        )
        await update.message.reply_text(r.output_text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка OpenAI: {e}")

async def on_channel_or_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    chat_id = str(msg.chat_id)

    # only from configured sources
    if SOURCE_CHANNEL_ID and chat_id != str(SOURCE_CHANNEL_ID) and DISCUSSION_GROUP_ID and chat_id != str(DISCUSSION_GROUP_ID):
        return

    raw = msg.text.strip()
    if not looks_like_prompt(raw):
        return

    title, clean, tags, tool = openai_extract_prompt(raw)
    origin = "channel" if msg.chat.type == "channel" else "comment"
    pid = add_prompt(chat_id, msg.message_id, origin, title, clean, tags, tool)

    # auto-broadcast to subscribers
    card = nice_prompt_card(latest_prompt())
    kb = kb_prompt_actions(pid)
    for uid in list_subscribers():
        try:
            await context.bot.send_message(chat_id=uid, text=card, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

async def on_error(update, context):
    log.exception("Unhandled error", exc_info=context.error)

# ---------------- Webhook ----------------
@app.get("/")
async def health():
    return {"ok": True}

@app.post("/webhook")
async def webhook(req: Request):
    if WEBHOOK_SECRET:
        st = req.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if st != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="bad secret token")

    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def startup():
    global tg_app
    init_db()

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY missing (OpenAI features will fail)")

    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("myid", cmd_myid))

    tg_app.add_handler(CallbackQueryHandler(on_button))

    # channel + groups collector
    tg_app.add_handler(MessageHandler((filters.ChatType.CHANNEL | filters.ChatType.GROUPS) & filters.TEXT, on_channel_or_group))

    # private chat text
    tg_app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))

    tg_app.add_error_handler(on_error)

    await tg_app.initialize()
    await tg_app.start()

    if PUBLIC_URL:
        await tg_app.bot.set_webhook(
            url=f"{PUBLIC_URL}/webhook",
            secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET else None,
            allowed_updates=Update.ALL_TYPES
        )
        log.info("Webhook set: %s/webhook", PUBLIC_URL)
