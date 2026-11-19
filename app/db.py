import asyncio
import aiosqlite
import random
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH = "data/bot.sqlite3"
CITY = "Чистополь"

log = logging.getLogger(__name__)

# ================= SCHEMA =================
# Таблицы — БЕЗ closed_at в CREATE (он добавится миграцией)
SCHEMA_TABLES = '''
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
'''

# Индексы — создаются ТОЛЬКО после миграций
SCHEMA_INDEXES = '''
CREATE INDEX IF NOT EXISTS idx_users_feed ON users(is_active, is_banned, city, age, gender);
CREATE INDEX IF NOT EXISTS idx_reactions_to ON reactions(to_user_id, reaction);
CREATE INDEX IF NOT EXISTS idx_reactions_from ON reactions(from_user_id);
CREATE INDEX IF NOT EXISTS idx_reports_target ON reports(target_id);
CREATE INDEX IF NOT EXISTS idx_views_target ON views(target_id);
CREATE INDEX IF NOT EXISTS idx_roulette_rooms_open ON roulette_rooms(closed_at, user1_id, user2_id);
'''

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN premium_until TEXT DEFAULT NULL",
    "ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN video_id TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN referral_id INTEGER",
    "ALTER TABLE roulette_rooms ADD COLUMN closed_at TEXT DEFAULT NULL",
]

_DB_WRITE_LOCK = asyncio.Lock()


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


async def _exec(db, sql, params=(), retries=3):
    last = None
    for i in range(retries):
        try:
            return await db.execute(sql, params)
        except aiosqlite.OperationalError as e:
            last = e
            if "locked" in str(e).lower() and i < retries - 1:
                await asyncio.sleep(0.15 * (i + 1))
                continue
            raise
    if last:
        raise last


async def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")

    # 1) таблицы
    await db.executescript(SCHEMA_TABLES)

    # 2) миграции (ADD COLUMN)
    for stmt in MIGRATIONS:
        try:
            await db.execute(stmt)
        except aiosqlite.OperationalError:
            pass

    # 3) индексы — только после миграций
    await db.executescript(SCHEMA_INDEXES)

    # 4) чистка NULL
    for q in [
        "UPDATE users SET target_age_min=14 WHERE target_age_min IS NULL",
        "UPDATE users SET target_age_max=99 WHERE target_age_max IS NULL",
        "UPDATE users SET is_active=1 WHERE is_active IS NULL",
        "UPDATE users SET is_banned=0 WHERE is_banned IS NULL",
        "UPDATE users SET photos='' WHERE photos IS NULL",
        "UPDATE users SET video_id='' WHERE video_id IS NULL",
        "UPDATE users SET bio='' WHERE bio IS NULL",
    ]:
        await db.execute(q)

    # 5) закрываем зависшие комнаты старше 1 часа
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db.execute(
        "UPDATE roulette_rooms SET closed_at=? WHERE closed_at IS NULL AND created_at < ?",
        (now(), cutoff))
    await db.commit()
    return db


# ================= USERS =================

async def get_user(db, uid):
    cur = await _exec(db, "SELECT * FROM users WHERE user_id=?", (uid,))
    return await cur.fetchone()


