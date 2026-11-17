import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (Message, CallbackQuery, InputMediaPhoto, LabeledPrice, 
                           PreCheckoutQuery, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove)
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

active_cards = {}
user_photos = {} 

# --- ВСТРОЕННЫЕ КЛАВИАТУРЫ ДЛЯ БЕЗОПАСНОСТИ ОТ КЭША ---
def premium_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней - 50 ⭐", callback_data="buy_premium_7")],
        [InlineKeyboardButton(text="30 дней - 150 ⭐", callback_data="buy_premium_30")]
    ])

def age_filter_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="14-17", callback_data="age_14_17"), InlineKeyboardButton(text="18-24", callback_data="age_18_24")],
        [InlineKeyboardButton(text="25-35", callback_data="age_25_35"), InlineKeyboardButton(text="36-99", callback_data="age_36_99")],
        [InlineKeyboardButton(text="Все возраста", callback_data="age_all")]
    ])

def user_link(user):
    if user.get("username"):
        return f"https://t.me/{user['username']}"
    return f"tg://user?id={user['user_id']}"

def profile_caption(p):
    if not p or "name" not in p.keys(): return "Ошибка анкеты"
    verified = " ✅" if p["is_verified"] else ""
    name = p["name"].capitalize()
    age = p["age"]
    city = p["city"].capitalize()
    bio = (p["bio"] or '').strip() if "bio" in p.keys() else ''
    if bio.lower() in ["пропустить", "пропустить фото", "без описания"]:
        bio = ""
    if bio:
        return f"{name}{verified}, {age}, {city} — {bio}"
    return f"{name}{verified}, {age}, {city}"

async def show_profile(message: Message, p, mode="feed"):
    if not p:
        await message.answer("Анкеты закончились.", reply_markup=main_menu())
        return
    active_cards[message.from_user.id] = (mode, p["user_id"])
    if mode == "feed":
        markup = feed_kb()
    elif mode == "incoming":
        markup = incoming_kb()
    elif mode == "mine":
        markup = edit_profile_kb()
    else:
        markup = None
    caption = profile_caption(p)
    photos = p["photos"].split(",") if ("photos" in p.keys() and p["photos"]) else []
    if photos:
        if mode == "feed":
            await message.answer_photo(photos[0], caption=caption, reply_markup=markup, has_spoiler=False)
        elif len(photos) == 1:
            await message.answer_photo(photos[0], caption=caption, reply_markup=markup, has_spoiler=False)
        else:
            media = [InputMediaPhoto(media=ph, caption=caption if i == 0 else None) for i, ph in enumerate(photos)]
            await message.answer_media_group(media)
            if markup:
                await message.answer("👆 Твоя анкета.", reply_markup=markup)
    elif "video_id" in p.keys() and p["video_id"]:
        await message.answer_video(p["video_id"], caption=caption, reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)

async def start_registration(message: Message, state: FSMContext, is_edit=False):
    await state.clear()
    await state.update_data(user_id=message.from_user.id, username=message.from_user.username, photos=[], is_edit=is_edit)
    await state.set_state(Registration.gender)
    await message.answer("Привет! Давай создадим анкету. Какой у тебя пол?", reply_markup=gender_kb())

async def notify_new_profiles(bot: Bot, conn):
    while True:
        await asyncio.sleep(1800)
        try:
            thirty_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            cur_new = await conn.execute("SELECT COUNT(*) as cnt FROM users WHERE created_at > ? AND is_active=1 AND is_banned=0", (thirty_mins_ago,))
            new_count = (await cur_new.fetchone())["cnt"]
            if new_count > 1:
                cur = await conn.execute("SELECT user_id, created_at FROM users WHERE is_active=1 AND is_banned=0")
                users = await cur.fetchall()
                for u in users:
                    if u["created_at"] > thirty_mins_ago:
                        continue
                    try:
                        await bot.send_message(u["user_id"], f"🔔 За последние 30 минут появилось {new_count} новых анкет в {CITY}! Загляни в поиск.")
                    except: pass
        except Exception as e:
            logging.error(f"Ошибка фоновой задачи: {e}")

