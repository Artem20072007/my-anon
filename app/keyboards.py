from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# --- РЕПЛАЙ-КЛАВИАТУРЫ ДЛЯ РЕГИСТРАЦИИ ---
def gender_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Я девушка"), KeyboardButton(text="Я парень")]], resize_keyboard=True)

def target_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="С девушками"), KeyboardButton(text="С парнями")], 
        [KeyboardButton(text="Без разницы")]
    ], resize_keyboard=True)

def city_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Чистополь")]], resize_keyboard=True)

def photo_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Взять из профиля Telegram")],
        [KeyboardButton(text="Сохранить фото")]
    ], resize_keyboard=True)

def photo_edit_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Оставить текущее")],
        [KeyboardButton(text="Взять из профиля Telegram")],
        [KeyboardButton(text="Сохранить фото")]
    ], resize_keyboard=True)

def confirm_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Да, все верно")], [KeyboardButton(text="✏️ Изменить анкету")]], resize_keyboard=True)

# --- ГЛАВНОЕ МЕНЮ (Reply, как на скрине 1) ---
def main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔎 Смотреть анкеты"), KeyboardButton(text="🎲 Чат-рулетка")],
        [KeyboardButton(text="💗 Кто меня лайкнул"), KeyboardButton(text="👤 Моя анкета")],
        [KeyboardButton(text="⭐ Премиум"), KeyboardButton(text="🔗 Пригласить друга")]
    ], resize_keyboard=True)

# --- МЕНЮ ПРОФИЛЯ (Reply, как на скрине 2) ---
def profile_menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✏️ Изменить информацию")],
        [KeyboardButton(text="📸 Мои фотографии")],
        [KeyboardButton(text="🎂 Фильтр возраста")],
        [KeyboardButton(text="🚻 Кого показывать")],
        [KeyboardButton(text="📍 Мой город")],
        [KeyboardButton(text="🌙 Сон / Поиск")],
        [KeyboardButton(text="👁 Предпросмотр анкеты")]
    ], resize_keyboard=True)

# --- ИНЛАЙН-КНОПКИ ДЛЯ ЛЕНТЫ И ЛАЙКОВ ---
def feed_kb(target_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️", callback_data=f"like:{target_id}"),
         InlineKeyboardButton(text="👎", callback_data=f"dislike:{target_id}")],
        [InlineKeyboardButton(text="💤 Скрыть", callback_data="sleep"),
         InlineKeyboardButton(text="↩️ Отменить", callback_data="undo")]
    ])

def incoming_kb(target_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💚 Ответить взаимностью", callback_data=f"like_back:{target_id}")],
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"skip:{target_id}")]
    ])

def edit_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить описание", callback_data="edit_bio")],
        [InlineKeyboardButton(text="🖼 Изменить фото", callback_data="edit_photo")],
        [InlineKeyboardButton(text="👤 Изменить имя", callback_data="edit_name")],
        [InlineKeyboardButton(text="🎂 Изменить возраст", callback_data="edit_age")],
        [InlineKeyboardButton(text="❌ Назад в профиль", callback_data="menu_profile")]
    ])

def age_filter_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="14-17", callback_data="age_14_17"), InlineKeyboardButton(text="18-24", callback_data="age_18_24")],
        [InlineKeyboardButton(text="25-35", callback_data="age_25_35"), InlineKeyboardButton(text="36-99", callback_data="age_36_99")],
        [InlineKeyboardButton(text="Все возраста", callback_data="age_all")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="age_close")]
    ])

def premium_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней - 50 ⭐️", callback_data="buy_premium_7")],
        [InlineKeyboardButton(text="30 дней - 150 ⭐️", callback_data="buy_premium_30")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="age_close")]
    ])

def likes_nav_kb(offset, total, prefix):
    row = []
    if offset > 0:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{prefix}_page_{offset-1}"))
    if offset + 1 < total:
        row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"{prefix}_page_{offset+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None

def back_to_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В меню", callback_data="go_menu")]
    ])

def roulette_found_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💚 Начать общение", callback_data="roulette_start")],
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="roulette_skip")]
    ])

def roulette_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔄 Следующий"), KeyboardButton(text="🚪 Выйти")]
    ], resize_keyboard=True)

# ========== АДМИН-МЕНЮ ==========
def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="🚨 Жалобы", callback_data="adm_reports_0")],
        [InlineKeyboardButton(text="🧹 Очистить БД", callback_data="adm_clear_db")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="adm_close")]
    ])

def admin_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_menu")]
    ])

def admin_clear_confirm1_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Я уверен, продолжить", callback_data="adm_clear_step2")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_menu")]
    ])

def admin_clear_confirm2_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💀 УДАЛИТЬ ВСЁ ОКОНЧАТЕЛЬНО", callback_data="adm_clear_final")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_menu")]
    ])
