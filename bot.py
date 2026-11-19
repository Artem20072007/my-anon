import asyncio
import logging
import os
import traceback
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (Message, CallbackQuery, InputMediaPhoto, LabeledPrice,
                           PreCheckoutQuery, ReplyKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from app import db
from app.states import Registration, EditProfile, AdminPanel
from app.keyboards import *

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
MIN_AGE = 14
MAX_AGE = 99
CITY = "Чистополь"

conn = None
STOP_WORDS = ["член", "секс", "анал", "интим", "пошлый", "ебаться"]

# per-user буферы фото с TTL
user_photos = {}     # uid -> {"photos": [...], "ts": monotonic}
PHOTO_TTL = 3600

# анти-дабл-клик: (uid, action, target) -> monotonic
_action_lock = {}
ACTION_COOLDOWN = 1.0


def _gc_photos():
    now_m = time.monotonic()
    stale = [uid for uid, v in user_photos.items() if now_m - v.get("ts", 0) > PHOTO_TTL]
    for uid in stale:
        user_photos.pop(uid, None)


def _photos_of(uid):
    v = user_photos.get(uid)
    return v["photos"] if v else []


def _photos_set(uid, lst):
    user_photos[uid] = {"photos": lst, "ts": time.monotonic()}


def _photos_clear(uid):
    user_photos.pop(uid, None)


def _try_action(uid, action, target=None):
    key = (uid, action, target)
    now_m = time.monotonic()
    last = _action_lock.get(key, 0)
    if now_m - last < ACTION_COOLDOWN:
        return False
    _action_lock[key] = now_m
    return True


# ================= УТИЛИТЫ =================

async def safe_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


async def safe_answer(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        pass


def parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def user_link(user):
    if user is None:
        return "https://t.me"
    try:
        if "username" in user.keys() and user["username"]:
            return f"https://t.me/{user['username']}"
        return f"tg://user?id={user['user_id']}"
    except Exception:
        return "https://t.me"


def profile_caption(p, stats=None):
    if not p or "name" not in p.keys():
        return "Ошибка анкеты"

    premium_mark = ""
    if "premium_until" in p.keys() and p["premium_until"]:
        pu = db._parse_dt(p["premium_until"])
        if pu and pu > datetime.now(timezone.utc):
            premium_mark = "🌟 Премиум!\n"

    verified = " ✅" if p["is_verified"] else ""
    name = (p["name"] or "").strip().capitalize() or "Без имени"
    age = p["age"]
    city = (p["city"] or "").capitalize()
    bio = (p["bio"] or '').strip() if "bio" in p.keys() else ''

    if bio.lower() in ["пропустить", "пропустить фото", "пропустить шаг", "без описания"]:
        bio = ""

    header = f"{premium_mark}{name}{verified}, {age}\n📍 {city}"
    header += f"\n💬 {bio}" if bio else "\n💬 Пока нет описания"

    if stats:
        header += f"\n\n❤️ Лайки: {stats[0]}\n👀 Просмотры: {stats[1]}"
    return header


async def show_profile(message: Message, p, mode="feed", offset: int = 0, extra_kb=None):
    if not p:
        await message.answer("Анкеты закончились.", reply_markup=main_menu())
        return

    if mode == "feed":
        markup = feed_kb(p["user_id"])
    elif mode == "incoming":
        markup = incoming_kb(p["user_id"], offset)
    elif mode == "mine":
        markup = profile_menu_kb()
    elif mode == "match":
        link = user_link(p)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать сообщение", url=link)]
        ])
    else:
        markup = extra_kb

    try:
        stats = await db.get_profile_stats(conn, p["user_id"])
    except Exception:
        log.exception("get_profile_stats")
        stats = None

    if mode == "feed" and message.chat.id != p["user_id"]:
        try:
            await db.register_view(conn, message.chat.id, p["user_id"])
        except Exception:
            log.exception("register_view")

    caption = profile_caption(p, stats)
    photos = p["photos"].split(",") if ("photos" in p.keys() and p["photos"]) else []

    if photos:
        if mode in ("feed", "incoming", "match") or len(photos) == 1:
            await message.answer_photo(photos[0], caption=caption, reply_markup=markup)
        else:
            media = [InputMediaPhoto(media=ph, caption=caption if i == 0 else None)
                     for i, ph in enumerate(photos)]
            await message.answer_media_group(media)
            if markup:
                await message.answer("👆 Твоя анкета.", reply_markup=markup)
    elif "video_id" in p.keys() and p["video_id"]:
        await message.answer_video(p["video_id"], caption=caption, reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)


class BanMiddleware(BaseMiddleware):
    def __init__(self, conn):
        self.conn = conn

    async def __call__(self, handler, event, data):
        uid = event.from_user.id if hasattr(event, "from_user") and event.from_user else None
        if uid:
            try:
                banned = await db.is_banned(self.conn, uid)
            except Exception:
                log.exception("is_banned")
                banned = False
            if banned:
                if isinstance(event, Message):
                    await event.answer("🚫 Ты заблокирован.")
                elif isinstance(event, CallbackQuery):
                    await safe_answer(event, "🚫 Ты заблокирован.", show_alert=True)
                return
        return await handler(event, data)


async def start_registration(message: Message, state: FSMContext, is_edit=False, pending_ref=None):
    await state.clear()
    _photos_clear(message.from_user.id)
    init = {"user_id": message.from_user.id,
            "username": message.from_user.username,
            "photos": [], "is_edit": is_edit}
    if pending_ref:
        init["pending_ref"] = pending_ref
    await state.update_data(**init)
    await state.set_state(Registration.gender)
    await message.answer("Привет! Давай создадим анкету. Какой у тебя пол?", reply_markup=gender_kb())


async def notify_new_profiles(bot: Bot, conn):
    last_notify = {}
    while True:
        await asyncio.sleep(1800)
        try:
            thirty_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            cur = await conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE created_at > ? AND is_active=1 AND is_banned=0",
                (thirty_mins_ago,))
            new_count = (await cur.fetchone())["cnt"]
            if new_count <= 1:
                continue
            cur = await conn.execute("SELECT user_id, created_at FROM users WHERE is_active=1 AND is_banned=0")
            users = await cur.fetchall()
            now_dt = datetime.now(timezone.utc)
            last_notify = {k: v for k, v in last_notify.items()
                           if (now_dt - v) < timedelta(hours=24)}
            for u in users:
                if u["created_at"] > thirty_mins_ago:
                    continue
                uid = u["user_id"]
                last = last_notify.get(uid)
                if last and (now_dt - last) < timedelta(hours=3):
                    continue
                try:
                    await bot.send_message(
                        uid, f"🔔 За последние 30 минут появилось {new_count} новых анкет в {CITY}!")
                    last_notify[uid] = now_dt
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"notify: {e}")


