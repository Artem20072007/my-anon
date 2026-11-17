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

    @dp.message(Registration.photo, F.text == "Оставить текущее")
    async def reg_photo_keep(m: Message, state: FSMContext):
        d = await state.get_data()
        if not d.get("is_edit"):
            return await m.answer("Это кнопка для редактирования.", reply_markup=photo_kb())
        old_user = await db.get_user(conn, m.from_user.id)
        if old_user and old_user["photos"]:
            photos = old_user["photos"].split(",")
            await state.update_data(photos=photos, video_id="")
            await state.set_state(Registration.confirm)
            await m.answer_photo(photo=photos[0], caption=f"Проверь анкету:\n👤 {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Без описания'}\n\nПодтверди создание.", reply_markup=confirm_kb())
        else:
            await m.answer("У тебя нет старого фото. Отправь новое.", reply_markup=photo_kb())

    @dp.message(Registration.photo, F.text == "Взять из профиля Telegram")
    async def reg_photo_telegram(m: Message, state: FSMContext):
        try:
            photos = await bot.get_user_profile_photos(m.from_user.id, limit=1)
            if not photos.total_count:
                return await m.answer("В твоем профиле Telegram нет фото. Отправь своё фото.", reply_markup=photo_kb())
            file_id = photos.photos[0][-1].file_id
            await state.update_data(photos=[file_id], video_id="")
            await state.set_state(Registration.confirm)
            d = await state.get_data()
            await m.answer_photo(photo=file_id, caption=f"Проверь анкету:\n👤 {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Без описания'}\n\nПодтверди создание.", reply_markup=confirm_kb())
        except Exception:
            await m.answer("Не удалось получить фото из профиля. Отправь фото вручную.", reply_markup=photo_kb())

    @dp.message(Registration.photo)
    async def reg_media_bad(m: Message, state: FSMContext):
        d = await state.get_data()
        kb = photo_edit_kb() if d.get("is_edit") else photo_kb()
        await m.answer("Пожалуйста, отправь фото, видео (до 15 сек) или выбери кнопку ниже.", reply_markup=kb)

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

    @dp.callback_query(F.data == "undo")
    async def handle_undo(callback: CallbackQuery):
        uid = callback.from_user.id
        last_target_id = await db.get_last_reaction(conn, uid)
        if not last_target_id:
            return await callback.answer("Нечего отменять. Ты пока никого не оценил.", show_alert=True)
        await db.undo_reaction(conn, uid, last_target_id)
        target_user = await db.get_user(conn, last_target_id)
        if target_user and target_user["is_active"] and not target_user["is_banned"]:
            await callback.message.delete()
            await show_profile(callback.message, target_user, mode="feed")
            await callback.answer("Действие отменено!")
        else:
            await callback.answer("Анкета недоступна.", show_alert=True)
            me = await db.get_user(conn, uid)
            p = await db.next_profile(conn, me)
            if p: await show_profile(callback.message, p)
            else: await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
            await callback.message.delete()

    @dp.callback_query(F.data == "sleep")
    async def handle_sleep(callback: CallbackQuery):
        await callback.answer("Анкета скрыта")
        await callback.message.delete()
        await callback.message.answer("Ты скрыл анкету из поиска.", reply_markup=main_menu())
        await conn.execute("UPDATE users SET is_active=0 WHERE user_id=?", (callback.from_user.id,))
        await conn.commit()

    @dp.callback_query(F.data == "report")
    async def handle_report(callback: CallbackQuery):
        card = active_cards.get(callback.from_user.id)
        if not card: return await callback.answer("Ошибка.")
        await db.add_report(conn, callback.from_user.id, card[1], "Жалоба из ленты")
        await callback.answer("Жалоба отправлена модератору.")
        await callback.message.delete()

    @dp.callback_query(F.data == "block")
    async def handle_block(callback: CallbackQuery):
        card = active_cards.get(callback.from_user.id)
        if not card: return await callback.answer("Ошибка.")
        await db.block_user(conn, callback.from_user.id, card[1])
        await callback.answer("Пользователь заблокирован.")
        await callback.message.delete()

    @dp.message(F.text == "⭐ Премиум")
    async def premium_menu(m: Message):
        is_prem = await db.check_premium(conn, m.from_user.id)
        status = "✅ У тебя активен Премиум!" if is_prem else "❌ У тебя нет Премиума."
        text = f"{status}\n\nЧто дает Премиум:\n1. Видишь **ВСЕХ**, кто тебя лайкнул.\n2. Твоя анкета показывается первой.\n3. Возможность отменять лайки без ограничений.\n\nВыбери тариф:"
        await m.answer(text, reply_markup=premium_kb())

    @dp.callback_query(F.data.startswith("buy_premium_"))
    async def buy_premium(callback: CallbackQuery):
        days = int(callback.data.split("_")[-1])
        price = 50 if days == 7 else 150
        title = f"Премиум на {days} дней"
        await bot.send_invoice(chat_id=callback.from_user.id, title=title, description="Доступ к скрытым функциям бота знакомств", payload=f"premium_{days}", provider_token="", currency="XTR", prices=[LabeledPrice(label=title, amount=price)], start_parameter="premium")
        await callback.answer()

    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        await bot.answer_pre_checkout_query(query.id, ok=True)

    @dp.message(F.successful_payment)
    async def successful_payment(m: Message):
        payload = m.successful_payment.invoice_payload
        days = int(payload.split("_")[-1])
        await db.set_premium(conn, m.from_user.id, days)
        await m.answer(f"🎉 Спасибо за покупку! Премиум активирован на {days} дней.")

    @dp.message(F.text == "💖 Кто меня лайкнул")
    async def likes_in(m: Message):
        rows = await db.incoming_likes(conn, m.from_user.id)
        if not rows: return await m.answer("Пока нет лайков.", reply_markup=main_menu())
        is_prem = await db.check_premium(conn, m.from_user.id)
        if is_prem:
            await m.answer("Ты видишь всех, кто тебя лайкнул (Премиум активен):")
            for p in rows[:10]: await show_profile(m, p, mode="incoming")
        else:
            await show_profile(m, rows[0], mode="incoming")
            if len(rows) > 1:
                await m.answer(f"🔒 А еще тебя лайкнули {len(rows)-1} человек. Купи Премиум, чтобы увидеть их всех!", reply_markup=premium_kb())

    @dp.message(F.text == "❤️ Мои лайки")
    async def my_likes(m: Message):
        rows = await db.my_likes(conn, m.from_user.id)
        if not rows: return await m.answer("Ты пока никого не лайкнул.", reply_markup=main_menu())
        for p in rows[:3]: await show_profile(m, p, mode="mine")

    @dp.callback_query(F.data == "like_back")
    async def cb_like_back(callback: CallbackQuery):
        uid = callback.from_user.id
        card = active_cards.get(uid)
        if not card or card[0] != "incoming": return await callback.answer("Ошибка.")
        target = card[1]
        await db.add_reaction(conn, uid, target, "like")
        await db.add_match(conn, uid, target)
        other = await db.get_user(conn, target)
        me = await db.get_user(conn, uid)
        await callback.message.answer(f"💚 Взаимно! Написать: {user_link(other)}", reply_markup=main_menu())
        try:
            await bot.send_message(target, f"💚 Взаимно! Написать: {user_link(me)}")
        except: pass
        await callback.message.delete()
        active_cards.pop(uid, None)
        await callback.answer()

    @dp.callback_query(F.data == "skip")
    async def cb_skip(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.message.delete()
        active_cards.pop(uid, None)
        rows = await db.incoming_likes(conn, uid)
        if rows: await show_profile(callback.message, rows[0], mode="incoming")
        else: await callback.message.answer("Лайки закончились.", reply_markup=main_menu())
        await callback.answer()

    @dp.message(F.text == "🎂 Фильтр возраста")
    async def age_filter(m: Message):
        await m.answer("Выбери диапазон возрастов для поиска:", reply_markup=age_filter_kb())

    @dp.callback_query(F.data.startswith("age_"))
    async def set_age_filter(callback: CallbackQuery):
        val = callback.data
        if val == "age_all":
            mn, mx = 14, 99
        else:
            parts = val.replace("age_", "").split("_")
            mn, mx = int(parts[0]), int(parts[1])
        await conn.execute("UPDATE users SET target_age_min=?, target_age_max=? WHERE user_id=?", (mn, mx, callback.from_user.id))
        await conn.commit()
        await callback.message.delete()
        await callback.message.answer(f"✅ Фильтр возраста установлен: от {mn} до {mx} лет.", reply_markup=main_menu())
        await callback.answer()

    @dp.message(F.text == "🔗 Пригласить друга")
    async def invite_friend(m: Message):
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{m.from_user.id}"
        await m.answer(f"Пригласи друга по этой ссылке:\n{ref_link}\n\nКогда он зарегистрируется, ты получишь +3 дня Премиума!")

    @dp.message(F.text == "👤 Моя анкета")
    async def my_profile(m: Message):
        u = await db.get_user(conn, m.from_user.id)
        if u: await show_profile(m, u, mode="mine")
        else: await m.answer("Нет анкеты.", reply_markup=main_menu())

    @dp.callback_query(F.data == "edit_profile")
    async def cb_edit_profile(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.answer("Что хочешь изменить в анкете?", reply_markup=edit_menu_kb())
        await callback.answer()

    @dp.callback_query(F.data == "edit_name")
    async def cb_edit_name(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.name)
        await callback.message.answer("Введи новое имя:")
        await callback.answer()

    @dp.message(EditProfile.name)
    async def save_new_name(m: Message, state: FSMContext):
        if not m.text or len(m.text) > 30: return await m.answer("Имя от 1 до 30 символов.")
        await conn.execute("UPDATE users SET name=? WHERE user_id=?", (m.text, m.from_user.id))
        await conn.commit()
        await state.clear()
        await m.answer("✅ Имя обновлено!", reply_markup=main_menu())

    @dp.callback_query(F.data == "edit_age")
    async def cb_edit_age(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.age)
        await callback.message.answer(f"Введи новый возраст (от {MIN_AGE}):")
        await callback.answer()

    @dp.message(EditProfile.age)
    async def save_new_age(m: Message, state: FSMContext):
        if not m.text.isdigit() or int(m.text) < MIN_AGE: return await m.answer(f"Возраст от {MIN_AGE} лет.")
        await conn.execute("UPDATE users SET age=? WHERE user_id=?", (int(m.text), m.from_user.id))
        await conn.commit()
        await state.clear()
        await m.answer("✅ Возраст обновлён!", reply_markup=main_menu())

    @dp.callback_query(F.data == "edit_bio")
    async def cb_edit_bio(callback: CallbackQuery, state: FSMContext):
        await state.set_state(EditProfile.bio)
        await callback.message.answer("Введи новое описание (или напиши 'Пропустить'):")
        await callback.answer()

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
        await callback.message.answer("Отправь новые фото (до 3-х). Ты можешь добавить их из профиля Telegram.", reply_markup=photo_edit_kb())
        await callback.answer()

    @dp.message(EditProfile.photo, F.photo)
    async def save_new_photo(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos: user_photos[uid] = []
        if len(user_photos[uid]) >= 3: return await m.answer("Ты уже добавил 3 фото. Нажми «Сохранить фото».")
        new_photo = m.photo[-1].file_id
        if new_photo in user_photos[uid]: return await m.answer("Ты уже добавил это фото.")
        user_photos[uid].append(new_photo)
        if len(user_photos[uid]) < 3:
            await m.answer(f"Фото добавлено ({len(user_photos[uid])}/3). Еще одно или нажми «Сохранить фото».", reply_markup=photo_edit_kb())
        else:
            await m.answer(f"Фото добавлено ({len(user_photos[uid])}/3). Нажми «Сохранить фото».", reply_markup=photo_edit_kb())

    @dp.message(EditProfile.photo, F.text == "Взять из профиля Telegram")
    async def edit_photo_from_telegram(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos: user_photos[uid] = []
        if len(user_photos[uid]) >= 3: return await m.answer("Ты уже добавил 3 фото. Нажми «Сохранить фото».")
        try:
            photos = await bot.get_user_profile_photos(m.from_user.id, limit=1)
            if not photos.total_count: return await m.answer("В твоем профиле Telegram нет фото.")
            file_id = photos.photos[0][-1].file_id
            if file_id in user_photos[uid]: return await m.answer("Это фото уже добавлено.")
            user_photos[uid].append(file_id)
            if len(user_photos[uid]) < 3:
                await m.answer(f"Фото из профиля добавлено ({len(user_photos[uid])}/3). Еще одно или нажми «Сохранить фото».", reply_markup=photo_edit_kb())
            else:
                await m.answer(f"Фото из профиля добавлено ({len(user_photos[uid])}/3). Нажми «Сохранить фото».", reply_markup=photo_edit_kb())
        except Exception:
            await m.answer("Не удалось получить фото из профиля.")

    @dp.message(EditProfile.photo, F.text == "Сохранить фото")
    async def edit_photo_save(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos or not user_photos[uid]:
            return await m.answer("Ты не добавил ни одного фото! Отправь фото.")
        photos_str = ",".join(user_photos[uid])
        await conn.execute("UPDATE users SET photos=?, video_id='' WHERE user_id=?", (photos_str, uid))
        await conn.commit()
        user_photos.pop(uid, None)
        await state.clear()
        await m.answer("✅ Фото обновлены!", reply_markup=main_menu())

    @dp.message(EditProfile.photo)
    async def edit_photo_bad(m: Message, state: FSMContext):
        await m.answer("Пожалуйста, отправь фото или нажми кнопку.", reply_markup=photo_edit_kb())

    @dp.callback_query(F.data == "cancel_edit")
    async def cb_cancel_edit(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        user_photos.pop(callback.from_user.id, None)
        await callback.message.delete()
        await callback.message.answer("Редактирование отменено.", reply_markup=main_menu())
        await callback.answer()

    @dp.message(F.text == "🌙 Сон / Поиск")
    async def toggle(m: Message):
        u = await db.get_user(conn, m.from_user.id)
        new = 0 if u["is_active"] else 1
        await conn.execute("UPDATE users SET is_active=? WHERE user_id=?", (new, m.from_user.id))
        await conn.commit()
        await m.answer("Анкета скрыта 🌙" if not new else "Анкета в поиске 💚", reply_markup=main_menu())

    @dp.message(F.text == "ℹ️ Помощь")
    async def help_msg(m: Message):
        await m.answer("Помощь: лайкай анкеты. Контакты только при взаимности. В меню есть фильтр возраста, премиум и приглашение друзей.", reply_markup=main_menu())

    try:
        await dp.start_polling(bot)
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
