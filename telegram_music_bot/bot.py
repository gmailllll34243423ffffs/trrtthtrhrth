import asyncio
import json
import logging
import os
import sys
from typing import Optional, List, Dict, Any

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def get_or_prompt_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if token and token != "YOUR_BOT_TOKEN_HERE":
        return token
    
    if os.path.exists("token.txt"):
        with open("token.txt", "r", encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t

    if not sys.stdin.isatty():
        print("[!] ERROR: BOT_TOKEN is not set in environment or .env.")
        sys.exit(1)

    print("\n" + "=" * 55)
    print(" Токен Telegram-бота не найден!")
    print(" Получите его у @BotFather в Telegram.")
    print("=" * 55)
    entered = input("Введите токен бота: ").strip()
    if not entered:
        print("Ошибка: токен не может быть пустым.")
        sys.exit(1)
    
    with open(".env", "w", encoding="utf-8") as f:
        f.write(f"BOT_TOKEN={entered}\n")
    print("✅ Токен сохранен в .env\n")
    return entered

BOT_TOKEN = get_or_prompt_token()
DB_PATH = "music_dating.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("music_dating_bot")

# -----------------------------------------------------------------------------
# Database Layer
# -----------------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                city TEXT,
                age INTEGER,
                name TEXT,
                songs TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER,
                to_user_id INTEGER,
                reaction_type TEXT, -- 'like', 'dislike'
                valentine_msg TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(from_user_id, to_user_id) ON CONFLICT REPLACE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id INTEGER,
                user2_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user1_id, user2_id) ON CONFLICT IGNORE
            )
        """)
        await db.commit()

async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                data = dict(row)
                data["songs"] = json.loads(data["songs"]) if data["songs"] else []
                return data
            return None

async def upsert_user(user_id: int, username: str, city: str, age: int, name: str, songs: List[str]):
    songs_json = json.dumps(songs, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, city, age, name, songs, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                city = excluded.city,
                age = excluded.age,
                name = excluded.name,
                songs = excluded.songs,
                is_active = 1
        """, (user_id, username, city.strip().lower(), age, name.strip(), songs_json))
        await db.commit()

async def set_user_active(user_id: int, active: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_active = ? WHERE user_id = ?", (1 if active else 0, user_id))
        await db.commit()

async def record_reaction(from_user_id: int, to_user_id: int, r_type: str, val_msg: Optional[str] = None) -> bool:
    """Records like/dislike. Returns True if mutual match detected."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO reactions (from_user_id, to_user_id, reaction_type, valentine_msg)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET
                reaction_type = excluded.reaction_type,
                valentine_msg = excluded.valentine_msg
        """, (from_user_id, to_user_id, r_type, val_msg))
        await db.commit()

        if r_type == "like":
            async with db.execute("""
                SELECT 1 FROM reactions
                WHERE from_user_id = ? AND to_user_id = ? AND reaction_type = 'like'
            """, (to_user_id, from_user_id)) as cur:
                is_mutual = await cur.fetchone()
                if is_mutual:
                    u1, u2 = sorted([from_user_id, to_user_id])
                    await db.execute("INSERT OR IGNORE INTO matches (user1_id, user2_id) VALUES (?, ?)", (u1, u2))
                    await db.commit()
                    return True
    return False

async def get_next_candidate(viewer_id: int) -> Optional[Dict[str, Any]]:
    viewer = await get_user(viewer_id)
    if not viewer:
        return None

    viewer_city = viewer["city"]
    viewer_age = viewer["age"]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT * FROM users
            WHERE user_id != ?
              AND is_active = 1
              AND LOWER(city) = LOWER(?)
              AND ABS(age - ?) <= 1
              AND user_id NOT IN (
                  SELECT to_user_id FROM reactions WHERE from_user_id = ?
              )
            ORDER BY RANDOM()
            LIMIT 1
        """
        async with db.execute(query, (viewer_id, viewer_city, viewer_age, viewer_id)) as cursor:
            row = await cursor.fetchone()
            if row:
                candidate = dict(row)
                candidate["songs"] = json.loads(candidate["songs"]) if candidate["songs"] else []
                return candidate
    return None

async def get_pending_likes_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        query = """
            SELECT COUNT(*) FROM reactions r
            WHERE r.to_user_id = ?
              AND r.reaction_type = 'like'
              AND r.from_user_id NOT IN (
                  SELECT to_user_id FROM reactions WHERE from_user_id = ?
              )
        """
        async with db.execute(query, (user_id, user_id)) as cursor:
            res = await cursor.fetchone()
            return res[0] if res else 0

async def get_next_incoming_like(user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT u.*, r.valentine_msg
            FROM reactions r
            JOIN users u ON u.user_id = r.from_user_id
            WHERE r.to_user_id = ?
              AND r.reaction_type = 'like'
              AND r.from_user_id NOT IN (
                  SELECT to_user_id FROM reactions WHERE from_user_id = ?
              )
            LIMIT 1
        """
        async with db.execute(query, (user_id, user_id)) as cursor:
            row = await cursor.fetchone()
            if row:
                data = dict(row)
                data["songs"] = json.loads(data["songs"]) if data["songs"] else []
                return data
    return None