async def render_incoming(message: Message, uid, offset: int):
    try:
        total = await db.count_incoming_likes(conn, uid)
    except Exception:
        log.exception("count_incoming_likes")
        return await message.answer("⚠️ Ошибка БД, попробуй позже.", reply_markup=main_menu())

    if total == 0:
        return await message.answer("Пока нет лайков.", reply_markup=main_menu())

    if offset < 0:
        offset = 0
    if offset >= total:
        offset = total - 1

    try:
        is_prem = await db.check_premium(conn, uid)
    except Exception:
        is_prem = False

    if not is_prem and offset > 0:
        return await message.answer("🔒 Для листания нужен Премиум.", reply_markup=premium_kb())

    try:
        rows = await db.incoming_likes(conn, uid, offset=offset, limit=1)
    except Exception:
        log.exception("incoming_likes")
        return await message.answer("⚠️ Ошибка БД, попробуй позже.", reply_markup=main_menu())

    if not rows:
        try:
            total = await db.count_incoming_likes(conn, uid)
        except Exception:
            total = 0
        if total == 0:
            return await message.answer("Пока нет лайков.", reply_markup=main_menu())
        offset = min(offset, total - 1)
        try:
            rows = await db.incoming_likes(conn, uid, offset=offset, limit=1)
        except Exception:
            rows = []

    if not rows:
        return await message.answer("Пока нет лайков.", reply_markup=main_menu())

    p = rows[0]
    await show_profile(message, p, mode="incoming", offset=offset)

    nav = likes_nav_kb(offset, total, "incoming") if is_prem else None
    header = f"💖 Лайки: {offset + 1} из {total}"
    if not is_prem and total > 1:
        header += "\n🔒 Премиум — листать всех."
    if nav:
        await message.answer(header, reply_markup=nav)
    else:
        await message.answer(header, reply_markup=back_to_menu_kb())


# ================= MAIN =================

