import aiosqlite
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
CREATE INDEX IF NOT EXISTS idx_users_feed ON users(is_active, is_banned, city, age, gender);
CREATE INDEX IF NOT EXISTS idx_reactions_to ON reactions(to_user_id, reaction);
CREATE INDEX IF NOT EXISTS idx_reactions_from ON reactions(from_user_id);
'''

def now():
    return datetime.now(timezone.utc).isoformat()

async def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(SCHEMA)
    try:
        await db.execute("ALTER TABLE users ADD COLUMN premium_until TEXT DEFAULT NULL")
    except aiosqlite.OperationalError:
        pass
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
    await db.execute('''INSERT INTO users
    (user_id,username,name,age,gender,target_gender,target_age_min,target_age_max,city,bio,photos,video_id,referral_id,created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(user_id) DO UPDATE SET 
    username=excluded.username,name=excluded.name,age=excluded.age,gender=excluded.gender,
    target_gender=excluded.target_gender,target_age_min=excluded.target_age_min,
    target_age_max=excluded.target_age_max,city=excluded.city,bio=excluded.bio,
    photos=excluded.photos,video_id=excluded.video_id,is_active=1''',
    (data["user_id"],data.get("username"),data["name"],data["age"],data["gender"],
     data["target_gender"],data.get("target_age_min", 14),data.get("target_age_max", 99),
     CITY,data.get("bio", ""),photos_str,data.get("video_id",""),data.get("referral_id"),
     now()))
    await db.commit()

async def next_profile(db, me, exclude_seen=True):
    params = {
        "uid": me["user_id"], "city": CITY,
        "min_age": me["target_age_min"], "max_age": me["target_age_max"],
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
    query += """ ORDER BY
        CASE WHEN u.premium_until IS NOT NULL AND u.premium_until > datetime('now') THEN 0 ELSE 1 END,
        RANDOM() LIMIT 1"""
    cur = await db.execute(query, params)
    return await cur.fetchone()

async def add_reaction(db, from_id, to_id, reaction):
    if from_id == to_id: return False
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

async def incoming_likes(db, uid, offset=0, limit=1):
    """Кто меня лайкнул, но я ещё не ответил. Постранично."""
    cur = await db.execute('''SELECT u.* FROM users u
      JOIN reactions r ON r.from_user_id = u.user_id
      WHERE r.to_user_id = ? AND r.reaction = 'like'
        AND u.is_active = 1 AND u.is_banned = 0
        AND NOT EXISTS (SELECT 1 FROM reactions mine
            WHERE mine.from_user_id = ? AND mine.to_user_id = u.user_id)
      ORDER BY r.created_at DESC
      LIMIT ? OFFSET ?''', (uid, uid, limit, offset))
    return await cur.fetchall()

async def count_incoming_likes(db, uid):
    cur = await db.execute('''SELECT COUNT(*) AS n FROM users u
      JOIN reactions r ON r.from_user_id = u.user_id
      WHERE r.to_user_id = ? AND r.reaction = 'like'
        AND u.is_active = 1 AND u.is_banned = 0
        AND NOT EXISTS (SELECT 1 FROM reactions mine
            WHERE mine.from_user_id = ? AND mine.to_user_id = u.user_id)''', (uid, uid))
    row = await cur.fetchone()
    return row["n"] if row else 0

async def my_likes(db, uid, offset=0, limit=1):
    """Кого я лайкнул — постранично."""
    cur = await db.execute('''SELECT u.* FROM users u
      JOIN reactions r ON r.to_user_id = u.user_id
      WHERE r.from_user_id = ? AND r.reaction = 'like'
        AND u.is_banned = 0
      ORDER BY r.created_at DESC
      LIMIT ? OFFSET ?''', (uid, limit, offset))
    return await cur.fetchall()

async def count_my_likes(db, uid):
    cur = await db.execute('''SELECT COUNT(*) AS n FROM users u
      JOIN reactions r ON r.to_user_id = u.user_id
      WHERE r.from_user_id = ? AND r.reaction = 'like' AND u.is_banned = 0''', (uid,))
    row = await cur.fetchone()
    return row["n"] if row else 0

async def add_report(db, reporter, target, reason):
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
    if blocker == blocked: return
    await db.execute("INSERT OR IGNORE INTO blocks(blocker_id,blocked_id,created_at) VALUES(?,?,?)",
                     (blocker, blocked, now()))
    await db.commit()

async def undo_reaction(db, from_id, to_id):
    await db.execute("DELETE FROM reactions WHERE from_user_id=? AND to_user_id=?", (from_id, to_id))
    x, y = sorted((from_id, to_id))
    await db.execute("DELETE FROM matches WHERE user1_id=? AND user2_id=?", (x, y))
    await db.commit()

async def get_last_reaction(db, from_id):
    """Последний лайк за 30 мин, который НЕ стал матчем — его можно отменить."""
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

async def set_referral(db, uid, ref_id):
    """Возвращает True, если реферал записан впервые и ref_id != uid."""
    if uid == ref_id: return False
    cur = await db.execute("SELECT referral_id FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    if row and not row["referral_id"]:
        await db.execute("UPDATE users SET referral_id=? WHERE user_id=?", (ref_id, uid))
        await db.commit()
        return True
    return False

async def add_reward(db, uid, days):
    """Начисляет дни премиума, продлевая текущий срок."""
    cur = await db.execute("SELECT premium_until FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    if not row: return
    base = datetime.now(timezone.utc)
    if row["premium_until"]:
        try:
            pu = datetime.fromisoformat(row["premium_until"])
            if pu.tzinfo is None: pu = pu.replace(tzinfo=timezone.utc)
            if pu > base: base = pu
        except (ValueError, TypeError):
            pass
    new_until = (base + timedelta(days=days)).isoformat()
    await db.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, uid))
    await db.commit()

async def check_premium(db, uid):
    cur = await db.execute("SELECT premium_until FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    if not row or not row["premium_until"]:
        return False
    try:
        premium_until = datetime.fromisoformat(row["premium_until"])
        if premium_until.tzinfo is None:
            premium_until = premium_until.replace(tzinfo=timezone.utc)
        return premium_until > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False

async def set_premium(db, uid, days):
    await add_reward(db, uid, days)
