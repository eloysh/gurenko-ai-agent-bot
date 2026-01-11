import os
import json
import base64
import hmac
import hashlib
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, Header, HTTPException
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

from db import (
    init_db, upsert_user, get_user, set_state, get_state, set_vip,
    add_prompt, list_prompts, mark_prompt_seen, toggle_favorite,
    add_referral, list_notified_users, toggle_notify,
    add_freepik_task, get_freepik_task
)
from freepik_client import FreepikClient


# ---------------- ENV ----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")  # https://xxxx.onrender.com
TG_WEBHOOK_SECRET_TOKEN = os.getenv("TG_WEBHOOK_SECRET_TOKEN", "").strip()  # header secret
TG_WEBHOOK_PATH_SECRET = os.getenv("TG_WEBHOOK_PATH_SECRET", "").strip()  # URL secret

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # e.g. @gurenko_kristina_ai or -100...
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "").strip()  # https://t.me/xxx

CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # numeric id of your channel, ex: -100123...
DISCUSSION_GROUP_ID = os.getenv("DISCUSSION_GROUP_ID", "").strip()  # numeric id of discussion group, ex: -100456...

OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0") or "0")

FREEPIK_API_KEY = os.getenv("FREEPIK_API_KEY", "").strip()
FREEPIK_WEBHOOK_SECRET = os.getenv("FREEPIK_WEBHOOK_SECRET", "").strip()  # for verifying Freepik webhook signature

VIP_STARS_PRICE = int(os.getenv("VIP_STARS_PRICE", "299") or "299")  # 299 Stars


if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")
if not PUBLIC_BASE_URL:
    raise RuntimeError("Missing PUBLIC_BASE_URL env var")
if not TG_WEBHOOK_PATH_SECRET:
    raise RuntimeError("Missing TG_WEBHOOK_PATH_SECRET env var")
if not REQUIRED_CHANNEL:
    raise RuntimeError("Missing REQUIRED_CHANNEL env var")
if not REQUIRED_CHANNEL_URL:
    # can still work, but subscribe button won't open
    REQUIRED_CHANNEL_URL = "https://t.me/" + REQUIRED_CHANNEL.lstrip("@")

if not FREEPIK_API_KEY:
    raise RuntimeError("Missing FREEPIK_API_KEY env var")

# ---------------- APP INIT ----------------
app = FastAPI()
tg_app: Application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
freepik = FreepikClient(FREEPIK_API_KEY)

init_db()


# ---------------- UI ----------------
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Фото", callback_data="m:image"),
         InlineKeyboardButton("🎥 Видео", callback_data="m:video")],
        [InlineKeyboardButton("📚 База промптов", callback_data="m:library"),
         InlineKeyboardButton("🆕 Новые промты", callback_data="m:new")],
        [InlineKeyboardButton("⭐ VIP", callback_data="m:vip"),
         InlineKeyboardButton("🎁 Реферал", callback_data="m:ref")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="m:notify"),
         InlineKeyboardButton("📷 Instagram", url=os.getenv("INSTAGRAM_URL", "https://instagram.com"))],
    ])

def kb_subscribe() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подписаться", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub")]
    ])

def kb_image_models() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Flux Dev (быстро)", callback_data="img:flux"),
         InlineKeyboardButton("HyperFlux (качество)", callback_data="img:hyper")],
        [InlineKeyboardButton("Mystic (арт/стиль)", callback_data="img:mystic")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="m:back")]
    ])

def kb_video_models() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Kling Standard", callback_data="vid:kling_std"),
         InlineKeyboardButton("Kling Pro", callback_data="vid:kling_pro")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="m:back")]
    ])


# ---------------- HELPERS ----------------
async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Telegram returns statuses: member/administrator/creator
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

async def gate_or_ask_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    ok = await is_subscribed(user.id, context)
    if ok:
        return True

    text = (
        "🔒 Доступ закрыт.\n\n"
        f"Чтобы пользоваться ботом — подпишись на канал {REQUIRED_CHANNEL} и нажми «Проверить подписку»."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=kb_subscribe())
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=kb_subscribe())
    return False

