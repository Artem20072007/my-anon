import asyncio
import logging
import os
import random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
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

active_cards = {}   # uid -> (mode, target_id)
user_photos = {}    # uid -> [file_id]
likes_state = {}    # uid -> {"offset": int, "kind": "incoming"|"mine"}

def user_link(user):
    if "username" in user.keys() and user["username"]:
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

async def show_profile(message: Message, p, mode="feed", extra_kb=None):
    if not p:
        await message.answer("Анкеты закончились.", reply_markup=main_menu())
        return
    if mode in ("feed", "incoming"):
        active_cards[message.chat.id] = (mode, p["user_id"])
    if mode == "feed":
        markup = feed_kb()
    elif mode == "incoming":
        markup = incoming_kb()
    elif mode == "mine":
        markup = edit_profile_kb()
    elif mode == "liked":
        markup = liked_kb(user_link(p))
    else:
        markup = extra_kb
    caption = profile_caption(p)
    photos = p["photos"].split(",") if ("photos" in p.keys() and p["photos"]) else []
    if photos:
        if mode in ("feed", "incoming", "liked") or len(photos) == 1:
            await message.answer_photo(photos[0], caption=caption, reply_markup=markup)
        else:
            media = [InputMediaPhoto(media=ph, caption=caption if i == 0 else None) for i, ph in enumerate(photos)]
            await message.answer_media_group(media)
            if markup:
                await message.answer("👆 Твоя анкета.", reply_markup=markup)
    elif "video_id" in p.keys() and p["video_id"]:
        await message.answer_video(p["video_id"], caption=caption, reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)

# ---------- Middleware: бан ----------
class BanMiddleware(BaseMiddleware):
    def __init__(self, conn): self.conn = conn
    async def __call__(self, handler, event, data):
        uid = event.from_user.id if hasattr(event, "from_user") and event.from_user else None
        if uid and await db.is_banned(self.conn, uid):
            if isinstance(event, Message):
                await event.answer("🚫 Ты заблокирован.")
            elif isinstance(event, CallbackQuery):
                await event.answer("🚫 Ты заблокирован.", show_alert=True)
            elif isinstance(event, PreCheckoutQuery):
                await event.answer(ok=False, error_message="Ты заблокирован.")
            return
        return await handler(event, data)

async def start_registration(message: Message, state: FSMContext, is_edit=False):
    await state.clear()
    user_photos.pop(message.from_user.id, None)
    await state.update_data(user_id=message.from_user.id, username=message.from_user.username,
                            photos=[], is_edit=is_edit)
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
                    if u["created_at"] > thirty_mins_ago: continue
                    try:
                        await bot.send_message(u["user_id"], f"🔔 За последние 30 минут появилось {new_count} новых анкет в {CITY}! Загляни в поиск.")
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Ошибка фоновой задачи: {e}")