async def main():
    global conn
    if not TOKEN:
        raise RuntimeError("Нет токена")
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    conn = await db.connect()
    dp.message.middleware(BanMiddleware(conn))
    dp.callback_query.middleware(BanMiddleware(conn))
    dp.pre_checkout_query.middleware(BanMiddleware(conn))
    task = asyncio.create_task(notify_new_profiles(bot, conn))

    @dp.errors()
    async def on_error(event, exception):
        log.error(f"Unhandled: {exception}\n{traceback.format_exc()}")
        try:
            if isinstance(event, Message):
                await event.answer("⚠️ Ошибка, попробуй ещё раз.", reply_markup=main_menu())
            elif isinstance(event, CallbackQuery):
                await safe_answer(event, "⚠️ Ошибка, попробуй ещё раз.", show_alert=True)
        except Exception:
            pass
        return True

    # ================= START / CANCEL =================
    @dp.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        await state.clear()
        args = m.text.split() if m.text else []
        try:
            user = await db.get_user(conn, m.from_user.id)
        except Exception:
            log.exception("start get_user")
            return await m.answer("⚠️ Ошибка БД, попробуй ещё раз.", reply_markup=main_menu())
        if not user:
            pending_ref = None
            if len(args) > 1 and args[1].startswith("ref_"):
                ref_id = parse_int(args[1].replace("ref_", ""))
                if ref_id and ref_id != m.from_user.id:
                    pending_ref = ref_id
            await start_registration(m, state, pending_ref=pending_ref)
        else:
            await m.answer("С возвращением! Главное меню:", reply_markup=main_menu())

    @dp.message(Command("cancel"), StateFilter("*"))
    @dp.message(F.text == "Отмена", StateFilter("*"))
    async def cancel(m: Message, state: FSMContext):
        await state.clear()
        _photos_clear(m.from_user.id)
        await m.answer("Действие отменено.", reply_markup=main_menu())

    # ================= РЕГИСТРАЦИЯ =================
    @dp.message(Registration.gender)
    async def reg_gender(m: Message, state: FSMContext):
        val = {"Я парень": "m", "Я девушка": "f"}.get(m.text)
        if not val:
            return await m.answer("Выбери пол на кнопках.", reply_markup=gender_kb())
        await state.update_data(gender=val)
        await state.set_state(Registration.target_gender)
        await m.answer("С кем хочешь знакомиться?", reply_markup=target_kb())

    @dp.message(Registration.target_gender)
    async def reg_target(m: Message, state: FSMContext):
        val = {"С парнями": "m", "С девушками": "f", "Без разницы": "all"}.get(m.text)
        if not val:
            return await m.answer("Выбери вариант на кнопках.", reply_markup=target_kb())
        await state.update_data(target_gender=val)
        await state.set_state(Registration.city)
        await m.answer(f"📍 В каком городе ты живёшь?\n\nПока мы работаем только в {CITY}.", reply_markup=city_kb())

    @dp.message(Registration.city)
    async def reg_city(m: Message, state: FSMContext):
        if (m.text or "").strip().lower() not in ["чистополь", "г. чистополь"]:
            return await m.answer(f"❌ Пока мы работаем только в {CITY}.", reply_markup=city_kb())
        await state.update_data(city=CITY)
        await state.set_state(Registration.name)
        await m.answer("Как тебя зовут?", reply_markup=ReplyKeyboardRemove())

    @dp.message(Registration.name)
    async def reg_name(m: Message, state: FSMContext):
        name = (m.text or "").strip()
        if not name or len(name) > 30:
            return await m.answer("Имя от 1 до 30 символов.")
        await state.update_data(name=name)
        await state.set_state(Registration.age)
        await m.answer(f"🎂 Сколько тебе лет? (от {MIN_AGE})")

    @dp.message(Registration.age)
    async def reg_age(m: Message, state: FSMContext):
        if not m.text or not m.text.isdigit() or not (MIN_AGE <= int(m.text) <= MAX_AGE):
            return await m.answer(f"Возраст от {MIN_AGE} до {MAX_AGE} лет.")
        await state.update_data(age=int(m.text))
        await state.set_state(Registration.bio)
        bio_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить шаг")]],
                                     resize_keyboard=True)
        await m.answer("💬 Расскажи о себе!\n\nИли пропусти.", reply_markup=bio_kb)

    @dp.message(Registration.bio)
    async def reg_bio(m: Message, state: FSMContext):
        text = (m.text or "").strip().lower()
        bio_value = "" if text in ["пропустить", "пропустить шаг"] else (m.text or "")
        await state.update_data(bio=bio_value)
        await state.set_state(Registration.photo)
        d = await state.get_data()
        await m.answer("Пришли фото или видео (до 15 сек).",
                       reply_markup=photo_edit_kb() if d.get("is_edit") else photo_kb())

    @dp.message(Registration.photo, F.photo)
    async def reg_photo(m: Message, state: FSMContext):
        uid = m.from_user.id
        photos = _photos_of(uid)
        if m.photo[-1].file_id in photos:
            return await m.answer("Ты уже добавил это фото.")
        photos.append(m.photo[-1].file_id)
        _photos_set(uid, photos)
        await state.update_data(photos=photos, video_id="")
        d = await state.get_data()
        kb = photo_edit_kb() if d.get("is_edit") else photo_kb()
        if len(photos) < 3:
            await m.answer(f"Фото ({len(photos)}/3). Ещё или «Сохранить».", reply_markup=kb)
        else:
            await state.set_state(Registration.confirm)
            await m.answer_photo(
                photo=photos[0],
                caption=f"Проверь: {d.get('name')}, {d.get('age')}\n📍 {d.get('city')}\n📝 {d.get('bio') or 'Нет'}",
                reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.video)
    async def reg_video(m: Message, state: FSMContext):
        if m.video.duration > 15:
            return await m.answer("Видео до 15 сек!")
        await state.update_data(video_id=m.video.file_id, photos=[])
        _photos_clear(m.from_user.id)
        await state.set_state(Registration.confirm)
        d = await state.get_data()
        await m.answer_video(
            video=m.video.file_id,
            caption=f"Проверь: {d.get('name')}, {d.get('age')}\n📍 {d.get('city')}\n📝 {d.get('bio') or 'Нет'}",
            reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.text == "Сохранить фото")
    async def reg_photo_save(m: Message, state: FSMContext):
        photos = _photos_of(m.from_user.id)
        if not photos:
            return await m.answer("Нет фото!")
        await state.update_data(photos=photos, video_id="")
        await state.set_state(Registration.confirm)
        d = await state.get_data()
        await m.answer_photo(
            photo=photos[0],
            caption=f"Проверь: {d.get('name')}, {d.get('age')}\n📍 {d.get('city')}\n📝 {d.get('bio') or 'Нет'}",
            reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.text == "Взять из профиля Telegram")
    async def reg_photo_telegram(m: Message, state: FSMContext):
        uid = m.from_user.id
        photos = _photos_of(uid)
        if len(photos) >= 3:
            d = await state.get_data()
            return await m.answer("Уже 3 фото.",
                                  reply_markup=photo_edit_kb() if d.get("is_edit") else photo_kb())
        try:
            prof = await bot.get_user_profile_photos(uid, limit=1)
            if not prof.total_count:
                d = await state.get_data()
                return await m.answer("Нет фото в профиле.",
                                      reply_markup=photo_edit_kb() if d.get("is_edit") else photo_kb())
            file_id = prof.photos[0][-1].file_id
            if file_id in photos:
                return await m.answer("Уже добавлено.")
            photos.append(file_id)
            _photos_set(uid, photos)
            await state.update_data(photos=photos, video_id="")
            d = await state.get_data()
            kb = photo_edit_kb() if d.get("is_edit") else photo_kb()
            if len(photos) < 3:
                await m.answer(f"Добавлено ({len(photos)}/3).", reply_markup=kb)
            else:
                await state.set_state(Registration.confirm)
                await m.answer_photo(
                    photo=photos[0],
                    caption=f"Проверь: {d.get('name')}, {d.get('age')}",
                    reply_markup=confirm_kb())
        except Exception:
            log.exception("get_user_profile_photos")
            await m.answer("Ошибка получения фото.")

    @dp.message(Registration.photo)
    async def reg_media_bad(m: Message, state: FSMContext):
        d = await state.get_data()
        await m.answer("Отправь фото/видео или нажми кнопку.",
                       reply_markup=photo_edit_kb() if d.get("is_edit") else photo_kb())

    @dp.message(Registration.confirm)
    async def reg_confirm(m: Message, state: FSMContext):
        if m.text == "✏️ Изменить анкету":
            d = await state.get_data()
            return await start_registration(m, state, is_edit=d.get("is_edit", False),
                                            pending_ref=d.get("pending_ref"))
        if m.text != "✅ Да, все верно":
            return await m.answer("Подтверди кнопкой.")
        d = await state.get_data()
        if not d.get("photos") and not d.get("video_id"):
            return await m.answer("❌ Нужно фото или видео!")
        if not d.get("name") or not d.get("age") or not d.get("gender") or not d.get("target_gender"):
            await state.clear()
            return await m.answer("❌ Данные потеряны. /start", reply_markup=main_menu())

        pending_ref = d.get("pending_ref")
        if pending_ref:
            d["referral_id"] = pending_ref
        try:
            await db.save_user(conn, d)
        except Exception:
            log.exception(f"save_user failed data={d}")
            await state.clear()
            _photos_clear(m.from_user.id)
            return await m.answer("❌ Не удалось сохранить анкету. Попробуй /start заново.",
                                  reply_markup=main_menu())

        if pending_ref:
            try:
                await db.add_reward(conn, pending_ref, 3)
                await bot.send_message(pending_ref, "🎁 Друг зарегистрировался! Тебе +3 дня Премиума ⭐")
            except Exception:
                log.exception("add_reward referrer")

        await state.clear()
        _photos_clear(m.from_user.id)
        await m.answer("Анкета готова! Главное меню:", reply_markup=main_menu())

    # ================= ГЛАВНОЕ МЕНЮ =================
    @dp.message(F.text == "🔎 Смотреть анкеты")
    async def feed(m: Message, state: FSMContext):
        await state.clear()
        _gc_photos()
        try:
            me = await db.get_user(conn, m.from_user.id)
        except Exception:
            log.exception("feed get_user")
            return await m.answer("⚠️ Ошибка БД, попробуй ещё раз.", reply_markup=main_menu())
        if not me:
            return await m.answer("Сначала создай анкету /start", reply_markup=main_menu())
        try:
            p = await db.next_profile(conn, me)
        except Exception:
            log.exception("next_profile")
            return await m.answer("⚠️ Ошибка поиска. Попробуй ещё раз.", reply_markup=main_menu())
        if p:
            await show_profile(m, p)
        else:
            await m.answer("Анкеты закончились. Заходи позже!", reply_markup=main_menu())

    @dp.message(F.text == "👤 Моя анкета")
    async def my_profile(m: Message, state: FSMContext):
        await state.clear()
        try:
            u = await db.get_user(conn, m.from_user.id)
        except Exception:
            log.exception("my_profile")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=main_menu())
        if not u:
            return await m.answer("Нет анкеты.", reply_markup=main_menu())
        await show_profile(m, u, mode="mine")

    @dp.message(F.text == "⭐ Премиум")
    async def premium_menu(m: Message, state: FSMContext):
        await state.clear()
        try:
            is_prem = await db.check_premium(conn, m.from_user.id)
        except Exception:
            is_prem = False
        status = "✅ Премиум активен!" if is_prem else "❌ Премиума нет."
        text = (f"{status}\n\nЧто даёт Премиум:\n1. Видишь всех, кто лайкнул.\n"
                f"2. Анкета показывается первой.\n3. Отмена лайков.\n\nВыбери тариф:")
        await m.answer(text, reply_markup=premium_kb())

    @dp.message(F.text == "🔗 Пригласить друга")
    async def invite_friend(m: Message, state: FSMContext):
        await state.clear()
        try:
            bot_info = await bot.get_me()
            await m.answer(f"Пригласи друга:\nhttps://t.me/{bot_info.username}?start=ref_{m.from_user.id}\n\n"
                           "За регистрацию — +3 дня Премиума!")
        except Exception:
            log.exception("invite_friend")
            await m.answer("Попробуй позже.")

    @dp.message(F.text == "💗 Кто меня лайкнул")
    async def likes_in(m: Message, state: FSMContext):
        await state.clear()
        await render_incoming(m, m.from_user.id, 0)

    # ================= ПРОФИЛЬ =================
    @dp.message(F.text == "🔙 Вернуться назад")
    async def prof_back(m: Message, state: FSMContext):
        await state.clear()
        await m.answer("🏠 Главное меню:", reply_markup=main_menu())

    @dp.message(F.text == "✏️ Изменить информацию")
    async def prof_edit_info(m: Message, state: FSMContext):
        await state.clear()
        await m.answer("✏️ Что изменить?", reply_markup=edit_menu_kb())

    @dp.message(F.text == "🎂 Фильтр возраста")
    async def prof_age_filter(m: Message, state: FSMContext):
        await state.clear()
        try:
            me = await db.get_user(conn, m.from_user.id)
        except Exception:
            me = None
        cur = f"Сейчас: {me['target_age_min'] or 14}–{me['target_age_max'] or 99} лет.\n\n" if me else ""
        await m.answer(f"{cur}Выбери диапазон:", reply_markup=age_filter_kb())

    @dp.message(F.text == "🚻 Кого показывать")
    async def prof_target_gender(m: Message, state: FSMContext):
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Девушки", callback_data="set_tg_f"),
             InlineKeyboardButton(text="Парни", callback_data="set_tg_m")],
            [InlineKeyboardButton(text="Все", callback_data="set_tg_all")]
        ])
        await m.answer("👫 Кого показывать?", reply_markup=kb)

    @dp.message(F.text == "📍 Мой город")
    async def prof_city(m: Message, state: FSMContext):
        await state.clear()
        await m.answer(f"📍 {CITY}. Бот работает только здесь.", reply_markup=back_to_menu_kb())

    @dp.message(F.text == "🌙 Сон / Поиск")
    async def prof_toggle(m: Message, state: FSMContext):
        await state.clear()
        try:
            u = await db.get_user(conn, m.from_user.id)
        except Exception:
            log.exception("prof_toggle")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=main_menu())
        if not u:
            return await m.answer("Нет анкеты.", reply_markup=main_menu())
        new = 0 if u["is_active"] else 1
        try:
            await conn.execute("UPDATE users SET is_active=? WHERE user_id=?", (new, m.from_user.id))
            await conn.commit()
        except Exception:
            log.exception("prof_toggle update")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=main_menu())
        await m.answer(f"{'🌙 Анкета скрыта' if not new else '💚 В поиске'}", reply_markup=main_menu())

    @dp.message(F.text == "👁 Предпросмотр анкеты")
    async def prof_preview(m: Message, state: FSMContext):
        await state.clear()
        try:
            u = await db.get_user(conn, m.from_user.id)
        except Exception:
            log.exception("prof_preview")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=main_menu())
        if not u:
            return await m.answer("Нет анкеты.", reply_markup=main_menu())
        try:
            stats = await db.get_profile_stats(conn, u["user_id"])
        except Exception:
            stats = None
        caption = "👁 Предпросмотр:\n\n" + profile_caption(u, stats)
        photos = u["photos"].split(",") if u["photos"] else []
        if photos:
            await m.answer_photo(photos[0], caption=caption, reply_markup=back_to_menu_kb())
        elif u["video_id"]:
            await m.answer_video(u["video_id"], caption=caption, reply_markup=back_to_menu_kb())
        else:
            await m.answer(caption, reply_markup=back_to_menu_kb())

    # ================= CALLBACKS =================
    @dp.callback_query(F.data == "close_menu")
    async def cb_close_menu(callback: CallbackQuery):
        await safe_delete(callback.message)
        await safe_answer(callback)

    @dp.callback_query(F.data.startswith("set_tg_"))
    async def cb_set_tg(callback: CallbackQuery):
        val = callback.data.split("_")[-1]
        if val not in ("m", "f", "all"):
            return await safe_answer(callback, "Ошибка", show_alert=True)
        try:
            await conn.execute("UPDATE users SET target_gender=? WHERE user_id=?",
                               (val, callback.from_user.id))
            await conn.commit()
        except Exception:
            log.exception("set_tg")
            return await safe_answer(callback, "Ошибка БД", show_alert=True)
        await safe_delete(callback.message)
        await callback.message.answer("✅ Обновлено!", reply_markup=main_menu())
        await safe_answer(callback)

    @dp.callback_query(F.data == "go_menu")
    async def go_menu(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await safe_delete(callback.message)
        await callback.message.answer("🏠 Главное меню", reply_markup=main_menu())
        await safe_answer(callback)

    @dp.callback_query(F.data == "go_profile")
    async def go_profile(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await safe_delete(callback.message)
        try:
            u = await db.get_user(conn, callback.from_user.id)
        except Exception:
            u = None
        if u:
            await show_profile(callback.message, u, mode="mine")
        else:
            await callback.message.answer("Нет анкеты.", reply_markup=main_menu())
        await safe_answer(callback)

    @dp.callback_query(F.data.startswith("like:") | F.data.startswith("dislike:"))
    async def handle_reaction(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            action, target_str = callback.data.split(":", 1)
            target = int(target_str)
        except (ValueError, AttributeError):
            return await safe_answer(callback, "Ошибка данных", show_alert=True)
        if target == uid:
            return await safe_answer(callback, "Себя нельзя.", show_alert=True)
        if not _try_action(uid, action, target):
            return await safe_answer(callback)  # дабл-клик, тихо глотаем

        try:
            target_user = await db.get_user(conn, target)
        except Exception:
            target_user = None

        if not target_user or target_user["is_banned"]:
            await safe_delete(callback.message)
            await safe_answer(callback, "Анкета недоступна.", show_alert=True)
            try:
                me = await db.get_user(conn, uid)
                p = await db.next_profile(conn, me) if me else None
            except Exception:
                p = None
            if p:
                await show_profile(callback.message, p)
            else:
                await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
            return

        await safe_delete(callback.message)
        ok = False
        try:
            ok = await db.add_reaction(conn, uid, target, action)
        except Exception:
            log.exception("add_reaction")

        if action == "like" and ok:
            try:
                is_mutual = await db.mutual_like(conn, uid, target)
            except Exception:
                is_mutual = False
            if is_mutual:
                try:
                    await db.add_match(conn, uid, target)
                except Exception:
                    log.exception("add_match")
                other = await db.get_user(conn, target)
                me = await db.get_user(conn, uid)
                await callback.message.answer("💚 <b>Взаимная симпатия!</b>", parse_mode="HTML")
                if other:
                    await show_profile(callback.message, other, mode="match")
                try:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Написать", url=user_link(me))]])
                    await bot.send_message(target,
                        "💚 <b>Взаимная симпатия!</b>\n\nНажми кнопку ниже, чтобы начать общение.",
                        parse_mode="HTML", reply_markup=kb)
                except Exception:
                    pass

        try:
            me = await db.get_user(conn, uid)
            p = await db.next_profile(conn, me) if me else None
        except Exception:
            log.exception("next_profile after reaction")
            p = None
        if p:
            await show_profile(callback.message, p)
        else:
            await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
        await safe_answer(callback)

    @dp.callback_query(F.data == "undo")
    async def handle_undo(callback: CallbackQuery):
        uid = callback.from_user.id
        if not _try_action(uid, "undo"):
            return await safe_answer(callback)
        try:
            last_target_id = await db.get_last_reaction(conn, uid)
        except Exception:
            last_target_id = None
        if not last_target_id:
            return await safe_answer(callback, "Нечего отменять.", show_alert=True)
        try:
            await db.undo_reaction(conn, uid, last_target_id)
        except Exception:
            log.exception("undo_reaction")
        try:
            target_user = await db.get_user(conn, last_target_id)
        except Exception:
            target_user = None
        await safe_delete(callback.message)
        if target_user and target_user["is_active"]:
            await show_profile(callback.message, target_user, mode="feed")
        else:
            try:
                me = await db.get_user(conn, uid)
                p = await db.next_profile(conn, me) if me else None
            except Exception:
                p = None
            if p:
                await show_profile(callback.message, p)
            else:
                await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
        await safe_answer(callback, "Отменено!")

    @dp.callback_query(F.data == "sleep")
    async def handle_sleep(callback: CallbackQuery):
        try:
            await conn.execute("UPDATE users SET is_active=0 WHERE user_id=?",
                               (callback.from_user.id,))
            await conn.commit()
        except Exception:
            log.exception("sleep")
            return await safe_answer(callback, "Ошибка БД", show_alert=True)
        await safe_delete(callback.message)
        await callback.message.answer("🌙 Анкета скрыта.", reply_markup=main_menu())
        await safe_answer(callback)

    @dp.callback_query(F.data.startswith("report:"))
    async def handle_report(callback: CallbackQuery):
        target = parse_int(callback.data.split(":", 1)[1])
        if not target:
            return await safe_answer(callback, "Ошибка", show_alert=True)
        if not _try_action(callback.from_user.id, "report", target):
            return await safe_answer(callback)
        try:
            res = await db.add_report(conn, callback.from_user.id, target, "Жалоба")
        except Exception:
            log.exception("add_report")
            res = None
        if res and res >= 3:
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, f"🚨 Автобан: {target}")
                except Exception:
                    pass
        await safe_delete(callback.message)
        await safe_answer(callback, "Жалоба отправлена.")
        try:
            me = await db.get_user(conn, callback.from_user.id)
            p = await db.next_profile(conn, me) if me else None
        except Exception:
            p = None
        if p:
            await show_profile(callback.message, p)
        else:
            await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())

    @dp.callback_query(F.data.startswith("block:"))
    async def handle_block(callback: CallbackQuery):
        target = parse_int(callback.data.split(":", 1)[1])
        if not target:
            return await safe_answer(callback, "Ошибка", show_alert=True)
        if not _try_action(callback.from_user.id, "block", target):
            return await safe_answer(callback)
        try:
            await db.block_user(conn, callback.from_user.id, target)
        except Exception:
            log.exception("block_user")
        await safe_delete(callback.message)
        await safe_answer(callback, "Заблокирован.")
        try:
            me = await db.get_user(conn, callback.from_user.id)
            p = await db.next_profile(conn, me) if me else None
        except Exception:
            p = None
        if p:
            await show_profile(callback.message, p)
        else:
            await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())

    # ================= РУЛЕТКА =================
    @dp.message(F.text == "🎲 Чат-рулетка")
    async def start_roulette(m: Message, state: FSMContext):
        await state.clear()
        await _roulette_search(m, state)

    async def _roulette_search(m: Message, state: FSMContext, msg: Message = None):
        try:
            me = await db.get_user(conn, m.from_user.id)
        except Exception:
            log.exception("roulette get_user")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=main_menu())
        if not me:
            return await m.answer("Сначала создай анкету /start")

        try:
            if await db.roulette_get_partner(conn, m.from_user.id):
                return await m.answer("Ты уже в чате!", reply_markup=roulette_reply_kb())
        except Exception:
            pass

        for _ in range(5):
            if msg is None:
                msg = await m.answer("🔍 Ищем собеседника...")
            try:
                partner_id = await db.roulette_join_queue(conn, m.from_user.id)
            except Exception:
                log.exception("roulette_join_queue")
                partner_id = None

            if partner_id:
                try:
                    partner = await db.get_user(conn, partner_id)
                except Exception:
                    partner = None
                if partner and not partner["is_banned"] and partner["is_active"]:
                    await safe_delete(msg)
                    await m.answer(
                        f"🎉 Собеседник найден!\n\n{profile_caption(partner)}\n\nНажми «Начать общение»",
                        reply_markup=roulette_found_kb())
                    try:
                        await bot.send_message(
                            partner_id,
                            f"🎉 Собеседник найден!\n\n{profile_caption(me)}\n\nНажми «Начать»",
                            reply_markup=roulette_found_kb())
                    except Exception:
                        log.exception("notify roulette partner")
                    return
                else:
                    try:
                        await msg.edit_text("⏳ Ищем другого...")
                    except Exception:
                        pass
                    msg = None
                    continue
            else:
                try:
                    await msg.edit_text("⏳ Ожидаем собеседника...")
                except Exception:
                    pass
                return
        await m.answer("❌ Не удалось найти. Попробуй позже.", reply_markup=main_menu())

    @dp.callback_query(F.data == "roulette_start")
    async def roulette_start_chat(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            partner_id = await db.roulette_get_partner(conn, uid)
        except Exception:
            partner_id = None
        if not partner_id:
            return await safe_answer(callback, "Собеседник ушел.", show_alert=True)
        await safe_delete(callback.message)
        await callback.message.answer("💬 Чат открыт! Напиши первое сообщение.",
                                      reply_markup=roulette_reply_kb())
        await safe_answer(callback)
        try:
            await bot.send_message(partner_id, "💬 Собеседник начал чат!",
                                   reply_markup=roulette_reply_kb())
        except Exception:
            pass

    @dp.callback_query(F.data == "roulette_skip")
    async def roulette_skip(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await safe_answer(callback, "Ищем нового...")
        await safe_delete(callback.message)
        try:
            partner_id = await db.roulette_leave(conn, uid)
        except Exception:
            partner_id = None
        if partner_id:
            try:
                await bot.send_message(partner_id, "💔 Собеседник покинул чат.",
                                       reply_markup=main_menu())
            except Exception:
                pass
        await _roulette_search(callback.message, state)

    @dp.message(F.text == "🔄 Следующий")
    async def roulette_btn_next(m: Message, state: FSMContext):
        try:
            partner_id = await db.roulette_leave(conn, m.from_user.id)
        except Exception:
            partner_id = None
        if partner_id:
            try:
                await bot.send_message(partner_id, "💔 Собеседник покинул чат.",
                                       reply_markup=main_menu())
            except Exception:
                pass
        await m.answer("Ищем нового...")
        await _roulette_search(m, state)

    @dp.message(F.text == "🚪 Выйти")
    async def roulette_btn_exit(m: Message, state: FSMContext):
        try:
            partner_id = await db.roulette_leave(conn, m.from_user.id)
        except Exception:
            partner_id = None
        if partner_id:
            try:
                await bot.send_message(partner_id, "🚪 Собеседник вышел.",
                                       reply_markup=main_menu())
            except Exception:
                pass
        await m.answer("Ты вышел из рулетки.", reply_markup=main_menu())

    @dp.message(StateFilter(None), F.photo)
    async def roulette_photo_handler(m: Message, state: FSMContext):
        uid = m.from_user.id
        try:
            partner_id = await db.roulette_get_partner(conn, uid)
        except Exception:
            partner_id = None
        if not partner_id:
            return await m.answer("Не понял. Используй меню.", reply_markup=main_menu())
        try:
            await bot.send_photo(partner_id, m.photo[-1].file_id, caption="📷 Фото")
        except Exception:
            await m.answer("Не удалось отправить.")

    @dp.message(StateFilter(None), F.text,
                lambda m: m.text and not m.text.startswith("/")
                          and m.text not in ["🔄 Следующий", "🚪 Выйти"])
    async def roulette_message_handler(m: Message, state: FSMContext):
        uid = m.from_user.id
        try:
            partner_id = await db.roulette_get_partner(conn, uid)
        except Exception:
            partner_id = None
        if partner_id:
            text_lower = (m.text or "").lower()
            if any(w in text_lower for w in STOP_WORDS):
                return await m.answer("⚠️ Запрещённые слова!")
            try:
                await bot.send_message(partner_id, f"👤 Собеседник: {m.text}")
            except Exception:
                await m.answer("Не удалось отправить.")
        else:
            await m.answer("Не понял. Используй меню.", reply_markup=main_menu())

    # ================= ЛАЙКИ =================
    @dp.callback_query(F.data.startswith("incoming_page_"))
    async def incoming_page(callback: CallbackQuery):
        offset = parse_int(callback.data.split("_")[-1], 0)
        await safe_delete(callback.message)
        await render_incoming(callback.message, callback.from_user.id, offset)
        await safe_answer(callback)

    @dp.callback_query(F.data.startswith("like_back:"))
    async def cb_like_back(callback: CallbackQuery):
        uid = callback.from_user.id
        parts = callback.data.split(":")
        target = parse_int(parts[1]) if len(parts) > 1 else None
        offset = parse_int(parts[2], 0) if len(parts) > 2 else 0
        if not target:
            return await safe_answer(callback, "Ошибка", show_alert=True)
        if not _try_action(uid, "like_back", target):
            return await safe_answer(callback)
        try:
            other = await db.get_user(conn, target)
        except Exception:
            other = None
        await safe_delete(callback.message)
        if not other or other["is_banned"]:
            await safe_answer(callback, "Анкета недоступна.", show_alert=True)
            await render_incoming(callback.message, uid, offset)
            return

        try:
            matched = await db.like_back(conn, uid, target)
        except Exception:
            log.exception("like_back")
            matched = False
        try:
            me = await db.get_user(conn, uid)
        except Exception:
            me = None

        if matched:
            await callback.message.answer("💚 <b>Взаимная симпатия!</b>", parse_mode="HTML")
            await show_profile(callback.message, other, mode="match")
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Написать", url=user_link(me))]])
                await bot.send_message(target,
                    "💚 <b>Взаимная симпатия!</b>\n\nНажми кнопку ниже.",
                    parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass

        await safe_answer(callback)
        await render_incoming(callback.message, uid, offset)

    @dp.callback_query(F.data.startswith("skip:"))
    async def cb_skip(callback: CallbackQuery):
        uid = callback.from_user.id
        parts = callback.data.split(":")
        target = parse_int(parts[1]) if len(parts) > 1 else None
        offset = parse_int(parts[2], 0) if len(parts) > 2 else 0
        if not target:
            return await safe_answer(callback, "Ошибка", show_alert=True)
        if not _try_action(uid, "skip", target):
            return await safe_answer(callback)

        ok = False
        try:
            ok = await db.skip_incoming_like(conn, uid, target)
        except Exception:
            log.exception("skip_incoming_like")

        await safe_delete(callback.message)
        if not ok:
            await safe_answer(callback, "Ошибка, попробуй ещё раз.", show_alert=True)
            # перерисовываем тот же offset, но target мог не записаться — 
            # принудительно сдвигаем offset на следующий, чтобы не показать того же
            await render_incoming(callback.message, uid, offset + 1)
            return

        await safe_answer(callback)
        await render_incoming(callback.message, uid, offset)

    @dp.callback_query(F.data.startswith("age_"))
    async def set_age_filter(callback: CallbackQuery):
        val = callback.data
        if val == "age_all":
            mn, mx = MIN_AGE, MAX_AGE
        else:
            parts = val.replace("age_", "").split("_")
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                return await safe_answer(callback, "Ошибка", show_alert=True)
            mn, mx = int(parts[0]), int(parts[1])
        try:
            await conn.execute("UPDATE users SET target_age_min=?, target_age_max=? WHERE user_id=?",
                               (mn, mx, callback.from_user.id))
            await conn.commit()
        except Exception:
            log.exception("set_age_filter")
            return await safe_answer(callback, "Ошибка БД", show_alert=True)
        await safe_delete(callback.message)
        await callback.message.answer(f"✅ {mn}–{mx} лет.", reply_markup=main_menu())
        await safe_answer(callback)

    # ================= РЕДАКТИРОВАНИЕ =================
    @dp.callback_query(F.data == "edit_name")
    async def cb_edit_name(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.name)
        await safe_delete(callback.message)
        await callback.message.answer("Введи новое имя:")
        await safe_answer(callback)

    @dp.message(EditProfile.name)
    async def save_new_name(m: Message, state: FSMContext):
        name = (m.text or "").strip()
        if not name or len(name) > 30:
            return await m.answer("Имя от 1 до 30 символов.")
        try:
            await conn.execute("UPDATE users SET name=? WHERE user_id=?", (name, m.from_user.id))
            await conn.commit()
        except Exception:
            log.exception("save_new_name")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=main_menu())
        await state.clear()
        await m.answer("✅ Имя обновлено!", reply_markup=main_menu())

    @dp.callback_query(F.data == "edit_age")
    async def cb_edit_age(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.age)
        await safe_delete(callback.message)
        await callback.message.answer(f"Возраст ({MIN_AGE}–{MAX_AGE}):")
        await safe_answer(callback)

    @dp.message(EditProfile.age)
    async def save_new_age(m: Message, state: FSMContext):
        if not m.text or not m.text.isdigit() or not (MIN_AGE <= int(m.text) <= MAX_AGE):
            return await m.answer(f"Возраст {MIN_AGE}–{MAX_AGE}.")
        try:
            await conn.execute("UPDATE users SET age=? WHERE user_id=?", (int(m.text), m.from_user.id))
            await conn.commit()
        except Exception:
            log.exception("save_new_age")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=main_menu())
        await state.clear()
        await m.answer("✅ Возраст обновлён!", reply_markup=main_menu())

    @dp.callback_query(F.data == "edit_bio")
    async def cb_edit_bio(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.bio)
        await safe_delete(callback.message)
        await callback.message.answer("Введи новое описание (или 'Пропустить'):")
        await safe_answer(callback)

    @dp.message(EditProfile.bio)
    async def save_new_bio(m: Message, state: FSMContext):
        text = (m.text or "").strip()
        bio_value = "" if text.lower() in ["пропустить", "пропустить фото"] else text
        try:
            await conn.execute("UPDATE users SET bio=? WHERE user_id=?", (bio_value, m.from_user.id))
            await conn.commit()
        except Exception:
            log.exception("save_new_bio")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=main_menu())
        await state.clear()
        await m.answer("✅ Описание обновлено!", reply_markup=main_menu())

    @dp.callback_query(F.data == "edit_photo")
    async def cb_edit_photo(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.photo)
        _photos_set(callback.from_user.id, [])
        await safe_delete(callback.message)
        await callback.message.answer("Отправь новые фото (до 3) или нажми кнопки ниже.",
                                      reply_markup=photo_edit_kb())
        await safe_answer(callback)

    @dp.message(EditProfile.photo, F.text == "Оставить текущее")
    async def edit_photo_keep(m: Message, state: FSMContext):
        uid = m.from_user.id
        try:
            old_u = await db.get_user(conn, uid)
        except Exception:
            old_u = None
        has_photos = bool(old_u and old_u["photos"])
        has_video = bool(old_u and "video_id" in old_u.keys() and old_u["video_id"])
        if has_photos:
            _photos_set(uid, old_u["photos"].split(","))
            await m.answer("Текущее фото оставлено. Можно добавить ещё.",
                           reply_markup=photo_edit_kb())
        elif has_video:
            _photos_set(uid, [])
            await m.answer("Сейчас видео. Отправь фото или «Отмена» — видео сохранится.",
                           reply_markup=photo_edit_kb())
        else:
            await m.answer("Не было фото. Отправь новое или возьми из профиля.",
                           reply_markup=photo_edit_kb())

    @dp.message(EditProfile.photo, F.photo)
    async def save_new_photo(m: Message, state: FSMContext):
        uid = m.from_user.id
        photos = _photos_of(uid)
        if len(photos) >= 3:
            return await m.answer("Уже 3 фото. «Сохранить фото».", reply_markup=photo_edit_kb())
        new_photo = m.photo[-1].file_id
        if new_photo in photos:
            return await m.answer("Уже добавлено.", reply_markup=photo_edit_kb())
        photos.append(new_photo)
        _photos_set(uid, photos)
        await m.answer(f"Добавлено ({len(photos)}/3).", reply_markup=photo_edit_kb())

    @dp.message(EditProfile.photo, F.text == "Взять из профиля Telegram")
    async def edit_photo_from_telegram(m: Message, state: FSMContext):
        uid = m.from_user.id
        photos = _photos_of(uid)
        if len(photos) >= 3:
            return await m.answer("Уже 3 фото.", reply_markup=photo_edit_kb())
        try:
            prof = await bot.get_user_profile_photos(uid, limit=1)
            if not prof.total_count:
                return await m.answer("Нет фото в профиле Telegram.", reply_markup=photo_edit_kb())
            file_id = prof.photos[0][-1].file_id
            if file_id in photos:
                return await m.answer("Уже добавлено.", reply_markup=photo_edit_kb())
            photos.append(file_id)
            _photos_set(uid, photos)
            await m.answer(f"Добавлено ({len(photos)}/3).", reply_markup=photo_edit_kb())
        except Exception:
            log.exception("edit_photo_from_telegram")
            await m.answer("Не удалось получить фото.", reply_markup=photo_edit_kb())

    @dp.message(EditProfile.photo, F.text == "Сохранить фото")
    async def edit_photo_save(m: Message, state: FSMContext):
        uid = m.from_user.id
        photos = _photos_of(uid)
        if not photos:
            return await m.answer("Нет фото! Добавь или «Оставить текущее».",
                                  reply_markup=photo_edit_kb())
        photos_str = ",".join(photos)
        try:
            await conn.execute("UPDATE users SET photos=?, video_id='' WHERE user_id=?",
                               (photos_str, uid))
            await conn.commit()
        except Exception:
            log.exception("edit_photo_save")
            return await m.answer("⚠️ Ошибка БД.", reply_markup=photo_edit_kb())
        _photos_clear(uid)
        await state.clear()
        await m.answer("✅ Фото обновлены!", reply_markup=main_menu())

    @dp.message(EditProfile.photo)
    async def edit_photo_bad(m: Message, state: FSMContext):
        await m.answer("Отправь фото или нажми кнопку.", reply_markup=photo_edit_kb())

    # ================= ПРЕМИУМ =================
    @dp.callback_query(F.data.startswith("buy_premium_"))
    async def buy_premium(callback: CallbackQuery):
        days = parse_int(callback.data.split("_")[-1])
        if days not in (7, 30):
            return await safe_answer(callback, "Ошибка тарифа", show_alert=True)
        price = 50 if days == 7 else 150
        title = f"Премиум на {days} дней"
        try:
            await bot.send_invoice(chat_id=callback.from_user.id, title=title,
                                   description="Доступ к скрытым функциям",
                                   payload=f"premium_{days}", provider_token="", currency="XTR",
                                   prices=[LabeledPrice(label=title, amount=price)])
        except Exception as e:
            log.error(f"invoice: {e}")
        await safe_answer(callback)

    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        try:
            if not query.invoice_payload.startswith("premium_"):
                return await bot.answer_pre_checkout_query(query.id, ok=False,
                                                           error_message="Неизвестный товар")
            days = int(query.invoice_payload.split("_")[-1])
        except (ValueError, AttributeError):
            return await bot.answer_pre_checkout_query(query.id, ok=False,
                                                       error_message="Ошибка данных")
        expected = 50 if days == 7 else 150
        if query.total_amount != expected:
            return await bot.answer_pre_checkout_query(query.id, ok=False,
                                                       error_message="Неверная сумма")
        if query.currency != "XTR":
            return await bot.answer_pre_checkout_query(query.id, ok=False,
                                                       error_message="Неверная валюта")
        await bot.answer_pre_checkout_query(query.id, ok=True)

    @dp.message(F.successful_payment)
    async def successful_payment(m: Message):
        try:
            days = int(m.successful_payment.invoice_payload.split("_")[-1])
            if days not in (7, 30):
                raise ValueError
        except (ValueError, AttributeError):
            return await m.answer("❌ Ошибка платежа. Напиши в поддержку.",
                                  reply_markup=main_menu())
        try:
            ok = await db.set_premium(conn, m.from_user.id, days)
        except Exception:
            log.exception("set_premium")
            ok = False
        if not ok:
            return await m.answer("❌ Не удалось активировать. Напиши в поддержку.",
                                  reply_markup=main_menu())
        await m.answer(f"🎉 Премиум активирован на {days} дней.", reply_markup=main_menu())

    # ================= АДМИНКА =================
    @dp.message(Command("admin"))
    async def admin_panel(m: Message, state: FSMContext):
        if m.from_user.id not in ADMIN_IDS:
            return await m.answer("❌ Нет доступа.")
        await state.clear()
        await m.answer("👑 Админ-панель:", reply_markup=admin_menu_kb())

    @dp.callback_query(F.data == "adm_menu", F.from_user.id.in_(ADMIN_IDS))
    async def adm_menu(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await safe_delete(callback.message)
        await callback.message.answer("👑 Админ-панель:", reply_markup=admin_menu_kb())
        await safe_answer(callback)

    @dp.callback_query(F.data == "adm_close", F.from_user.id.in_(ADMIN_IDS))
    async def adm_close(callback: CallbackQuery):
        await safe_delete(callback.message)
        await safe_answer(callback, "Закрыто.")

    @dp.callback_query(F.data == "adm_stats", F.from_user.id.in_(ADMIN_IDS))
    async def adm_stats(callback: CallbackQuery):
        try:
            cur = await conn.execute("SELECT COUNT(*) n FROM users")
            total = (await cur.fetchone())["n"]
            cur = await conn.execute("SELECT COUNT(*) n FROM users WHERE is_active=1 AND is_banned=0")
            active = (await cur.fetchone())["n"]
            cur = await conn.execute("SELECT COUNT(*) n FROM matches")
            matches = (await cur.fetchone())["n"]
            now_iso = datetime.now(timezone.utc).isoformat()
            cur = await conn.execute(
                "SELECT COUNT(*) n FROM users WHERE premium_until IS NOT NULL AND premium_until > ?",
                (now_iso,))
            premium = (await cur.fetchone())["n"]
        except Exception:
            log.exception("adm_stats")
            return await safe_answer(callback, "Ошибка БД", show_alert=True)
        await safe_delete(callback.message)
        await callback.message.answer(
            f"📊 <b>Статистика</b>\n\n👥 Всего: {total}\n🟢 Активных: {active}\n"
            f"⭐ Премиум: {premium}\n❤️ Матчей: {matches}",
            parse_mode="HTML", reply_markup=admin_back_kb())
        await safe_answer(callback)

    @dp.callback_query(F.data == "adm_premium", F.from_user.id.in_(ADMIN_IDS))
    async def adm_premium(callback: CallbackQuery):
        await safe_delete(callback.message)
        await callback.message.answer("⭐ Выдача премиума\n\nВыбери срок:",
                                      reply_markup=admin_premium_kb())
        await safe_answer(callback)

    @dp.callback_query(F.data.startswith("adm_prem_"), F.from_user.id.in_(ADMIN_IDS))
    async def adm_prem_choose(callback: CallbackQuery, state: FSMContext):
        days = parse_int(callback.data.split("_")[-1])
        if days not in (7, 30, 90):
            return await safe_answer(callback, "Ошибка срока", show_alert=True)
        await state.set_state(AdminPanel.give_premium)
        await state.update_data(adm_prem_days=days)
        await safe_delete(callback.message)
        await callback.message.answer(
            f"Выбрано: {days} дней.\n\nОтправь <b>ID пользователя</b>:",
            parse_mode="HTML", reply_markup=admin_back_kb())
        await safe_answer(callback)

    @dp.message(AdminPanel.give_premium, F.from_user.id.in_(ADMIN_IDS))
    async def adm_prem_apply(m: Message, state: FSMContext):
        data = await state.get_data()
        days = data.get("adm_prem_days", 0)
        if days <= 0:
            await state.clear()
            return await m.answer("❌ Срок не выбран.", reply_markup=admin_back_kb())
        if not m.text or not m.text.strip().isdigit():
            return await m.answer("❌ Нужно число (ID).", reply_markup=admin_back_kb())
        target_id = int(m.text.strip())
        try:
            target = await db.get_user(conn, target_id)
        except Exception:
            target = None
        if not target:
            await state.clear()
            return await m.answer("❌ Пользователь не найден.", reply_markup=admin_back_kb())
        try:
            ok = await db.add_reward(conn, target_id, days)
        except Exception:
            ok = False
        if not ok:
            await state.clear()
            return await m.answer("❌ Не удалось выдать.", reply_markup=admin_back_kb())
        await state.clear()
        await m.answer(f"✅ Премиум на {days} дней выдан {target_id}.",
                       reply_markup=admin_back_kb())
        try:
            await bot.send_message(target_id, f"🎁 Тебе выдан Премиум на {days} дней!")
        except Exception:
            pass

    @dp.callback_query(F.data == "adm_clear_db", F.from_user.id.in_(ADMIN_IDS))
    async def adm_clear_db(callback: CallbackQuery):
        await safe_delete(callback.message)
        await callback.message.answer("⚠️ Очистить БД?", reply_markup=admin_clear_confirm1_kb())
        await safe_answer(callback)

    @dp.callback_query(F.data == "adm_clear_step2", F.from_user.id.in_(ADMIN_IDS))
    async def adm_clear_step2(callback: CallbackQuery):
        await safe_delete(callback.message)
        await callback.message.answer("🛑 Подтверди:", reply_markup=admin_clear_confirm2_kb())
        await safe_answer(callback)

    @dp.callback_query(F.data == "adm_clear_final", F.from_user.id.in_(ADMIN_IDS))
    async def adm_clear_final(callback: CallbackQuery):
        await safe_delete(callback.message)
        await safe_answer(callback, "Очистка...")
        msg = await callback.message.answer("⏳ Удаляю...")
        try:
            await conn.commit()
            await conn.execute("PRAGMA foreign_keys=OFF")
            for table in ["reactions", "skipped_likes", "reports", "matches", "blocks",
                          "users", "views", "roulette_rooms", "roulette_queue"]:
                await conn.execute(f"DELETE FROM {table}")
            await conn.commit()
            await conn.execute("VACUUM")
            await conn.commit()
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.commit()
            user_photos.clear()
            _action_lock.clear()
            await msg.edit_text("✅ База очищена!", reply_markup=admin_back_kb())
        except Exception as e:
            log.exception("adm_clear_final")
            await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=admin_back_kb())

    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