def _parse_ref(start_arg: str) -> Optional[int]:
    # expecting /start ref_12345
    if not start_arg:
        return None
    if start_arg.startswith("ref_"):
        try:
            return int(start_arg.replace("ref_", "").strip())
        except Exception:
            return None
    return None

def _extract_prompts_from_comment(text: str) -> list[str]:
    """
    Логика максимально практичная для твоего формата:
    - если в комменте несколько строк — считаем каждую непустую строку отдельным промптом,
    - игнорируем строки короче 20 символов,
    - если есть маркеры 'ПРОМТ:'/'PROMPT:' — берем всё после них.
    """
    if not text:
        return []
    cleaned = text.strip()
    if "ПРОМТ:" in cleaned.upper():
        # берём после первого "ПРОМТ:"
        idx = cleaned.upper().find("ПРОМТ:")
        cleaned = cleaned[idx + len("ПРОМТ:"):].strip()
    if "PROMPT:" in cleaned.upper():
        idx = cleaned.upper().find("PROMPT:")
        cleaned = cleaned[idx + len("PROMPT:"):].strip()

    parts = [p.strip(" \t\r\n•-—") for p in cleaned.split("\n")]
    out = []
    for p in parts:
        if len(p) >= 20:
            out.append(p)
    # если получился 1 большой блок — оставим как 1 промпт
    if not out and len(cleaned) >= 20:
        out = [cleaned]
    return out

async def send_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id=chat_id,
        text="🔥 *Gurenko AI Agent* — выбирай, что делаем:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main()
    )