async def main():
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
            if len(args) > 1 and args[1].startswith("ref_"):
                try:
                    ref_id = int(args[1].replace("ref_", ""))
                    if ref_id != m.from_user.id:
                        # сохраняем в state, в БД запишем при save_user
                        await state.update_data(pending_ref=ref_id)
                        await m.answer("🎉 Ты пришел по приглашению друга! Создай анкету, и друг получит +3 дня Премиума.")
                except (ValueError, TypeError) as e:
                    logging.warning(f"ref parse: {e}")
            await start_registration(m, state)
        else:
            await m.answer("С возвращением!", reply_markup=main_menu())

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
        await m.answer(f"В каком ты городе? Наш бот работает только в городе {CITY}.", reply_markup=city_kb())

    @dp.message(Registration.city)
    async def reg_city(m: Message, state: FSMContext):
        if (m.text or "").strip().lower() not in ["чистополь", "г. чистополь"]:
            return await m.answer(f"❌ Бот работает только в {CITY}. Нажми кнопку ниже.", reply_markup=city_kb())
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
        if not m.text.isdigit() or int(m.text) < MIN_AGE or int(m.text) > MAX_AGE:
            return await m.answer(f"Возраст от {MIN_AGE} до {MAX_AGE} лет.")
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

    def _photo_kb_for(d):
        return photo_edit_kb() if d.get("is_edit") else photo_kb()

    @dp.message(Registration.photo, F.photo)
    async def reg_photo(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos: user_photos[uid] = []
        if m.photo[-1].file_id in user_photos[uid]:
            return await m.answer("Ты уже добавил это фото.")
        user_photos[uid].append(m.photo[-1].file_id)
        await state.update_data(photos=user_photos[uid], video_id="")
        d = await state.get_data()
        kb = _photo_kb_for(d)
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
        user_photos.pop(m.from_user.id, None)
        await state.set_state(Registration.confirm)
        d = await state.get_data()
        await m.answer_video(video=m.video.file_id, caption=f"Проверь анкету:\n👤 {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Без описания'}\n\nПодтверди создание.", reply_markup=confirm_kb())

    @dp.message(Registration.photo, F.text == "Сохранить фото")
    async def reg_photo_save(m: Message, state: FSMContext):
        uid = m.from_user.id
        if uid not in user_photos or not user_photos[uid]:
            return await m.answer("Ты не добавил ни одного фото! Отправь фото.")
        await state.update_data(photos=user_photos[uid], video_id="")
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
        uid = m.from_user.id
        d = await state.get_data()
        kb = _photo_kb_for(d)
        if uid not in user_photos: user_photos[uid] = []
        if len(user_photos[uid]) >= 3:
            return await m.answer("Ты уже добавил 3 фото. Нажми «Сохранить фото».", reply_markup=kb)
        try:
            photos = await bot.get_user_profile_photos(m.from_user.id, limit=1)
            if not photos.total_count:
                return await m.answer("В твоем профиле Telegram нет фото. Отправь своё фото.", reply_markup=kb)
            file_id = photos.photos[0][-1].file_id
            if file_id in user_photos[uid]:
                return await m.answer("Это фото уже добавлено.")
            user_photos[uid].append(file_id)
            await state.update_data(photos=user_photos[uid], video_id="")
            if len(user_photos[uid]) < 3:
                await m.answer(f"Фото из профиля добавлено ({len(user_photos[uid])}/3). Еще одно или нажми «Сохранить фото».", reply_markup=kb)
            else:
                await state.set_state(Registration.confirm)
                await m.answer_photo(photo=user_photos[uid][0], caption=f"Проверь анкету:\n👤 {d['name']}, {d['age']}\n📍 {d['city']}\n📝 {d.get('bio') or 'Без описания'}\n\nПодтверди создание.", reply_markup=confirm_kb())
        except Exception as e:
            logging.error(f"profile photo: {e}")
            await m.answer("Не удалось получить фото из профиля. Отправь фото вручную.", reply_markup=kb)

    @dp.message(Registration.photo)
    async def reg_media_bad(m: Message, state: FSMContext):
        d = await state.get_data()
        await m.answer("Пожалуйста, отправь фото, видео (до 15 сек) или выбери кнопку ниже.", reply_markup=_photo_kb_for(d))

    @dp.message(Registration.confirm)
    async def reg_confirm(m: Message, state: FSMContext):
        if m.text == "✏️ Изменить анкету":
            d = await state.get_data()
            # сохраняем pending_ref при перезапуске регистрации
            pending_ref = d.get("pending_ref")
            await start_registration(m, state, is_edit=d.get("is_edit", False))
            if pending_ref:
                await state.update_data(pending_ref=pending_ref)
            return
        if m.text != "✅ Да, все верно":
            return await m.answer("Подтверди кнопкой.")
        d = await state.get_data()
        if not d.get("photos") and not d.get("video_id"):
            return await m.answer("❌ Нельзя создать анкету без фото или видео! Отправь фото или видео.")
        pending_ref = d.get("pending_ref")
        # записываем реферал в БД сразу при создании анкеты
        d["referral_id"] = pending_ref
        await db.save_user(conn, d)
        if pending_ref:
            try:
                await db.add_reward(conn, pending_ref, 3)
                await bot.send_message(pending_ref, "🎁 По твоей ссылке зарегистрировался друг! Тебе начислено +3 дня Премиума ⭐")
            except Exception as e:
                logging.error(f"ref reward: {e}")
        await state.clear()
        user_photos.pop(m.from_user.id, None)
        await m.answer("Анкета готова! Ищем пару.", reply_markup=main_menu())

    # ---------- Лента ----------
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
        card = active_cards.pop(uid, None)
        if not card or card[0] != "feed":
            return await callback.answer("Анкета уже неактивна.")
        target, reaction = card[1], callback.data
        await callback.message.delete()
        await db.add_reaction(conn, uid, target, reaction)
        if reaction == "like" and await db.mutual_like(conn, uid, target):
            await db.add_match(conn, uid, target)
            other = await db.get_user(conn, target)
            me = await db.get_user(conn, uid)
            await callback.message.answer(f"💚 Взаимная симпатия! Написать: {user_link(other)}", reply_markup=main_menu())
            try:
                await bot.send_message(target, f"💚 Взаимная симпатия! Написать: {user_link(me)}")
            except Exception:
                pass
        me = await db.get_user(conn, uid)
        p = await db.next_profile(conn, me)
        if p: await show_profile(callback.message, p)
        else: await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
        await callback.answer()

    @dp.callback_query(F.data == "undo")
    async def handle_undo(callback: CallbackQuery):
        uid = callback.from_user.id
        last_target_id = await db.get_last_reaction(conn, uid)
        if not last_target_id:
            return await callback.answer("Нечего отменять (или прошло 30 минут).", show_alert=True)
        await db.undo_reaction(conn, uid, last_target_id)
        target_user = await db.get_user(conn, last_target_id)
        await callback.message.delete()
        if target_user and target_user["is_active"] and not target_user["is_banned"]:
            await show_profile(callback.message, target_user, mode="feed")
            await callback.answer("Действие отменено!")
        else:
            me = await db.get_user(conn, uid)
            p = await db.next_profile(conn, me)
            if p: await show_profile(callback.message, p)
            else: await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())
            await callback.answer("Анкета недоступна.")

    @dp.callback_query(F.data == "sleep")
    async def handle_sleep(callback: CallbackQuery):
        active_cards.pop(callback.from_user.id, None)
        await conn.execute("UPDATE users SET is_active=0 WHERE user_id=?", (callback.from_user.id,))
        await conn.commit()
        await callback.message.delete()
        await callback.message.answer("Ты скрыл анкету из поиска.", reply_markup=main_menu())
        await callback.answer("Анкета скрыта")

    @dp.callback_query(F.data == "report")
    async def handle_report(callback: CallbackQuery):
        card = active_cards.pop(callback.from_user.id, None)
        if not card: return await callback.answer("Ошибка.")
        await db.add_report(conn, callback.from_user.id, card[1], "Жалоба из ленты")
        await callback.message.delete()
        await callback.answer("Жалоба отправлена модератору.")
        me = await db.get_user(conn, callback.from_user.id)
        p = await db.next_profile(conn, me)
        if p: await show_profile(callback.message, p)
        else: await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())

    @dp.callback_query(F.data == "block")
    async def handle_block(callback: CallbackQuery):
        card = active_cards.pop(callback.from_user.id, None)
        if not card: return await callback.answer("Ошибка.")
        await db.block_user(conn, callback.from_user.id, card[1])
        await callback.message.delete()
        await callback.answer("Пользователь заблокирован.")
        me = await db.get_user(conn, callback.from_user.id)
        p = await db.next_profile(conn, me)
        if p: await show_profile(callback.message, p)
        else: await callback.message.answer("Анкеты закончились.", reply_markup=main_menu())

    # ---------- Премиум ----------
    @dp.message(F.text == "⭐ Премиум")
    async def premium_menu(m: Message):
        is_prem = await db.check_premium(conn, m.from_user.id)
        status = "✅ У тебя активен Премиум!" if is_prem else "❌ У тебя нет Премиума."
        text = (f"{status}\n\nЧто дает Премиум:\n"
                "1. Видишь *ВСЕХ*, кто тебя лайкнул.\n"
                "2. Твоя анкета показывается первой.\n"
                "3. Возможность отменять лайки без ограничений.\n\n"
                "Выбери тариф:")
        await m.answer(text, reply_markup=premium_kb(), parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("buy_premium_"))
    async def buy_premium(callback: CallbackQuery):
        try:
            days = int(callback.data.split("_")[-1])
        except ValueError:
            return await callback.answer("Ошибка тарифа.", show_alert=True)
        price = 50 if days == 7 else 150
        title = f"Премиум на {days} дней"
        await bot.send_invoice(chat_id=callback.from_user.id, title=title,
                               description="Доступ к скрытым функциям бота знакомств",
                               payload=f"premium_{days}", provider_token="", currency="XTR",
                               prices=[LabeledPrice(label=title, amount=price)])
        await callback.answer()

    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        if not query.invoice_payload.startswith("premium_"):
            return await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неизвестный товар")
        try:
            days = int(query.invoice_payload.split("_")[-1])
        except ValueError:
            return await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неизвестный тариф")
        expected = 50 if days == 7 else 150
        if query.total_amount != expected:
            return await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неверная сумма")
        await bot.answer_pre_checkout_query(query.id, ok=True)

    @dp.message(F.successful_payment)
    async def successful_payment(m: Message):
        payload = m.successful_payment.invoice_payload
        try:
            days = int(payload.split("_")[-1])
        except ValueError:
            return await m.answer("Ошибка платежа. Напиши в поддержку.")
        await db.set_premium(conn, m.from_user.id, days)
        await m.answer(f"🎉 Спасибо за покупку! Премиум активирован на {days} дней.")

    # ---------- Кто меня лайкнул (постранично) ----------
    async def render_incoming(message: Message, uid, offset: int):
        total = await db.count_incoming_likes(conn, uid)
        if total == 0:
            likes_state.pop(uid, None)
            await message.answer("Пока нет лайков.", reply_markup=main_menu())
            return
        is_prem = await db.check_premium(conn, uid)
        if not is_prem and offset > 0:
            await message.answer("🔒 Чтобы листать лайки, нужен Премиум.", reply_markup=premium_kb())
            return
        if offset >= total: offset = total - 1
        if offset < 0: offset = 0
        rows = await db.incoming_likes(conn, uid, offset=offset, limit=1)
        if not rows:
            await message.answer("Больше лайков нет.", reply_markup=main_menu())
            return
        p = rows[0]
        likes_state[uid] = {"offset": offset, "kind": "incoming"}
        await show_profile(message, p, mode="incoming")
        nav = likes_nav_kb(offset, total, "incoming")
        header = f"💖 Кто меня лайкнул: {offset+1} из {total}"
        if not is_prem and total > 1:
            header += "\n🔒 Премиум — листать всех."
        if nav:
            await message.answer(header, reply_markup=nav)
        else:
            await message.answer(header, reply_markup=main_menu() if not is_prem else None)

    @dp.message(F.text == "💖 Кто меня лайкнул")
    async def likes_in(m: Message):
        await render_incoming(m, m.from_user.id, 0)

    @dp.callback_query(F.data.startswith("incoming_page_"))
    async def incoming_page(callback: CallbackQuery):
        offset = int(callback.data.split("_")[-1])
        await callback.message.delete()
        await render_incoming(callback.message, callback.from_user.id, offset)
        await callback.answer()

    @dp.callback_query(F.data == "like_back")
    async def cb_like_back(callback: CallbackQuery):
        uid = callback.from_user.id
        card = active_cards.pop(uid, None)
        if not card or card[0] != "incoming":
            return await callback.answer("Ошибка.")
        target = card[1]
        other = await db.get_user(conn, target)
        if not other or other["is_banned"]:
            await callback.message.delete()
            await callback.answer("Анкета недоступна.")
            return
        await db.add_reaction(conn, uid, target, "like")
        await db.add_match(conn, uid, target)
        me = await db.get_user(conn, uid)
        await callback.message.delete()
        await callback.message.answer(f"💚 Взаимно! Написать: {user_link(other)}", reply_markup=main_menu())
        try:
            await bot.send_message(target, f"💚 Взаимно! Написать: {user_link(me)}")
        except Exception:
            pass
        await callback.answer()
        st = likes_state.get(uid)
        if st:
            await render_incoming(callback.message, uid, st["offset"])

    @dp.callback_query(F.data == "skip")
    async def cb_skip(callback: CallbackQuery):
        uid = callback.from_user.id
        card = active_cards.pop(uid, None)
        if card and card[0] == "incoming":
            # запоминаем пропуск, чтобы анкета не возвращалась
            await db.skip_incoming_like(conn, uid, card[1])
        await callback.message.delete()
        st = likes_state.get(uid)
        offset = st["offset"] if st else 0
        # после пропуска на текущей позиции подтягивается следующая, поэтому offset не меняем
        # если пропустили последнюю — total уменьшится, render_incoming сам скорректирует
        await render_incoming(callback.message, uid, offset)
        await callback.answer()

    # ---------- Мои лайки (постранично, без редактирования) ----------
    async def render_my_likes(message: Message, uid, offset: int):
        total = await db.count_my_likes(conn, uid)
        if total == 0:
            await message.answer("Ты пока никого не лайкнул.", reply_markup=main_menu())
            return
        if offset >= total: offset = total - 1
        if offset < 0: offset = 0
        rows = await db.my_likes(conn, uid, offset=offset, limit=1)
        if not rows:
            await message.answer("Больше нет.", reply_markup=main_menu())
            return
        likes_state[uid] = {"offset": offset, "kind": "mine"}
        await show_profile(message, rows[0], mode="liked")
        nav = likes_nav_kb(offset, total, "mylikes")
        header = f"❤️ Мои лайки: {offset+1} из {total}"
        if nav:
            await message.answer(header, reply_markup=nav)
        else:
            await message.answer(header, reply_markup=main_menu())

    @dp.message(F.text == "❤️ Мои лайки")
    async def my_likes(m: Message):
        await render_my_likes(m, m.from_user.id, 0)

    @dp.callback_query(F.data.startswith("mylikes_page_"))
    async def mylikes_page(callback: CallbackQuery):
        offset = int(callback.data.split("_")[-1])
        await callback.message.delete()
        await render_my_likes(callback.message, callback.from_user.id, offset)
        await callback.answer()

    # ---------- Фильтр возраста ----------
    @dp.message(F.text == "🎂 Фильтр возраста")
    async def age_filter(m: Message):
        me = await db.get_user(conn, m.from_user.id)
        cur = f"Сейчас: от {me['target_age_min']} до {me['target_age_max']} лет." if me else ""
        await m.answer(f"{cur}\n\nВыбери диапазон возрастов для поиска:", reply_markup=age_filter_kb())

    @dp.callback_query(F.data == "age_close")
    async def age_close(callback: CallbackQuery):
        await callback.message.delete()
        await callback.answer()

    @dp.callback_query(F.data.startswith("age_"))
    async def set_age_filter(callback: CallbackQuery):
        val = callback.data
        if val == "age_all":
            mn, mx = MIN_AGE, MAX_AGE
        else:
            try:
                parts = val.replace("age_", "").split("_")
                mn, mx = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                return await callback.answer("Ошибка фильтра.", show_alert=True)
        if not (MIN_AGE <= mn <= mx <= MAX_AGE):
            return await callback.answer("Некорректный диапазон.", show_alert=True)
        await conn.execute("UPDATE users SET target_age_min=?, target_age_max=? WHERE user_id=?",
                           (mn, mx, callback.from_user.id))
        await conn.commit()
        await callback.message.delete()
        await callback.message.answer(f"✅ Фильтр возраста установлен: от {mn} до {mx} лет.", reply_markup=main_menu())
        await callback.answer()

    # ---------- Рефералка ----------
    @dp.message(F.text == "🔗 Пригласить друга")
    async def invite_friend(m: Message):
        me = await db.get_user(conn, m.from_user.id)
        if not me:
            return await m.answer("Сначала создай анкету /start")
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{m.from_user.id}"
        await m.answer(f"Пригласи друга по этой ссылке:\n{ref_link}\n\nКогда он зарегистрируется, ты получишь +3 дня Премиума!")

    # ---------- Моя анкета ----------
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
        await callback.message.answer(f"Введи новый возраст (от {MIN_AGE} до {MAX_AGE}):")
        await callback.answer()

    @dp.message(EditProfile.age)
    async def save_new_age(m: Message, state: FSMContext):
        if not m.text.isdigit() or not (MIN_AGE <= int(m.text) <= MAX_AGE):
            return await m.answer(f"Возраст от {MIN_AGE} до {MAX_AGE} лет.")
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
        await m.answer(f"Фото добавлено ({len(user_photos[uid])}/3). Еще одно или нажми «Сохранить фото».", reply_markup=photo_edit_kb())

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
            await m.answer(f"Фото из профиля добавлено ({len(user_photos[uid])}/3). Еще одно или нажми «Сохранить фото».", reply_markup=photo_edit_kb())
        except Exception as e:
            logging.error(f"edit profile photo: {e}")
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
        if not u: return await m.answer("Сначала создай анкету /start", reply_markup=main_menu())
        new = 0 if u["is_active"] else 1
        await conn.execute("UPDATE users SET is_active=? WHERE user_id=?", (new, m.from_user.id))
        await conn.commit()
        await m.answer("Анкета скрыта 🌙" if not new else "Анкета в поиске 💚", reply_markup=main_menu())

    @dp.message(F.text == "ℹ️ Помощь")
    async def help_msg(m: Message):
        await m.answer("Помощь: лайкай анкеты. Контакты только при взаимности. В меню есть фильтр возраста, премиум и приглашение друзей.", reply_markup=main_menu())

    # ---------- Админка ----------
    @dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
    async def admin_panel(m: Message):
        cur = await conn.execute("SELECT COUNT(*) n FROM users")
        u = (await cur.fetchone())["n"]
        cur = await conn.execute("SELECT COUNT(*) n FROM reports")
        r = (await cur.fetchone())["n"]
        await m.answer(f"👑 Админ\nЮзеров: {u}\nЖалоб: {r}\n\n/verify <id> — верифицировать\n/ban <id> — забанить\n/unban <id> — разбанить")

    @dp.message(Command("verify"), F.from_user.id.in_(ADMIN_IDS))
    async def admin_verify(m: Message):
        try:
            uid = int(m.text.split()[1])
        except (IndexError, ValueError):
            return await m.answer("Использование: /verify <id>")
        await db.verify_user(conn, uid)
        await m.answer(f"✅ Юзер {uid} верифицирован.")

    @dp.message(Command("ban"), F.from_user.id.in_(ADMIN_IDS))
    async def admin_ban(m: Message):
        try:
            uid = int(m.text.split()[1])
        except (IndexError, ValueError):
            return await m.answer("Использование: /ban <id>")
        await conn.execute("UPDATE users SET is_banned=1, is_active=0 WHERE user_id=?", (uid,))
        await conn.commit()
        await m.answer(f"🚫 Юзер {uid} забанен.")

    @dp.message(Command("unban"), F.from_user.id.in_(ADMIN_IDS))
    async def admin_unban(m: Message):
        try:
            uid = int(m.text.split()[1])
        except (IndexError, ValueError):
            return await m.answer("Использование: /unban <id>")
        await conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
        await conn.commit()
        await m.answer(f"✅ Юзер {uid} разбанен.")

    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