async def is_banned(db, uid):
    cur = await _exec(db, "SELECT is_banned FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    return bool(row and row["is_banned"])


async def save_user(db, data):
    user_id = data.get("user_id")
    name = (data.get("name") or "").strip()
    age = data.get("age")
    gender = data.get("gender")
    target_gender = data.get("target_gender")

    if not user_id or not name or not isinstance(age, int) or not (14 <= age <= 99):
        raise ValueError("save_user: bad payload")
    if gender not in ("m", "f"):
        raise ValueError("save_user: bad gender")
    if target_gender not in ("m", "f", "all"):
        raise ValueError("save_user: bad target_gender")

    photos_str = ",".join(data.get("photos") or [])
    old = await get_user(db, user_id)

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

    async with _DB_WRITE_LOCK:
        try:
            await _exec(db, '''INSERT INTO users
            (user_id,username,name,age,gender,target_gender,target_age_min,target_age_max,
             city,bio,photos,video_id,referral_id,is_active,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,name=excluded.name,age=excluded.age,
            gender=excluded.gender,target_gender=excluded.target_gender,
            target_age_min=excluded.target_age_min,target_age_max=excluded.target_age_max,
            city=excluded.city,bio=excluded.bio,photos=excluded.photos,
            video_id=excluded.video_id,referral_id=excluded.referral_id,
            is_active=excluded.is_active''',
            (user_id, data.get("username"), name, age, gender,
             target_gender, target_age_min, target_age_max,
             CITY, data.get("bio", ""), photos_str, data.get("video_id", ""),
             referral_id, is_active, now()))
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()
            raise


async def next_profile(db, me, exclude_seen=True):
    if not me:
        return None
    params = {
        "uid": me["user_id"], "city": CITY,
        "min_age": me["target_age_min"] or 14,
        "max_age": me["target_age_max"] or 99,
        "target_gender": me["target_gender"] or "all",
        "gender": me["gender"]
    }
    q = '''SELECT u.* FROM users u
      WHERE u.user_id != :uid AND u.city = :city COLLATE NOCASE
      AND u.is_active = 1 AND u.is_banned = 0
      AND u.age BETWEEN :min_age AND :max_age
      AND (u.gender = :target_gender OR :target_gender = 'all')
      AND (u.target_gender = :gender OR u.target_gender = 'all')
      AND NOT EXISTS (SELECT 1 FROM blocks b
          WHERE (b.blocker_id = u.user_id AND b.blocked_id = :uid)
             OR (b.blocker_id = :uid AND b.blocked_id = u.user_id))'''
    if exclude_seen:
        q += ''' AND NOT EXISTS (SELECT 1 FROM reactions r
              WHERE r.from_user_id = :uid AND r.to_user_id = u.user_id)
              AND NOT EXISTS (SELECT 1 FROM skipped_likes s
              WHERE s.user_id = :uid AND s.target_id = u.user_id)'''
    q += " ORDER BY u.created_at DESC LIMIT 100"

    cur = await _exec(db, q, params)
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


# ================= REACTIONS =================

async def add_reaction(db, from_id, to_id, reaction):
    if from_id == to_id:
        return False
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "INSERT INTO reactions(from_user_id,to_user_id,reaction,created_at) VALUES(?,?,?,?)",
                (from_id, to_id, reaction, now()))
            if reaction == "like":
                await _exec(db,
                    "DELETE FROM skipped_likes WHERE user_id=? AND target_id=?",
                    (from_id, to_id))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            await db.rollback()
            return False
        except aiosqlite.Error:
            await db.rollback()
            return False


async def mutual_like(db, a, b):
    cur = await _exec(db,
        "SELECT 1 FROM reactions WHERE from_user_id=? AND to_user_id=? AND reaction='like'",
        (b, a))
    return await cur.fetchone() is not None


async def add_match(db, a, b):
    x, y = sorted((a, b))
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "INSERT OR IGNORE INTO matches(user1_id,user2_id,created_at) VALUES(?,?,?)",
                (x, y, now()))
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()


async def like_back(db, uid, target):
    if uid == target:
        return False
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "INSERT OR IGNORE INTO reactions(from_user_id,to_user_id,reaction,created_at) VALUES(?,?,'like',?)",
                (uid, target, now()))
            await _exec(db,
                "DELETE FROM skipped_likes WHERE user_id=? AND target_id=?",
                (uid, target))
            cur = await _exec(db,
                "SELECT 1 FROM reactions WHERE from_user_id=? AND to_user_id=? AND reaction='like'",
                (target, uid))
            mutual = await cur.fetchone()
            if mutual:
                x, y = sorted((uid, target))
                await _exec(db,
                    "INSERT OR IGNORE INTO matches(user1_id,user2_id,created_at) VALUES(?,?,?)",
                    (x, y, now()))
            await db.commit()
            return mutual is not None
        except aiosqlite.Error:
            await db.rollback()
            return False