async def broadcast_new_prompt(prompt_text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    # аккуратно: можно выключить у пользователя через "Уведомления"
    user_ids = list_notified_users()
    msg = "🆕 *Новый промпт из канала:*\n\n" + prompt_text
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


# ---------------- COMMANDS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    upsert_user(user.id, user.username, user.first_name)

    # referral
    if context.args:
        ref = _parse_ref(context.args[0])
        if ref:
            add_referral(referrer_id=ref, referred_id=user.id)

    # gate
    if not await gate_or_ask_sub(update, context):
        return

    await send_menu(update.effective_chat.id, context)

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    await update.message.reply_text(f"Твой user_id: `{user.id}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/start — меню\n"
        "/myid — узнать свой Telegram user id\n"
        "/help — помощь"
    )


# ---------------- CALLBACKS (MENU) ----------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    user = update.effective_user
    if not user:
        return
    upsert_user(user.id, user.username, user.first_name)

    # gate for everything except check_sub
    if q.data != "check_sub":
        if not await gate_or_ask_sub(update, context):
            return

    data = q.data

    if data == "check_sub":
        ok = await is_subscribed(user.id, context)
        if not ok:
            await q.message.reply_text("Пока не вижу подписку 😕 Подпишись и нажми ещё раз.", reply_markup=kb_subscribe())
            return
        await q.message.reply_text("✅ Подписка подтверждена! Добро пожаловать 🔥")
        await send_menu(q.message.chat_id, context)
        return

    if data == "m:back":
        await send_menu(q.message.chat_id, context)
        return

    if data == "m:image":
        await q.message.reply_text("Выбери модель для *Фото*:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_image_models())
        return

    if data == "m:video":
        await q.message.reply_text("Выбери модель для *Видео*:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_video_models())
        return

    if data.startswith("img:"):
        model = data.split(":", 1)[1]
        set_state(user.id, "await_prompt", {"kind": "image", "model": model})
        await q.message.reply_text(
            "🖼️ Ок! Пришли *текст промпта* одним сообщением.\n\n"
            "Подсказка: можешь вставить промпт из канала — бот понимает большие тексты.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data.startswith("vid:"):
        model = data.split(":", 1)[1]
        set_state(user.id, "await_video_prompt", {"kind": "video", "model": model})
        await q.message.reply_text(
            "🎥 Ок! Теперь пришли *фото* (как картинку) — потом бот попросит текст промпта для движения.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data == "m:library":
        prompts = list_prompts(limit=8, only_new=False)
        if not prompts:
            await q.message.reply_text("Пока база пуста. Добавь промпты комментами под постами в канале 🙂")
            return
        txt = "📚 *Последние промпты:*\n\n"
        for p in prompts:
            txt += f"• `{p['prompt_id']}` {p['text'][:120]}\n"
        txt += "\nХочешь сохранить в избранное? Напиши: `fav 123`"
        await q.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
        return

    if data == "m:new":
        prompts = list_prompts(limit=8, only_new=True)
        if not prompts:
            await q.message.reply_text("🆕 Новых промптов пока нет.")
            return
        txt = "🆕 *Новые промпты:*\n\n"
        for p in prompts:
            txt += f"• `{p['prompt_id']}` {p['text'][:140]}\n"
            mark_prompt_seen(int(p["prompt_id"]))
        await q.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
        return

    if data == "m:notify":
        newv = toggle_notify(user.id)
        await q.message.reply_text("🔔 Уведомления: " + ("ВКЛ ✅" if newv == 1 else "ВЫКЛ ❌"))
        return

    if data == "m:ref":
        link = f"https://t.me/{(await context.bot.get_me()).username}?start=ref_{user.id}"
        await q.message.reply_text(
            "🎁 *Твоя реферальная ссылка:*\n"
            f"{link}\n\n"
            "За каждого приглашённого — бонусы (можно настроить: VIP/кредиты).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data == "m:vip":
        await q.message.reply_text(
            "⭐ *VIP доступ*\n\n"
            f"Цена: *{VIP_STARS_PRICE} ⭐*\n"
            "VIP даёт приоритет, больше генераций, доступ к спец-разделам.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Купить за {VIP_STARS_PRICE} ⭐", callback_data="vip:buy")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="m:back")]
            ])
        )
        return

    if data == "vip:buy":
        # Stars invoices use currency XTR and empty provider_token 
        prices = [LabeledPrice(label="VIP доступ", amount=VIP_STARS_PRICE)]
        await context.bot.send_invoice(
            chat_id=q.message.chat_id,
            title="VIP доступ",
            description="VIP доступ к Gurenko AI Agent",
            payload="vip_299",
            provider_token="",  # for Stars
            currency="XTR",
            prices=prices
        )
        return


# ---------------- TEXT / STATE ----------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    upsert_user(user.id, user.username, user.first_name)

    # gate
    if not await gate_or_ask_sub(update, context):
        return

    text = (update.message.text or "").strip()

    # favorites: "fav 123"
    if text.lower().startswith("fav "):
        try:
            pid = int(text.split(" ", 1)[1].strip())
            added = toggle_favorite(user.id, pid)
            await update.message.reply_text("⭐ В избранном!" if added else "❌ Убрала из избранного.")
        except Exception:
            await update.message.reply_text("Формат: `fav 123`", parse_mode=ParseMode.MARKDOWN)
        return

    state, payload = get_state(user.id)

    # image prompt
    if state == "await_prompt" and payload and payload.get("kind") == "image":
        model = payload.get("model")
        set_state(user.id, None, None)

        await update.message.reply_text("⏳ Генерирую… Как будет готово — пришлю сюда.")

        webhook_url = f"{PUBLIC_BASE_URL}/webhook/freepik"

        try:
            if model == "flux":
                res = await freepik.text_to_image_flux_dev(text, webhook_url=webhook_url)
            elif model == "hyper":
                res = await freepik.text_to_image_hyperflux(text, webhook_url=webhook_url)
            elif model == "mystic":
                res = await freepik.mystic(text, webhook_url=webhook_url)
            else:
                res = await freepik.text_to_image_flux_dev(text, webhook_url=webhook_url)

            # ожидаем что Freepik вернет task id
            task_id = str(res.get("id") or res.get("data", {}).get("id") or res.get("task_id") or "")
            if task_id:
                add_freepik_task(task_id, user.id, update.effective_chat.id, kind="image")
            else:
                await update.message.reply_text("⚠️ Не нашла task_id в ответе Freepik. Пришли лог ответа — подстрою парсер.")
        except Exception as e:
            await update.message.reply_text(f"Ошибка генерации: {e}")
        return

    # video flow (step 1 -> wait photo)
    if state == "await_video_prompt" and payload and payload.get("kind") == "video":
        # user wrote text instead of photo
        await update.message.reply_text("Сначала пришли *фото* как картинку 🙂", parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text("Выбери действие в меню: /start")


# ---------------- PHOTO (VIDEO FLOW) ----------------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    upsert_user(user.id, user.username, user.first_name)

    # gate
    if not await gate_or_ask_sub(update, context):
        return

    state, payload = get_state(user.id)
    if state != "await_video_prompt" or not payload or payload.get("kind") != "video":
        await update.message.reply_text("Фото получила 🙂 Но чтобы сделать видео — нажми 🎥 Видео в меню.")
        return

    # download photo bytes -> base64
    photo = update.message.photo[-1]
    file = await photo.get_file()
    b = await file.download_as_bytearray()
    image_b64 = base64.b64encode(bytes(b)).decode("utf-8")

    # now ask for motion prompt
    payload["image_b64"] = image_b64
    payload["step"] = "need_text"
    set_state(user.id, "await_video_text", payload)

    await update.message.reply_text(
        "Отлично! Теперь пришли *текст промпта* для движения/сцены.\n"
        "Например: “Камера медленно приближается, лёгкий снег, улыбка, кинематографично”.",
        parse_mode=ParseMode.MARKDOWN
    )


async def on_video_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    upsert_user(user.id, user.username, user.first_name)

    if not await gate_or_ask_sub(update, context):
        return

    state, payload = get_state(user.id)
    if state != "await_video_text" or not payload:
        return

    model = payload.get("model")
    image_b64 = payload.get("image_b64")
    prompt = (update.message.text or "").strip()
    set_state(user.id, None, None)

    await update.message.reply_text("⏳ Делаю видео… пришлю результат, как будет готово.")

    webhook_url = f"{PUBLIC_BASE_URL}/webhook/freepik"

    try:
        if model == "kling_std":
            res = await freepik.kling_image_to_video_standard(image_b64, prompt, webhook_url=webhook_url)
        else:
            res = await freepik.kling_image_to_video_pro(image_b64, prompt, webhook_url=webhook_url)

        task_id = str(res.get("id") or res.get("data", {}).get("id") or res.get("task_id") or "")
        if task_id:
            add_freepik_task(task_id, user.id, update.effective_chat.id, kind="video")
        else:
            await update.message.reply_text("⚠️ Не нашла task_id в ответе Freepik. Пришли лог ответа — подстрою парсер.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка генерации видео: {e}")


# ---------------- PAYMENTS ----------------
async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    if q:
        await q.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.successful_payment:
        return
    user = update.effective_user
    if not user:
        return
    set_vip(user.id, True)
    await msg.reply_text("✅ VIP активирован! Спасибо 💛\n\nЖми /start и пользуйся.")


# ---------------- CHANNEL POSTS + COMMENTS INGEST ----------------
async def on_discussion_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Забираем промпты из комментариев в discussion group:
    У комментария обычно есть reply_to_message, которое является форвардом поста из канала.
    """
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    if not chat:
        return

    if DISCUSSION_GROUP_ID and str(chat.id) != str(DISCUSSION_GROUP_ID):
        return  # не наш discussion group

    r = update.message.reply_to_message
    if not r or not r.forward_from_chat:
        return

    # проверяем, что это именно комментарий к посту из нашего канала
    if CHANNEL_ID and str(r.forward_from_chat.id) != str(CHANNEL_ID):
        return

    post_id = getattr(r, "forward_from_message_id", None)
    prompts = _extract_prompts_from_comment(update.message.text)

    if not prompts:
        return

    for p in prompts:
        add_prompt(
            text=p,
            tags="channel_comment",
            source="telegram_comment",
            source_chat_id=str(r.forward_from_chat.id),
            source_post_id=str(post_id) if post_id else None,
            created_by=update.effective_user.id if update.effective_user else None
        )
        # можно рассылать как "новый промпт"
        await broadcast_new_prompt(p, context)


# ---------------- WEBHOOKS ----------------
@app.get("/")
async def root() -> Dict[str, Any]:
    return {"ok": True}

@app.post(f"/webhook/telegram/{TG_WEBHOOK_PATH_SECRET}")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if TG_WEBHOOK_SECRET_TOKEN:
        if x_telegram_bot_api_secret_token != TG_WEBHOOK_SECRET_TOKEN:
            raise HTTPException(status_code=403, detail="Bad telegram secret token")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

def _verify_freepik_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    Freepik webhook security: HMAC signature check (docs) :contentReference[oaicite:12]{index=12}
    """
    if not signature or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # signature может приходить как hex
    return hmac.compare_digest(digest, signature)

@app.post("/webhook/freepik")
async def freepik_webhook(
    request: Request,
    x_freepik_signature: Optional[str] = Header(default=None),
):
    raw = await request.body()

    # если настроил secret — проверяем подпись
    if FREEPIK_WEBHOOK_SECRET:
        if not _verify_freepik_signature(raw, x_freepik_signature or "", FREEPIK_WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Bad Freepik signature")

    payload = json.loads(raw.decode("utf-8") or "{}")

    # ожидаем наличие task id + urls результата
    task_id = str(payload.get("id") or payload.get("task_id") or payload.get("data", {}).get("id") or "")
    status = str(payload.get("status") or payload.get("data", {}).get("status") or "")

    task = get_freepik_task(task_id) if task_id else None
    if not task:
        return {"ok": True}

    chat_id = int(task["chat_id"])
    kind = task["kind"]

    # вытащим url результата
    result_url = (
        payload.get("result_url")
        or payload.get("url")
        or payload.get("data", {}).get("url")
        or payload.get("data", {}).get("result", {}).get("url")
    )

    # fallback: список url
    if not result_url:
        arr = payload.get("data", {}).get("urls") or payload.get("urls") or []
        if isinstance(arr, list) and arr:
            result_url = arr[0]

    if status and status.lower() in ("failed", "error"):
        await tg_app.bot.send_message(chat_id, f"❌ Freepik: генерация не удалась.\n{payload}")
        return {"ok": True}

    if not result_url:
        # пришёл статус без url — просто сообщим
        await tg_app.bot.send_message(chat_id, f"ℹ️ Freepik статус: {status}\n(жду финальный результат)")
        return {"ok": True}

    # отправка в Telegram по типу
    if kind == "image":
        try:
            await tg_app.bot.send_photo(chat_id, photo=result_url, caption="✅ Готово! 🖼️")
        except Exception:
            await tg_app.bot.send_message(chat_id, f"✅ Готово! Вот ссылка:\n{result_url}")
    else:
        try:
            await tg_app.bot.send_video(chat_id, video=result_url, caption="✅ Готово! 🎥")
        except Exception:
            await tg_app.bot.send_message(chat_id, f"✅ Готово! Вот ссылка:\n{result_url}")

    return {"ok": True}


# ---------------- STARTUP ----------------
@app.on_event("startup")
async def on_startup() -> None:
    await tg_app.initialize()
    await tg_app.start()

    # Handlers
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(CommandHandler("myid", cmd_myid))

    tg_app.add_handler(CallbackQueryHandler(on_callback))

    tg_app.add_handler(PreCheckoutQueryHandler(precheckout))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # discussion comments ingest
    tg_app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, on_discussion_comment))

    # stateful inputs
    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_video_text), group=1)
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=2)

    # set webhook
    url = f"{PUBLIC_BASE_URL}/webhook/telegram/{TG_WEBHOOK_PATH_SECRET}"
    await tg_app.bot.set_webhook(url=url, secret_token=TG_WEBHOOK_SECRET_TOKEN if TG_WEBHOOK_SECRET_TOKEN else None)
