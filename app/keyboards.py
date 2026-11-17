from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

def gender_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Я девушка"), KeyboardButton(text="Я парень")]], resize_keyboard=True)

def target_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Девушки"), KeyboardButton(text="Парни")], [KeyboardButton(text="Все равно")]], resize_keyboard=True)

def city_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Чистополь")]], resize_keyboard=True)

def phone_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]], resize_keyboard=True)

def photo_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Сохранить фото")]], resize_keyboard=True)

def photo_edit_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Оставить текущее")],
        [KeyboardButton(text="Взять из профиля Telegram")],
        [KeyboardButton(text="Сохранить фото")]
    ], resize_keyboard=True)

def confirm_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Да, все верно")], [KeyboardButton(text="✏️ Изменить анкету")]], resize_keyboard=True)

def main_menu():
    # ИСПРАВЛЕНИЕ: добавлены фильтр возраста и рефералка
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔎 Смотреть анкеты"), KeyboardButton(text="👤 Моя анкета")],
        [KeyboardButton(text="💖 Кто меня лайкнул"), KeyboardButton(text="❤️ Мои лайки")],
        [KeyboardButton(text="🎂 Фильтр возраста"), KeyboardButton(text="🔗 Пригласить друга")],
        [KeyboardButton(text="🌙 Сон / Поиск"), KeyboardButton(text="⭐ Премиум")],
        [KeyboardButton(text="ℹ️ Помощь")]
    ], resize_keyboard=True)

def feed_kb():
    # ИСПРАВЛЕНИЕ: добавлена кнопка «Отменить»
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️", callback_data="like"), InlineKeyboardButton(text="👎", callback_data="dislike")],
        [InlineKeyboardButton(text="💤 Скрыть", callback_data="sleep"), InlineKeyboardButton(text="↩️ Отменить", callback_data="undo")]
    ])

def incoming_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💚 Ответить взаимностью", callback_data="like_back")],
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip")]
    ])

def edit_profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать анкету", callback_data="edit_profile")]
    ])

def edit_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить описание", callback_data="edit_bio")],
        [InlineKeyboardButton(text="🖼 Изменить фото", callback_data="edit_photo")],
        [InlineKeyboardButton(text="👤 Изменить имя", callback_data="edit_name")],
        [InlineKeyboardButton(text="🎂 Изменить возраст", callback_data="edit_age")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]
    ])