# -----------------------------------------------------------------------------
# States
# -----------------------------------------------------------------------------
class RegState(StatesGroup):
    step_city = State()
    step_age = State()
    step_name = State()
    step_songs = State()

class FeedState(StatesGroup):
    browsing = State()
    writing_valentine = State()
    viewing_incoming = State()

# -----------------------------------------------------------------------------
# Keyboards
# -----------------------------------------------------------------------------
def get_feed_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👍"), KeyboardButton(text="👎")],
            [KeyboardButton(text="📧"), KeyboardButton(text="💤")]
        ],
        resize_keyboard=True
    )

def get_incoming_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👍"), KeyboardButton(text="👎")],
            [KeyboardButton(text="💤")]
        ],
        resize_keyboard=True
    )

def get_dormant_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚀 Смотреть анкеты")],
            [KeyboardButton(text="👤 Моя анкета")]
        ],
        resize_keyboard=True
    )

def format_profile(user_data: Dict[str, Any], show_valentine: Optional[str] = None) -> str:
    city_cap = user_data["city"].capitalize() if user_data.get("city") else "Не указан"
    songs = user_data.get("songs", [])
    
    songs_formatted = []
    for idx, song in enumerate(songs, start=1):
        songs_formatted.append(f"  {idx}. 🎵 {song}")
    
    songs_text = "\n".join(songs_formatted) if songs_formatted else "  (список треков пуст)"

    text = (
        f"🎧 <b>{user_data['name']}</b>, {user_data['age']} г. {city_cap}\n\n"
        f"<b>Топ-3 любимых трека:</b>\n"
        f"{songs_text}\n"
    )

    if show_valentine:
        text += f"\n💌 <b>Валентинка:</b>\n<i>«{show_valentine}»</i>\n"

    return text