async def incoming_likes(db, uid, offset=0, limit=1):
    cur = await _exec(db, '''SELECT u.* FROM users u
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
    cur = await _exec(db, '''SELECT COUNT(*) AS n FROM users u
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
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "INSERT OR IGNORE INTO skipped_likes(user_id,target_id,created_at) VALUES(?,?,?)",
                (uid, target_id, now()))
            await db.commit()
            return True
        except aiosqlite.Error:
            await db.rollback()
            return False


async def add_report(db, reporter, target, reason):
    if reporter == target:
        return None
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "INSERT INTO reports(reporter_id,target_id,reason,created_at) VALUES(?,?,?,?)",
                (reporter, target, reason, now()))
            cur = await _exec(db,
                "SELECT COUNT(DISTINCT reporter_id) AS n FROM reports WHERE target_id=?",
                (target,))
            count = (await cur.fetchone())["n"]
            if count >= 3:
                await _exec(db,
                    "UPDATE users SET is_active=0,is_banned=1 WHERE user_id=?",
                    (target,))
            await db.commit()
            return count
        except aiosqlite.IntegrityError:
            await db.rollback()
            return None
        except aiosqlite.Error:
            await db.rollback()
            return None


async def block_user(db, blocker, blocked):
    if blocker == blocked:
        return
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "INSERT OR IGNORE INTO blocks(blocker_id,blocked_id,created_at) VALUES(?,?,?)",
                (blocker, blocked, now()))
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()


async def undo_reaction(db, from_id, to_id):
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "DELETE FROM reactions WHERE from_user_id=? AND to_user_id=?",
                (from_id, to_id))
            cur = await _exec(db,
                "SELECT 1 FROM reactions WHERE from_user_id=? AND to_user_id=? AND reaction='like'",
                (to_id, from_id))
            mutual = await cur.fetchone()
            if not mutual:
                x, y = sorted((from_id, to_id))
                await _exec(db,
                    "DELETE FROM matches WHERE user1_id=? AND user2_id=?",
                    (x, y))
            await db.commit()
            return not bool(mutual)
        except aiosqlite.Error:
            await db.rollback()
            return False


async def get_last_reaction(db, from_id):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    cur = await _exec(db,
        """SELECT r.to_user_id FROM reactions r
           WHERE r.from_user_id=? AND r.created_at > ? AND r.reaction='like'
           AND NOT EXISTS (SELECT 1 FROM matches m WHERE
               (m.user1_id=r.from_user_id AND m.user2_id=r.to_user_id) OR
               (m.user2_id=r.from_user_id AND m.user1_id=r.to_user_id))
           ORDER BY r.id DESC LIMIT 1""", (from_id, cutoff))
    row = await cur.fetchone()
    return row["to_user_id"] if row else None


# ================= PREMIUM / STATS =================

async def verify_user(db, uid):
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db, "UPDATE users SET is_verified=1 WHERE user_id=?", (uid,))
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()


