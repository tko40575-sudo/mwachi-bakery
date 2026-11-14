#!/usr/bin/env python3
"""
Mwachi Bakery — Full-Stack Python Web Application (Phone & Tablet Only)
Zero-dependency Python 3 server (http.server + sqlite3) with:
- Phone & Tablet Only access (blocks PC/Laptop desktop browsers)
- Default Black & White Monochrome Theme (customizable by Admin)
- Guest Mode & Admin Mode (Display-Only Menu & Typed Location, No Ordering)
- Pre-seeded Admin (Mwachi / mwachipma) & VVIP Customer (Tino Ko / ti#@k@65)
- Guest Registration & Login
- Admin Menu Management: Add, Edit & Delete items (Image Upload, Price, Description, Allergens, Stock Status & Live Oven Countdown)
- Customer Ratings (1-5 Stars), Comments, Admin Replies & Comment Moderation
- VVIP & Special Digital Membership Card with Member ID & SVG QR Code
- Swipeable Photo Gallery & Editable Bakery Story
- Admin Customer Promotion (Regular -> Special -> VVIP)
- Admin Typed Location & Store Hours (with Copy Address button)
- Admin Announcements (All, Special & VVIP, VVIP Only)
- Admin Customization (Bakery/Admin Name, Theme Color, Custom Favicon Upload, Default Shop SVG Favicon)
- Spinning Bread Loading Screen & Settings Modal with Logout
"""

import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import threading
import secrets
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "mwachi_data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "mwachi.db"

HOST = os.environ.get("MWACHI_HOST", "0.0.0.0")
PORT = int(os.environ.get("MWACHI_PORT", "5000"))

SESSIONS: dict[str, dict] = {}
EVENT_CLIENTS: list[queue.Queue] = []
EVENTS_LOCK = threading.Lock()


def broadcast_event(event_name: str = "refresh") -> None:
    with EVENTS_LOCK:
        dead = []
        for q in EVENT_CLIENTS:
            try:
                q.put_nowait(event_name)
            except Exception:
                dead.append(q)
        for q in dead:
            if q in EVENT_CLIENTS:
                EVENT_CLIENTS.remove(q)

DEFAULT_SHOP_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#0a0a0a"/>
  <path d="M10 24 L16 12 H48 L54 24 Z" fill="#ffffff"/>
  <path d="M10 24 Q15.5 31 21 24 Q26.5 31 32 24 Q37.5 31 43 24 Q48.5 31 54 24" fill="#d4d4d8"/>
  <rect x="14" y="28" width="36" height="24" rx="3" fill="#ffffff"/>
  <rect x="26" y="36" width="12" height="16" rx="2" fill="#0a0a0a"/>
  <circle cx="35.5" cy="44" r="1.3" fill="#ffffff"/>
  <rect x="17" y="34" width="6" height="7" rx="1" fill="#27272a"/>
  <rect x="41" y="34" width="6" height="7" rx="1" fill="#27272a"/>
  <rect x="11" y="51" width="42" height="4" rx="2" fill="#e4e4e7"/>