async def main():
    if not TOKEN: raise RuntimeError("Нет токена")
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    conn = await db.connect()
    asyncio.create_task(notify_new_profiles(bot, conn))

    @dp.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        args = m.text.split()
        user = await db.get_user(conn, m.from_user.id)
        
        if not user:
            if len(args) > 1 and args[1].startswith("ref_"):
                try:
                    ref_id = int(args[1].replace("ref_", ""))
                    if ref_id != m.from_user.id:
                        if await db.set_referral(conn, m.from_user.id, ref_id):
                            await db.add_reward(conn, ref_id, 3)
                            await m.answer("🎉 Ты пришел по приглашению друга! Ему начислено +3 дня Премиума.")
                            try:
                                await bot.send_message(ref_id, "🎁 По твоей ссылке зарегистрировался друг! Тебе начислено +3 дня Премиума ⭐")
                            except: pass
                except: pass
            await start_registration(m, state)
        else:
            await m.answer("С возвращением!", reply_markup=main_menu())

    @dp.message(Command("cancel"), StateFilter("*"))
    @dp.message(F.text == "Отмена", StateFilter("*"))
    async def cancel(m: Message, state: FSMContext):
        await state.clear()
        user_photos.pop(m.from_user.id, None)
        await m.answer("Действие отменено.", reply_markup=main_menu())

    # --- РЕГИСТРАЦИЯ И ОСТАЛЬНАЯ ЛОГИКА ОСТАЕТСЯ ПРЕЖНЕЙ ---
    # (оставлено для краткости, код работает)
    @dp.message(Registration.gender)
    async def reg_gender(m: Message, state: FSMContext):
        val = {"Я парень":"m", "Я девушка":"f"}.get(m.text)
        if not val: return await m.answer("Жми кнопки.", reply_markup=gender_kb())
        await state.update_data(gender=val)
        await state.set_state(Registration.target_gender)
        await m.answer("Кого показывать?", reply_markup=target_kb())

    @dp.message(Registration.target_gender)
    async def reg_target(m: Message, state: FSMContext):
        val = {"Парни":"m", "Девушки":"f", "Все равно":"all"}.get(m.text)
        if not val: return await m.answer("Жми кнопки.", reply_markup=target_kb())
        await state.update_data(target_gender=val)
        await state.set_state(Registration.city)
        city_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Чистополь")]], resize_keyboard=True)
        await m.answer(f"В каком ты городе? Наш бот работает только в городе {CITY}.", reply_markup=city_kb)

    @dp.message(Registration.city)
    async def reg_city(m: Message, state: FSMContext):
        if (m.text or "").strip().lower() not in ["чистополь", "г. чистополь"]:
            city_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Чистополь")]], resize_keyboard=True)
            return await m.answer(f"❌ Бот работает только в {CITY}. Нажми кнопку ниже.", reply_markup=city_kb)
        await state.update_data(city=CITY)
        await state.set_state(Registration.name)
        await m.answer("Как мне тебя называть?", reply_markup=ReplyKeyboardRemove())

    @dp.message(Registration.name)
    async def reg_name(m: Message, state: FSMContext):
        if not m.text or len(m.text) > 30: return await m.answer("Имя от 1 до 30 символов.")
        await state.update_data(name=m.text)
        await state.set_state(Registration.age)
        await m.answer(f"Сколько тебе лет? (от {MIN_AGE})")

    @dp.message(Registration.age)
    async def reg_age(m: Message, state: FSMContext):
        if not m.text.isdigit() or int(m.text) < MIN_AGE: return await m.answer(f"Возраст от {MIN_AGE} лет.")
        await state.update_data(age=int(m.text))
        await state.set_state(Registration.bio)
        bio_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить")]], resize_keyboard=True)
        await m.answer("Расскажи о себе и кого хочешь найти, чем предлагаешь заняться. Это поможет лучше подобрать тебе компанию.", reply_markup=bio_kb)

    @dp.message(Registration.bio)
    async def reg_bio(m: Message, state: FSMContext):
        text = (m.text or "").strip().lower()
        bio_value = "" if text in ["пропустить", "пропустить фото"] else m.text
        await state.update_data(bio=bio_value)
        await state.set_state(Registration.photo)
        d = await state.get_data()
        if d.get("is_edit"):
            await m.answer("Пришли новое фото или выбери действие:", reply_markup=photo_edit_kb())
        else:
            await m.answer("Пришли своё фото или запиши видео (до 15 сек).\nАнкеты, где видно лицо, собирают больше лайков ❤️", reply_markup=photo_kb())

    # --- ОБРАБОТКА ФОТО ---
    @dp.message(Registration.photo, F.photo)
    async def reg_photo(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos: user_photos[uid] = []
        if m.photo[-1].file_id in user_photos[uid]:
            return await m.answer("Ты уже добавил это фото.")
        user_photos[uid].append(m.photo[-1].file_id)
        await state.update_data(photos=user_photos[uid], video_id="")
        d = await state.get_data()
        kb = photo_edit_kb() if d.get("is_edit") else photo_kb()
        if len(user_photos[uid]) < 3:
            await m.answer(f"Фото добавлено ({len(user_photos[uid])}/3). Еще одно или нажми «Сохранить фото».", reply_markup=kb)
        else:
            await state.set_state(Registration.confirm)
            await m.answer_photo(photo=user_photos[uid][0], caption=f"Проверь анкету:\n👤 {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Без описания'}\n\nПодтверди создание.", reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.video)
    async def reg_video(m: Message, state: FSMContext):
        if m.video.duration > 15:
            return await m.answer("Видео должно быть не длиннее 15 секунд! Попробуй еще раз или пришли фото.")
        await state.update_data(video_id=m.video.file_id, photos=[])
        await state.set_state(Registration.confirm)
        d = await state.get_data()
        await m.answer_video(video=m.video.file_id, caption=f"Проверь анкету:\n👤 {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Без описания'}\n\nПодтверди создание.", reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.text == "Сохранить фото")
    async def reg_photo_save(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos or not user_photos[uid]:
            return await m.answer("Ты не добавил ни одного фото! Отправь фото.")
        await state.update_data(photos=user_photos[uid])
        await state.set_state(Registration.confirm)
        d = await state.get_data()
        await m.answer_photo(photo=user_photos[uid][0], caption=f"Проверь анкету:\n👤 {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Без описания'}\n\nПодтверди создание.", reply_markup=confirm_kb())

    @dp.message(Registration.confirm)
    async def reg_confirm(m: Message, state: FSMContext):
        if m.text == "✏️ Изменить анкету":
            d = await state.get_data()
            return await start_registration(m, state, is_edit=d.get("is_edit", False))
        if m.text != "✅ Да, все верно":
            return await m.answer("Подтверди кнопкой.")
        d = await state.get_data()
        if not d.get("photos") and not d.get("video_id"):
            return await m.answer("❌ Нельзя создать анкету без фото или видео! Отправь фото или видео.")
        await db.save_user(conn, d)
        await state.clear()
        user_photos.pop(m.from_user.id, None)
        await m.answer("Анкета готова! Ищем пару.", reply_markup=main_menu())

    # --- ПОИСК И ЛАЙКИ ---
    @dp.message(F.text == "🔎 Смотреть анкеты")
    async def feed(m: Message):
        me = await db.get_user(conn, m.from_user.id)
        if not me: return await m.answer("Сначала создай анкету /start")
        p = await db.next_profile(conn, me)
        if p: await show_profile(m, p)
        else: await m.answer("Анкеты закончились. Заходи позже!", reply_markup=main_menu())

    @dp.callback_query(F.data.in_({"like", "dislike"}))
    async def handle_reaction(callback: CallbackQuery):
        uid = callback.from_user.id
        card = active_cards.get(uid)
        if not card or card[0] != "feed": return await callback.answer("Анкета уже неактивна.")
        target, reaction = card[1], callback.data
        await db.add_reaction(conn, uid, target, reaction)
        if reaction == "like" and await db.mutual_like(conn, uid, target):
            await db.add_match(conn, uid, target)
            other = await db.get_user(conn, target)
            me = await db.get_user(conn, uid)
            await callback.message.answer(f"💚 Взаимная симпатия! Написать: {user_link(other)}", reply_markup=main_menu())
            try:
                await bot.send_message(target, f"💚 Взаимная симпатия! Написать: {user_link(me)}")
            except: pass
        me = await db.get_user(conn, uid)
        p = await db.next_profile(conn, me)
        if p: await show_profile(callback.message, p)
        else: await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
        await callback.message.delete()
        await callback.answer()

    # --- ПРЕМИУМ ---
    @dp.message(F.text == "⭐ Премиум")
    async def premium_menu(m: Message):
        is_prem = await db.check_premium(conn, m.from_user.id)
        status = "✅ У тебя активен Премиум!" if is_prem else "❌ У тебя нет Премиума."
        text = f"{status}\n\nЧто дает Премиум:\n1. Видишь **ВСЕХ**, кто тебя лайкнул.\n2. Твоя анкета показывается первой.\n3. Возможность отменять лайки без ограничений.\n\nВыбери тариф:"
        await m.answer(text, reply_markup=premium_kb())

    try:
        await dp.start_polling(bot)
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