# -----------------------------------------------------------------------------
# Bot Initialization & Handlers
# -----------------------------------------------------------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user:
        await state.set_state(RegState.step_city)
        await message.answer(
            "Привет! Добро пожаловать в музыкальный поиск пар 🎵\n\n"
            "Здесь встречают не по внешности, а по музыкальному вкусу.\n\n"
            "<b>Вопрос 1 из 4:</b> Из какого ты города?",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await set_user_active(user_id, True)
        likes_count = await get_pending_likes_count(user_id)
        if likes_count > 0:
            await state.set_state(FeedState.viewing_incoming)
            await message.answer(
                f"С возвращением! У тебя есть новые оценки ({likes_count})! 💌\n"
                "Давай посмотрим, кому понравился твой вкус:",
                reply_markup=get_incoming_keyboard()
            )
            await show_next_incoming_card(message, user_id, state)
        else:
            await state.set_state(FeedState.browsing)
            await message.answer(
                "Ты снова в эфире! Ищем людей с похожим вкусом...",
                reply_markup=get_feed_keyboard()
            )
            await show_next_candidate_card(message, user_id, state)

# -----------------------------------------------------------------------------
# Registration Flow (4 Steps in separate messages)
# -----------------------------------------------------------------------------
@dp.message(RegState.step_city)
async def process_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    if len(city) < 2:
        await message.answer("Пожалуйста, введи настоящее название города.")
        return
    await state.update_data(city=city)
    await state.set_state(RegState.step_age)
    await message.answer("<b>Вопрос 2 из 4:</b> Сколько тебе лет?", parse_mode="HTML")

@dp.message(RegState.step_age)
async def process_age(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not (12 <= int(text) <= 100):
        await message.answer("Введи корректный возраст цифрами (от 12 до 100).")
        return
    await state.update_data(age=int(text))
    await state.set_state(RegState.step_name)
    await message.answer("<b>Вопрос 3 из 4:</b> Как тебя зовут?", parse_mode="HTML")

@dp.message(RegState.step_name)
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2 or len(name) > 40:
        await message.answer("Имя должно быть от 2 до 40 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(RegState.step_songs)
    await message.answer(
        "<b>Вопрос 4 из 4:</b> Назови свои <b>любимые песни</b> (максимум 3).\n\n"
        "Напиши их через перенос строки или через запятую, например:\n"
        "<i>1. Nirvana - Smells Like Teen Spirit\n"
        "2. Daft Punk - Instant Crush\n"
        "3. Кино - Пачка сигарет</i>",
        parse_mode="HTML"
    )

@dp.message(RegState.step_songs)
async def process_songs(message: types.Message, state: FSMContext):
    raw_text = message.text.strip()
    lines = [line.strip().lstrip("0123456789.-) ") for line in raw_text.splitlines() if line.strip()]
    if len(lines) == 1 and "," in lines[0]:
        lines = [s.strip() for s in lines[0].split(",") if s.strip()]

    if not lines:
        await message.answer("Укажи хотя бы один трек.")
        return

    songs = lines[:3]
    data = await state.get_data()
    username = message.from_user.username or ""

    await upsert_user(
        user_id=message.from_user.id,
        username=username,
        city=data["city"],
        age=data["age"],
        name=data["name"],
        songs=songs
    )

    my_profile = await get_user(message.from_user.id)
    profile_preview = format_profile(my_profile)

    await message.answer(
        f"✅ <b>Анкета сохранена!</b>\n\n{profile_preview}\n"
        "Управление лентой:\n"
        "👍 — Лайк\n"
        "👎 — Дизлайк\n"
        "📧 — Написать валентинку (считается как лайк + записка)\n"
        "💤 — Поставить анкету на паузу",
        parse_mode="HTML"
    )

    await state.set_state(FeedState.browsing)
    await show_next_candidate_card(message, message.from_user.id, state)

# -----------------------------------------------------------------------------
# Feed & Deck Display
# -----------------------------------------------------------------------------
async def show_next_candidate_card(target_msg: types.Message, user_id: int, state: FSMContext):
    candidate = await get_next_candidate(user_id)
    if not candidate:
        await target_msg.answer(
            "Пока что все анкеты в твоем городе просмотрены! 😴\n"
            "Загляни позже или подожди, пока новые меломаны заполнят анкеты.",
            reply_markup=get_dormant_keyboard()
        )
        await state.clear()
        return

    await state.update_data(current_target_id=candidate["user_id"])
    card_text = format_profile(candidate)

    await target_msg.answer(
        card_text,
        parse_mode="HTML",
        reply_markup=get_feed_keyboard()
    )

async def show_next_incoming_card(target_msg: types.Message, user_id: int, state: FSMContext):
    candidate = await get_next_incoming_like(user_id)
    if not candidate:
        await target_msg.answer(
            "Ты просмотрел все входящие лайки! Переходим в обычный поиск...",
            reply_markup=get_feed_keyboard()
        )
        await state.set_state(FeedState.browsing)
        await show_next_candidate_card(target_msg, user_id, state)
        return

    await state.update_data(current_incoming_id=candidate["user_id"])
    card_text = format_profile(candidate, show_valentine=candidate.get("valentine_msg"))

    await target_msg.answer(
        f"💌 <b>Кто-то оценил твой музыкальный вкус:</b>\n\n{card_text}",
        parse_mode="HTML",
        reply_markup=get_incoming_keyboard()
    )

# -----------------------------------------------------------------------------
# Deck Controls: 👍, 👎, 📧, 💤
# -----------------------------------------------------------------------------
@dp.message(FeedState.browsing, F.text == "👍")
async def on_like(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("current_target_id")
    if not target_id:
        await show_next_candidate_card(message, message.from_user.id, state)
        return

    is_match = await record_reaction(message.from_user.id, target_id, "like")
    if is_match:
        await notify_match(message.from_user.id, target_id)
    else:
        try:
            await bot.send_message(
                target_id,
                "🔔 Кому-то понравилась твоя песня! Заканчивай оценивать и посмотри кто там 👀\n\nНажми /start чтобы увидеть анкету!"
            )
        except Exception:
            pass

    await show_next_candidate_card(message, message.from_user.id, state)

@dp.message(FeedState.browsing, F.text == "👎")
async def on_dislike(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("current_target_id")
    if target_id:
        await record_reaction(message.from_user.id, target_id, "dislike")

    await show_next_candidate_card(message, message.from_user.id, state)

@dp.message(FeedState.browsing, F.text == "📧")
async def on_valentine_start(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("current_target_id")
    if not target_id:
        await show_next_candidate_card(message, message.from_user.id, state)
        return

    await state.set_state(FeedState.writing_valentine)
    await message.answer(
        "Напиши текст валентинки 💌 (это засчитается как лайк, и получатель сразу увидит твое сообщение):",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        )
    )

@dp.message(FeedState.writing_valentine)
async def on_valentine_submit(message: types.Message, state: FSMContext):
    if message.text == "Отмена":
        await state.set_state(FeedState.browsing)
        await show_next_candidate_card(message, message.from_user.id, state)
        return

    val_text = message.text.strip()
    data = await state.get_data()
    target_id = data.get("current_target_id")

    if target_id:
        is_match = await record_reaction(message.from_user.id, target_id, "like", val_msg=val_text)
        await message.answer("Валентинка отправлена! 💌")
        if is_match:
            await notify_match(message.from_user.id, target_id)
        else:
            try:
                await bot.send_message(
                    target_id,
                    "🔔 Кому-то понравилась твоя песня и тебе прислали валентинку! Заканчивай оценивать и посмотри кто там 👀\n\nНажми /start чтобы прочитать!"
                )
            except Exception:
                pass

    await state.set_state(FeedState.browsing)
    await show_next_candidate_card(message, message.from_user.id, state)

@dp.message(FeedState.browsing, F.text == "💤")
@dp.message(FeedState.viewing_incoming, F.text == "💤")
async def on_sleep(message: types.Message, state: FSMContext):
    await set_user_active(message.from_user.id, False)
    await state.clear()
    await message.answer(
        "Анкета отправлена в спящий режим 💤\n"
        "Тебя больше не показывают другим в поиске. Когда захочешь вернуться — просто нажми кнопку ниже.",
        reply_markup=get_dormant_keyboard()
    )

# -----------------------------------------------------------------------------
# Incoming Likes Handling
# -----------------------------------------------------------------------------
@dp.message(FeedState.viewing_incoming, F.text == "👍")
async def on_incoming_like(message: types.Message, state: FSMContext):
    data = await state.get_data()
    sender_id = data.get("current_incoming_id")
    if sender_id:
        await record_reaction(message.from_user.id, sender_id, "like")
        await notify_match(message.from_user.id, sender_id)

    await show_next_incoming_card(message, message.from_user.id, state)

@dp.message(FeedState.viewing_incoming, F.text == "👎")
async def on_incoming_dislike(message: types.Message, state: FSMContext):
    data = await state.get_data()
    sender_id = data.get("current_incoming_id")
    if sender_id:
        await record_reaction(message.from_user.id, sender_id, "dislike")

    await show_next_incoming_card(message, message.from_user.id, state)

# -----------------------------------------------------------------------------
# Dormant State / Menu Handlers
# -----------------------------------------------------------------------------
@dp.message(F.text == "🚀 Смотреть анкеты")
async def on_wake_up(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await cmd_start(message, state)
        return

    await set_user_active(user_id, True)
    likes_count = await get_pending_likes_count(user_id)
    if likes_count > 0:
        await state.set_state(FeedState.viewing_incoming)
        await message.answer(
            f"Тебя оценили {likes_count} человек! Смотрим:",
            reply_markup=get_incoming_keyboard()
        )
        await show_next_incoming_card(message, user_id, state)
    else:
        await state.set_state(FeedState.browsing)
        await message.answer("Просыпаемся! Погнали искать единомышленников 🎧", reply_markup=get_feed_keyboard())
        await show_next_candidate_card(message, user_id, state)

@dp.message(F.text == "👤 Моя анкета")
async def on_my_profile(message: types.Message):
    user = await get_user(message.from_user.id)
    if user:
        card = format_profile(user)
        status = "В поиске" if user["is_active"] else "Спит 💤"
        await message.answer(f"Твоя анкета (Статус: {status}):\n\n{card}\nЧтобы заполнить заново, отправь /start")
    else:
        await message.answer("Анкета не найдена. Напиши /start для регистрации.")

# -----------------------------------------------------------------------------
# Match Notification
# -----------------------------------------------------------------------------
async def notify_match(u1_id: int, u2_id: int):
    user1 = await get_user(u1_id)
    user2 = await get_user(u2_id)
    if not user1 or not user2:
        return

    u1_contact = f"@{user1['username']}" if user1.get("username") else f"<a href='tg://user?id={u1_id}'>{user1['name']}</a>"
    u2_contact = f"@{user2['username']}" if user2.get("username") else f"<a href='tg://user?id={u2_id}'>{user2['name']}</a>"

    # Notify User 1
    try:
        await bot.send_message(
            u1_id,
            f"🔥 <b>Взаимное совпадение по музыкальному вкусу!</b>\n\n"
            f"Вам понравились треки друг друга с <b>{user2['name']}</b> ({user2['age']}, г. {user2['city'].capitalize()})!\n"
            f"Связь: {u2_contact}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {u1_id}: {e}")

    # Notify User 2
    try:
        await bot.send_message(
            u2_id,
            f"🔥 <b>Взаимное совпадение по музыкальному вкусу!</b>\n\n"
            f"Вам понравились треки друг друга с <b>{user1['name']}</b> ({user1['age']}, г. {user1['city'].capitalize()})!\n"
            f"Связь: {u1_contact}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {u2_id}: {e}")

# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------
async def main():
    await init_db()
    logger.info("Database initialized. Starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