async def add_reward(db, uid, days):
    async with _DB_WRITE_LOCK:
        try:
            cur = await _exec(db,
                "SELECT premium_until FROM users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            if not row:
                return False
            base = datetime.now(timezone.utc)
            pu = _parse_dt(row["premium_until"])
            if pu and pu > base:
                base = pu
            new_until = (base + timedelta(days=days)).isoformat()
            await _exec(db,
                "UPDATE users SET premium_until=? WHERE user_id=?", (new_until, uid))
            await db.commit()
            return True
        except aiosqlite.Error:
            await db.rollback()
            return False


async def check_premium(db, uid):
    cur = await _exec(db,
        "SELECT premium_until FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    if not row or not row["premium_until"]:
        return False
    pu = _parse_dt(row["premium_until"])
    return bool(pu and pu > datetime.now(timezone.utc))


async def set_premium(db, uid, days):
    return await add_reward(db, uid, days)


async def has_like(db, from_id, to_id):
    cur = await _exec(db,
        "SELECT 1 FROM reactions WHERE from_user_id=? AND to_user_id=? AND reaction='like'",
        (from_id, to_id))
    return await cur.fetchone() is not None


async def register_view(db, viewer_id, target_id):
    if viewer_id == target_id:
        return
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "INSERT OR IGNORE INTO views (viewer_id, target_id, created_at) VALUES (?, ?, ?)",
                (viewer_id, target_id, now()))
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()


async def get_profile_stats(db, uid):
    cur = await _exec(db,
        "SELECT COUNT(*) n FROM reactions WHERE from_user_id=? AND reaction='like'", (uid,))
    likes = (await cur.fetchone())["n"]
    cur = await _exec(db,
        "SELECT COUNT(*) n FROM views WHERE target_id=?", (uid,))
    views = (await cur.fetchone())["n"]
    return likes, views


# ================= ROULETTE =================

ROULETTE_COOLDOWN_MIN = 30


async def roulette_join_queue(db, uid):
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db,
                "UPDATE roulette_rooms SET closed_at=? WHERE closed_at IS NULL AND (user1_id=? OR user2_id=?)",
                (now(), uid, uid))
            await _exec(db, "DELETE FROM roulette_queue WHERE user_id=?", (uid,))

            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ROULETTE_COOLDOWN_MIN)).isoformat()

            cur = await _exec(db, """SELECT q.user_id FROM roulette_queue q
                JOIN users u ON u.user_id = q.user_id
                WHERE q.user_id != ? AND u.is_active=1 AND u.is_banned=0
                  AND NOT EXISTS (SELECT 1 FROM roulette_rooms r
                      WHERE r.closed_at IS NULL AND (r.user1_id=q.user_id OR r.user2_id=q.user_id))
                  AND NOT EXISTS (SELECT 1 FROM roulette_rooms h
                      WHERE h.created_at > ?
                        AND ((h.user1_id=? AND h.user2_id=q.user_id)
                          OR (h.user2_id=? AND h.user1_id=q.user_id)))
                ORDER BY q.joined_at ASC LIMIT 1""",
                (uid, cutoff, uid, uid))
            partner = await cur.fetchone()
            if partner:
                partner_id = partner["user_id"]
                await _exec(db, "DELETE FROM roulette_queue WHERE user_id=?", (partner_id,))
                await _exec(db,
                    "INSERT INTO roulette_rooms (user1_id, user2_id, created_at) VALUES (?, ?, ?)",
                    (uid, partner_id, now()))
                await db.commit()
                return partner_id
            else:
                await _exec(db,
                    "INSERT INTO roulette_queue (user_id, joined_at) VALUES (?, ?)",
                    (uid, now()))
                await db.commit()
                return None
        except aiosqlite.Error:
            await db.rollback()
            return None


async def roulette_leave(db, uid):
    async with _DB_WRITE_LOCK:
        try:
            await _exec(db, "DELETE FROM roulette_queue WHERE user_id=?", (uid,))
            cur = await _exec(db,
                "SELECT room_id, user1_id, user2_id FROM roulette_rooms WHERE closed_at IS NULL AND (user1_id=? OR user2_id=?)",
                (uid, uid))
            room = await cur.fetchone()
            if room:
                partner_id = room["user2_id"] if room["user1_id"] == uid else room["user1_id"]
                await _exec(db,
                    "UPDATE roulette_rooms SET closed_at=? WHERE room_id=?",
                    (now(), room["room_id"]))
                await db.commit()
                return partner_id
            await db.commit()
            return None
        except aiosqlite.Error:
            await db.rollback()
            return None


async def roulette_get_partner(db, uid):
    cur = await _exec(db,
        "SELECT user1_id, user2_id FROM roulette_rooms WHERE closed_at IS NULL AND (user1_id=? OR user2_id=?)",
        (uid, uid))
    room = await cur.fetchone()
    if not room:
        return None
    partner_id = room["user2_id"] if room["user1_id"] == uid else room["user1_id"]
    partner = await get_user(db, partner_id)
    if not partner or partner["is_banned"] or not partner["is_active"]:
        async with _DB_WRITE_LOCK:
            try:
                await _exec(db,
                    "UPDATE roulette_rooms SET closed_at=? WHERE closed_at IS NULL AND (user1_id=? OR user2_id=?)",
                    (now(), uid, uid))
                await db.commit()
            except aiosqlite.Error:
                await db.rollback()
        return None
    return partner_id