</svg>"""


def hash_password(password: str, salt: str | None = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hex_hash = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return secrets.compare_digest(dk.hex(), hex_hash)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'guest',
                tier TEXT NOT NULL DEFAULT 'Regular',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS menu_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Bread',
                price TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                badge TEXT NOT NULL DEFAULT 'Fresh Daily',
                stock_status TEXT NOT NULL DEFAULT 'Available',
                next_batch_at TEXT NOT NULL DEFAULT '',
                allergens TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                menu_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment TEXT NOT NULL,
                admin_reply TEXT NOT NULL DEFAULT '',
                admin_reply_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (menu_id) REFERENCES menu_items(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                audience TEXT NOT NULL DEFAULT 'All',
                pinned INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gallery_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caption TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                menu_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, menu_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (menu_id) REFERENCES menu_items(id) ON DELETE CASCADE
            );
            """
        )

        # Safe migrations for existing databases
        ensure_column(conn, "menu_items", "stock_status", "TEXT NOT NULL DEFAULT 'Available'")
        ensure_column(conn, "menu_items", "next_batch_at", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "menu_items", "allergens", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "reviews", "admin_reply", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "reviews", "admin_reply_at", "TEXT NOT NULL DEFAULT ''")

        now = datetime.now(timezone.utc).isoformat()

        if not conn.execute("SELECT id FROM users WHERE username = ?", ("Mwachi",)).fetchone():
            conn.execute(
                "INSERT INTO users (username, password_hash, role, tier, created_at) VALUES (?, ?, 'admin', 'Admin', ?)",
                ("Mwachi", hash_password("mwachipma"), now),
            )

        if not conn.execute("SELECT id FROM users WHERE username = ?", ("Tino Ko",)).fetchone():
            conn.execute(
                "INSERT INTO users (username, password_hash, role, tier, created_at) VALUES (?, ?, 'guest', 'VVIP', ?)",
                ("Tino Ko", hash_password("ti#@k@65"), now),
            )

        default_settings = {
            "bakery_name": "Mwachi",
            "admin_display_name": "Mwachi Head Baker",
            "theme_color": "#ffffff",
            "location_name": "Mwachi Artisan Bakery",
            "location_address": "Pyinmana, Naypyidaw, Myanmar (Near Main Market Road)",
            "opening_hours": "Daily: 7:00 AM - 8:30 PM",
            "baking_schedule": "Sourdough 7:30 AM • Croissants 9:00 AM • Brioche 2:00 PM",
            "favicon_url": "/favicon.svg",
            "tagline": "Warm loaves, flaky pastries & sweet moments — display menu & visit us in-store.",
            "bakery_story": "Born from a love of slow fermentation and honest ingredients, Mwachi bakes small-batch loaves and pastries fresh every morning for our neighborhood.",
            "telegram_url": "",
            "tiktok_url": "",
        }
        for k, v in default_settings.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        if conn.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0] == 0:
            starter_items = [
                (
                    "Golden Butter Croissant",
                    "Pastry",
                    "4,500 MMK",
                    "27 flaky, honeycombed layers laminated with cultured butter and baked until crisp amber-gold.",
                    "",
                    "Fresh Now",
                    "Hot Out of the Oven",
                    "",
                    "Dairy, Gluten",
                ),
                (
                    "Mwachi Country Sourdough",
                    "Bread",
                    "7,500 MMK",
                    "36-hour slow-fermented artisan boule with a caramelized blistered crust and tender open crumb.",
                    "",
                    "Signature",
                    "Available",
                    "",
                    "Gluten, Egg-Free",
                ),
                (
                    "Matcha Cream Melonpan",
                    "Pastry",
                    "5,200 MMK",
                    "Pillow-soft brioche bun topped with crisp Uji matcha cookie crust and light whipped custard.",
                    "",
                    "Chef Pick",
                    "Available",
                    "",
                    "Dairy, Gluten",
                ),
                (
                    "Velvet Berry Shortcake",
                    "Cake",
                    "8,500 MMK",
                    "Airy vanilla sponge layered with Madagascar vanilla bean cream and fresh seasonal strawberries.",
                    "",
                    "Limited",
                    "Sold Out Today",
                    "",
                    "Dairy, Gluten",
                ),
            ]
            for name, cat, price, desc, img, badge, stock, next_batch, allergens in starter_items:
                conn.execute(
                    """INSERT INTO menu_items
                       (name, category, price, description, image_url, badge, stock_status, next_batch_at, allergens, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, cat, price, desc, img, badge, stock, next_batch, allergens, now),
                )

        if conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0:
            conn.execute(
                """INSERT INTO announcements (title, message, audience, pinned, created_at)
                   VALUES (?, ?, 'All', 1, ?)""",
                (
                    "Welcome to Mwachi Bakery!",
                    "Browse our fresh daily menu, check oven status, leave ratings & reviews, and view our typed store address to visit us in person.",
                    now,
                ),
            )


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_uploaded_image(data_url: str, prefix: str = "img") -> str:
    match = re.match(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", data_url.strip(), re.DOTALL)
    if not match:
        raise ValueError("Invalid image format. Please upload PNG, JPG, WEBP, GIF, SVG, or ICO.")

    subtype = match.group(1).lower()
    ext_map = {
        "png": "png",
        "jpeg": "jpg",
        "jpg": "jpg",
        "webp": "webp",
        "gif": "gif",
        "svg+xml": "svg",
        "x-icon": "ico",
        "vnd.microsoft.icon": "ico",
    }
    ext = ext_map.get(subtype)
    if not ext:
        raise ValueError(f"Unsupported image type: {subtype}")

    raw_bytes = base64.b64decode(match.group(2))
    if len(raw_bytes) > 5 * 1024 * 1024:
        raise ValueError("Image size exceeds 5 MB limit.")

    filename = f"{prefix}_{secrets.token_hex(8)}.{ext}"
    (UPLOAD_DIR / filename).write_bytes(raw_bytes)
    return f"/uploads/{filename}"


def render_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title id="doc-title">Mwachi Bakery</title>
  <link id="dynamic-favicon" rel="icon" href="/favicon.svg" />
  <link id="apple-touch-icon" rel="apple-touch-icon" href="/favicon.svg" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <meta name="theme-color" content="#080808" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <style>
    :root {
      --accent: #ffffff;
      --accent-soft: rgba(255, 255, 255, 0.12);
      --accent-glow: rgba(255, 255, 255, 0.25);
      --bg: #080808;
      --bg-elevated: #141414;
      --bg-card: rgba(20, 20, 20, 0.95);
      --border: rgba(255, 255, 255, 0.16);
      --text: #ffffff;
      --text-muted: #a1a1aa;
      --gold: #f5c542;
      --special: #60a5fa;
      --danger: #ef4444;
      --hot: #fb923c;
      --ok: #4ade80;
      --radius: 16px;
      --shadow: 0 14px 36px rgba(0, 0, 0, 0.55);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

    body {
      font-family: 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: radial-gradient(circle at 15% 8%, var(--accent-soft), transparent 40%), var(--bg);
      color: var(--text);
      min-height: 100vh;
      line-height: 1.5;
      overflow-x: hidden;
      padding-bottom: 88px;
    }

    /* Spinning Bread Loading Screen */
    #loading-screen {
      position: fixed;
      inset: 0;
      z-index: 9999;
      background: radial-gradient(circle at center, #18181b 0%, #050505 100%);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 18px;
      transition: opacity 0.4s ease, visibility 0.4s ease;
    }
    #loading-screen.hidden { opacity: 0; visibility: hidden; pointer-events: none; }
    .bread-spinner-wrap { position: relative; width: 112px; height: 112px; display: grid; place-items: center; }
    .bread-orbit {
      position: absolute; inset: 0; border-radius: 50%;
      border: 3px dashed var(--accent); opacity: 0.55; animation: orbitSpin 6s linear infinite;
    }
    .bread-svg {
      width: 70px; height: 70px;
      animation: breadSpin 2.1s cubic-bezier(0.45, 0.05, 0.55, 0.95) infinite;
      filter: drop-shadow(0 6px 16px var(--accent-glow));
    }
    .steam { position: absolute; top: -12px; display: flex; gap: 10px; }
    .steam span {
      width: 6px; height: 18px; border-radius: 999px;
      background: rgba(255, 255, 255, 0.45); animation: steamRise 1.6s ease-in-out infinite;
    }
    .steam span:nth-child(2) { animation-delay: 0.35s; height: 22px; }
    .steam span:nth-child(3) { animation-delay: 0.7s; }
    .loading-title { font-size: 1.35rem; font-weight: 800; letter-spacing: 0.04em; color: #ffffff; }
    .loading-sub { font-size: 0.9rem; color: var(--text-muted); text-align: center; padding: 0 20px; }
    .loading-bar { width: 200px; height: 5px; background: rgba(255,255,255,0.14); border-radius: 999px; overflow: hidden; }
    .loading-bar-fill { height: 100%; width: 45%; background: var(--accent); border-radius: 999px; animation: barSlide 1.3s ease-in-out infinite; }
    @keyframes breadSpin { 0% { transform: rotate(0) scale(1); } 50% { transform: rotate(180deg) scale(1.08); } 100% { transform: rotate(360deg) scale(1); } }
    @keyframes orbitSpin { to { transform: rotate(-360deg); } }
    @keyframes steamRise { 0%, 100% { transform: translateY(4px) scaleY(0.8); opacity: 0.2; } 50% { transform: translateY(-8px) scaleY(1.15); opacity: 0.8; } }
    @keyframes barSlide { 0% { transform: translateX(-110%); } 100% { transform: translateX(260%); } }

    .container { width: min(1160px, 94vw); margin: 0 auto; padding: 14px 0 24px; }

    .topbar {
      position: sticky; top: 0; z-index: 50;
      backdrop-filter: blur(14px); background: rgba(8, 8, 8, 0.9); border-bottom: 1px solid var(--border);
    }
    .topbar-inner {
      width: min(1160px, 94vw); margin: 0 auto; padding: 10px 0;
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
    }
    .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
    .brand-icon {
      width: 38px; height: 38px; flex-shrink: 0; border-radius: 10px;
      background: var(--accent-soft); border: 1px solid var(--accent);
      display: grid; place-items: center; overflow: hidden;
    }
    .brand-icon img { width: 26px; height: 26px; object-fit: cover; border-radius: 5px; }
    .brand-name { font-size: 1.15rem; font-weight: 800; letter-spacing: 0.02em; }
    .brand-badge {
      font-size: 0.7rem; padding: 2px 8px; border-radius: 999px;
      background: var(--accent-soft); border: 1px solid var(--border); color: var(--text); font-weight: 600; white-space: nowrap;
    }

    .bottom-nav {
      position: fixed; bottom: 0; left: 0; right: 0; z-index: 60;
      background: rgba(12, 12, 12, 0.96); backdrop-filter: blur(16px); border-top: 1px solid var(--border);
      display: flex; justify-content: space-around; padding: 8px 10px calc(8px + env(safe-area-inset-bottom)); gap: 8px;
    }
    .bottom-nav-btn {
      flex: 1; min-height: 46px; border: 1px solid transparent; background: transparent;
      color: var(--text-muted); border-radius: 12px; font-size: 0.82rem; font-weight: 700;
      display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; cursor: pointer;
    }
    .bottom-nav-btn.active { background: var(--accent-soft); border-color: var(--accent); color: var(--text); }

    .btn {
      min-height: 44px; border: 1px solid var(--border); background: var(--bg-elevated);
      color: var(--text); padding: 10px 15px; border-radius: 12px; font-size: 0.92rem; font-weight: 600;
      cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 6px; text-decoration: none;
    }
    .btn:active { transform: scale(0.98); }
    .btn-primary { background: var(--accent); border-color: var(--accent); color: #09090b; }
    .btn-danger { background: rgba(239, 68, 68, 0.16); border-color: rgba(239, 68, 68, 0.5); color: #fca5a5; }
    .btn-sm { min-height: 36px; padding: 6px 11px; font-size: 0.82rem; border-radius: 10px; }

    input, textarea, select {
      width: 100%; min-height: 44px; padding: 10px 13px; border-radius: 12px;
      border: 1px solid var(--border); background: #0d0d0d; color: var(--text); font-size: 16px; outline: none;
    }
    textarea { min-height: 82px; }
    input:focus, textarea:focus, select:focus { border-color: var(--accent); }
    label { display: block; font-size: 0.83rem; font-weight: 600; color: var(--text-muted); margin-bottom: 5px; }
    .field { margin-bottom: 13px; }

    .card {
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 18px; box-shadow: var(--shadow);
    }

    .tier-badge {
      font-size: 0.7rem; font-weight: 700; padding: 3px 9px; border-radius: 999px;
      text-transform: uppercase; letter-spacing: 0.04em; display: inline-flex; align-items: center; gap: 4px;
    }
    .tier-VVIP { background: rgba(245, 197, 66, 0.22); border: 1px solid var(--gold); color: #fde68a; }
    .tier-Special { background: rgba(96, 165, 250, 0.18); border: 1px solid var(--special); color: #bfdbfe; }
    .tier-Admin { background: var(--accent-soft); border: 1px solid var(--accent); color: var(--text); }
    .tier-Regular { background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: var(--text-muted); }

    .auth-shell { min-height: calc(100vh - 120px); display: grid; place-items: center; padding: 16px 0; }
    .auth-card { width: min(440px, 100%); }
    .auth-tabs {
      display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 16px;
      background: #0d0d0d; padding: 5px; border-radius: 12px; border: 1px solid var(--border);
    }
    .auth-tab { min-height: 40px; border: none; border-radius: 9px; background: transparent; color: var(--text-muted); font-weight: 700; cursor: pointer; font-size: 0.92rem; }
    .auth-tab.active { background: var(--accent); color: #09090b; }

    .hero { display: grid; grid-template-columns: 1fr; gap: 14px; margin: 12px 0 18px; }
    @media (min-width: 720px) { .hero { grid-template-columns: 1.2fr 1fr; } }
    .hero-title { font-size: 1.65rem; font-weight: 800; line-height: 1.18; margin-bottom: 8px; }
    .bakery-pills { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .pill { font-size: 0.78rem; padding: 5px 10px; border-radius: 999px; background: rgba(255,255,255,0.06); border: 1px solid var(--border); color: var(--text-muted); }
    .announcement-item { padding: 12px; border-radius: 12px; background: rgba(255,255,255,0.04); border-left: 3px solid var(--accent); margin-bottom: 10px; }
    .announcement-item:last-child { margin-bottom: 0; }

    .toolbar { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
    .filter-row { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px; }
    .filter-chip {
      min-height: 36px; padding: 6px 13px; border-radius: 999px; border: 1px solid var(--border);
      background: var(--bg-elevated); color: var(--text-muted); font-size: 0.84rem; font-weight: 600; cursor: pointer; white-space: nowrap;
    }
    .filter-chip.active { background: var(--accent); color: #09090b; border-color: var(--accent); }

    .menu-grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
    @media (min-width: 640px) { .menu-grid { grid-template-columns: repeat(2, 1fr); } }
    @media (min-width: 1024px) { .menu-grid { grid-template-columns: repeat(3, 1fr); } }
    .menu-card { display: flex; flex-direction: column; overflow: hidden; padding: 0; }
    .menu-img-wrap { position: relative; height: 190px; background: #18181b; display: grid; place-items: center; overflow: hidden; }
    .menu-img-wrap img { width: 100%; height: 100%; object-fit: cover; }
    .menu-badge-float {
      position: absolute; top: 12px; left: 12px; background: rgba(8,8,8,0.88); border: 1px solid var(--accent);
      color: var(--text); padding: 4px 10px; border-radius: 999px; font-size: 0.72rem; font-weight: 700;
    }
    .stock-pill {
      position: absolute; top: 12px; right: 12px; padding: 4px 10px; border-radius: 999px;
      font-size: 0.72rem; font-weight: 700; background: rgba(8,8,8,0.9);
    }
    .stock-hot { border: 1px solid var(--hot); color: #fed7aa; }
    .stock-ok { border: 1px solid var(--ok); color: #bbf7d0; }
    .stock-out { border: 1px solid var(--danger); color: #fecaca; }

    .countdown-bar {
      font-size: 0.78rem; font-weight: 700; padding: 6px 10px; border-radius: 10px;
      background: rgba(251, 146, 60, 0.14); border: 1px solid rgba(251, 146, 60, 0.45); color: #fed7aa;
    }
    .allergen-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 2px; }
    .allergen-tag {
      font-size: 0.7rem; padding: 2px 8px; border-radius: 999px;
      background: rgba(255,255,255,0.07); border: 1px solid var(--border); color: var(--text-muted);
    }
    .allergen-check-grid { display: flex; flex-wrap: wrap; gap: 8px; }
    .allergen-check {
      display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px;
      border-radius: 10px; border: 1px solid var(--border); background: #0d0d0d; font-size: 0.84rem; cursor: pointer;
    }
    .allergen-check input { width: auto; min-height: auto; }

    .menu-body { padding: 15px; display: flex; flex-direction: column; flex: 1; gap: 8px; }
    .menu-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
    .menu-price { font-weight: 800; color: var(--accent); white-space: nowrap; }
    .stars { color: var(--gold); font-size: 0.92rem; }
    .reviews-box { margin-top: 8px; padding-top: 10px; border-top: 1px solid var(--border); }
    .review-item { font-size: 0.84rem; padding: 9px 10px; border-radius: 10px; background: rgba(255,255,255,0.04); margin-bottom: 8px; }
    .admin-reply-box {
      margin-top: 6px; padding: 6px 9px; border-left: 2px solid var(--accent);
      background: rgba(255,255,255,0.05); border-radius: 6px; font-size: 0.8rem;
    }

    /* Swipeable Photo Gallery & Story */
    .gallery-section { margin-top: 22px; }
    .gallery-track {
      display: flex; gap: 12px; overflow-x: auto; scroll-snap-type: x mandatory;
      padding: 6px 2px 10px; -webkit-overflow-scrolling: touch;
    }
    .gallery-slide {
      flex: 0 0 82%; max-width: 340px; scroll-snap-align: start;
      border-radius: 14px; overflow: hidden; border: 1px solid var(--border); background: #0d0d0d;
    }
    .gallery-slide img { width: 100%; height: 190px; object-fit: cover; display: block; }
    .gallery-caption { padding: 9px 12px; font-size: 0.84rem; color: var(--text-muted); }

    /* Typed Location Card */
    .location-card { margin-top: 22px; border-color: var(--accent); }
    .address-box {
      background: #0d0d0d; border: 1px solid var(--border); border-radius: 12px;
      padding: 14px; margin: 10px 0 14px; white-space: pre-line; font-size: 0.95rem;
    }

    /* VVIP / Special Digital Card */
    .vip-card-visual {
      background: linear-gradient(135deg, #18181b 0%, #09090b 100%);
      border: 1.5px solid var(--gold); border-radius: 18px; padding: 20px; text-align: center;
    }
    .vip-qr-wrap {
      display: inline-block; padding: 10px; background: #ffffff; border-radius: 12px; margin: 14px 0;
    }

    .admin-grid { display: grid; grid-template-columns: 1fr; gap: 16px; margin-top: 14px; }
    @media (min-width: 720px) { .admin-grid { grid-template-columns: repeat(2, 1fr); } }

    .modal-backdrop {
      position: fixed; inset: 0; z-index: 120; background: rgba(0,0,0,0.82); backdrop-filter: blur(5px);
      display: none; align-items: flex-end; justify-content: center; padding: 0;
    }
    @media (min-width: 640px) { .modal-backdrop { align-items: center; padding: 16px; } }
    .modal-backdrop.open { display: flex; }
    .modal-card { width: min(500px, 100%); max-height: 90vh; overflow-y: auto; border-bottom-left-radius: 0; border-bottom-right-radius: 0; }
    @media (min-width: 640px) { .modal-card { border-radius: var(--radius); } }

    .toast {
      position: fixed; top: 72px; left: 50%; transform: translateX(-50%); z-index: 200;
      padding: 11px 16px; border-radius: 12px; background: #141414; border: 1px solid var(--accent);
      color: var(--text); box-shadow: var(--shadow); display: none; width: min(340px, 90vw);
      text-align: center; font-size: 0.9rem; font-weight: 600;
    }
    .toast.show { display: block; }
    .hidden { display: none !important; }
    .muted { color: var(--text-muted); font-size: 0.88rem; }
    .row-between { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
  </style>
</head>
<body>

  <!-- Spinning Bread Loading Screen -->
  <div id="loading-screen">
    <div class="bread-spinner-wrap">
      <div class="steam"><span></span><span></span><span></span></div>
      <div class="bread-orbit"></div>
      <svg class="bread-svg" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 34C12 21 21 14 32 14C43 14 52 21 52 34C52 43 46 49 32 49C18 49 12 43 12 34Z" fill="#ffffff" stroke="#a1a1aa" stroke-width="2.5"/>
        <path d="M20 24C24 27 25 32 23 37" stroke="#18181b" stroke-width="3" stroke-linecap="round"/>
        <path d="M32 21C36 25 37 31 35 37" stroke="#18181b" stroke-width="3" stroke-linecap="round"/>
        <path d="M44 24C46 28 46 33 43 37" stroke="#18181b" stroke-width="3" stroke-linecap="round"/>
      </svg>
    </div>
    <div class="loading-title" id="loading-bakery-name">Mwachi Bakery</div>
    <div class="loading-sub">Warming the ovens &amp; preparing the display menu...</div>
    <div class="loading-bar"><div class="loading-bar-fill"></div></div>
  </div>

  <header class="topbar" id="app-topbar">
    <div class="topbar-inner">
      <div class="brand">
        <div class="brand-icon"><img id="nav-favicon-img" src="/favicon.svg" alt="Mwachi Shop" /></div>
        <div style="min-width:0;">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
            <span class="brand-name" id="nav-bakery-name">Mwachi</span>
            <span class="brand-badge">Display Only</span>
          </div>
          <div class="muted" id="nav-admin-name" style="font-size:0.74rem;"></div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;">
        <button class="btn btn-sm" id="lang-toggle-btn" onclick="toggleLanguage()">🇲🇲 မြန်မာ</button>
        <span id="nav-user-pill" class="tier-badge tier-Regular hidden"></span>
      </div>
    </div>
  </header>

  <main class="container" id="app-main">
    <!-- Auth View -->
    <section id="auth-view" class="auth-shell">
      <div class="card auth-card">
        <div style="text-align:center;margin-bottom:16px;">
          <h1 style="font-size:1.55rem;font-weight:800;" id="auth-bakery-title">Mwachi Bakery</h1>
          <p class="muted">Sign in to view our menu &amp; location.</p>
        </div>

        <div class="auth-tabs">
          <button class="auth-tab active" id="tab-login" onclick="setAuthTab('login')">Sign In</button>
          <button class="auth-tab" id="tab-register" onclick="setAuthTab('register')">Guest Sign Up</button>
        </div>

        <form id="login-form" onsubmit="handleLogin(event)">
          <div class="field">
            <label for="login-username">Username</label>
            <input id="login-username" type="text" placeholder="Enter your username" required />
          </div>
          <div class="field">
            <label for="login-password">Password</label>
            <input id="login-password" type="password" placeholder="Enter your password" required />
          </div>
          <button type="submit" class="btn btn-primary" style="width:100%;">Sign In</button>
        </form>

        <form id="register-form" class="hidden" onsubmit="handleRegister(event)">
          <div class="field">
            <label for="reg-username">Choose Guest Username</label>
            <input id="reg-username" type="text" placeholder="e.g. BakerFan99" required />
          </div>
          <div class="field">
            <label for="reg-password">Choose Password</label>
            <input id="reg-password" type="password" placeholder="Create a password" required />
          </div>
          <button type="submit" class="btn btn-primary" style="width:100%;">Create Guest Account</button>
        </form>
      </div>
    </section>

    <!-- Storefront View -->
    <section id="store-view" class="hidden">
      <div id="vip-banner" class="card hidden" style="margin-bottom:14px;border-color:var(--gold);">
        <div class="row-between">
          <div>
            <strong id="vip-banner-title" style="color:#fde68a;font-size:1rem;">👑 Welcome, VVIP!</strong>
            <p class="muted" style="margin-top:3px;">Show your digital membership card at the counter in-store.</p>
          </div>
          <button class="btn btn-sm" style="border-color:var(--gold);color:#fde68a;" onclick="openVipCardModal()">🪪 My Card</button>
        </div>
      </div>

      <div class="hero">
        <div class="card">
          <div class="brand-badge" style="display:inline-block;margin-bottom:8px;">In-Store Bakery Showcase</div>
          <h1 class="hero-title" id="hero-bakery-name">Mwachi Bakery</h1>
          <p class="muted" id="hero-tagline"></p>
          <div class="bakery-pills">
            <span class="pill" id="pill-hours">🕒 Daily</span>
            <span class="pill" id="pill-schedule">🥖 Fresh Batches</span>
            <span class="pill">🛍️ Order via Telegram &amp; TikTok</span>
          </div>
          <div style="margin-top:14px;">
            <button class="btn btn-primary" onclick="openOrderUsModal()">🛍️ Order Us</button>
          </div>
        </div>

        <div class="card">
          <div class="row-between" style="margin-bottom:10px;">
            <h2 style="font-size:1.05rem;">📢 Announcements</h2>
            <span class="muted" id="ann-count"></span>
          </div>
          <div id="announcements-list"></div>
        </div>
      </div>

      <div class="toolbar">
        <div class="filter-row" id="category-filters">
          <button class="filter-chip active" onclick="setCategory('All')">All</button>
          <button class="filter-chip" onclick="setCategory('Bread')">Bread</button>
          <button class="filter-chip" onclick="setCategory('Pastry')">Pastry</button>
          <button class="filter-chip" onclick="setCategory('Cake')">Cake</button>
          <button class="filter-chip" onclick="setCategory('Beverage')">Beverage</button>
        </div>
        <div class="filter-row" id="allergen-filters">
          <button class="filter-chip active" onclick="setAllergenFilter('All')">All Ingredients</button>
          <button class="filter-chip" onclick="setAllergenFilter('Egg-Free')">Egg-Free</button>
          <button class="filter-chip" onclick="setAllergenFilter('No Dairy')">No Dairy</button>
          <button class="filter-chip" onclick="setAllergenFilter('No Nuts')">No Nuts</button>
          <button class="filter-chip" onclick="setAllergenFilter('Hot Out of the Oven')">🔥 Hot Now</button>
          <button class="filter-chip" onclick="setAllergenFilter('Favorites')">❤️ Favorites</button>
        </div>
        <div style="display:grid;grid-template-columns:1fr auto;gap:8px;">
          <input type="search" id="menu-search" placeholder="Search breads, cakes, ingredients..." oninput="renderMenu()" />
          <select id="menu-sort" style="width:auto;min-width:155px;" onchange="renderMenu()">
            <option value="newest">Sort: Newest</option>
            <option value="highest_rated">Sort: Highest Rated</option>
            <option value="most_reviewed">Sort: Most Reviewed</option>
            <option value="price_low">Sort: Price Low → High</option>
            <option value="price_high">Sort: Price High → Low</option>
          </select>
        </div>
      </div>

      <div class="menu-grid" id="menu-grid"></div>

      <!-- Bakery Story & Swipeable Photo Gallery -->
      <div class="card gallery-section">
        <h2 style="font-size:1.15rem;margin-bottom:6px;">📖 Our Bakery Story &amp; Gallery</h2>
        <p class="muted" id="store-story-text" style="margin-bottom:12px;"></p>
        <div class="gallery-track" id="gallery-track"></div>
      </div>

      <!-- Typed Location Card (No Google Maps) -->
      <div class="card location-card">
        <h2 style="font-size:1.18rem;margin-bottom:6px;">📍 Store Location</h2>
        <p style="font-weight:700;font-size:1.02rem;" id="loc-name">Mwachi Artisan Bakery</p>
        <div class="address-box" id="loc-address"></div>
        <div class="row-between">
          <span class="muted" id="loc-hours"></span>
          <button class="btn btn-primary btn-sm" onclick="copyAddress()">📋 Copy Address</button>
        </div>
      </div>
    </section>

    <!-- Admin Management View -->
    <section id="admin-view" class="hidden">
      <div class="row-between" style="margin-bottom:10px;">
        <div>
          <h1 style="font-size:1.4rem;font-weight:800;">🛠️ Admin Panel</h1>
          <p class="muted">Manage menu, oven timers, gallery, announcements, location &amp; customer tiers.</p>
        </div>
      </div>

      <div class="card" style="margin-bottom:16px;">
        <div class="row-between" style="margin-bottom:10px;"><h2 style="font-size:1.08rem;">📊 Bakery Analytics Overview</h2><a class="btn btn-sm btn-primary" href="/api/backup" download="mwachi_backup.json">💾 Download JSON Backup</a></div>
        <div id="admin-analytics-box" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;"></div>
      </div>

      <div class="admin-grid">
        <!-- Add Menu Item -->
        <div class="card">
          <h2 style="font-size:1.08rem;margin-bottom:12px;">🥐 Add New Menu Item</h2>
          <form onsubmit="handleAddMenu(event)">
            <div class="field">
              <label>Item Name</label>
              <input id="new-menu-name" required placeholder="e.g. Hokkaido Milk Bread" />
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
              <div class="field">
                <label>Category</label>
                <select id="new-menu-category">
                  <option>Bread</option>
                  <option>Pastry</option>
                  <option>Cake</option>
                  <option>Beverage</option>
                </select>
              </div>
              <div class="field">
                <label>Display Price</label>
                <input id="new-menu-price" placeholder="e.g. 6,000 MMK" />
              </div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
              <div class="field">
                <label>Stock Status</label>
                <select id="new-menu-stock">
                  <option value="Available">Available</option>
                  <option value="Hot Out of the Oven">Hot Out of the Oven</option>
                  <option value="Sold Out Today">Sold Out Today</option>
                </select>
              </div>
              <div class="field">
                <label>Badge</label>
                <input id="new-menu-badge" value="Fresh Daily" />
              </div>
            </div>
            <div class="field">
              <label>Next Fresh Batch Time (Optional Countdown)</label>
              <input id="new-menu-next-batch" type="datetime-local" />
            </div>
            <div class="field">
              <label>Allergen &amp; Ingredient Tags</label>
              <div class="allergen-check-grid" id="new-menu-allergens">
                <label class="allergen-check"><input type="checkbox" value="Dairy" /> Dairy</label>
                <label class="allergen-check"><input type="checkbox" value="Gluten" /> Gluten</label>
                <label class="allergen-check"><input type="checkbox" value="Nuts" /> Nuts</label>
                <label class="allergen-check"><input type="checkbox" value="Egg-Free" /> Egg-Free</label>
                <label class="allergen-check"><input type="checkbox" value="Vegan" /> Vegan</label>
              </div>
            </div>
            <div class="field">
              <label>Description</label>
              <textarea id="new-menu-desc" required placeholder="Describe ingredients and flavor..."></textarea>
            </div>
            <div class="field">
              <label>Upload Item Image</label>
              <input id="new-menu-img" type="file" accept="image/*" />
            </div>
            <button type="submit" class="btn btn-primary" style="width:100%;">+ Add to Menu</button>
          </form>
        </div>

        <!-- Photo Gallery & Story -->
        <div class="card">
          <h2 style="font-size:1.08rem;margin-bottom:12px;">📸 Bakery Story &amp; Photo Gallery</h2>
          <form onsubmit="handleSaveStory(event)" style="margin-bottom:16px;">
            <div class="field">
              <label>Bakery Story</label>
              <textarea id="set-bakery-story" rows="3"></textarea>
            </div>
            <button type="submit" class="btn btn-sm btn-primary" style="width:100%;">Save Story</button>
          </form>
          <form onsubmit="handleAddGalleryPhoto(event)">
            <div class="field">
              <label>Upload Gallery Photo</label>
              <input id="new-gallery-img" type="file" accept="image/*" required />
            </div>
            <div class="field">
              <label>Photo Caption</label>
              <input id="new-gallery-caption" placeholder="e.g. Morning croissants cooling on the rack" />
            </div>
            <button type="submit" class="btn btn-primary" style="width:100%;">+ Add Gallery Photo</button>
          </form>
          <div id="admin-gallery-list" style="margin-top:12px;"></div>
        </div>

        <!-- Post Announcement -->
        <div class="card">
          <h2 style="font-size:1.08rem;margin-bottom:12px;">📢 Post Announcement</h2>
          <form onsubmit="handleAddAnnouncement(event)">
            <div class="field">
              <label>Title</label>
              <input id="new-ann-title" required placeholder="e.g. Fresh Croissants at 3 PM!" />
            </div>
            <div class="field">
              <label>Audience</label>
              <select id="new-ann-audience">
                <option value="All">All Visitors</option>
                <option value="Special & VVIP">Special &amp; VVIP Customers</option>
                <option value="VVIP Only">VVIP Only</option>
              </select>
            </div>
            <div class="field">
              <label>Message</label>
              <textarea id="new-ann-msg" required placeholder="Write your announcement..."></textarea>
            </div>
            <button type="submit" class="btn btn-primary" style="width:100%;">Publish Announcement</button>
          </form>
          <div id="admin-ann-list" style="margin-top:14px;"></div>
        </div>

        <!-- Typed Location Settings -->
        <div class="card">
          <h2 style="font-size:1.08rem;margin-bottom:12px;">📍 Type Store Location</h2>
          <form onsubmit="handleSaveLocation(event)">
            <div class="field">
              <label>Shop Name</label>
              <input id="set-loc-name" required />
            </div>
            <div class="field">
              <label>Typed Address &amp; Directions</label>
              <textarea id="set-loc-address" required></textarea>
            </div>
            <div class="field">
              <label>Opening Hours</label>
              <input id="set-opening-hours" />
            </div>
            <div class="field">
              <label>Daily Baking Schedule</label>
              <input id="set-baking-schedule" />
            </div>
            <button type="submit" class="btn btn-primary" style="width:100%;">Save Location</button>
          </form>
        </div>

        <!-- Order Us Social Links (Telegram & TikTok) -->
        <div class="card">
          <h2 style="font-size:1.08rem;margin-bottom:12px;">📲 Order Us Links (Telegram &amp; TikTok)</h2>
          <form onsubmit="handleSaveOrderLinks(event)">
            <div class="field">
              <label>Telegram Link</label>
              <input id="set-telegram-url" type="url" placeholder="https://t.me/your_bakery" />
            </div>
            <div class="field">
              <label>TikTok Link</label>
              <input id="set-tiktok-url" type="url" placeholder="https://www.tiktok.com/@your_bakery" />
            </div>
            <button type="submit" class="btn btn-primary" style="width:100%;">Save Order Links</button>
          </form>
        </div>

        <!-- Customer Promotion -->
        <div class="card">
          <h2 style="font-size:1.08rem;margin-bottom:10px;">👑 Promote Customers</h2>
          <p class="muted" style="margin-bottom:10px;">Upgrade guests to <strong>Special</strong> or <strong>VVIP</strong>.</p>
          <div id="admin-users-list"></div>
        </div>
      </div>
    </section>
  </main>

  <!-- Mobile & Tablet Bottom Nav -->
  <nav class="bottom-nav hidden" id="bottom-nav">
    <button class="bottom-nav-btn active" id="bnav-store" onclick="switchView('store')">
      <span>🥐</span><span>Menu &amp; Map</span>
    </button>
    <button class="bottom-nav-btn hidden" id="bnav-card" onclick="openVipCardModal()">
      <span>🪪</span><span>My Card</span>
    </button>
    <button class="bottom-nav-btn hidden" id="bnav-admin" onclick="switchView('admin')">
      <span>🛠️</span><span>Admin</span>
    </button>
    <button class="bottom-nav-btn" onclick="openOrderUsModal()">
      <span>🛍️</span><span>Order Us</span>
    </button>
    <button class="bottom-nav-btn" onclick="openSettingsModal()">
      <span>⚙️</span><span>Settings</span>
    </button>
  </nav>

  <!-- Settings Modal -->
  <div class="modal-backdrop" id="settings-modal">
    <div class="card modal-card">
      <div class="row-between" style="margin-bottom:14px;">
        <h2 style="font-size:1.15rem;">⚙️ Settings</h2>
        <button class="btn btn-sm" onclick="closeSettingsModal()">✕</button>
      </div>
      <div class="card" style="padding:14px;margin-bottom:14px;background:#0d0d0d;">
        <div class="row-between">
          <div>
            <div style="font-weight:700;" id="settings-user-name">User</div>
            <div class="muted" id="settings-user-role">Guest</div>
          </div>
          <button class="btn btn-danger btn-sm" onclick="handleLogout()">🚪 Logout</button>
        </div>
      </div>
      <div class="card" style="padding:14px;margin-bottom:14px;background:#0d0d0d;">
        <h3 style="font-size:0.98rem;margin-bottom:10px;">🔒 Change Password</h3>
        <form onsubmit="handleChangePassword(event)">
          <div class="field"><label>Current Password</label><input id="pwd-current" type="password" required /></div>
          <div class="field"><label>New Password</label><input id="pwd-new" type="password" required minlength="4" /></div>
          <button type="submit" class="btn btn-primary btn-sm" style="width:100%;">Update Password</button>
        </form>
      </div>

      <div class="card" style="padding:14px;margin-bottom:14px;background:#0d0d0d;">
        <h3 style="font-size:0.98rem;margin-bottom:8px;">📝 My Review History</h3>
        <div id="my-reviews-list" style="max-height:180px;overflow-y:auto;"></div>
      </div>

      <div id="admin-settings-section" class="hidden">
        <form onsubmit="handleSaveBranding(event)">
          <div class="field"><label>Bakery Website Name</label><input id="set-bakery-name" required /></div>
          <div class="field"><label>Admin Display Name</label><input id="set-admin-name" required /></div>
          <div class="field"><label>Tagline</label><input id="set-tagline" /></div>
          <div class="field">
            <label>Theme Color (Default: Black &amp; White)</label>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
              <input id="set-theme-color" type="color" style="width:58px;height:44px;padding:3px;" />
              <button type="button" class="btn btn-sm" onclick="pickTheme('#ffffff')">Black &amp; White</button>
              <button type="button" class="btn btn-sm" onclick="pickTheme('#d97736')">Amber</button>
              <button type="button" class="btn btn-sm" onclick="pickTheme('#10b981')">Matcha</button>
              <button type="button" class="btn btn-sm" onclick="pickTheme('#f43f5e')">Berry</button>
            </div>
          </div>
          <div class="field">
            <label>Upload Custom Favicon (Default is Shop Icon)</label>
            <input id="set-favicon-file" type="file" accept="image/*" />
          </div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;">
            <button type="submit" class="btn btn-primary" style="flex:1;">Save Branding &amp; Theme</button>
            <button type="button" class="btn" onclick="resetFavicon()">🏪 Reset Shop Favicon</button>
          </div>
        </form>
      </div>
    </div>
  </div>

  <!-- Edit Menu Modal (Admin) -->
  <div class="modal-backdrop" id="edit-menu-modal">
    <div class="card modal-card">
      <div class="row-between" style="margin-bottom:12px;">
        <h2 style="font-size:1.15rem;">✏️ Edit Menu Item</h2>
        <button class="btn btn-sm" onclick="closeEditMenuModal()">✕</button>
      </div>
      <form onsubmit="handleSaveEditMenu(event)">
        <input type="hidden" id="edit-menu-id" />
        <div class="field"><label>Item Name</label><input id="edit-menu-name" required /></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
          <div class="field">
            <label>Category</label>
            <select id="edit-menu-category">
              <option>Bread</option><option>Pastry</option><option>Cake</option><option>Beverage</option>
            </select>
          </div>
          <div class="field"><label>Display Price</label><input id="edit-menu-price" /></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
          <div class="field">
            <label>Stock Status</label>
            <select id="edit-menu-stock">
              <option value="Available">Available</option>
              <option value="Hot Out of the Oven">Hot Out of the Oven</option>
              <option value="Sold Out Today">Sold Out Today</option>
            </select>
          </div>
          <div class="field"><label>Badge</label><input id="edit-menu-badge" /></div>
        </div>
        <div class="field">
          <label>Next Fresh Batch Time (Countdown)</label>
          <input id="edit-menu-next-batch" type="datetime-local" />
        </div>
        <div class="field">
          <label>Allergen &amp; Ingredient Tags</label>
          <div class="allergen-check-grid" id="edit-menu-allergens">
            <label class="allergen-check"><input type="checkbox" value="Dairy" /> Dairy</label>
            <label class="allergen-check"><input type="checkbox" value="Gluten" /> Gluten</label>
            <label class="allergen-check"><input type="checkbox" value="Nuts" /> Nuts</label>
            <label class="allergen-check"><input type="checkbox" value="Egg-Free" /> Egg-Free</label>
            <label class="allergen-check"><input type="checkbox" value="Vegan" /> Vegan</label>
          </div>
        </div>
        <div class="field"><label>Description</label><textarea id="edit-menu-desc" required></textarea></div>
        <div class="field"><label>Replace Image (Optional)</label><input id="edit-menu-img" type="file" accept="image/*" /></div>
        <button type="submit" class="btn btn-primary" style="width:100%;">Save Changes</button>
      </form>
    </div>
  </div>

  <!-- Rate & Comment Modal -->
  <div class="modal-backdrop" id="review-modal">
    <div class="card modal-card">
      <div class="row-between" style="margin-bottom:12px;">
        <h2 style="font-size:1.1rem;" id="review-modal-title">Rate &amp; Comment</h2>
        <button class="btn btn-sm" onclick="closeReviewModal()">✕</button>
      </div>
      <form onsubmit="handleSubmitReview(event)">
        <input type="hidden" id="review-menu-id" />
        <div class="field">
          <label>Your Rating</label>
          <select id="review-rating">
            <option value="5">★★★★★ (5 - Amazing!)</option>
            <option value="4">★★★★☆ (4 - Delicious)</option>
            <option value="3">★★★☆☆ (3 - Good)</option>
            <option value="2">★★☆☆☆ (2 - Okay)</option>
            <option value="1">★☆☆☆☆ (1 - Needs Work)</option>
          </select>
        </div>
        <div class="field"><label>Your Comment</label><textarea id="review-comment" required></textarea></div>
        <button type="submit" class="btn btn-primary" style="width:100%;">Post Review</button>
      </form>
    </div>
  </div>

  <!-- Admin Reply Modal -->
  <div class="modal-backdrop" id="reply-modal">
    <div class="card modal-card">
      <div class="row-between" style="margin-bottom:12px;">
        <h2 style="font-size:1.1rem;">💬 Reply to Review</h2>
        <button class="btn btn-sm" onclick="closeReplyModal()">✕</button>
      </div>
      <form onsubmit="handleSubmitReply(event)">
        <input type="hidden" id="reply-review-id" />
        <div class="field"><label>Admin Reply</label><textarea id="reply-text" required placeholder="Thank the customer or share a baker's note..."></textarea></div>
        <button type="submit" class="btn btn-primary" style="width:100%;">Post Reply</button>
      </form>
    </div>
  </div>

  <!-- VVIP / Special Membership Card Modal -->
  <div class="modal-backdrop" id="vip-card-modal">
    <div class="card modal-card">
      <div class="row-between" style="margin-bottom:12px;">
        <h2 style="font-size:1.15rem;">🪪 Digital Membership Card</h2>
        <button class="btn btn-sm" onclick="closeVipCardModal()">✕</button>
      </div>
      <div class="vip-card-visual">
        <div class="muted" id="vip-card-bakery" style="letter-spacing:0.1em;text-transform:uppercase;font-size:0.75rem;">MWACHI BAKERY</div>
        <div style="font-size:1.45rem;font-weight:800;margin-top:6px;" id="vip-card-name">Tino Ko</div>
        <div style="margin-top:6px;"><span class="tier-badge tier-VVIP" id="vip-card-tier">VVIP</span></div>
        <div class="vip-qr-wrap" id="vip-qr-box"></div>
        <div style="font-family:monospace;font-size:0.95rem;font-weight:700;color:#fde68a;" id="vip-card-id">MWC-VVIP-0002</div>
        <p class="muted" style="margin-top:8px;font-size:0.8rem;">Present this card at the bakery counter for in-store member perks.</p>
      </div>
    </div>
  </div>

  <!-- Order Us Modal (Telegram & TikTok) -->
  <div class="modal-backdrop" id="order-us-modal">
    <div class="card modal-card">
      <div class="row-between" style="margin-bottom:12px;">
        <h2 style="font-size:1.15rem;">🛍️ Order Us</h2>
        <button class="btn btn-sm" onclick="closeOrderUsModal()">✕</button>
      </div>
      <p class="muted" style="margin-bottom:14px;">Message or follow us on Telegram and TikTok to place an order:</p>
      <div style="display:flex;flex-direction:column;gap:10px;" id="order-us-links"></div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    let STATE = {
      user: null, settings: {}, menu: [], announcements: [], users: [], gallery: [], favorites: [], myReviews: [], analytics: null,
      category: 'All', allergenFilter: 'All', view: 'store'
    };

    let LANG = localStorage.getItem('mwachi_lang') || 'en';
    const I18N = {
      en: {
        langBtn: '🇲🇲 မြန်မာ',
        signIn: 'Sign In',
        signUp: 'Guest Sign Up',
        displayOnly: 'Display Only',
        showcase: 'In-Store Bakery Showcase',
        announcements: '📢 Announcements',
        orderUs: '🛍️ Order Us',
        storyGallery: '📖 Our Bakery Story & Gallery',
        storeLocation: '📍 Store Location',
        copyAddr: '📋 Copy Address',
        navMenu: 'Menu & Map',
        navCard: 'My Card',
        navAdmin: 'Admin',
        navSettings: 'Settings',
        searchPlaceholder: 'Search breads, cakes, ingredients...',
        catAll: 'All', catBread: 'Bread', catPastry: 'Pastry', catCake: 'Cake', catBeverage: 'Beverage',
        allIng: 'All Ingredients', eggFree: 'Egg-Free', noDairy: 'No Dairy', noNuts: 'No Nuts', hotNow: '🔥 Hot Now', favs: '❤️ Favorites',
        sortNewest: 'Sort: Newest', sortRated: 'Sort: Highest Rated', sortReviewed: 'Sort: Most Reviewed', sortLow: 'Sort: Price Low → High', sortHigh: 'Sort: Price High → Low'
      },
      my: {
        langBtn: '🇬🇧 EN',
        signIn: 'ဝင်ရောက်မည်',
        signUp: 'ဧည့်သည်အကောင့်ဖွင့်ရန်',
        displayOnly: 'မီနူးကြည့်ရန်သာ',
        showcase: 'မုန့်တိုက် မီနူးနှင့် တည်နေရာ',
        announcements: '📢 အသိပေးချက်များ',
        orderUs: '🛍️ မှာယူရန်',
        storyGallery: '📖 မုန့်တိုက်အကြောင်းနှင့် ဓာတ်ပုံများ',
        storeLocation: '📍 ဆိုင်တည်နေရာ',
        copyAddr: '📋 လိပ်စာကူးယူရန်',
        navMenu: 'မီနူးနှင့် လိပ်စာ',
        navCard: 'ကတ်',
        navAdmin: 'စီမံခန့်ခွဲမှု',
        navSettings: 'ဆက်တင်',
        searchPlaceholder: 'ပေါင်မုန့်၊ ကိတ်မုန့် ရှာရန်...',
        catAll: 'အားလုံး', catBread: 'ပေါင်မုန့်', catPastry: 'မုန့်ချို', catCake: 'ကိတ်မုန့်', catBeverage: 'အအေး/ကော်ဖီ',
        allIng: 'ပါဝင်ပစ္စည်းအားလုံး', eggFree: 'ကြက်ဥမပါ', noDairy: 'နို့ထွက်မပါ', noNuts: 'အစေ့အဆန်မပါ', hotNow: '🔥 ပူပူနွေးနွေး', favs: '❤️ အကြိုက်ဆုံး',
        sortNewest: 'စီရန်: အသစ်ဆုံး', sortRated: 'စီရန်: အဆင့်အမြင့်ဆုံး', sortReviewed: 'စီရန်: မှတ်ချက်အများဆုံး', sortLow: 'စီရန်: ဈေးအနည်းမှအများ', sortHigh: 'စီရန်: ဈေးအများမှအနည်း'
      }
    };

    function applyLanguage() {
      const t = I18N[LANG] || I18N.en;
      const setTxt = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
      setTxt('lang-toggle-btn', t.langBtn);
      setTxt('tab-login', t.signIn);
      setTxt('tab-register', t.signUp);
      const searchEl = document.getElementById('menu-search');
      if (searchEl) searchEl.placeholder = t.searchPlaceholder;
      const bStore = document.querySelector('#bnav-store span:last-child'); if (bStore) bStore.textContent = t.navMenu;
      const bCard = document.querySelector('#bnav-card span:last-child'); if (bCard) bCard.textContent = t.navCard;
      const bAdmin = document.querySelector('#bnav-admin span:last-child'); if (bAdmin) bAdmin.textContent = t.navAdmin;
      const bSet = document.querySelector('#bottom-nav button:last-child span:last-child'); if (bSet) bSet.textContent = t.navSettings;

      const catBtns = document.querySelectorAll('#category-filters .filter-chip');
      const catKeys = ['catAll', 'catBread', 'catPastry', 'catCake', 'catBeverage'];
      catBtns.forEach((b, i) => { if (catKeys[i]) b.textContent = t[catKeys[i]]; });

      const allgBtns = document.querySelectorAll('#allergen-filters .filter-chip');
      const allgKeys = ['allIng', 'eggFree', 'noDairy', 'noNuts', 'hotNow', 'favs'];
      allgBtns.forEach((b, i) => { if (allgKeys[i]) b.textContent = t[allgKeys[i]]; });

      const sortOpts = document.querySelectorAll('#menu-sort option');
      const sortKeys = ['sortNewest', 'sortRated', 'sortReviewed', 'sortLow', 'sortHigh'];
      sortOpts.forEach((o, i) => { if (sortKeys[i]) o.textContent = t[sortKeys[i]]; });

      const orderBtn = document.querySelector('#store-view .hero .btn-primary');
      if (orderBtn) orderBtn.textContent = t.orderUs;
      const annH2 = document.querySelector('#store-view .hero .card:nth-child(2) h2');
      if (annH2) annH2.textContent = t.announcements;
      const galH2 = document.querySelector('.gallery-section h2');
      if (galH2) galH2.textContent = t.storyGallery;
      const locH2 = document.querySelector('.location-card h2');
      if (locH2) locH2.textContent = t.storeLocation;
      const copyBtn = document.querySelector('.location-card .btn-primary');
      if (copyBtn) copyBtn.textContent = t.copyAddr;
    }

    function toggleLanguage() {
      LANG = LANG === 'en' ? 'my' : 'en';
      localStorage.setItem('mwachi_lang', LANG);
      applyLanguage();
    }

    let _eventSource = null;
    function connectRealtime() {
      if (_eventSource) return;
      _eventSource = new EventSource('/api/events');
      _eventSource.onmessage = async (ev) => {
        if (ev.data === 'refresh' && STATE.user) {
          await loadBootstrap();
        }
      };
      _eventSource.onerror = () => {
        // Browser auto-reconnects EventSource automatically
      };
    }

    function disconnectRealtime() {
      if (_eventSource) {
        _eventSource.close();
        _eventSource = null;
      }
    }

    function showToast(msg) {
      const el = document.getElementById('toast');
      el.textContent = msg;
      el.classList.add('show');
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
    }

    function esc(str) {
      return String(str ?? '').replace(/[&<>"']/g, s => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[s]));
    }

    function readFileAsDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || 'Request failed');
      return data;
    }

    function getCheckedAllergens(containerId) {
      return Array.from(document.querySelectorAll(`#${containerId} input[type="checkbox"]:checked`))
        .map(cb => cb.value).join(', ');
    }

    function setCheckedAllergens(containerId, csv) {
      const set = new Set((csv || '').split(',').map(s => s.trim()).filter(Boolean));
      document.querySelectorAll(`#${containerId} input[type="checkbox"]`).forEach(cb => {
        cb.checked = set.has(cb.value);
      });
    }

    function generateQrSvg(seedText) {
      // Deterministic 15x15 QR-like matrix SVG with finder patterns
      const size = 15;
      let hash = 2166136261;
      for (let i = 0; i < seedText.length; i++) {
        hash ^= seedText.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
      }
      const cells = [];
      const isFinder = (r, c) =>
        (r < 5 && c < 5) || (r < 5 && c >= size - 5) || (r >= size - 5 && c < 5);
      for (let r = 0; r < size; r++) {
        for (let c = 0; c < size; c++) {
          let on = false;
          if (isFinder(r, c)) {
            const lr = r >= size - 5 ? r - (size - 5) : r;
            const lc = c >= size - 5 ? c - (size - 5) : c;
            on = lr === 0 || lr === 4 || lc === 0 || lc === 4 || (lr === 2 && lc === 2);
          } else {
            const v = Math.imul(hash ^ (r * 31 + c * 17), 2654435761) >>> 0;
            on = (v % 10) < 5;
          }
          if (on) cells.push(`<rect x="${c * 8}" y="${r * 8}" width="8" height="8" fill="#09090b"/>`);
        }
      }
      return `<svg width="120" height="120" viewBox="0 0 ${size * 8} ${size * 8}" xmlns="http://www.w3.org/2000/svg">${cells.join('')}</svg>`;
    }

    function applyThemeAndBranding() {
      const s = STATE.settings;
      const accent = s.theme_color || '#ffffff';
      document.documentElement.style.setProperty('--accent', accent);
      document.documentElement.style.setProperty('--accent-soft', accent + '24');
      document.documentElement.style.setProperty('--accent-glow', accent + '45');

      const bakeryName = s.bakery_name || 'Mwachi';
      document.getElementById('doc-title').textContent = bakeryName + ' Bakery';
      document.getElementById('nav-bakery-name').textContent = bakeryName;
      document.getElementById('auth-bakery-title').textContent = bakeryName + ' Bakery';
      document.getElementById('loading-bakery-name').textContent = bakeryName + ' Bakery';
      document.getElementById('hero-bakery-name').textContent = bakeryName + ' Bakery';
      document.getElementById('nav-admin-name').textContent = 'By ' + (s.admin_display_name || 'Mwachi Head Baker');
      document.getElementById('hero-tagline').textContent = s.tagline || '';
      document.getElementById('pill-hours').textContent = '🕒 ' + (s.opening_hours || 'Daily');
      document.getElementById('pill-schedule').textContent = '🥖 ' + (s.baking_schedule || 'Fresh Daily');
      document.getElementById('store-story-text').textContent = s.bakery_story || '';

      const fav = s.favicon_url || '/favicon.svg';
      document.getElementById('dynamic-favicon').href = fav + '?v=' + Date.now();
      document.getElementById('nav-favicon-img').src = fav + '?v=' + Date.now();

      document.getElementById('loc-name').textContent = s.location_name || bakeryName;
      document.getElementById('loc-address').textContent = s.location_address || 'Address not set yet.';
      document.getElementById('loc-hours').textContent = s.opening_hours || '';
    }

    async function loadBootstrap() {
      const data = await api('/api/bootstrap');
      STATE.user = data.user;
      STATE.settings = data.settings || {};
      STATE.menu = data.menu || [];
      STATE.announcements = data.announcements || [];
      STATE.users = data.users || [];
      STATE.gallery = data.gallery || [];
      STATE.favorites = data.favorites || [];
      STATE.myReviews = data.my_reviews || [];
      STATE.analytics = data.analytics || null;

      applyThemeAndBranding();

      if (!STATE.user) {
        document.getElementById('auth-view').classList.remove('hidden');
        document.getElementById('store-view').classList.add('hidden');
        document.getElementById('admin-view').classList.add('hidden');
        document.getElementById('bottom-nav').classList.add('hidden');
        document.getElementById('nav-user-pill').classList.add('hidden');
      } else {
        document.getElementById('auth-view').classList.add('hidden');
        document.getElementById('bottom-nav').classList.remove('hidden');

        const u = STATE.user;
        const badge = document.getElementById('nav-user-pill');
        badge.classList.remove('hidden');
        badge.className = 'tier-badge tier-' + u.tier;
        badge.textContent = (u.tier === 'VVIP' ? '👑 ' : u.tier === 'Special' ? '⭐ ' : '') + u.username;

        const isAdmin = u.role === 'admin';
        const isVipOrSpecial = u.tier === 'VVIP' || u.tier === 'Special';
        document.getElementById('bnav-admin').classList.toggle('hidden', !isAdmin);
        document.getElementById('bnav-card').classList.toggle('hidden', !isVipOrSpecial);
        document.getElementById('admin-settings-section').classList.toggle('hidden', !isAdmin);

        const vipBanner = document.getElementById('vip-banner');
        if (isVipOrSpecial) {
          vipBanner.classList.remove('hidden');
          document.getElementById('vip-banner-title').textContent =
            u.tier === 'VVIP' ? `👑 Welcome, ${u.username} (VVIP)!` : `⭐ Welcome, ${u.username} (Special)!`;
        } else {
          vipBanner.classList.add('hidden');
        }

        switchView(STATE.view === 'admin' && isAdmin ? 'admin' : 'store');
        renderAnnouncements();
        renderMenu();
        renderGallery();
        if (isAdmin) renderAdminPanels();
        connectRealtime();
      }
    }

    function switchView(view) {
      STATE.view = view;
      document.getElementById('store-view').classList.toggle('hidden', view !== 'store');
      document.getElementById('admin-view').classList.toggle('hidden', view !== 'admin');
      document.getElementById('bnav-store').classList.toggle('active', view === 'store');
      document.getElementById('bnav-admin').classList.toggle('active', view === 'admin');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function setAuthTab(tab) {
      document.getElementById('tab-login').classList.toggle('active', tab === 'login');
      document.getElementById('tab-register').classList.toggle('active', tab === 'register');
      document.getElementById('login-form').classList.toggle('hidden', tab !== 'login');
      document.getElementById('register-form').classList.toggle('hidden', tab !== 'register');
    }

    async function handleLogin(e) {
      e.preventDefault();
      try {
        await api('/api/login', {
          method: 'POST',
          body: JSON.stringify({
            username: document.getElementById('login-username').value.trim(),
            password: document.getElementById('login-password').value
          })
        });
        STATE.view = 'store';
        await loadBootstrap();
        showToast('Signed in!');
      } catch (err) { showToast(err.message); }
    }

    async function handleRegister(e) {
      e.preventDefault();
      try {
        await api('/api/register', {
          method: 'POST',
          body: JSON.stringify({
            username: document.getElementById('reg-username').value.trim(),
            password: document.getElementById('reg-password').value
          })
        });
        STATE.view = 'store';
        await loadBootstrap();
        showToast('Guest account created!');
      } catch (err) { showToast(err.message); }
    }

    async function handleLogout() {
      await api('/api/logout', { method: 'POST' });
      closeSettingsModal();
      disconnectRealtime();
      STATE.user = null;
      await loadBootstrap();
      showToast('Logged out.');
    }

    function setCategory(cat) {
      STATE.category = cat;
      document.querySelectorAll('#category-filters .filter-chip').forEach(btn => {
        btn.classList.toggle('active', (btn.getAttribute('onclick') || '').includes(`'${cat}'`));
      });
      renderMenu();
    }

    function setAllergenFilter(val) {
      STATE.allergenFilter = val;
      document.querySelectorAll('#allergen-filters .filter-chip').forEach(btn => {
        btn.classList.toggle('active', (btn.getAttribute('onclick') || '').includes(`'${val}'`));
      });
      renderMenu();
    }

    function copyAddress() {
      const addr = document.getElementById('loc-address').textContent;
      if (navigator.clipboard) navigator.clipboard.writeText(addr).then(() => showToast('Address copied!'));
      else showToast(addr);
    }

    function renderAnnouncements() {
      const box = document.getElementById('announcements-list');
      document.getElementById('ann-count').textContent = `${STATE.announcements.length} active`;
      if (!STATE.announcements.length) {
        box.innerHTML = '<p class="muted">No announcements right now.</p>';
        return;
      }
      box.innerHTML = STATE.announcements.map(a => `
        <div class="announcement-item">
          <div class="row-between">
            <strong>${esc(a.title)}</strong>
            <span class="tier-badge ${a.audience === 'VVIP Only' ? 'tier-VVIP' : a.audience === 'Special & VVIP' ? 'tier-Special' : 'tier-Regular'}">${esc(a.audience)}</span>
          </div>
          <p class="muted" style="margin-top:4px;">${esc(a.message)}</p>
        </div>
      `).join('');
    }

    function formatCountdown(targetIso) {
      if (!targetIso) return '';
      const diff = new Date(targetIso).getTime() - Date.now();
      if (isNaN(diff) || diff <= 0) return '';
      const hrs = Math.floor(diff / 3600000);
      const mins = Math.floor((diff % 3600000) / 60000);
      const secs = Math.floor((diff % 60000) / 1000);
      return `⏳ Next fresh batch in ${String(hrs).padStart(2,'0')}:${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}`;
    }

    function updateAllCountdowns() {
      document.querySelectorAll('[data-next-batch]').forEach(el => {
        const txt = formatCountdown(el.getAttribute('data-next-batch'));
        if (txt) {
          el.textContent = txt;
          el.classList.remove('hidden');
        } else {
          el.classList.add('hidden');
        }
      });
    }

    function renderMenu() {
      const grid = document.getElementById('menu-grid');
      const q = (document.getElementById('menu-search').value || '').toLowerCase();
      const af = STATE.allergenFilter;

      const items = STATE.menu.filter(item => {
        const matchCat = STATE.category === 'All' || item.category === STATE.category;
        const allg = (item.allergens || '').toLowerCase();
        const matchQ = !q || item.name.toLowerCase().includes(q) || item.description.toLowerCase().includes(q) || allg.includes(q);
        let matchAllg = true;
        if (af === 'Egg-Free') matchAllg = allg.includes('egg-free');
        else if (af === 'No Dairy') matchAllg = !allg.includes('dairy');
        else if (af === 'No Nuts') matchAllg = !allg.includes('nuts');
        else if (af === 'Hot Out of the Oven') matchAllg = item.stock_status === 'Hot Out of the Oven';
        else if (af === 'Favorites') matchAllg = STATE.favorites.includes(item.id);
        return matchCat && matchQ && matchAllg;
      });

      const sortBy = (document.getElementById('menu-sort')?.value) || 'newest';
      const parsePrice = p => parseFloat(String(p || '').replace(/[^0-9.]/g, '')) || 0;
      items.sort((a, b) => {
        if (sortBy === 'highest_rated') return (b.avg_rating || 0) - (a.avg_rating || 0);
        if (sortBy === 'most_reviewed') return (b.reviews?.length || 0) - (a.reviews?.length || 0);
        if (sortBy === 'price_low') return parsePrice(a.price) - parsePrice(b.price);
        if (sortBy === 'price_high') return parsePrice(b.price) - parsePrice(a.price);
        return b.id - a.id;
      });

      if (!items.length) {
        grid.innerHTML = '<div class="card"><p class="muted">No bakery items match your filter.</p></div>';
        return;
      }

      const isAdmin = STATE.user && STATE.user.role === 'admin';

      grid.innerHTML = items.map(item => {
        const avg = item.avg_rating ? Number(item.avg_rating).toFixed(1) : 'New';
        const starIcons = item.avg_rating ? '★'.repeat(Math.round(item.avg_rating)) + '☆'.repeat(5 - Math.round(item.avg_rating)) : '☆☆☆☆☆';
        const imgHtml = item.image_url
          ? `<img src="${esc(item.image_url)}" alt="${esc(item.name)}" />`
          : `<svg width="60" height="60" viewBox="0 0 64 64" fill="none"><path d="M12 34C12 21 21 14 32 14C43 14 52 21 52 34C52 43 46 49 32 49C18 49 12 43 12 34Z" fill="#ffffff" opacity="0.85"/></svg>`;

        const st = item.stock_status || 'Available';
        const stClass = st === 'Hot Out of the Oven' ? 'stock-hot' : st === 'Sold Out Today' ? 'stock-out' : 'stock-ok';
        const stIcon = st === 'Hot Out of the Oven' ? '🔥 ' : st === 'Sold Out Today' ? '⛔ ' : '✅ ';

        const tagsHtml = (item.allergens || '').split(',').map(t => t.trim()).filter(Boolean)
          .map(t => `<span class="allergen-tag">${esc(t)}</span>`).join('');

        const reviewsHtml = (item.reviews || []).map(r => `
          <div class="review-item">
            <div class="row-between">
              <span><strong>${esc(r.username)}</strong> <span class="tier-badge tier-${esc(r.tier)}">${esc(r.tier)}</span></span>
              <span class="stars">${'★'.repeat(r.rating)}</span>
            </div>
            <div class="muted" style="margin-top:3px;">${esc(r.comment)}</div>
            ${r.admin_reply ? `<div class="admin-reply-box">↳ <strong>Baker Reply:</strong> ${esc(r.admin_reply)}</div>` : ''}
            ${isAdmin ? `
              <div style="display:flex;gap:6px;margin-top:6px;">
                <button class="btn btn-sm" onclick="openReplyModal(${r.id}, '${esc(r.admin_reply || '').replace(/'/g, "\\'")}')">💬 Reply</button>
                <button class="btn btn-danger btn-sm" onclick="deleteReview(${r.id})">🗑️ Delete</button>
              </div>
            ` : ''}
          </div>
        `).join('') || '<div class="muted" style="font-size:0.82rem;">No reviews yet — be the first to rate!</div>';

        return `
          <article class="card menu-card">
            <div class="menu-img-wrap">
              ${imgHtml}
              <span class="menu-badge-float">${esc(item.badge || item.category)}</span>
              <span class="stock-pill ${stClass}">${stIcon}${esc(st)}</span>
            </div>
            <div class="menu-body">
              <div class="menu-head">
                <div>
                  <div style="font-size:0.75rem;color:var(--text-muted);">${esc(item.category)}</div>
                  <h3 style="font-size:1.08rem;font-weight:700;">${esc(item.name)}</h3>
                </div>
                <span class="menu-price">${esc(item.price || 'Display')}</span>
              </div>
              <p class="muted">${esc(item.description)}</p>
              ${tagsHtml ? `<div class="allergen-row">${tagsHtml}</div>` : ''}
              <div class="countdown-bar hidden" data-next-batch="${esc(item.next_batch_at || '')}"></div>
              <div class="row-between" style="margin-top:4px;">
                <span class="stars">${starIcons} <span class="muted">(${avg} • ${(item.reviews || []).length})</span></span>
                <div style="display:flex;gap:6px;flex-wrap:wrap;">
                  <button class="btn btn-sm" onclick="toggleFavorite(${item.id})">${STATE.favorites.includes(item.id) ? '❤️' : '🤍'}</button>
                  <button class="btn btn-sm" onclick="openReviewModal(${item.id}, '${esc(item.name).replace(/'/g, "\\'")}')">★ Rate</button>
                  ${isAdmin ? `<button class="btn btn-sm" onclick="openEditMenuModal(${item.id})">✏️ Edit</button>` : ''}
                  ${isAdmin ? `<button class="btn btn-danger btn-sm" onclick="deleteMenuItem(${item.id})">🗑️</button>` : ''}
                </div>
              </div>
              <div class="reviews-box">${reviewsHtml}</div>
            </div>
          </article>
        `;
      }).join('');

      updateAllCountdowns();
    }

    function renderGallery() {
      const track = document.getElementById('gallery-track');
      if (!STATE.gallery.length) {
        track.innerHTML = '<div class="muted" style="font-size:0.85rem;">No gallery photos uploaded yet. Admin can add bakery photos in the Admin Panel!</div>';
        return;
      }
      track.innerHTML = STATE.gallery.map(g => `
        <div class="gallery-slide">
          <img src="${esc(g.image_url)}" alt="${esc(g.caption || 'Mwachi Bakery')}" />
          ${g.caption ? `<div class="gallery-caption">${esc(g.caption)}</div>` : ''}
        </div>
      `).join('');
    }

    function renderAdminPanels() {
      const s = STATE.settings;
      document.getElementById('set-loc-name').value = s.location_name || '';
      document.getElementById('set-loc-address').value = s.location_address || '';
      document.getElementById('set-opening-hours').value = s.opening_hours || '';
      document.getElementById('set-baking-schedule').value = s.baking_schedule || '';
      document.getElementById('set-bakery-story').value = s.bakery_story || '';
      document.getElementById('set-telegram-url').value = s.telegram_url || '';
      document.getElementById('set-tiktok-url').value = s.tiktok_url || '';

      const an = STATE.analytics;
      if (an) {
        const topRated = (an.top_rated || []).map(x => `${esc(x.name)} (${x.avg.toFixed(1)}★)`).join(', ') || 'No ratings yet';
        const mostRev = (an.most_reviewed || []).map(x => `${esc(x.name)} (${x.count})`).join(', ') || 'No reviews yet';
        document.getElementById('admin-analytics-box').innerHTML = `
          <div class="review-item"><div class="muted">Guests</div><strong style="font-size:1.3rem;">${an.total_guests}</strong></div>
          <div class="review-item"><div class="muted">Special</div><strong style="font-size:1.3rem;color:var(--special);">${an.special_count}</strong></div>
          <div class="review-item"><div class="muted">VVIP</div><strong style="font-size:1.3rem;color:var(--gold);">${an.vvip_count}</strong></div>
          <div class="review-item"><div class="muted">Menu Items</div><strong style="font-size:1.3rem;">${an.total_menu}</strong></div>
          <div class="review-item"><div class="muted">Total Reviews</div><strong style="font-size:1.3rem;">${an.total_reviews}</strong></div>
          <div class="review-item" style="grid-column:1 / -1;">
            <div><strong>⭐ Top Rated:</strong> <span class="muted">${topRated}</span></div>
            <div style="margin-top:4px;"><strong>💬 Most Reviewed:</strong> <span class="muted">${mostRev}</span></div>
          </div>
        `;
      }
      document.getElementById('admin-ann-list').innerHTML = STATE.announcements.map(a => `
        <div class="announcement-item row-between">
          <div><strong>${esc(a.title)}</strong> <span class="muted">(${esc(a.audience)})</span></div>
          <button class="btn btn-danger btn-sm" onclick="deleteAnnouncement(${a.id})">Delete</button>
        </div>
      `).join('');

      document.getElementById('admin-gallery-list').innerHTML = STATE.gallery.map(g => `
        <div class="review-item row-between">
          <span>${esc(g.caption || 'Bakery Photo')}</span>
          <button class="btn btn-danger btn-sm" onclick="deleteGalleryPhoto(${g.id})">Delete</button>
        </div>
      `).join('');

      document.getElementById('admin-users-list').innerHTML = STATE.users
        .filter(u => u.role !== 'admin')
        .map(u => `
          <div class="review-item row-between">
            <div>
              <strong>${esc(u.username)}</strong>
              <span class="tier-badge tier-${esc(u.tier)}" style="margin-left:6px;">${esc(u.tier)}</span>
            </div>
            <select style="width:auto;min-height:38px;padding:5px 9px;font-size:0.86rem;" onchange="promoteUser(${u.id}, this.value)">
              <option value="Regular" ${u.tier === 'Regular' ? 'selected' : ''}>Regular</option>
              <option value="Special" ${u.tier === 'Special' ? 'selected' : ''}>Special</option>
              <option value="VVIP" ${u.tier === 'VVIP' ? 'selected' : ''}>VVIP</option>
            </select>
          </div>
        `).join('') || '<p class="muted">No guest accounts yet.</p>';
    }

    async function handleAddMenu(e) {
      e.preventDefault();
      try {
        const fileInput = document.getElementById('new-menu-img');
        let image_data = '';
        if (fileInput.files && fileInput.files[0]) image_data = await readFileAsDataUrl(fileInput.files[0]);
        await api('/api/menu', {
          method: 'POST',
          body: JSON.stringify({
            name: document.getElementById('new-menu-name').value.trim(),
            category: document.getElementById('new-menu-category').value,
            price: document.getElementById('new-menu-price').value.trim(),
            stock_status: document.getElementById('new-menu-stock').value,
            badge: document.getElementById('new-menu-badge').value.trim(),
            next_batch_at: document.getElementById('new-menu-next-batch').value,
            allergens: getCheckedAllergens('new-menu-allergens'),
            description: document.getElementById('new-menu-desc').value.trim(),
            image_data
          })
        });
        e.target.reset();
        await loadBootstrap();
        showToast('Menu item added!');
      } catch (err) { showToast(err.message); }
    }

    function openEditMenuModal(id) {
      const item = STATE.menu.find(m => m.id === id);
      if (!item) return;
      document.getElementById('edit-menu-id').value = item.id;
      document.getElementById('edit-menu-name').value = item.name;
      document.getElementById('edit-menu-category').value = item.category;
      document.getElementById('edit-menu-price').value = item.price || '';
      document.getElementById('edit-menu-stock').value = item.stock_status || 'Available';
      document.getElementById('edit-menu-badge').value = item.badge || '';
      document.getElementById('edit-menu-next-batch').value = item.next_batch_at || '';
      document.getElementById('edit-menu-desc').value = item.description || '';
      document.getElementById('edit-menu-img').value = '';
      setCheckedAllergens('edit-menu-allergens', item.allergens || '');
      document.getElementById('edit-menu-modal').classList.add('open');
    }

    function closeEditMenuModal() { document.getElementById('edit-menu-modal').classList.remove('open'); }

    async function handleSaveEditMenu(e) {
      e.preventDefault();
      try {
        const id = document.getElementById('edit-menu-id').value;
        const fileInput = document.getElementById('edit-menu-img');
        const payload = {
          name: document.getElementById('edit-menu-name').value.trim(),
          category: document.getElementById('edit-menu-category').value,
          price: document.getElementById('edit-menu-price').value.trim(),
          stock_status: document.getElementById('edit-menu-stock').value,
          badge: document.getElementById('edit-menu-badge').value.trim(),
          next_batch_at: document.getElementById('edit-menu-next-batch').value,
          allergens: getCheckedAllergens('edit-menu-allergens'),
          description: document.getElementById('edit-menu-desc').value.trim()
        };
        if (fileInput.files && fileInput.files[0]) payload.image_data = await readFileAsDataUrl(fileInput.files[0]);
        await api(`/api/menu/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
        closeEditMenuModal();
        await loadBootstrap();
        showToast('Menu item updated!');
      } catch (err) { showToast(err.message); }
    }

    async function deleteMenuItem(id) {
      await api(`/api/menu/${id}`, { method: 'DELETE' });
      await loadBootstrap();
      showToast('Menu item removed.');
    }

    async function handleSaveStory(e) {
      e.preventDefault();
      await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ bakery_story: document.getElementById('set-bakery-story').value.trim() })
      });
      await loadBootstrap();
      showToast('Bakery story updated!');
    }

    async function handleAddGalleryPhoto(e) {
      e.preventDefault();
      try {
        const file = document.getElementById('new-gallery-img').files[0];
        if (!file) return;
        const image_data = await readFileAsDataUrl(file);
        await api('/api/gallery', {
          method: 'POST',
          body: JSON.stringify({
            caption: document.getElementById('new-gallery-caption').value.trim(),
            image_data
          })
        });
        e.target.reset();
        await loadBootstrap();
        showToast('Photo added to gallery!');
      } catch (err) { showToast(err.message); }
    }

    async function deleteGalleryPhoto(id) {
      await api(`/api/gallery/${id}`, { method: 'DELETE' });
      await loadBootstrap();
      showToast('Gallery photo deleted.');
    }

    async function handleAddAnnouncement(e) {
      e.preventDefault();
      await api('/api/announcements', {
        method: 'POST',
        body: JSON.stringify({
          title: document.getElementById('new-ann-title').value.trim(),
          audience: document.getElementById('new-ann-audience').value,
          message: document.getElementById('new-ann-msg').value.trim()
        })
      });
      e.target.reset();
      await loadBootstrap();
      showToast('Announcement published!');
    }

    async function deleteAnnouncement(id) {
      await api(`/api/announcements/${id}`, { method: 'DELETE' });
      await loadBootstrap();
      showToast('Announcement deleted.');
    }

    async function handleSaveLocation(e) {
      e.preventDefault();
      await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({
          location_name: document.getElementById('set-loc-name').value.trim(),
          location_address: document.getElementById('set-loc-address').value.trim(),
          opening_hours: document.getElementById('set-opening-hours').value.trim(),
          baking_schedule: document.getElementById('set-baking-schedule').value.trim()
        })
      });
      await loadBootstrap();
      showToast('Store location saved!');
    }

    async function promoteUser(userId, tier) {
      await api('/api/users/promote', { method: 'POST', body: JSON.stringify({ user_id: userId, tier }) });
      await loadBootstrap();
      showToast(`Customer updated to ${tier}!`);
    }

    function openSettingsModal() {
      const u = STATE.user, s = STATE.settings;
      document.getElementById('settings-user-name').textContent = u.username;
      document.getElementById('settings-user-role').textContent = `${u.role.toUpperCase()} • ${u.tier}`;
      if (u.role === 'admin') {
        document.getElementById('set-bakery-name').value = s.bakery_name || 'Mwachi';
        document.getElementById('set-admin-name').value = s.admin_display_name || '';
        document.getElementById('set-tagline').value = s.tagline || '';
        document.getElementById('set-theme-color').value = s.theme_color || '#ffffff';
      }
      const myRevBox = document.getElementById('my-reviews-list');
      myRevBox.innerHTML = (STATE.myReviews || []).length
        ? STATE.myReviews.map(r => `
            <div class="review-item">
              <div class="row-between"><strong>${esc(r.menu_name)}</strong><span class="stars">${'★'.repeat(r.rating)}</span></div>
              <div class="muted">${esc(r.comment)}</div>
            </div>
          `).join('')
        : '<div class="muted" style="font-size:0.84rem;">You have not posted any reviews yet.</div>';
      document.getElementById('settings-modal').classList.add('open');
    }

    function closeSettingsModal() { document.getElementById('settings-modal').classList.remove('open'); }
    function pickTheme(hex) { document.getElementById('set-theme-color').value = hex; }

    async function handleSaveBranding(e) {
      e.preventDefault();
      const payload = {
        bakery_name: document.getElementById('set-bakery-name').value.trim(),
        admin_display_name: document.getElementById('set-admin-name').value.trim(),
        tagline: document.getElementById('set-tagline').value.trim(),
        theme_color: document.getElementById('set-theme-color').value
      };
      const favFile = document.getElementById('set-favicon-file');
      if (favFile.files && favFile.files[0]) payload.favicon_data = await readFileAsDataUrl(favFile.files[0]);
      await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) });
      favFile.value = '';
      await loadBootstrap();
      closeSettingsModal();
      showToast('Branding & theme saved!');
    }

    async function resetFavicon() {
      await api('/api/settings', { method: 'POST', body: JSON.stringify({ favicon_url: '/favicon.svg' }) });
      await loadBootstrap();
      showToast('Restored default shop favicon!');
    }

    function openReviewModal(menuId, itemName) {
      document.getElementById('review-menu-id').value = menuId;
      document.getElementById('review-modal-title').textContent = `Rate: ${itemName}`;
      document.getElementById('review-comment').value = '';
      document.getElementById('review-modal').classList.add('open');
    }

    function closeReviewModal() { document.getElementById('review-modal').classList.remove('open'); }

    async function handleSubmitReview(e) {
      e.preventDefault();
      await api('/api/reviews', {
        method: 'POST',
        body: JSON.stringify({
          menu_id: Number(document.getElementById('review-menu-id').value),
          rating: Number(document.getElementById('review-rating').value),
          comment: document.getElementById('review-comment').value.trim()
        })
      });
      closeReviewModal();
      await loadBootstrap();
      showToast('Thank you for your review!');
    }

    function openReplyModal(reviewId, existing) {
      document.getElementById('reply-review-id').value = reviewId;
      document.getElementById('reply-text').value = existing || '';
      document.getElementById('reply-modal').classList.add('open');
    }

    function closeReplyModal() { document.getElementById('reply-modal').classList.remove('open'); }

    async function handleSubmitReply(e) {
      e.preventDefault();
      const id = document.getElementById('reply-review-id').value;
      await api(`/api/reviews/${id}/reply`, {
        method: 'POST',
        body: JSON.stringify({ reply: document.getElementById('reply-text').value.trim() })
      });
      closeReplyModal();
      await loadBootstrap();
      showToast('Reply posted!');
    }

    async function deleteReview(id) {
      await api(`/api/reviews/${id}`, { method: 'DELETE' });
      await loadBootstrap();
      showToast('Comment deleted.');
    }

    function normalizeExternalUrl(url) {
      const u = (url || '').trim();
      if (!u) return '';
      return /^https?:\/\//i.test(u) ? u : 'https://' + u;
    }

    function openOrderUsModal() {
      const s = STATE.settings || {};
      const tg = normalizeExternalUrl(s.telegram_url);
      const tt = normalizeExternalUrl(s.tiktok_url);
      const box = document.getElementById('order-us-links');
      box.innerHTML = `
        ${tg ? `<a class="btn btn-primary" href="${esc(tg)}" target="_blank" rel="noopener noreferrer" style="width:100%;">✈️ Order on Telegram</a>`
             : `<div class="review-item muted">✈️ Telegram link not set by admin yet.</div>`}
        ${tt ? `<a class="btn" href="${esc(tt)}" target="_blank" rel="noopener noreferrer" style="width:100%;border-color:var(--accent);">🎵 Visit our TikTok</a>`
             : `<div class="review-item muted">🎵 TikTok link not set by admin yet.</div>`}
      `;
      document.getElementById('order-us-modal').classList.add('open');
    }

    function closeOrderUsModal() { document.getElementById('order-us-modal').classList.remove('open'); }

    async function handleSaveOrderLinks(e) {
      e.preventDefault();
      await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({
          telegram_url: document.getElementById('set-telegram-url').value.trim(),
          tiktok_url: document.getElementById('set-tiktok-url').value.trim()
        })
      });
      await loadBootstrap();
      showToast('Telegram & TikTok links saved!');
    }

    async function toggleFavorite(menuId) {
      try {
        const res = await api('/api/favorites/toggle', {
          method: 'POST',
          body: JSON.stringify({ menu_id: menuId })
        });
        await loadBootstrap();
        showToast(res.favorited ? 'Saved to Favorites!' : 'Removed from Favorites.');
      } catch (err) { showToast(err.message); }
    }

    async function handleChangePassword(e) {
      e.preventDefault();
      try {
        await api('/api/password', {
          method: 'POST',
          body: JSON.stringify({
            current_password: document.getElementById('pwd-current').value,
            new_password: document.getElementById('pwd-new').value
          })
        });
        e.target.reset();
        showToast('Password updated!');
      } catch (err) { showToast(err.message); }
    }

    function openVipCardModal() {
      const u = STATE.user;
      if (!u) return;
      const memberCode = `MWC-${u.tier.toUpperCase()}-${String(u.id).padStart(4, '0')}`;
      document.getElementById('vip-card-bakery').textContent = (STATE.settings.bakery_name || 'MWACHI') + ' BAKERY';
      document.getElementById('vip-card-name').textContent = u.username;
      const tEl = document.getElementById('vip-card-tier');
      tEl.textContent = (u.tier === 'VVIP' ? '👑 ' : '⭐ ') + u.tier + ' MEMBER';
      tEl.className = 'tier-badge tier-' + u.tier;
      document.getElementById('vip-card-id').textContent = memberCode;
      document.getElementById('vip-qr-box').innerHTML = generateQrSvg(memberCode + ':' + u.username);
      document.getElementById('vip-card-modal').classList.add('open');
    }

    function closeVipCardModal() { document.getElementById('vip-card-modal').classList.remove('open'); }

        setInterval(updateAllCountdowns, 1000);

    window.addEventListener('DOMContentLoaded', async () => {
      applyLanguage();
      if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(() => {}); }
      try {
        await loadBootstrap();
        applyLanguage();
      } catch (err) {
        showToast('Error loading bakery data.');
      } finally {
        setTimeout(() => {
          document.getElementById('loading-screen').classList.add('hidden');
        }, 700);
      }
    });
  </script>
</body>
</html>"""


class MwachiHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def send_json(self, status: int, payload: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def get_current_user(self, conn: sqlite3.Connection) -> dict | None:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None
        jar = cookies.SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get("mwachi_session")
        if not morsel:
            return None
        sess = conn.execute(
            "SELECT user_id FROM sessions WHERE token = ?",
            (morsel.value,),
        ).fetchone()
        if not sess:
            return None
        row = conn.execute(
            "SELECT id, username, role, tier, created_at FROM users WHERE id = ?",
            (sess["user_id"],),
        ).fetchone()
        return dict(row) if row else None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            page = render_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if path == "/manifest.webmanifest":
            with get_db() as conn:
                s = get_settings(conn)
            fav = s.get("favicon_url") or "/favicon.svg"
            manifest = {
                "name": f"{s.get('bakery_name', 'Mwachi')} Bakery",
                "short_name": s.get("bakery_name", "Mwachi"),
                "description": s.get("tagline", "Mwachi Bakery Display Menu & Location"),
                "start_url": "/",
                "display": "standalone",
                "background_color": "#080808",
                "theme_color": s.get("theme_color", "#ffffff"),
                "icons": [
                    {"src": fav, "sizes": "192x192", "type": "image/svg+xml" if fav.endswith(".svg") else "image/png", "purpose": "any maskable"},
                    {"src": fav, "sizes": "512x512", "type": "image/svg+xml" if fav.endswith(".svg") else "image/png", "purpose": "any maskable"},
                ],
            }
            data = json.dumps(manifest).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/sw.js":
            sw_code = b"self.addEventListener('install', () => self.skipWaiting());\nself.addEventListener('activate', e => e.waitUntil(self.clients.claim()));\nself.addEventListener('fetch', () => {});\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(sw_code)))
            self.end_headers()
            self.wfile.write(sw_code)
            return

        if path in ("/favicon.svg", "/favicon.ico"):
            data = DEFAULT_SHOP_FAVICON_SVG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/uploads/"):
            filename = Path(path).name
            target = (UPLOAD_DIR / filename).resolve()
            if not str(target).startswith(str(UPLOAD_DIR.resolve())) or not target.is_file():
                self.send_json(404, {"error": "File not found"})
                return
            mime, _ = mimetypes.guess_type(str(target))
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: queue.Queue = queue.Queue()
            with EVENTS_LOCK:
                EVENT_CLIENTS.append(q)
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=20)
                        self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with EVENTS_LOCK:
                    if q in EVENT_CLIENTS:
                        EVENT_CLIENTS.remove(q)
            return

        if path == "/api/backup":
            with get_db() as conn:
                user = self.get_current_user(conn)
                if not user or user["role"] != "admin":
                    self.send_json(403, {"error": "Admin access required"})
                    return
                backup_data = {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "settings": get_settings(conn),
                    "users": [dict(r) for r in conn.execute("SELECT id, username, role, tier, created_at FROM users").fetchall()],
                    "menu_items": [dict(r) for r in conn.execute("SELECT * FROM menu_items").fetchall()],
                    "reviews": [dict(r) for r in conn.execute("SELECT * FROM reviews").fetchall()],
                    "announcements": [dict(r) for r in conn.execute("SELECT * FROM announcements").fetchall()],
                    "gallery_photos": [dict(r) for r in conn.execute("SELECT * FROM gallery_photos").fetchall()],
                    "favorites": [dict(r) for r in conn.execute("SELECT * FROM favorites").fetchall()],
                }
                payload = json.dumps(backup_data, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="mwachi_backup.json"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            return

        if path == "/api/bootstrap":
            with get_db() as conn:
                user = self.get_current_user(conn)
                settings = get_settings(conn)
                if not user:
                    self.send_json(200, {"user": None, "settings": settings})
                    return

                menu_rows = conn.execute("SELECT * FROM menu_items ORDER BY id DESC").fetchall()
                menu_list = []
                for m in menu_rows:
                    reviews = conn.execute(
                        """SELECT r.id, r.rating, r.comment, r.admin_reply, r.admin_reply_at, r.created_at, u.username, u.tier
                           FROM reviews r
                           JOIN users u ON u.id = r.user_id
                           WHERE r.menu_id = ?
                           ORDER BY r.id DESC""",
                        (m["id"],),
                    ).fetchall()
                    rev_list = [dict(r) for r in reviews]
                    avg = sum(r["rating"] for r in rev_list) / len(rev_list) if rev_list else None
                    item_dict = dict(m)
                    item_dict["reviews"] = rev_list
                    item_dict["avg_rating"] = avg
                    menu_list.append(item_dict)

                if user["role"] == "admin" or user["tier"] == "VVIP":
                    allowed = ("All", "Special & VVIP", "VVIP Only")
                elif user["tier"] == "Special":
                    allowed = ("All", "Special & VVIP")
                else:
                    allowed = ("All",)
                placeholders = ",".join("?" for _ in allowed)
                ann_rows = conn.execute(
                    f"SELECT * FROM announcements WHERE audience IN ({placeholders}) ORDER BY id DESC",
                    allowed,
                ).fetchall()

                gallery_rows = conn.execute("SELECT * FROM gallery_photos ORDER BY id DESC").fetchall()

                users_list = []
                if user["role"] == "admin":
                    users_list = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT id, username, role, tier, created_at FROM users ORDER BY id DESC"
                        ).fetchall()
                    ]

                fav_rows = conn.execute(
                    "SELECT menu_id FROM favorites WHERE user_id = ?", (user["id"],)
                ).fetchall()
                favorites = [r["menu_id"] for r in fav_rows]

                my_rev_rows = conn.execute(
                    """SELECT r.id, r.rating, r.comment, r.created_at, m.name AS menu_name
                       FROM reviews r
                       JOIN menu_items m ON m.id = r.menu_id
                       WHERE r.user_id = ?
                       ORDER BY r.id DESC""",
                    (user["id"],),
                ).fetchall()
                my_reviews = [dict(r) for r in my_rev_rows]

                analytics = None
                if user["role"] == "admin":
                    guest_rows = [u for u in users_list if u["role"] != "admin"]
                    rated_items = [
                        {"name": m["name"], "avg": m["avg_rating"]}
                        for m in menu_list if m["avg_rating"] is not None
                    ]
                    rated_items.sort(key=lambda x: x["avg"], reverse=True)
                    reviewed_items = [
                        {"name": m["name"], "count": len(m["reviews"])}
                        for m in menu_list if len(m["reviews"]) > 0
                    ]
                    reviewed_items.sort(key=lambda x: x["count"], reverse=True)
                    analytics = {
                        "total_guests": len(guest_rows),
                        "special_count": sum(1 for u in guest_rows if u["tier"] == "Special"),
                        "vvip_count": sum(1 for u in guest_rows if u["tier"] == "VVIP"),
                        "total_menu": len(menu_list),
                        "total_reviews": sum(len(m["reviews"]) for m in menu_list),
                        "top_rated": rated_items[:3],
                        "most_reviewed": reviewed_items[:3],
                    }

                self.send_json(
                    200,
                    {
                        "user": user,
                        "settings": settings,
                        "menu": menu_list,
                        "announcements": [dict(a) for a in ann_rows],
                        "gallery": [dict(g) for g in gallery_rows],
                        "users": users_list,
                        "favorites": favorites,
                        "my_reviews": my_reviews,
                        "analytics": analytics,
                    },
                )
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            body = self.read_json()
        except Exception:
            self.send_json(400, {"error": "Invalid JSON body"})
            return

        if path == "/api/login":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            with get_db() as conn:
                row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                if not row or not verify_password(password, row["password_hash"]):
                    self.send_json(401, {"error": "Invalid username or password"})
                    return
                token = secrets.token_urlsafe(32)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                    (token, row["id"], now),
                )
                conn.commit()
                self.send_json(
                    200,
                    {"ok": True, "role": row["role"], "tier": row["tier"]},
                    {"Set-Cookie": f"mwachi_session={token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"},
                )
            return

        if path == "/api/register":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            if len(username) < 2 or len(password) < 4:
                self.send_json(400, {"error": "Username (>=2 chars) and password (>=4 chars) required"})
                return
            with get_db() as conn:
                if conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
                    self.send_json(409, {"error": "Username already exists"})
                    return
                now = datetime.now(timezone.utc).isoformat()
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, role, tier, created_at) VALUES (?, ?, 'guest', 'Regular', ?)",
                    (username, hash_password(password), now),
                )
                token = secrets.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                    (token, cur.lastrowid, now),
                )
                conn.commit()
                self.send_json(
                    201,
                    {"ok": True},
                    {"Set-Cookie": f"mwachi_session={token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"},
                )
            return

        if path == "/api/logout":
            cookie_header = self.headers.get("Cookie", "")
            if cookie_header:
                jar = cookies.SimpleCookie()
                jar.load(cookie_header)
                morsel = jar.get("mwachi_session")
                if morsel:
                    with get_db() as conn:
                        conn.execute("DELETE FROM sessions WHERE token = ?", (morsel.value,))
            self.send_json(
                200,
                {"ok": True},
                {"Set-Cookie": "mwachi_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"},
            )
            return

        with get_db() as conn:
            user = self.get_current_user(conn)
            if not user:
                self.send_json(401, {"error": "Authentication required"})
                return

            if path == "/api/favorites/toggle":
                menu_id = body.get("menu_id")
                if not menu_id:
                    self.send_json(400, {"error": "menu_id required"})
                    return
                existing = conn.execute(
                    "SELECT id FROM favorites WHERE user_id = ? AND menu_id = ?",
                    (user["id"], menu_id),
                ).fetchone()
                if existing:
                    conn.execute("DELETE FROM favorites WHERE id = ?", (existing["id"],))
                    self.send_json(200, {"ok": True, "favorited": False})
                else:
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "INSERT INTO favorites (user_id, menu_id, created_at) VALUES (?, ?, ?)",
                        (user["id"], menu_id, now),
                    )
                    self.send_json(200, {"ok": True, "favorited": True})
                return

            if path == "/api/password":
                cur_pw = body.get("current_password") or ""
                new_pw = body.get("new_password") or ""
                if len(new_pw) < 4:
                    self.send_json(400, {"error": "New password must be at least 4 characters"})
                    return
                urow = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
                if not urow or not verify_password(cur_pw, urow["password_hash"]):
                    self.send_json(401, {"error": "Current password is incorrect"})
                    return
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_pw), user["id"]))
                self.send_json(200, {"ok": True})
                return

            if path == "/api/reviews":
                menu_id = body.get("menu_id")
                rating = int(body.get("rating", 5))
                comment = (body.get("comment") or "").strip()
                if not menu_id or not (1 <= rating <= 5) or not comment:
                    self.send_json(400, {"error": "Valid menu item, rating (1-5), and comment required"})
                    return
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO reviews (menu_id, user_id, rating, comment, created_at) VALUES (?, ?, ?, ?, ?)",
                    (menu_id, user["id"], rating, comment, now),
                )
                conn.commit()
                broadcast_event("refresh")
                self.send_json(201, {"ok": True})
                return

            if user["role"] != "admin":
                self.send_json(403, {"error": "Admin access required"})
                return

            if path.startswith("/api/reviews/") and path.endswith("/reply"):
                review_id = path.split("/")[-2]
                reply = (body.get("reply") or "").strip()
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE reviews SET admin_reply = ?, admin_reply_at = ? WHERE id = ?",
                    (reply, now, review_id),
                )
                conn.commit()
                broadcast_event("refresh")
                self.send_json(200, {"ok": True})
                return

            if path == "/api/menu":
                name = (body.get("name") or "").strip()
                category = (body.get("category") or "Bread").strip()
                price = (body.get("price") or "").strip()
                badge = (body.get("badge") or "Fresh Daily").strip()
                stock_status = body.get("stock_status") or "Available"
                next_batch_at = (body.get("next_batch_at") or "").strip()
                allergens = (body.get("allergens") or "").strip()
                description = (body.get("description") or "").strip()
                image_data = body.get("image_data") or ""
                if not name or not description:
                    self.send_json(400, {"error": "Name and description are required"})
                    return
                image_url = ""
                if image_data:
                    try:
                        image_url = save_uploaded_image(image_data, prefix="menu")
                    except ValueError as exc:
                        self.send_json(400, {"error": str(exc)})
                        return
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """INSERT INTO menu_items
                       (name, category, price, description, image_url, badge, stock_status, next_batch_at, allergens, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, category, price, description, image_url, badge, stock_status, next_batch_at, allergens, now),
                )
                conn.commit()
                broadcast_event("refresh")
                self.send_json(201, {"ok": True})
                return

            if path == "/api/gallery":
                caption = (body.get("caption") or "").strip()
                image_data = body.get("image_data") or ""
                if not image_data:
                    self.send_json(400, {"error": "Image is required"})
                    return
                try:
                    image_url = save_uploaded_image(image_data, prefix="gallery")
                except ValueError as exc:
                    self.send_json(400, {"error": str(exc)})
                    return
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO gallery_photos (caption, image_url, created_at) VALUES (?, ?, ?)",
                    (caption, image_url, now),
                )
                conn.commit()
                broadcast_event("refresh")
                self.send_json(201, {"ok": True})
                return

            if path == "/api/announcements":
                title = (body.get("title") or "").strip()
                message = (body.get("message") or "").strip()
                audience = body.get("audience") or "All"
                if audience not in ("All", "Special & VVIP", "VVIP Only"):
                    audience = "All"
                if not title or not message:
                    self.send_json(400, {"error": "Title and message are required"})
                    return
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO announcements (title, message, audience, pinned, created_at) VALUES (?, ?, ?, 1, ?)",
                    (title, message, audience, now),
                )
                conn.commit()
                broadcast_event("refresh")
                self.send_json(201, {"ok": True})
                return

            if path == "/api/settings":
                if body.get("favicon_data"):
                    try:
                        body["favicon_url"] = save_uploaded_image(body["favicon_data"], prefix="favicon")
                    except ValueError as exc:
                        self.send_json(400, {"error": str(exc)})
                        return
                allowed_keys = {
                    "bakery_name",
                    "admin_display_name",
                    "theme_color",
                    "location_name",
                    "location_address",
                    "opening_hours",
                    "baking_schedule",
                    "favicon_url",
                    "tagline",
                    "bakery_story",
                    "telegram_url",
                    "tiktok_url",
                }
                for k, v in body.items():
                    if k in allowed_keys and isinstance(v, str):
                        conn.execute(
                            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (k, v.strip()),
                        )
                conn.commit()
                broadcast_event("refresh")
                self.send_json(200, {"ok": True})
                return

            if path == "/api/users/promote":
                uid = body.get("user_id")
                tier = body.get("tier")
                if tier not in ("Regular", "Special", "VVIP"):
                    self.send_json(400, {"error": "Invalid customer tier"})
                    return
                conn.execute("UPDATE users SET tier = ? WHERE id = ? AND role != 'admin'", (tier, uid))
                conn.commit()
                broadcast_event("refresh")
                self.send_json(200, {"ok": True})
                return

        self.send_json(404, {"error": "Not found"})

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            body = self.read_json()
        except Exception:
            self.send_json(400, {"error": "Invalid JSON body"})
            return

        with get_db() as conn:
            user = self.get_current_user(conn)
            if not user or user["role"] != "admin":
                self.send_json(403, {"error": "Admin access required"})
                return

            if path.startswith("/api/menu/"):
                item_id = path.rsplit("/", 1)[-1]
                existing = conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
                if not existing:
                    self.send_json(404, {"error": "Menu item not found"})
                    return

                image_url = existing["image_url"]
                if body.get("image_data"):
                    try:
                        image_url = save_uploaded_image(body["image_data"], prefix="menu")
                    except ValueError as exc:
                        self.send_json(400, {"error": str(exc)})
                        return

                conn.execute(
                    """UPDATE menu_items
                       SET name = ?, category = ?, price = ?, description = ?, image_url = ?,
                           badge = ?, stock_status = ?, next_batch_at = ?, allergens = ?
                       WHERE id = ?""",
                    (
                        (body.get("name") or existing["name"]).strip(),
                        (body.get("category") or existing["category"]).strip(),
                        (body.get("price") if body.get("price") is not None else existing["price"]).strip(),
                        (body.get("description") or existing["description"]).strip(),
                        image_url,
                        (body.get("badge") if body.get("badge") is not None else existing["badge"]).strip(),
                        body.get("stock_status") or existing["stock_status"],
                        (body.get("next_batch_at") if body.get("next_batch_at") is not None else existing["next_batch_at"]).strip(),
                        (body.get("allergens") if body.get("allergens") is not None else existing["allergens"]).strip(),
                        item_id,
                    ),
                )
                conn.commit()
                broadcast_event("refresh")
                self.send_json(200, {"ok": True})
                return

        self.send_json(404, {"error": "Not found"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        with get_db() as conn:
            user = self.get_current_user(conn)
            if not user or user["role"] != "admin":
                self.send_json(403, {"error": "Admin access required"})
                return

            if path.startswith("/api/menu/"):
                item_id = path.rsplit("/", 1)[-1]
                conn.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
                conn.commit()
                broadcast_event("refresh")
                self.send_json(200, {"ok": True})
                return

            if path.startswith("/api/announcements/"):
                ann_id = path.rsplit("/", 1)[-1]
                conn.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
                conn.commit()
                broadcast_event("refresh")
                self.send_json(200, {"ok": True})
                return

            if path.startswith("/api/gallery/"):
                gal_id = path.rsplit("/", 1)[-1]
                conn.execute("DELETE FROM gallery_photos WHERE id = ?", (gal_id,))
                conn.commit()
                broadcast_event("refresh")
                self.send_json(200, {"ok": True})
                return

            if path.startswith("/api/reviews/"):
                rev_id = path.rsplit("/", 1)[-1]
                conn.execute("DELETE FROM reviews WHERE id = ?", (rev_id,))
                conn.commit()
                broadcast_event("refresh")
                self.send_json(200, {"ok": True})
                return

        self.send_json(404, {"error": "Not found"})


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), MwachiHandler)
    print(f"Mwachi Bakery running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Mwachi Bakery...")
        server.server_close()


if __name__ == "__main__":
    main()