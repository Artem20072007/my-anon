import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (Message, CallbackQuery, InputMediaPhoto, LabeledPrice,
                           PreCheckoutQuery, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
                           InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from app import db
from app.states import Registration, EditProfile
from app.keyboards import *

load_dotenv()
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
MIN_AGE = 14
MAX_AGE = 99
CITY = "Чистополь"

conn = None

user_photos = {}
likes_state = {}
STOP_WORDS = ["член", "секс", "анал", "интим", "пошлый", "ебаться"]

async def safe_delete(message: Message):
    try: await message.delete()
    except Exception: pass

async def safe_answer(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    try: await callback.answer(text=text, show_alert=show_alert)
    except Exception: pass

def user_link(user):
    if "username" in user.keys() and user["username"]:
        return f"https://t.me/{user['username']}"
    return f"tg://user?id={user['user_id']}"

def profile_caption(p, stats=None):
    if not p or "name" not in p.keys(): return "Ошибка анкеты"
    verified = " ✅" if p["is_verified"] else ""
    name = (p["name"] or "").strip().capitalize() or "Без имени"
    age = p["age"]
    city = (p["city"] or "").capitalize()
    bio = (p["bio"] or '').strip() if "bio" in p.keys() else ''
    if bio.lower() in ["пропустить", "пропустить фото", "пропустить шаг", "без описания"]: bio = ""
    
    premium_mark = ""
    if "premium_until" in p.keys() and p["premium_until"]:
        from app.db import _parse_dt
        pu = _parse_dt(p["premium_until"])
        if pu and pu > datetime.now(timezone.utc): premium_mark = " 🌟 Премиум"

    header = f"{name}{verified}{premium_mark}, {age}\n📍 {city}"
    header += f"\n💬 {bio}" if bio else "\n💬 Пока нет описания"
    if stats:
        header += f"\n\n❤️ Лайки: {stats[0]}\n👀 Просмотры: {stats[1]}"
    return header

async def show_profile(message: Message, p, mode="feed", extra_kb=None):
    if not p:
        await message.answer("Анкеты закончились.", reply_markup=main_menu())
        return
    if mode == "feed": markup = feed_kb(p["user_id"])
    elif mode == "incoming": markup = incoming_kb(p["user_id"])
    elif mode == "mine": markup = profile_menu_kb() # ТУТ REPLY-КЛАВИАТУРА ПРОФИЛЯ
    elif mode == "match": markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать", url=user_link(p))]])
    else: markup = extra_kb
        
    stats = await db.get_profile_stats(conn, p["user_id"])
    if mode == "feed" and message.chat.id != p["user_id"]:
        await db.register_view(conn, message.chat.id, p["user_id"])
        
    caption = profile_caption(p, stats)
    photos = p["photos"].split(",") if ("photos" in p.keys() and p["photos"]) else []
    
    if photos:
        if mode in ("feed", "incoming", "match") or len(photos) == 1:
            await message.answer_photo(photos[0], caption=caption, reply_markup=markup)
        else:
            media = [InputMediaPhoto(media=ph, caption=caption if i == 0 else None) for i, ph in enumerate(photos)]
            await message.answer_media_group(media)
            if markup: await message.answer("👆 Твоя анкета.", reply_markup=markup)
    elif "video_id" in p.keys() and p["video_id"]:
        await message.answer_video(p["video_id"], caption=caption, reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)

class BanMiddleware(BaseMiddleware):
    def __init__(self, conn): self.conn = conn
    async def __call__(self, handler, event, data):
        uid = event.from_user.id if hasattr(event, "from_user") and event.from_user else None
        if uid and await db.is_banned(self.conn, uid):
            if isinstance(event, Message): await event.answer("🚫 Ты заблокирован.")
            elif isinstance(event, CallbackQuery): await safe_answer(event, "🚫 Ты заблокирован.", show_alert=True)
            return
        return await handler(event, data)

async def start_registration(message: Message, state: FSMContext, is_edit=False, pending_ref=None):
    await state.clear()
    user_photos.pop(message.from_user.id, None)
    init = {"user_id": message.from_user.id, "username": message.from_user.username, "photos": [], "is_edit": is_edit}
    if pending_ref: init["pending_ref"] = pending_ref
    await state.update_data(**init)
    await state.set_state(Registration.gender)
    await message.answer("Привет! Давай создадим анкету. Какой у тебя пол?", reply_markup=gender_kb())

async def notify_new_profiles(bot: Bot, conn):
    last_notify = {}
    while True:
        await asyncio.sleep(1800)
        try:
            thirty_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            cur_new = await conn.execute("SELECT COUNT(*) as cnt FROM users WHERE created_at > ? AND is_active=1 AND is_banned=0", (thirty_mins_ago,))
            new_count = (await cur_new.fetchone())["cnt"]
            if new_count <= 1: continue
            cur = await conn.execute("SELECT user_id, created_at FROM users WHERE is_active=1 AND is_banned=0")
            users = await cur.fetchall()
            now_dt = datetime.now(timezone.utc)
            last_notify = {k: v for k, v in last_notify.items() if (now_dt - v) < timedelta(hours=24)}
            for u in users:
                if u["created_at"] > thirty_mins_ago: continue
                uid = u["user_id"]
                last = last_notify.get(uid)
                if last and (now_dt - last) < timedelta(hours=3): continue
                try:
                    await bot.send_message(uid, f"🔔 За последние 30 минут появилось {new_count} новых анкет в {CITY}!")
                    last_notify[uid] = now_dt
                except Exception: pass
        except Exception as e:
            logging.error(f"Ошибка фоновой задачи: {e}")

async def main():
    global conn
    if not TOKEN: raise RuntimeError("Нет токена")
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    conn = await db.connect()
    dp.message.middleware(BanMiddleware(conn))
    dp.callback_query.middleware(BanMiddleware(conn))
    dp.pre_checkout_query.middleware(BanMiddleware(conn))
    task = asyncio.create_task(notify_new_profiles(bot, conn))

    @dp.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        args = m.text.split()
        user = await db.get_user(conn, m.from_user.id)
        if not user:
            pending_ref = None
            if len(args) > 1 and args[1].startswith("ref_"):
                try:
                    ref_id = int(args[1].replace("ref_", ""))
                    if ref_id != m.from_user.id: pending_ref = ref_id
                except Exception: pass
            await start_registration(m, state, pending_ref=pending_ref)
        else:
            await m.answer("С возвращением! Главное меню:", reply_markup=main_menu())

    @dp.message(Command("cancel"), StateFilter("*"))
    @dp.message(F.text == "Отмена", StateFilter("*"))
    async def cancel(m: Message, state: FSMContext):
        await state.clear()
        user_photos.pop(m.from_user.id, None)
        likes_state.pop(m.from_user.id, None)
        await m.answer("Действие отменено.", reply_markup=main_menu())

    # ---------- Регистрация ----------
    @dp.message(Registration.gender)
    async def reg_gender(m: Message, state: FSMContext):
        val = {"Я парень":"m", "Я девушка":"f"}.get(m.text)
        if not val: return await m.answer("Выбери свой пол на кнопках ниже.", reply_markup=gender_kb())
        await state.update_data(gender=val)
        await state.set_state(Registration.target_gender)
        await m.answer("С кем хочешь знакомиться?", reply_markup=target_kb())

    @dp.message(Registration.target_gender)
    async def reg_target(m: Message, state: FSMContext):
        val = {"С парнями":"m", "С девушками":"f", "Без разницы":"all"}.get(m.text)
        if not val: return await m.answer("Выбери вариант на кнопках.", reply_markup=target_kb())
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
        if not name or len(name) > 30: return await m.answer("Имя от 1 до 30 символов.")
        await state.update_data(name=name)
        await state.set_state(Registration.age)
        await m.answer(f"🎂 Сколько тебе лет? (от {MIN_AGE})")

    @dp.message(Registration.age)
    async def reg_age(m: Message, state: FSMContext):
        if not m.text or not m.text.isdigit() or int(m.text) < MIN_AGE or int(m.text) > MAX_AGE:
            return await m.answer(f"Возраст от {MIN_AGE} до {MAX_AGE} лет.")
        await state.update_data(age=int(m.text))
        await state.set_state(Registration.bio)
        bio_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить шаг")]], resize_keyboard=True)
        await m.answer("💬 Расскажи немного о себе!\n\nНапиши пару слов или пропусти шаг.", reply_markup=bio_kb)

    @dp.message(Registration.bio)
    async def reg_bio(m: Message, state: FSMContext):
        text = (m.text or "").strip().lower()
        bio_value = "" if text in ["пропустить", "пропустить шаг"] else m.text
        await state.update_data(bio=bio_value)
        await state.set_state(Registration.photo)
        d = await state.get_data()
        await m.answer("Пришли своё фото или видео (до 15 сек).", reply_markup=photo_edit_kb() if d.get("is_edit") else photo_kb())

    def _photo_kb_for(d): return photo_edit_kb() if d.get("is_edit") else photo_kb()

    @dp.message(Registration.photo, F.photo)
    async def reg_photo(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos: user_photos[uid] = []
        if m.photo[-1].file_id in user_photos[uid]: return await m.answer("Ты уже добавил это фото.")
        user_photos[uid].append(m.photo[-1].file_id)
        await state.update_data(photos=user_photos[uid], video_id="")
        d = await state.get_data()
        if len(user_photos[uid]) < 3:
            await m.answer(f"Фото ({len(user_photos[uid])}/3). Еще одно или «Сохранить».", reply_markup=_photo_kb_for(d))
        else:
            await state.set_state(Registration.confirm)
            await m.answer_photo(photo=user_photos[uid][0], caption=f"Проверь: {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Нет'}", reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.video)
    async def reg_video(m: Message, state: FSMContext):
        if m.video.duration > 15: return await m.answer("Видео до 15 сек!")
        await state.update_data(video_id=m.video.file_id, photos=[])
        user_photos.pop(m.from_user.id, None)
        await state.set_state(Registration.confirm)
        d = await state.get_data()
        await m.answer_video(video=m.video.file_id, caption=f"Проверь: {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Нет'}", reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.text == "Сохранить фото")
    async def reg_photo_save(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos or not user_photos[uid]: return await m.answer("Нет фото!")
        await state.update_data(photos=user_photos[uid], video_id="")
        await state.set_state(Registration.confirm)
        d = await state.get_data()
        await m.answer_photo(photo=user_photos[uid][0], caption=f"Проверь: {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Нет'}", reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.text == "Взять из профиля Telegram")
    async def reg_photo_telegram(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos: user_photos[uid] = []
        if len(user_photos[uid]) >= 3: return await m.answer("Уже 3 фото.", reply_markup=_photo_kb_for(await state.get_data()))
        try:
            photos = await bot.get_user_profile_photos(m.from_user.id, limit=1)
            if not photos.total_count: return await m.answer("Нет фото в профиле.", reply_markup=_photo_kb_for(await state.get_data()))
            file_id = photos.photos[0][-1].file_id
            if file_id in user_photos[uid]: return await m.answer("Уже добавлено.")
            user_photos[uid].append(file_id)
            await state.update_data(photos=user_photos[uid], video_id="")
            d = await state.get_data()
            if len(user_photos[uid]) < 3:
                await m.answer(f"Добавлено ({len(user_photos[uid])}/3).", reply_markup=_photo_kb_for(d))
            else:
                await state.set_state(Registration.confirm)
                await m.answer_photo(photo=user_photos[uid][0], caption=f"Проверь: {d['name']}, {d['age']}", reply_markup=confirm_kb())
        except Exception: await m.answer("Ошибка получения фото.")

    @dp.message(Registration.photo)
    async def reg_media_bad(m: Message, state: FSMContext):
        await m.answer("Отправь фото/видео или нажми кнопку.", reply_markup=_photo_kb_for(await state.get_data()))

    @dp.message(Registration.confirm)
    async def reg_confirm(m: Message, state: FSMContext):
        if m.text == "✏️ Изменить анкету":
            d = await state.get_data()
            return await start_registration(m, state, is_edit=d.get("is_edit", False), pending_ref=d.get("pending_ref"))
        if m.text != "✅ Да, все верно": return await m.answer("Подтверди кнопкой.")
        d = await state.get_data()
        if not d.get("photos") and not d.get("video_id"): return await m.answer("❌ Нужно фото или видео!")
        pending_ref = d.get("pending_ref")
        if pending_ref: d["referral_id"] = pending_ref
        await db.save_user(conn, d)
        if pending_ref:
            try:
                await db.add_reward(conn, pending_ref, 3)
                await bot.send_message(pending_ref, "🎁 Друг зарегистрировался! Тебе +3 дня Премиума ⭐")
            except Exception: pass
        await state.clear()
        user_photos.pop(m.from_user.id, None)
        await m.answer("Анкета готова! Главное меню:", reply_markup=main_menu())

    # ---------- ОБРАБОТКА REPLY-КНОПОК ГЛАВНОГО МЕНЮ ----------
    @dp.message(F.text == "🔎 Смотреть анкеты")
    async def feed(m: Message):
        me = await db.get_user(conn, m.from_user.id)
        if not me: return await m.answer("Сначала создай анкету /start", reply_markup=main_menu())
        likes_state.pop(m.from_user.id, None)
        p = await db.next_profile(conn, me)
        if p: await show_profile(m, p)
        else: await m.answer("Анкеты закончились. Заходи позже!", reply_markup=main_menu())

    @dp.message(F.text == "👤 Моя анкета")
    async def my_profile(m: Message):
        u = await db.get_user(conn, m.from_user.id)
        if not u: return await m.answer("Нет анкеты.", reply_markup=main_menu())
        # Показываем анкету и ВКЛЮЧАЕМ REPLY-КЛАВИАТУРУ ПРОФИЛЯ
        await show_profile(m, u, mode="mine")

    @dp.message(F.text == "⭐ Премиум")
    async def premium_menu(m: Message):
        is_prem = await db.check_premium(conn, m.from_user.id)
        status = "✅ У тебя активен Премиум!" if is_prem else "❌ У тебя нет Премиума."
        text = f"{status}\n\nЧто дает Премиум:\n1. Видишь ВСЕХ, кто тебя лайкнул.\n2. Твоя анкета показывается первой.\n3. Отмена лайков.\n\nВыбери тариф:"
        await m.answer(text, reply_markup=premium_kb())

    @dp.message(F.text == "🔗 Пригласить друга")
    async def invite_friend(m: Message):
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{m.from_user.id}"
        await m.answer(f"Пригласи друга по этой ссылке:\n{ref_link}\n\nКогда он зарегистрируется, ты получишь +3 дня Премиума!")

    @dp.message(F.text == "💗 Кто меня лайкнул")
    async def likes_in(m: Message):
        await render_incoming(m, m.from_user.id, 0)

    # ---------- ОБРАБОТКА REPLY-КНОПОК ПРОФИЛЯ ----------
    @dp.message(F.text == "✏️ Изменить информацию")
    async def prof_edit_info(m: Message):
        await m.answer("✏️ Что хочешь изменить?", reply_markup=edit_menu_kb())

    @dp.message(F.text == "📸 Мои фотографии")
    async def prof_edit_photo(m: Message, state: FSMContext):
        await state.set_state(EditProfile.photo)
        user_photos[m.from_user.id] = []
        await m.answer("Отправь новые фото (до 3-х).", reply_markup=photo_edit_kb())

    @dp.message(F.text == "🎂 Фильтр возраста")
    async def prof_age_filter(m: Message):
        me = await db.get_user(conn, m.from_user.id)
        cur = f"Сейчас: от {me['target_age_min']} до {me['target_age_max']} лет.\n\n" if me else ""
        await m.answer(f"{cur}Выбери диапазон:", reply_markup=age_filter_kb())

    @dp.message(F.text == "🚻 Кого показывать")
    async def prof_target_gender(m: Message):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Девушки", callback_data="set_tg_f"), InlineKeyboardButton(text="Парни", callback_data="set_tg_m")],
            [InlineKeyboardButton(text="Все", callback_data="set_tg_all")]
        ])
        await m.answer("👫 Кого показывать в ленте?", reply_markup=kb)

    @dp.message(F.text == "📍 Мой город")
    async def prof_city(m: Message):
        await m.answer(f"📍 Твой город: {CITY}.\n\nБот работает только в этом городе.", reply_markup=back_to_menu_kb())

    @dp.message(F.text == "🌙 Сон / Поиск")
    async def prof_toggle(m: Message):
        u = await db.get_user(conn, m.from_user.id)
        if not u: return await m.answer("Нет анкеты.", reply_markup=main_menu())
        new = 0 if u["is_active"] else 1
        await conn.execute("UPDATE users SET is_active=? WHERE user_id=?", (new, m.from_user.id))
        await conn.commit()
        await m.answer(f"✅ Анкета {'скрыта 🌙' if not new else 'в поиске 💚'}", reply_markup=main_menu())

    @dp.message(F.text == "👁 Предпросмотр анкеты")
    async def prof_preview(m: Message):
        u = await db.get_user(conn, m.from_user.id)
        await show_profile(m, u, mode="feed")

    @dp.callback_query(F.data.startswith("set_tg_"))
    async def cb_set_tg(callback: CallbackQuery):
        val = callback.data.split("_")[-1]
        await conn.execute("UPDATE users SET target_gender=? WHERE user_id=?", (val, callback.from_user.id))
        await conn.commit()
        await safe_delete(callback.message)
        await callback.message.answer("✅ Настройки поиска обновлены!", reply_markup=main_menu())
        await safe_answer(callback)

    @dp.callback_query(F.data == "go_menu")
    async def go_menu(callback: CallbackQuery):
        likes_state.pop(callback.from_user.id, None)
        await safe_delete(callback.message)
        await callback.message.answer("🏠 Главное меню", reply_markup=main_menu())
        await safe_answer(callback)

    # ---------- ОСТАЛЬНАЯ ЛОГИКА (Лайки, Жалобы, Анкеты) ----------
    @dp.callback_query(F.data.startswith("like:") | F.data.startswith("dislike:"))
    async def handle_reaction(callback: CallbackQuery):
        uid = callback.from_user.id
        action, target_str = callback.data.split(":", 1)
        target = int(target_str)
        await safe_delete(callback.message)
        await db.add_reaction(conn, uid, target, action)
        
        if action == "like" and await db.mutual_like(conn, uid, target):
            await db.add_match(conn, uid, target)
            other = await db.get_user(conn, target)
            me = await db.get_user(conn, uid)
            await callback.message.answer("💚 <b>Взаимная симпатия!</b>", parse_mode="HTML")
            await show_profile(callback.message, other, mode="match")
            try: await bot.send_message(target, f"💚 Взаимная симпатия! Написать: {user_link(me)}")
            except Exception: pass
                
        me = await db.get_user(conn, uid)
        p = await db.next_profile(conn, me)
        if p: await show_profile(callback.message, p)
        else: await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
        await safe_answer(callback)

    @dp.callback_query(F.data == "undo")
    async def handle_undo(callback: CallbackQuery):
        uid = callback.from_user.id
        last_target_id = await db.get_last_reaction(conn, uid)
        if not last_target_id: return await safe_answer(callback, "Нечего отменять.", show_alert=True)
        await db.undo_reaction(conn, uid, last_target_id)
        target_user = await db.get_user(conn, last_target_id)
        await safe_delete(callback.message)
        if target_user and target_user["is_active"]:
            await show_profile(callback.message, target_user, mode="feed")
        else:
            me = await db.get_user(conn, uid)
            p = await db.next_profile(conn, me)
            if p: await show_profile(callback.message, p)
            else: await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
        await safe_answer(callback, "Отменено!")

    @dp.callback_query(F.data == "sleep")
    async def handle_sleep(callback: CallbackQuery):
        await conn.execute("UPDATE users SET is_active=0 WHERE user_id=?", (callback.from_user.id,))
        await conn.commit()
        await safe_delete(callback.message)
        await callback.message.answer("Ты скрыл анкету.", reply_markup=main_menu())
        await safe_answer(callback)

    @dp.callback_query(F.data.startswith("report:"))
    async def handle_report(callback: CallbackQuery):
        target = int(callback.data.split(":", 1)[1])
        res = await db.add_report(conn, callback.from_user.id, target, "Жалоба")
        if res and res >= 3:
            for admin_id in ADMIN_IDS:
                try: await bot.send_message(admin_id, f"🚨 Автобан: {target}")
                except: pass
        await safe_delete(callback.message)
        await safe_answer(callback, "Жалоба отправлена.")
        me = await db.get_user(conn, callback.from_user.id)
        p = await db.next_profile(conn, me)
        if p: await show_profile(callback.message, p)

    @dp.callback_query(F.data.startswith("block:"))
    async def handle_block(callback: CallbackQuery):
        target = int(callback.data.split(":", 1)[1])
        await db.block_user(conn, callback.from_user.id, target)
        await safe_delete(callback.message)
        await safe_answer(callback, "Заблокирован.")
        me = await db.get_user(conn, callback.from_user.id)
        p = await db.next_profile(conn, me)
        if p: await show_profile(callback.message, p)

    # ---------- ЧАТ-РУЛЕТКА ----------
    @dp.message(F.text == "🎲 Чат-рулетка")
    async def start_roulette(m: Message, state: FSMContext):
        me = await db.get_user(conn, m.from_user.id)
        if not me: return await m.answer("Сначала создай анкету /start")
        partner_id = await db.roulette_get_partner(conn, m.from_user.id)
        if partner_id: return await m.answer("Ты уже в чате!", reply_markup=roulette_reply_kb())

        msg = await m.answer("🔍 Ищем собеседника...")
        partner_id = await db.roulette_join_queue(conn, m.from_user.id)
        
        if partner_id:
            partner = await db.get_user(conn, partner_id)
            if partner:
                await msg.delete()
                await m.answer(f"🎉 Собеседник найден!\n\n{profile_caption(partner)}\n\nНажми «Начать общение»", reply_markup=roulette_found_kb())
                try: await bot.send_message(partner_id, f"🎉 Собеседник найден!\n\n{profile_caption(me)}\n\nНажми «Начать»", reply_markup=roulette_found_kb())
                except Exception: pass
        else:
            await msg.edit_text("⏳ Ожидаем собеседника...")

    @dp.callback_query(F.data == "roulette_start")
    async def roulette_start_chat(callback: CallbackQuery):
        uid = callback.from_user.id
        partner_id = await db.roulette_get_partner(conn, uid)
        if not partner_id: return await safe_answer(callback, "Собеседник ушел.", show_alert=True)
        await safe_delete(callback.message)
        await callback.message.answer("💬 Чат открыт! Напиши первое сообщение.", reply_markup=roulette_reply_kb())
        await safe_answer(callback)
        try: await bot.send_message(partner_id, "💬 Собеседник начал чат!", reply_markup=roulette_reply_kb())
        except Exception: pass

    @dp.callback_query(F.data.in_({"roulette_skip", "roulette_next"}))
    async def roulette_skip(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await safe_answer(callback, "Ищем нового...")
        await safe_delete(callback.message)
        partner_id = await db.roulette_leave(conn, uid)
        if partner_id:
            try: await bot.send_message(partner_id, "💔 Собеседник покинул чат.", reply_markup=main_menu())
            except Exception: pass
        await start_roulette(callback.message, state)

    @dp.message(F.text == "🔄 Следующий")
    async def roulette_btn_next(m: Message, state: FSMContext):
        partner_id = await db.roulette_leave(conn, m.from_user.id)
        if partner_id:
            try: await bot.send_message(partner_id, "💔 Собеседник покинул чат.", reply_markup=main_menu())
            except Exception: pass
        await m.answer("Ищем нового...")
        await start_roulette(m, state)

    @dp.message(F.text == "🚪 Выйти")
    async def roulette_btn_exit(m: Message, state: FSMContext):
        partner_id = await db.roulette_leave(conn, m.from_user.id)
        if partner_id:
            try: await bot.send_message(partner_id, "🚪 Собеседник вышел.", reply_markup=main_menu())
            except Exception: pass
        await m.answer("Ты вышел из рулетки.", reply_markup=main_menu())

    @dp.message(lambda m: not m.text.startswith("/") and m.text not in ["🔄 Следующий", "🚪 Выйти"])
    async def roulette_message_handler(m: Message, state: FSMContext):
        if await state.get_state() is not None: return
        uid = m.from_user.id
        partner_id = await db.roulette_get_partner(conn, uid)
        if partner_id:
            text_lower = (m.text or "").lower()
            if any(word in text_lower for word in STOP_WORDS):
                return await m.answer("⚠️ Запрещенные слова!")
            try:
                if m.text: await bot.send_message(partner_id, f"👤 Собеседник: {m.text}")
                elif m.photo: await bot.send_photo(partner_id, m.photo[-1].file_id, caption="📷 Фото")
            except Exception: await m.answer("Не удалось отправить.")

    # ---------- КТО ЛАЙКНУЛ ----------
    async def render_incoming(message: Message, uid, offset: int):
        total = await db.count_incoming_likes(conn, uid)
        if total == 0: return await message.answer("Пока нет лайков.", reply_markup=main_menu())
        is_prem = await db.check_premium(conn, uid)
        if not is_prem and offset > 0: return await message.answer("🔒 Для листания нужен Премиум.", reply_markup=premium_kb())
        
        rows = await db.incoming_likes(conn, uid, offset=offset, limit=1)
        if not rows: return await message.answer("Больше лайков нет.", reply_markup=main_menu())
        
        p = rows[0]
        likes_state[uid] = {"offset": offset, "kind": "incoming"}
        await show_profile(message, p, mode="incoming")
        nav = likes_nav_kb(offset, total, "incoming") if is_prem else None
        header = f"💖 Лайки: {offset+1} из {total}"
        if not is_prem and total > 1: header += "\n🔒 Премиум — листать всех."
        if nav: await message.answer(header, reply_markup=nav)
        else: await message.answer(header, reply_markup=back_to_menu_kb())

    @dp.callback_query(F.data.startswith("incoming_page_"))
    async def incoming_page(callback: CallbackQuery):
        offset = int(callback.data.split("_")[-1])
        await safe_delete(callback.message)
        await render_incoming(callback.message, callback.from_user.id, offset)
        await safe_answer(callback)

    @dp.callback_query(F.data.startswith("like_back:"))
    async def cb_like_back(callback: CallbackQuery):
        uid = callback.from_user.id
        target = int(callback.data.split(":", 1)[1])
        other = await db.get_user(conn, target)
        if not other or other["is_banned"]: return await safe_answer(callback, "Анкета недоступна.")
        
        matched = await db.like_back(conn, uid, target)
        me = await db.get_user(conn, uid)
        await safe_delete(callback.message)
        if matched:
            await callback.message.answer("💚 <b>Взаимная симпатия!</b>", parse_mode="HTML")
            await show_profile(callback.message, other, mode="match")
            try: await bot.send_message(target, f"💚 Взаимная симпатия! Написать: {user_link(me)}")
            except Exception: pass
        else:
            await callback.message.answer("Ты лайкнул анкету.", reply_markup=main_menu())
        await safe_answer(callback)
        st = likes_state.get(uid)
        if st: await render_incoming(callback.message, uid, st["offset"])

    @dp.callback_query(F.data.startswith("skip:"))
    async def cb_skip(callback: CallbackQuery):
        uid = callback.from_user.id
        target = int(callback.data.split(":", 1)[1])
        await db.skip_incoming_like(conn, uid, target)
        await safe_delete(callback.message)
        st = likes_state.get(uid)
        await render_incoming(callback.message, uid, st["offset"] if st else 0)
        await safe_answer(callback)

    @dp.callback_query(F.data.startswith("age_"))
    async def set_age_filter(callback: CallbackQuery):
        val = callback.data
        if val == "age_close":
            await safe_delete(callback.message)
            return await safe_answer(callback)
        if val == "age_all": mn, mx = MIN_AGE, MAX_AGE
        else:
            parts = val.replace("age_", "").split("_")
            mn, mx = int(parts[0]), int(parts[1])
        await conn.execute("UPDATE users SET target_age_min=?, target_age_max=? WHERE user_id=?", (mn, mx, callback.from_user.id))
        await conn.commit()
        await safe_delete(callback.message)
        await callback.message.answer(f"✅ Фильтр: от {mn} до {mx} лет.", reply_markup=main_menu())
        await safe_answer(callback)

    # ---------- РЕДАКТИРОВАНИЕ ПРОФИЛЯ ----------
    @dp.callback_query(F.data == "edit_name")
    async def cb_edit_name(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.name)
        await safe_delete(callback.message)
        await callback.message.answer("Введи новое имя:")
        await safe_answer(callback)

    @dp.message(EditProfile.name)
    async def save_new_name(m: Message, state: FSMContext):
        name = (m.text or "").strip()
        if not name or len(name) > 30: return await m.answer("Имя от 1 до 30 символов.")
        await conn.execute("UPDATE users SET name=? WHERE user_id=?", (name, m.from_user.id))
        await conn.commit()
        await state.clear()
        await m.answer("✅ Имя обновлено!", reply_markup=main_menu())

    @dp.callback_query(F.data == "edit_age")
    async def cb_edit_age(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.age)
        await safe_delete(callback.message)
        await callback.message.answer(f"Введи новый возраст (от {MIN_AGE} до {MAX_AGE}):")
        await safe_answer(callback)

    @dp.message(EditProfile.age)
    async def save_new_age(m: Message, state: FSMContext):
        if not m.text or not m.text.isdigit() or not (MIN_AGE <= int(m.text) <= MAX_AGE):
            return await m.answer(f"Возраст от {MIN_AGE} до {MAX_AGE} лет.")
        await conn.execute("UPDATE users SET age=? WHERE user_id=?", (int(m.text), m.from_user.id))
        await conn.commit()
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
        await conn.execute("UPDATE users SET bio=? WHERE user_id=?", (bio_value, m.from_user.id))
        await conn.commit()
        await state.clear()
        await m.answer("✅ Описание обновлено!", reply_markup=main_menu())

    @dp.callback_query(F.data == "edit_photo")
    async def cb_edit_photo(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.photo)
        user_photos[callback.from_user.id] = []
        await safe_delete(callback.message)
        await callback.message.answer("Отправь новые фото (до 3-х).", reply_markup=photo_edit_kb())
        await safe_answer(callback)

    @dp.message(EditProfile.photo, F.photo)
    async def save_new_photo(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos: user_photos[uid] = []
        if len(user_photos[uid]) >= 3: return await m.answer("Ты уже добавил 3 фото. Нажми «Сохранить фото».")
        new_photo = m.photo[-1].file_id
        if new_photo in user_photos[uid]: return await m.answer("Ты уже добавил это фото.")
        user_photos[uid].append(new_photo)
        await m.answer(f"Фото добавлено ({len(user_photos[uid])}/3).", reply_markup=photo_edit_kb())

    @dp.message(EditProfile.photo, F.text == "Взять из профиля Telegram")
    async def edit_photo_from_telegram(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos: user_photos[uid] = []
        if len(user_photos[uid]) >= 3: return await m.answer("Уже 3 фото.")
        try:
            photos = await bot.get_user_profile_photos(m.from_user.id, limit=1)
            if not photos.total_count: return await m.answer("Нет фото в профиле.")
            file_id = photos.photos[0][-1].file_id
            if file_id in user_photos[uid]: return await m.answer("Это фото уже добавлено.")
            user_photos[uid].append(file_id)
            await m.answer(f"Фото добавлено ({len(user_photos[uid])}/3).", reply_markup=photo_edit_kb())
        except Exception: await m.answer("Не удалось получить фото.")

    @dp.message(EditProfile.photo, F.text == "Сохранить фото")
    async def edit_photo_save(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos or not user_photos[uid]: return await m.answer("Нет фото!")
        photos_str = ",".join(user_photos[uid])
        await conn.execute("UPDATE users SET photos=?, video_id='' WHERE user_id=?", (photos_str, uid))
        await conn.commit()
        user_photos.pop(uid, None)
        await state.clear()
        await m.answer("✅ Фото обновлены!", reply_markup=main_menu())

    @dp.message(EditProfile.photo)
    async def edit_photo_bad(m: Message, state: FSMContext):
        await m.answer("Отправь фото или нажми кнопку.", reply_markup=photo_edit_kb())

    # ---------- ПРЕМИУМ ----------
    @dp.callback_query(F.data.startswith("buy_premium_"))
    async def buy_premium(callback: CallbackQuery):
        days = int(callback.data.split("_")[-1])
        price = 50 if days == 7 else 150
        title = f"Премиум на {days} дней"
        await bot.send_invoice(chat_id=callback.from_user.id, title=title,
                               description="Доступ к скрытым функциям",
                               payload=f"premium_{days}", provider_token="", currency="XTR",
                               prices=[LabeledPrice(label=title, amount=price)])
        await safe_answer(callback)

    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        if not query.invoice_payload.startswith("premium_"):
            return await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неизвестный товар")
        days = int(query.invoice_payload.split("_")[-1])
        expected = 50 if days == 7 else 150
        if query.total_amount != expected:
            return await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неверная сумма")
        await bot.answer_pre_checkout_query(query.id, ok=True)

    @dp.message(F.successful_payment)
    async def successful_payment(m: Message):
        days = int(m.successful_payment.invoice_payload.split("_")[-1])
        await db.set_premium(conn, m.from_user.id, days)
        await m.answer(f"🎉 Спасибо! Премиум активирован на {days} дней.", reply_markup=main_menu())

    # ---------- АДМИНКА ----------
    @dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
    async def admin_panel(m: Message):
        await m.answer("👑 Админ-панель:", reply_markup=admin_menu_kb())

    @dp.callback_query(F.data == "adm_menu", F.from_user.id.in_(ADMIN_IDS))
    async def adm_menu(callback: CallbackQuery):
        await safe_delete(callback.message)
        await callback.message.answer("👑 Админ-панель:", reply_markup=admin_menu_kb())
        await safe_answer(callback)

    @dp.callback_query(F.data == "adm_close", F.from_user.id.in_(ADMIN_IDS))
    async def adm_close(callback: CallbackQuery):
        await safe_delete(callback.message)
        await safe_answer(callback, "Закрыто.")

    @dp.callback_query(F.data == "adm_stats", F.from_user.id.in_(ADMIN_IDS))
    async def adm_stats(callback: CallbackQuery):
        cur = await conn.execute("SELECT COUNT(*) n FROM users")
        total = (await cur.fetchone())["n"]
        cur = await conn.execute("SELECT COUNT(*) n FROM matches")
        matches = (await cur.fetchone())["n"]
        await safe_delete(callback.message)
        await callback.message.answer(f"📊 Юзеров: {total}\n❤️ Матчей: {matches}", reply_markup=admin_back_kb())
        await safe_answer(callback)

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
            await conn.execute("PRAGMA foreign_keys=OFF")
            for table in ["reactions", "skipped_likes", "reports", "matches", "blocks", "users", "views", "roulette_rooms", "roulette_queue"]:
                await conn.execute(f"DELETE FROM {table}")
            await conn.commit()
            await conn.execute("VACUUM")
            await conn.commit()
            user_photos.clear()
            likes_state.clear()
            await msg.edit_text("✅ База очищена!", reply_markup=admin_back_kb())
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=admin_back_kb())

    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
