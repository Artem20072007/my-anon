import aiosqlite
import random
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH = "data/bot.sqlite3"
CITY = "Чистополь"

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 user_id INTEGER PRIMARY KEY,
 username TEXT,
 name TEXT NOT NULL,
 age INTEGER NOT NULL CHECK(age >= 14 AND age <= 99),
 gender TEXT NOT NULL CHECK(gender IN ('m','f')),
 target_gender TEXT NOT NULL CHECK(target_gender IN ('m','f','all')),
 target_age_min INTEGER DEFAULT 14,
 target_age_max INTEGER DEFAULT 99,
 city TEXT NOT NULL DEFAULT 'Чистополь',
 bio TEXT DEFAULT '',
 photos TEXT DEFAULT '',
 video_id TEXT DEFAULT '',
 is_verified INTEGER DEFAULT 0,
 referral_id INTEGER,
 is_active INTEGER NOT NULL DEFAULT 1,
 is_banned INTEGER NOT NULL DEFAULT 0,
 premium_until TEXT DEFAULT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 from_user_id INTEGER NOT NULL,
 to_user_id INTEGER NOT NULL,
 reaction TEXT NOT NULL CHECK(reaction IN ('like','dislike')),
 created_at TEXT NOT NULL,
 UNIQUE(from_user_id, to_user_id)
);
CREATE TABLE IF NOT EXISTS skipped_likes (
 user_id INTEGER NOT NULL,
 target_id INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(user_id, target_id)
);
CREATE TABLE IF NOT EXISTS reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 reporter_id INTEGER NOT NULL,
 target_id INTEGER NOT NULL,
 reason TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(reporter_id, target_id)
);
CREATE TABLE IF NOT EXISTS matches (
 user1_id INTEGER NOT NULL,
 user2_id INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(user1_id, user2_id)
);
CREATE TABLE IF NOT EXISTS blocks (
 blocker_id INTEGER NOT NULL,
 blocked_id INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(blocker_id, blocked_id)
);
CREATE TABLE IF NOT EXISTS views (
    viewer_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(viewer_id, target_id)
);
CREATE TABLE IF NOT EXISTS roulette_rooms (
    room_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user1_id INTEGER NOT NULL,
    user2_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roulette_queue (
    user_id INTEGER PRIMARY KEY,
    joined_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_feed ON users(is_active, is_banned, city, age, gender);
CREATE INDEX IF NOT EXISTS idx_reactions_to ON reactions(to_user_id, reaction);
CREATE INDEX IF NOT EXISTS idx_reactions_from ON reactions(from_user_id);
CREATE INDEX IF NOT EXISTS idx_reports_target ON reports(target_id);
CREATE INDEX IF NOT EXISTS idx_views_target ON views(target_id);
'''

def now():
    return datetime.now(timezone.utc).isoformat()

def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None

async def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.executescript(SCHEMA)
    # Безопасные миграции
    for stmt in [
        "ALTER TABLE users ADD COLUMN premium_until TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN video_id TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN referral_id INTEGER",
    ]:
        try:
            await db.execute(stmt)
        except aiosqlite.OperationalError:
            pass
    # 🔧 Фикс бага: старые строки после ALTER TABLE могут иметь NULL в target_age_*
    await db.execute("UPDATE users SET target_age_min=14 WHERE target_age_min IS NULL")
    await db.execute("UPDATE users SET target_age_max=99 WHERE target_age_max IS NULL")
    await db.execute("UPDATE users SET is_active=1 WHERE is_active IS NULL")
    await db.execute("UPDATE users SET is_banned=0 WHERE is_banned IS NULL")
    await db.commit()
    return db

async def get_user(db, uid):
    cur = await db.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    return await cur.fetchone()

async def is_banned(db, uid):
    cur = await db.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    return bool(row and row["is_banned"])

async def save_user(db, data):
    photos_str = ",".join(data.get("photos", []))
    old = await get_user(db, data["user_id"])

    if old:
        is_active = data.get("is_active", old["is_active"])
        target_age_min = data.get("target_age_min", old["target_age_min"]) or 14
        target_age_max = data.get("target_age_max", old["target_age_max"]) or 99
        referral_id = data.get("referral_id", old["referral_id"])
    else:
        is_active = 1
        target_age_min = data.get("target_age_min", 14)
        target_age_max = data.get("target_age_max", 99)
        referral_id = data.get("referral_id")

    await db.execute('''INSERT INTO users
    (user_id,username,name,age,gender,target_gender,target_age_min,target_age_max,city,bio,photos,video_id,referral_id,is_active,created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(user_id) DO UPDATE SET 
    username=excluded.username,name=excluded.name,age=excluded.age,gender=excluded.gender,
    target_gender=excluded.target_gender,target_age_min=excluded.target_age_min,
    target_age_max=excluded.target_age_max,city=excluded.city,bio=excluded.bio,
    photos=excluded.photos,video_id=excluded.video_id,referral_id=excluded.referral_id,
    is_active=excluded.is_active''',
    (data["user_id"], data.get("username"), data["name"], data["age"], data["gender"],
     data["target_gender"], target_age_min, target_age_max,
     CITY, data.get("bio", ""), photos_str, data.get("video_id", ""), referral_id,
     is_active, now()))
    await db.commit()

async def next_profile(db, me, exclude_seen=True):
    if not me:
        return None
    params = {
        "uid": me["user_id"], "city": CITY,
        "min_age": me["target_age_min"] or 14,
        "max_age": me["target_age_max"] or 99,
        "target_gender": me["target_gender"], "gender": me["gender"]
    }
    query = '''SELECT u.* FROM users u
      WHERE u.user_id != :uid AND u.city = :city COLLATE NOCASE
      AND u.is_active = 1 AND u.is_banned = 0
      AND u.age BETWEEN :min_age AND :max_age
      AND (u.gender = :target_gender OR :target_gender = 'all')
      AND (u.target_gender = :gender OR u.target_gender = 'all')
      AND NOT EXISTS (SELECT 1 FROM blocks b
          WHERE (b.blocker_id = u.user_id AND b.blocked_id = :uid)
             OR (b.blocker_id = :uid AND b.blocked_id = u.user_id))'''
    if exclude_seen:
        query += " AND NOT EXISTS (SELECT 1 FROM reactions r WHERE r.from_user_id = :uid AND r.to_user_id = u.user_id)"
    query += " ORDER BY u.created_at DESC LIMIT 100"
    cur = await db.execute(query, params)
    rows = await cur.fetchall()
    if not rows:
        return None

    now_dt = datetime.now(timezone.utc)
    premium, regular = [], []
    for row in rows:
        pu = _parse_dt(row["premium_until"]) if "premium_until" in row.keys() else None
        if pu and pu > now_dt:
            premium.append(row)
        else:
            regular.append(row)

    if premium:
        return random.choice(premium)
    return random.choice(regular) if regular else None

async def add_reaction(db, from_id, to_id, reaction):
    if from_id == to_id:
        return False
    try:
        await db.execute("INSERT INTO reactions(from_user_id,to_user_id,reaction,created_at) VALUES(?,?,?,?)",
                         (from_id, to_id, reaction, now()))
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False

async def mutual_like(db, a, b):
    cur = await db.execute("SELECT 1 FROM reactions WHERE from_user_id=? AND to_user_id=? AND reaction='like'", (b, a))
    return await cur.fetchone() is not None

async def add_match(db, a, b):
    x, y = sorted((a, b))
    await db.execute("INSERT OR IGNORE INTO matches(user1_id,user2_id,created_at) VALUES(?,?,?)", (x, y, now()))
    await db.commit()

async def like_back(db, uid, target):
    if uid == target:
        return False
    try:
        await db.execute(
            "INSERT OR IGNORE INTO reactions(from_user_id,to_user_id,reaction,created_at) VALUES(?,?,'like',?)",
            (uid, target, now()))
        # 🔧 Фикс: убираем из skipped, если он там был
        await db.execute("DELETE FROM skipped_likes WHERE user_id=? AND target_id=?", (uid, target))
        cur = await db.execute(
            "SELECT 1 FROM reactions WHERE from_user_id=? AND to_user_id=? AND reaction='like'",
            (target, uid))
        mutual = await cur.fetchone()
        if mutual:
            x, y = sorted((uid, target))
            await db.execute(
                "INSERT OR IGNORE INTO matches(user1_id,user2_id,created_at) VALUES(?,?,?)",
                (x, y, now()))
        await db.commit()
        return mutual is not None
    except aiosqlite.Error:
        await db.rollback()
        return False

async def incoming_likes(db, uid, offset=0, limit=1):
    cur = await db.execute('''SELECT u.* FROM users u
      JOIN reactions r ON r.from_user_id = u.user_id
      WHERE r.to_user_id = ? AND r.reaction = 'like'
        AND u.is_active = 1 AND u.is_banned = 0
        AND NOT EXISTS (SELECT 1 FROM reactions mine
            WHERE mine.from_user_id = ? AND mine.to_user_id = u.user_id)
        AND NOT EXISTS (SELECT 1 FROM skipped_likes s
            WHERE s.user_id = ? AND s.target_id = u.user_id)
      ORDER BY r.created_at DESC
      LIMIT ? OFFSET ?''', (uid, uid, uid, limit, offset))
    return await cur.fetchall()

async def count_incoming_likes(db, uid):
    cur = await db.execute('''SELECT COUNT(*) AS n FROM users u
      JOIN reactions r ON r.from_user_id = u.user_id
      WHERE r.to_user_id = ? AND r.reaction = 'like'
        AND u.is_active = 1 AND u.is_banned = 0
        AND NOT EXISTS (SELECT 1 FROM reactions mine
            WHERE mine.from_user_id = ? AND mine.to_user_id = u.user_id)
        AND NOT EXISTS (SELECT 1 FROM skipped_likes s
            WHERE s.user_id = ? AND s.target_id = u.user_id)''', (uid, uid, uid))
    row = await cur.fetchone()
    return row["n"] if row else 0

async def skip_incoming_like(db, uid, target_id):
    try:
        await db.execute("INSERT OR IGNORE INTO skipped_likes(user_id,target_id,created_at) VALUES(?,?,?)",
                         (uid, target_id, now()))
        await db.commit()
    except aiosqlite.Error:
        pass

async def add_report(db, reporter, target, reason):
    if reporter == target:
        return None
    try:
        await db.execute("INSERT INTO reports(reporter_id,target_id,reason,created_at) VALUES(?,?,?,?)",
                         (reporter, target, reason, now()))
        cur = await db.execute("SELECT COUNT(DISTINCT reporter_id) AS n FROM reports WHERE target_id=?", (target,))
        count = (await cur.fetchone())["n"]
        if count >= 3:
            await db.execute("UPDATE users SET is_active=0,is_banned=1 WHERE user_id=?", (target,))
        await db.commit()
        return count
    except aiosqlite.IntegrityError:
        return None

async def block_user(db, blocker, blocked):
    if blocker == blocked:
        return
    await db.execute("INSERT OR IGNORE INTO blocks(blocker_id,blocked_id,created_at) VALUES(?,?,?)",
                     (blocker, blocked, now()))
    await db.commit()

async def undo_reaction(db, from_id, to_id):
    await db.execute("DELETE FROM reactions WHERE from_user_id=? AND to_user_id=?", (from_id, to_id))
    cur = await db.execute("SELECT 1 FROM reactions WHERE from_user_id=? AND to_user_id=? AND reaction='like'",
                           (to_id, from_id))
    mutual = await cur.fetchone()
    if not mutual:
        x, y = sorted((from_id, to_id))
        await db.execute("DELETE FROM matches WHERE user1_id=? AND user2_id=?", (x, y))
    await db.commit()
    return not bool(mutual)

async def get_last_reaction(db, from_id):
    time_limit = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    cur = await db.execute(
        """SELECT r.to_user_id FROM reactions r
           WHERE r.from_user_id=? AND r.created_at > ? AND r.reaction='like'
           AND NOT EXISTS (SELECT 1 FROM matches m WHERE
               (m.user1_id=r.from_user_id AND m.user2_id=r.to_user_id) OR
               (m.user2_id=r.from_user_id AND m.user1_id=r.to_user_id))
           ORDER BY r.id DESC LIMIT 1""", (from_id, time_limit))
    row = await cur.fetchone()
    return row["to_user_id"] if row else None

async def verify_user(db, uid):
    await db.execute("UPDATE users SET is_verified=1 WHERE user_id=?", (uid,))
    await db.commit()

async def add_reward(db, uid, days):
    cur = await db.execute("SELECT premium_until FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    if not row:
        return
    base = datetime.now(timezone.utc)
    pu = _parse_dt(row["premium_until"])
    if pu and pu > base:
        base = pu
    new_until = (base + timedelta(days=days)).isoformat()
    await db.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, uid))
    await db.commit()

async def check_premium(db, uid):
    cur = await db.execute("SELECT premium_until FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    if not row or not row["premium_until"]:
        return False
    pu = _parse_dt(row["premium_until"])
    return bool(pu and pu > datetime.now(timezone.utc))

async def set_premium(db, uid, days):
    await add_reward(db, uid, days)

async def has_like(db, from_id, to_id):
    cur = await db.execute(
        "SELECT 1 FROM reactions WHERE from_user_id=? AND to_user_id=? AND reaction='like'",
        (from_id, to_id))
    return await cur.fetchone() is not None

# --- СТАТИСТИКА И РУЛЕТКА ---

async def register_view(db, viewer_id, target_id):
    if viewer_id == target_id:
        return
    try:
        await db.execute("INSERT OR IGNORE INTO views (viewer_id, target_id, created_at) VALUES (?, ?, ?)",
                         (viewer_id, target_id, now()))
        await db.commit()
    except aiosqlite.Error:
        pass

async def get_profile_stats(db, uid):
    cur = await db.execute("SELECT COUNT(*) n FROM reactions WHERE from_user_id=? AND reaction='like'", (uid,))
    likes = (await cur.fetchone())["n"]
    cur = await db.execute("SELECT COUNT(*) n FROM views WHERE target_id=?", (uid,))
    views = (await cur.fetchone())["n"]
    return likes, views

async def roulette_join_queue(db, uid):
    await db.execute("DELETE FROM roulette_queue WHERE user_id=?", (uid,))
    cur = await db.execute("SELECT user_id FROM roulette_queue WHERE user_id != ? ORDER BY joined_at ASC LIMIT 1", (uid,))
    partner = await cur.fetchone()
    if partner:
        partner_id = partner["user_id"]
        await db.execute("DELETE FROM roulette_queue WHERE user_id=?", (partner_id,))
        await db.execute("INSERT INTO roulette_rooms (user1_id, user2_id, created_at) VALUES (?, ?, ?)", (uid, partner_id, now()))
        await db.commit()
        return partner_id
    else:
        await db.execute("INSERT INTO roulette_queue (user_id, joined_at) VALUES (?, ?)", (uid, now()))
        await db.commit()
        return None

async def roulette_leave(db, uid):
    await db.execute("DELETE FROM roulette_queue WHERE user_id=?", (uid,))
    cur = await db.execute("SELECT room_id, user1_id, user2_id FROM roulette_rooms WHERE user1_id=? OR user2_id=?", (uid, uid))
    room = await cur.fetchone()
    if room:
        partner_id = room["user2_id"] if room["user1_id"] == uid else room["user1_id"]
        await db.execute("DELETE FROM roulette_rooms WHERE room_id=?", (room["room_id"],))
        await db.commit()
        return partner_id
    await db.commit()
    return None

async def roulette_get_partner(db, uid):
    cur = await db.execute("SELECT user1_id, user2_id FROM roulette_rooms WHERE user1_id=? OR user2_id=?", (uid, uid))
    room = await cur.fetchone()
    if room:
        return room["user2_id"] if room["user1_id"] == uid else room["user1_id"]
    return None
