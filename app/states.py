from aiogram.fsm.state import State, StatesGroup

class Registration(StatesGroup):
    gender = State()
    target_gender = State()
    city = State()
    name = State()
    age = State()
    bio = State()
    photo = State()
    confirm = State()

class EditProfile(StatesGroup):
    name = State()
    age = State()
    bio = State()
    photo = State()