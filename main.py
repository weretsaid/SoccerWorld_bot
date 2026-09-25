import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ============================================================
# BOT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8937928562:AAF3XL0-apZ8vVWO6SAynVzbPtfdHf2MYsA")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "-1003920866207").strip()
TRANSFER_CHANNEL_ID = os.getenv("TRANSFER_CHANNEL_ID", "-1004311047488").strip()

BASE_DIR = Path(__file__).resolve().parent

DB_CORE_FILE        = BASE_DIR / "database.json"
DB_ADMINS_FILE      = BASE_DIR / "admins.json"
DB_REQUESTS_FILE    = BASE_DIR / "transfer_requests.json"
DB_FREEAGENTS_FILE  = BASE_DIR / "free_agents.json"
DB_MODERATION_FILE  = BASE_DIR / "moderation.json"

DB_LOCK = asyncio.Lock()

MAX_PLAYERS = 12
FREE_AGENT_COOLDOWN_MINUTES = 60
CAREER_DAYS = 30

NATIONS = {
    "Россия": "🇷🇺",
    "Нидерланды": "🇳🇱",
    "Франция": "🇫🇷",
    "Германия": "🇩🇪",
    "England": "🏴",
    "Италия": "🇮🇹",
    "Бразилия": "🇧🇷",
    "Аргентина": "🇦🇷",
    "Испания": "🇪🇸",
    "Португалия": "🇵🇹",
    "США": "🇺🇸",
    "Марокко": "🇲🇦",
    "Южная Корея": "🇰🇷",
    "Хорватия": "🇭🇷",
    "Греция": "🇬🇷",
    "Украина": "🇺🇦",
}

POSITIONS = {
    "ST": "⚽️ Нападающий",
    "CM": "🎯 Полузащитник",
    "DF": "🧱 Защитник",
    "GK": "🧤 Вратарь",
    "ALL": "👑 Универсал",
}

DEFAULT_CORE = {"players": {}, "nations": {}, "history": []}
DEFAULT_ADMINS = {"ids": []}
DEFAULT_REQUESTS = {"items": {}}
DEFAULT_FREEAGENTS = {"items": {}}
DEFAULT_MODERATION = {"items": {}}


# ============================================================
# УТИЛИТЫ
# ============================================================

def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None


def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.stem + "_", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def _load_json(path: Path, default: dict) -> dict:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}

    for key, value in default.items():
        data.setdefault(key, json.loads(json.dumps(value)))

    _atomic_write(path, data)
    return data


# ============================================================
# STORAGE
# ============================================================

class Store:
    def __init__(self):
        self.core = _load_json(DB_CORE_FILE, DEFAULT_CORE)
        self.admins = _load_json(DB_ADMINS_FILE, DEFAULT_ADMINS)
        self.requests = _load_json(DB_REQUESTS_FILE, DEFAULT_REQUESTS)
        self.free_agents = _load_json(DB_FREEAGENTS_FILE, DEFAULT_FREEAGENTS)
        self.moderation = _load_json(DB_MODERATION_FILE, DEFAULT_MODERATION)

        for nation in NATIONS:
            self.core["nations"].setdefault(
                nation, {"owner_id": None, "players": [], "created_at": iso(now())}
            )

    async def save_all(self):
        async with DB_LOCK:
            await asyncio.to_thread(_atomic_write, DB_CORE_FILE, self.core)
            await asyncio.to_thread(_atomic_write, DB_ADMINS_FILE, self.admins)
            await asyncio.to_thread(_atomic_write, DB_REQUESTS_FILE, self.requests)
            await asyncio.to_thread(_atomic_write, DB_FREEAGENTS_FILE, self.free_agents)
            await asyncio.to_thread(_atomic_write, DB_MODERATION_FILE, self.moderation)

    async def save_core(self):
        async with DB_LOCK:
            await asyncio.to_thread(_atomic_write, DB_CORE_FILE, self.core)

    async def save_admins(self):
        async with DB_LOCK:
            await asyncio.to_thread(_atomic_write, DB_ADMINS_FILE, self.admins)

    async def save_requests(self):
        async with DB_LOCK:
            await asyncio.to_thread(_atomic_write, DB_REQUESTS_FILE, self.requests)

    async def save_free_agents(self):
        async with DB_LOCK:
            await asyncio.to_thread(_atomic_write, DB_FREEAGENTS_FILE, self.free_agents)

    async def save_moderation(self):
        async with DB_LOCK:
            await asyncio.to_thread(_atomic_write, DB_MODERATION_FILE, self.moderation)


STORE = Store()


def get_admin_ids():
    ids = {int(x) for x in STORE.admins["ids"] if str(x).lstrip("-").isdigit()}
    for item in os.getenv("ADMIN_IDS", "").split(","):
        item = item.strip()
        if item.lstrip("-").isdigit():
            ids.add(int(item))
    return ids


def is_admin(user_id):
    return user_id in get_admin_ids()


def get_player(user_id):
    return STORE.core["players"].get(str(user_id))


def find_player(query):
    query = (query or "").strip().lstrip("@")
    q = query.lower()
    if q.isdigit() and q in STORE.core["players"]:
        return q, STORE.core["players"][q]
    for user_id, player in STORE.core["players"].items():
        values = (
            str(player.get("roblox_username", "")).lower(),
            str(player.get("roblox_display_name", "")).lower(),
            str(player.get("telegram_username", "")).lstrip("@").lower(),
        )
        if q and q in values:
            return user_id, player
    return None, None


def nation_count(nation):
    return len(STORE.core["nations"].get(nation, {}).get("players", []))


def nation_owner_id(nation):
    return STORE.core["nations"].get(nation, {}).get("owner_id")


def nation_owner_player(nation):
    owner_id = nation_owner_id(nation)
    if not owner_id:
        return None, None
    player = get_player(int(owner_id))
    if not player:
        return None, None
    if player.get("nation") != nation:
        return None, None
    return int(owner_id), player


def owner_nation(user_id):
    for name in NATIONS:
        owner_id, _ = nation_owner_player(name)
        if owner_id and str(owner_id) == str(user_id):
            return name
    return None


def can_manage_nation(user_id, nation):
    if is_admin(user_id):
        return True
    owner_id, _ = nation_owner_player(nation)
    return owner_id is not None and str(owner_id) == str(user_id)


def position_text(player):
    return POSITIONS.get(player.get("position", "ALL"), POSITIONS["ALL"])


def career_active(player):
    until = parse_dt(player.get("career_until"))
    if not until:
        return True
    return now() >= until


def ban_active(player):
    if player.get("ban_permanent"):
        return True
    until = parse_dt(player.get("ban_until"))
    return bool(until and now() < until)


def ban_text(player):
    if not ban_active(player):
        return "❌ Нет"
    if player.get("ban_permanent"):
        return "✅ Да | навсегда"
    until = parse_dt(player.get("ban_until"))
    if until:
        return "✅ Да | до " + until.astimezone().strftime("%d.%m.%Y %H:%M")
    return "✅ Да"


def add_history(action, user_id=None, nation=None, details=""):
    STORE.core["history"].append(
        {
            "time": iso(now()),
            "action": action,
            "user_id": str(user_id) if user_id is not None else None,
            "nation": nation,
            "details": details,
        }
    )


# ============================================================
# COOLDOWN
# ============================================================

def free_agent_cooldown_left(user_id):
    entry = STORE.free_agents["items"].get(str(user_id))
    if not entry:
        return 0
    expires = parse_dt(entry.get("expires_at"))
    if not expires:
        return 0
    return max(0, int((expires - now()).total_seconds()))


def format_cooldown(seconds):
    if seconds <= 0:
        return "готово"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes >= 60:
        hours = minutes // 60
        minutes = minutes % 60
        return f"{hours}ч {minutes}м"
    return f"{minutes}м {secs:02d}с"


# ============================================================
# ПРОВЕРКА: ТОЛЬКО ЛИЧКА
# ============================================================

PRIVATE_ONLY_HINT = (
    "🔒 <b>Это меню работает только в личке с ботом.</b>\n\n"
    "Откройте личный чат с ботом и используйте команды там.\n"
    "Найти бота можно по ссылке: <a href=\"https://t.me/{bot_username}\">@{bot_username}</a>"
)


def is_private_chat(message_or_callback) -> bool:
    try:
        chat_type = message_or_callback.chat.type
    except AttributeError:
        try:
            chat_type = message_or_callback.message.chat.type
        except AttributeError:
            return False
    return chat_type == "private"


async def private_only_reply(message_or_callback, bot: Bot):
    """
    Ответ на попытку использовать приватную команду/меню вне ЛС.
    Работает и для Message, и для CallbackQuery.
    """
    try:
        me = await bot.get_me()
        username = me.username or "bot"
    except Exception:
        username = "bot"

    text = (
        "🔒 <b>Доступно только в личке с ботом.</b>\n\n"
        f"Откройте <a href=\"https://t.me/{username}\">@{username}</a> "
        "и используйте команды там."
    )

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.answer(
            "Доступно только в личке с ботом.", show_alert=True
        )
        try:
            await message_or_callback.message.answer(text)
        except Exception:
            pass
    else:
        await message_or_callback.answer(text)


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_inline_keyboard(user_id):
    player = get_player(user_id)
    rows = [
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
            InlineKeyboardButton(text="💫 Сборные", callback_data="menu:nations"),
        ],
        [
            InlineKeyboardButton(
                text="🔎 Искать сборную", callback_data="menu:free_agent"
            ),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
        ],
    ]

    if player:
        until = parse_dt(player.get("career_until"))
        if until and now() >= until:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🌹 Вернуть карьеру",
                        callback_data="menu:career_return",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🥀 Завершить карьеру",
                        callback_data="menu:career_end",
                    )
                ]
            )

    nation_owned = owner_nation(user_id)
    if nation_owned:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🏟 Управление клубом",
                    callback_data="club:menu",
                )
            ]
        )

    rows.append(
        [InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")]
        ]
    )


def back_and_settings_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")],
        ]
    )


def position_inline_keyboard():
    rows = []
    for code, title in POSITIONS.items():
        rows.append(
            [InlineKeyboardButton(text=title, callback_data=f"position:{code}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nations_keyboard():
    rows = []
    buffer = []
    for name, flag in NATIONS.items():
        buffer.append(
            InlineKeyboardButton(
                text=f"{flag} {name}",
                callback_data=f"nation:{name}",
            )
        )
        if len(buffer) == 2:
            rows.append(buffer)
            buffer = []
    if buffer:
        rows.append(buffer)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nation_detail_keyboard(nation, viewer_id=None):
    rows = []
    if nation in NATIONS:
        rows.append(
            [
                InlineKeyboardButton(
                    text="👥 Состав", callback_data=f"nation_squad:{nation}"
                )
            ]
        )
    if viewer_id and can_manage_nation(viewer_id, nation):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🏟 Управление клубом", callback_data="club:menu"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:nations")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu:main")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def club_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Состав", callback_data="club:squad"),
            ],
            [
                InlineKeyboardButton(
                    text="📨 Пригласить игрока", callback_data="club:invite"
                ),
                InlineKeyboardButton(
                    text="🚫 Выгнать игрока", callback_data="club:kick"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👑 Смена владельца", callback_data="club:change_owner"
                )
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main"),
                InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:close"),
            ],
        ]
    )


def club_cancel_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="club:menu")]
        ]
    )


def yes_no_keyboard(prefix, request_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да", callback_data=f"{prefix}:yes:{request_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Нет", callback_data=f"{prefix}:no:{request_id}"
                ),
            ]
        ]
    )


def moderation_keyboard(mod_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Опубликовать",
                    callback_data=f"mod:approve:{mod_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"mod:reject:{mod_id}",
                ),
            ]
        ]
    )


def transfer_keyboard(request_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить", callback_data=f"approve:{request_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить", callback_data=f"reject:{request_id}"
                ),
            ]
        ]
    )


# ============================================================
# FSM
# ============================================================

class Registration(StatesGroup):
    username = State()


class FreeAgent(StatesGroup):
    requirements = State()


class CareerEnd(StatesGroup):
    comment = State()


class CareerReturn(StatesGroup):
    comment = State()


class ClubInvite(StatesGroup):
    target = State()


class ClubKick(StatesGroup):
    target = State()


class ClubChangeOwner(StatesGroup):
    target = State()


router = Router()


# ============================================================
# SAFE EDIT
# ============================================================

async def safe_edit(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None
):
    try:
        await callback.message.edit_text(
            text, reply_markup=keyboard, disable_web_page_preview=True
        )
    except Exception:
        try:
            await callback.message.answer(text, reply_markup=keyboard)
        except Exception:
            pass


# ============================================================
# ROBLOX
# ============================================================

async def roblox_lookup(username):
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
            users = data.get("data", [])
            if not users:
                return None
            user = users[0]

        avatar = None
        avatar_url = (
            "https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={user['id']}&size=150x150&format=Png&isCircular=false"
        )
        async with session.get(avatar_url) as response:
            if response.status == 200:
                avatar_data = await response.json()
                items = avatar_data.get("data", [])
                if items:
                    avatar = items[0].get("imageUrl")

    return {
        "id": user["id"],
        "username": user["name"],
        "display_name": user["displayName"],
        "avatar": avatar,
    }


# ============================================================
# ТЕКСТЫ
# ============================================================

def profile_text(player):
    career = "✅ Активна"
    until = parse_dt(player.get("career_until"))
    if until and now() < until:
        days = max(1, (until - now()).days + 1)
        career += f" | {days} дн."

    cooldown = free_agent_cooldown_left(player.get("telegram_id"))
    cooldown_text = "готово" if cooldown <= 0 else format_cooldown(cooldown)

    nation_owned = owner_nation(player.get("telegram_id"))
    if nation_owned:
        owner_line = f"✅ Да | <b>{escape(nation_owned)}</b>"
    else:
        owner_line = "❌ Нет"

    return (
        "╭・<b>PLAYER PROFILE</b>\n"
        f"├ 🎮 <b>Roblox:</b> "
        f"<code>{escape(player.get('roblox_username', '-'))}</code>\n"
        f"├ <b>Username:</b> "
        f"{escape(player.get('telegram_username') or 'нет')}\n"
        f"├ <b>Display Name:</b> "
        f"{escape(player.get('telegram_display_name') or '-')}\n"
        f"├ <b>ID roblox:</b> "
        f"<code>{player.get('roblox_id', '-')}</code>\n"
        f"├ ⚜️ <b>Telegram:</b> "
        f"@{escape(player.get('telegram_username') or 'нет')}\n"
        f"├ 🆔 <b>ID:</b> "
        f"<code>{player.get('telegram_id', '-')}</code>\n"
        f"├ 🌏 <b>Сборная:</b> "
        f"<code>{escape(player.get('nation') or 'Свободный агент')}</code>\n"
        f"├ 👑 <b>Владелец сборной:</b> {owner_line}\n"
        f"├ ✨ <b>Статус карьеры:</b> {career}\n"
        f"├ ⏳ <b>Кулдаун заявки:</b> {cooldown_text}\n"
        f"├ ⛔️ <b>Забанен:</b> {ban_text(player)}\n"
        f"╰ ⚽️ <b>Позиция:</b> "
        f"<code>{escape(position_text(player))}</code>"
    )


def nations_overview_text():
    lines = ["💫 <b>СПИСОК СБОРНЫХ</b>\n"]
    for name, flag in NATIONS.items():
        owner_id, owner = nation_owner_player(name)
        owner_name = owner["roblox_username"] if owner else "Нет владельца"
        lines.append(
            f"{flag} <b>{escape(name)}</b>\n"
            f"   👔 Владелец: <code>{escape(owner_name)}</code>\n"
            f"   👥 Игроков: <b>{nation_count(name)}/{MAX_PLAYERS}</b>\n"
        )
    lines.append("Нажми на сборную, чтобы посмотреть подробнее 👇")
    return "\n".join(lines)


def nation_detail_text(nation):
    nation_data = STORE.core["nations"].get(nation)
    if not nation_data:
        return None

    flag = NATIONS.get(nation, "🏳")
    owner_id, owner = nation_owner_player(nation)
    owner_name = owner["roblox_username"] if owner else "—"

    players = nation_data.get("players", [])
    created = parse_dt(nation_data.get("created_at"))

    lines = [
        f"{flag} <b>СБОРНАЯ: {escape(nation)}</b>",
        "",
        f"👔 <b>Владелец:</b> <code>{escape(owner_name)}</code>",
        f"👥 <b>Игроков:</b> <b>{len(players)}/{MAX_PLAYERS}</b>",
        f"🕐 <b>Создана:</b> "
        f"{created.astimezone().strftime('%d.%m.%Y') if created else '—'}",
        "",
        "📋 <b>Состав:</b>",
    ]

    if not players:
        lines.append("   <i>Пока никого нет.</i>")
    else:
        for uid in players[:MAX_PLAYERS]:
            p = get_player(int(uid))
            if not p:
                continue
            marker = " 👑" if owner_id and str(owner_id) == str(uid) else ""
            lines.append(
                f"   • <b>{escape(p['roblox_username'])}</b> — "
                f"{escape(position_text(p))}{marker}"
            )

    return "\n".join(lines)


PLAYER_HELP_TEXT = (
    "❓ <b>ДОСТУПНЫЕ КОМАНДЫ</b>\n\n"
    "🏆 <b>Основное</b>\n"
    "<code>/start</code> — регистрация / главное меню\n"
    "<code>/profile [ник/id/@username]</code> — профиль игрока\n"
    "<code>/nations</code> — список всех сборных\n"
    "<code>/nation [название]</code> — информация о сборной\n"
    "<code>/help</code> — эта справка\n\n"
    "🎮 <b>Игровые действия</b>\n"
    "👤 <b>Профиль</b> · 🔎 <b>Искать сборную</b> · "
    "💫 <b>Сборные</b> · ⚙️ <b>Настройки</b>\n"
    "🥀 <b>Завершить карьеру</b> · 🌹 <b>Вернуть карьеру</b>\n"
    "🏟 <b>Управление клубом</b> — для владельцев сборных\n\n"
    f"⏳ Кулдаун свободного агента: {FREE_AGENT_COOLDOWN_MINUTES} мин.\n"
    "🔒 Все команды работают только в личке с ботом."
)

ADMIN_HELP_TEXT = (
    "👨‍💼 <b>КОМАНДЫ АДМИНИСТРАТОРА</b>\n\n"
    "🔄 <b>Трансферы</b>\n"
    "<code>/transfer_n [id/ник] [сборная]</code>\n"
    "<code>/delete_pl [id/ник] [сборная]</code>\n"
    "<code>/transfer_vld [id/ник] [сборная]</code>\n"
    "<code>/set_owner [id/ник] [сборная]</code>\n"
    "<code>/remove_owner [сборная]</code>\n\n"
    "🧑 <b>Игрок</b>\n"
    "<code>/changenickname [id/ник] [новый ник]</code>\n"
    "<code>/ban [id/ник] [7d/30d/perm] [причина]</code>\n"
    "<code>/unban [id/ник]</code>\n"
    "<code>/player_end [id/ник]</code>\n"
    "<code>/player_noend [id/ник]</code>\n"
    "<code>/off_coldaun [id/ник]</code>\n\n"
    "🛡 <b>Админы</b>\n"
    "<code>/add_admins [id/ник]</code>\n"
    "<code>/remove_admins [id/ник]</code>\n\n"
    "📜 <b>История</b>\n"
    "<code>/history_player [id/ник]</code>\n"
    "<code>/history_club [сборная]</code>\n\n"
    "📣 <b>Рассылка</b>\n"
    "<code>/post [текст]</code> — в канал\n"
    "<code>/post_bot [текст]</code> — всем игрокам"
)


# ============================================================
# ШАБЛОНЫ
# ============================================================

def template_owner_assigned(nation, roblox_nick):
    return (
        "👑 | <b>НАЗНАЧЕН ВЛАДЕЛЬЦЕМ</b>\n\n"
        f"Сборная: <b>{escape(nation)}</b>\n"
        f"Игрок: <b>{escape(roblox_nick)}</b>"
    )


def template_owner_changed(nation, old_nick, new_nick):
    return (
        "✨| <b>СМЕНА ВЛАДЕЛЬЦА</b>\n\n"
        f"Сборная: <b>{escape(nation)}</b>\n\n"
        f"Был: <b>{escape(old_nick or '—')}</b>\n"
        "⮃\n"
        f"Стал: <b>{escape(new_nick)}</b>"
    )


def template_owner_removed(nation, roblox_nick):
    return (
        "❌ | <b>СНЯТ С ВЛАДЕЛЬЦА</b>\n\n"
        f"Сборная: <b>{escape(nation)}</b>\n"
        f"Игрок: <b>{escape(roblox_nick or '—')}</b>"
    )


def template_free_agent(roblox_nick, requirements, position, contact):
    return (
        "🌏 │ <b>СВОБОДНЫЙ АГЕНТ</b>\n\n"
        f"● Игрок — <b>{escape(roblox_nick)}</b>\n"
        f"● Требования — <b>{escape(requirements)}</b>\n"
        f"● Позиция — <b>{escape(position)}</b>\n"
        f"● Связь — <b>{escape(contact)}</b>"
    )


def template_transfer_done(roblox_nick, old_nation, new_nation, position):
    return (
        "✅ | <b>Трансфер оформлен</b>\n"
        f"● Игрок — <b>{escape(roblox_nick)}</b>\n"
        f"● {escape(old_nation or 'Свободный агент')} → {escape(new_nation)}\n"
        f"● Позиция — <b>{escape(position)}</b>"
    )


# ============================================================
# АДМИН-ГРУППА
# ============================================================

def _admin_chat_id_int():
    if not ADMIN_CHAT_ID:
        return None
    try:
        return int(ADMIN_CHAT_ID)
    except ValueError:
        print(f"[ADMIN_CHAT] Некорректный ADMIN_CHAT_ID: {ADMIN_CHAT_ID!r}")
        return None


async def send_to_admin_chat(bot: Bot, text: str, keyboard=None) -> bool:
    chat_id = _admin_chat_id_int()
    if chat_id is None:
        print("[ADMIN_CHAT] ADMIN_CHAT_ID не задан или некорректен.")
        return False
    try:
        await bot.send_message(
            chat_id,
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return True
    except Exception as error:
        print(f"[ADMIN_CHAT] Ошибка отправки в {chat_id}: {error}")
        return False


async def publish_to_channel(bot: Bot, text: str) -> bool:
    """Прямая публикация в канал — без модерации."""
    if not TRANSFER_CHANNEL_ID:
        print("[CHANNEL] TRANSFER_CHANNEL_ID не задан.")
        return False
    try:
        await bot.send_message(
            TRANSFER_CHANNEL_ID,
            text,
            disable_web_page_preview=True,
        )
        return True
    except Exception as error:
        print(f"[CHANNEL] Ошибка публикации в канал: {error}")
        return False


async def submit_moderation(
    bot: Bot, text: str, author_id: int, kind: str
) -> bool:
    if not ADMIN_CHAT_ID:
        return False

    mod_id = str(int(now().timestamp() * 1000))
    STORE.moderation["items"][mod_id] = {
        "id": mod_id,
        "kind": kind,
        "text": text,
        "author_id": author_id,
        "status": "pending",
        "created_at": iso(now()),
    }
    await STORE.save_moderation()

    header = "🛡 <b>МОДЕРАЦИЯ ПУБЛИКАЦИИ</b>\n\n"
    keyboard = moderation_keyboard(mod_id)

    ok = await send_to_admin_chat(bot, header + text, keyboard)
    if not ok:
        STORE.moderation["items"][mod_id]["status"] = "send_failed"
        await STORE.save_moderation()
        try:
            await bot.send_message(
                author_id,
                "⚠️ Не удалось отправить публикацию в админ-группу. "
                "Публикация в канал отменена.",
            )
        except Exception:
            pass
        return False
    return True


@router.callback_query(F.data.startswith("mod:"))
async def moderation_callback(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только администратор.", show_alert=True)
        return

    _, action, mod_id = callback.data.split(":")
    item = STORE.moderation["items"].get(mod_id)
    if not item:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    if item.get("status") != "pending":
        who = item.get("moderated_by")
        who_name = f"ID {who}" if who else "другим админом"
        if who:
            try:
                user = await bot.get_chat(who)
                who_name = (
                    f"@{user.username}"
                    if getattr(user, "username", None)
                    else user.full_name or f"ID {who}"
                )
            except Exception:
                pass
        await callback.answer(f"Уже обработано: {who_name}", show_alert=True)
        return

    moderator = callback.from_user
    mod_name = (
        f"@{moderator.username}"
        if moderator.username
        else moderator.full_name or f"ID {moderator.id}"
    )

    if action == "approve":
        if TRANSFER_CHANNEL_ID:
            try:
                await bot.send_message(TRANSFER_CHANNEL_ID, item["text"])
            except Exception as error:
                await callback.answer(
                    f"Ошибка публикации в канал: {error}", show_alert=True
                )
                return
        item["status"] = "published"
        item["moderated_by"] = moderator.id
        item["moderated_at"] = iso(now())
        await STORE.save_moderation()

        new_text = (
            f"✅ <b>Опубликовано</b> — {escape(mod_name)}\n\n" + item["text"]
        )
        try:
            await callback.message.edit_text(
                new_text, reply_markup=None, disable_web_page_preview=True
            )
        except Exception:
            pass
        await callback.answer("Опубликовано.")
    else:
        item["status"] = "rejected"
        item["moderated_by"] = moderator.id
        item["moderated_at"] = iso(now())
        await STORE.save_moderation()

        new_text = (
            f"❌ <b>Отклонено</b> — {escape(mod_name)}\n\n" + item["text"]
        )
        try:
            await callback.message.edit_text(
                new_text, reply_markup=None, disable_web_page_preview=True
            )
        except Exception:
            pass

        try:
            await bot.send_message(
                item["author_id"],
                "❌ Ваша публикация была отклонена администрацией.",
            )
        except Exception:
            pass
        await callback.answer("Отклонено.")


# ============================================================
# START / REGISTRATION (только ЛС)
# ============================================================

@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return

    await state.clear()
    if not get_player(message.from_user.id):
        await message.answer(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Для регистрации отправь свой Roblox Username:"
        )
        await state.set_state(Registration.username)
        return

    await message.answer(
        "🏆 <b>Главное меню</b>\n\nВыбери действие ниже 👇",
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


@router.message(Registration.username)
async def registration_username(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    username = (message.text or "").strip()
    if not 3 <= len(username) <= 20:
        await message.answer("❌ Некорректный Roblox Username.")
        return

    await message.answer("🔎 Проверяю Roblox-аккаунт...")
    try:
        roblox = await roblox_lookup(username)
    except Exception:
        await message.answer("❌ Roblox API временно недоступен.")
        return

    if not roblox:
        await message.answer("❌ Roblox-аккаунт не найден. Отправь Username ещё раз:")
        return

    await state.update_data(roblox=roblox)
    caption = (
        "🖼 <b>Аватар Roblox</b>\n\n"
        f"🎮 <b>Username:</b> {escape(roblox['username'])}\n"
        f"🏷 <b>Display Name:</b> {escape(roblox['display_name'])}\n\n"
        "Это ваш аккаунт?"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="register:yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="register:no"),
            ]
        ]
    )

    if roblox.get("avatar"):
        try:
            await message.answer_photo(
                roblox["avatar"], caption=caption, reply_markup=keyboard
            )
        except Exception:
            await message.answer(caption, reply_markup=keyboard)
    else:
        await message.answer(caption, reply_markup=keyboard)

    await state.set_state(Registration.confirm)


@router.callback_query(F.data.startswith("register:"))
async def registration_confirm(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return

    choice = callback.data.split(":")[1]
    if choice == "no":
        await state.clear()
        await callback.message.answer("Хорошо. Отправь Roblox Username ещё раз:")
        await state.set_state(Registration.username)
        await callback.answer()
        return

    data = await state.get_data()
    roblox = data.get("roblox")
    if not roblox:
        await state.clear()
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    user_id = str(callback.from_user.id)
    STORE.core["players"][user_id] = {
        "telegram_id": callback.from_user.id,
        "telegram_username": callback.from_user.username or "",
        "telegram_display_name": callback.from_user.full_name or "",
        "roblox_id": roblox["id"],
        "roblox_username": roblox["username"],
        "roblox_display_name": roblox["display_name"],
        "roblox_avatar": roblox.get("avatar"),
        "nation": None,
        "position": "ALL",
        "career_until": None,
        "ban_until": None,
        "ban_permanent": False,
        "created_at": iso(now()),
    }
    add_history("registration", callback.from_user.id,
                details="Roblox: " + roblox["username"])
    await STORE.save_all()
    await state.clear()

    await callback.message.answer(
        "✅ <b>Регистрация завершена!</b>\n\nОткрываю главное меню 👇",
        reply_markup=main_inline_keyboard(callback.from_user.id),
    )
    await callback.answer()


# ============================================================
# МЕНЮ (только ЛС)
# ============================================================

@router.callback_query(F.data == "menu:close")
async def menu_close(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Закрыто.")


@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    await state.clear()
    await safe_edit(
        callback,
        "🏆 <b>Главное меню</b>\n\nВыбери действие ниже 👇",
        main_inline_keyboard(callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def menu_profile(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    player = get_player(callback.from_user.id)
    if not player:
        await callback.answer("Сначала зарегистрируйся.", show_alert=True)
        return
    await safe_edit(callback, profile_text(player), back_and_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:nations")
async def menu_nations(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    await safe_edit(callback, nations_overview_text(), nations_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("nation:"))
async def show_nation(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    nation = callback.data.split(":", 1)[1]
    text = nation_detail_text(nation)
    if not text:
        await callback.answer("Сборная не найдена.", show_alert=True)
        return
    await safe_edit(
        callback, text, nation_detail_keyboard(nation, callback.from_user.id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("nation_squad:"))
async def show_nation_squad(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    nation = callback.data.split(":", 1)[1]
    text = nation_detail_text(nation)
    if not text:
        await callback.answer("Сборная не найдена.", show_alert=True)
        return
    await safe_edit(
        callback, text, nation_detail_keyboard(nation, callback.from_user.id)
    )
    await callback.answer()


@router.callback_query(F.data == "menu:settings")
async def menu_settings(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    player = get_player(callback.from_user.id)
    if not player:
        await callback.answer("Сначала зарегистрируйся.", show_alert=True)
        return
    await safe_edit(
        callback,
        "⚙️ <b>Настройки позиции</b>\n\nВыбери свою позицию:",
        position_inline_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def menu_help(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    await safe_edit(callback, PLAYER_HELP_TEXT, back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:free_agent")
async def menu_free_agent(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    player = get_player(callback.from_user.id)
    if not player:
        await callback.answer("Сначала зарегистрируйся.", show_alert=True)
        return
    if ban_active(player):
        await callback.answer("⛔️ У тебя активный бан.", show_alert=True)
        return
    if not career_active(player):
        await callback.answer("🥀 Твоя карьера завершена.", show_alert=True)
        return

    left = free_agent_cooldown_left(callback.from_user.id)
    if left > 0:
        await safe_edit(
            callback,
            "⏳ <b>Кулдаун активен</b>\n"
            f"Повторная заявка доступна через "
            f"<b>{format_cooldown(left)}</b>.",
            back_keyboard(),
        )
        await callback.answer()
        return

    await safe_edit(
        callback,
        "🌏 │ <b>СВОБОДНЫЙ АГЕНТ</b>\n\n"
        "Напиши требования к будущей сборной (отправь сообщением):",
        back_keyboard(),
    )
    await state.set_state(FreeAgent.requirements)
    await callback.answer()


@router.callback_query(F.data == "menu:career_end")
async def menu_career_end(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    player = get_player(callback.from_user.id)
    if not player:
        await callback.answer("Сначала зарегистрируйся.", show_alert=True)
        return
    if ban_active(player):
        await callback.answer("⛔️ У тебя активный бан.", show_alert=True)
        return

    until = parse_dt(player.get("career_until"))
    if until and now() < until:
        await callback.answer(
            "🥀 Карьера уже завершена до "
            + until.astimezone().strftime("%d.%m.%Y %H:%M"),
            show_alert=True,
        )
        return

    await safe_edit(
        callback,
        "🥀 <b>Завершение карьеры</b>\n\n"
        "Напиши комментарий сообщением — он попадёт в публикацию:",
        back_keyboard(),
    )
    await state.set_state(CareerEnd.comment)
    await callback.answer()


@router.callback_query(F.data == "menu:career_return")
async def menu_career_return(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    player = get_player(callback.from_user.id)
    if not player:
        await callback.answer("Сначала зарегистрируйся.", show_alert=True)
        return

    until = parse_dt(player.get("career_until"))
    if until and now() < until:
        await callback.answer(
            "⏳ Вернуть карьеру можно после "
            + until.astimezone().strftime("%d.%m.%Y %H:%M"),
            show_alert=True,
        )
        return

    await safe_edit(
        callback,
        "🌹 <b>Возврат карьеры</b>\n\n"
        "Напиши комментарий сообщением:",
        back_keyboard(),
    )
    await state.set_state(CareerReturn.comment)
    await callback.answer()


@router.callback_query(F.data.startswith("position:"))
async def set_position(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    code = callback.data.split(":")[1]
    if code not in POSITIONS:
        await callback.answer("Неизвестная позиция.", show_alert=True)
        return
    player = get_player(callback.from_user.id)
    if not player:
        await callback.answer("Сначала зарегистрируйся.", show_alert=True)
        return
    player["position"] = code
    add_history("position_change", callback.from_user.id,
                player.get("nation"), code)
    await STORE.save_core()
    await safe_edit(
        callback,
        f"✅ Позиция изменена на <b>{escape(POSITIONS[code])}</b>",
        back_and_settings_keyboard(),
    )
    await callback.answer()


# ============================================================
# HELP / NATIONS (только ЛС)
# ============================================================

@router.message(Command("help"))
async def help_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    await message.answer(
        PLAYER_HELP_TEXT,
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


@router.message(Command("help_admins"))
async def help_admins_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return
    await message.answer(ADMIN_HELP_TEXT)


@router.message(Command("nations"))
async def nations_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    await message.answer(nations_overview_text(), reply_markup=nations_keyboard())


@router.message(Command("nation"))
async def nation_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Использование: <code>/nation [название]</code>\n"
            "Например: <code>/nation Россия</code>"
        )
        return

    nation = args[1].strip()
    if nation not in NATIONS:
        found = None
        for name in NATIONS:
            if name.lower() == nation.lower():
                found = name
                break
        if not found:
            await message.answer("❌ Сборная не найдена.")
            return
        nation = found

    text = nation_detail_text(nation)
    if not text:
        await message.answer("❌ Сборная не найдена.")
        return

    await message.answer(
        text, reply_markup=nation_detail_keyboard(nation, message.from_user.id)
    )


# ============================================================
# ПАНЕЛЬ УПРАВЛЕНИЯ КЛУБОМ (только ЛС)
# ============================================================

@router.callback_query(F.data == "club:menu")
async def club_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    await state.clear()
    nation = owner_nation(callback.from_user.id)
    if not nation and not is_admin(callback.from_user.id):
        await callback.answer("Только для владельцев сборной.", show_alert=True)
        return

    if not nation:
        await safe_edit(
            callback,
            "🛡 <b>Вы администратор, но не владелец сборной.</b>\n\n"
            "Используйте команды в ЛС: /transfer_n, /delete_pl, /set_owner, /transfer_vld.",
            back_keyboard(),
        )
        await callback.answer()
        return

    text = (
        f"🏟 <b>УПРАВЛЕНИЕ КЛУБОМ</b>\n\n"
        f"Сборная: <b>{escape(nation)}</b>\n"
        f"Игроков: <b>{nation_count(nation)}/{MAX_PLAYERS}</b>\n\n"
        "Выбери действие ниже 👇"
    )
    await safe_edit(callback, text, club_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "club:squad")
async def club_squad(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    nation = owner_nation(callback.from_user.id)
    if not nation:
        await callback.answer("Только для владельцев сборной.", show_alert=True)
        return
    text = nation_detail_text(nation)
    await safe_edit(
        callback, text, nation_detail_keyboard(nation, callback.from_user.id)
    )
    await callback.answer()


@router.callback_query(F.data == "club:invite")
async def club_invite(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    nation = owner_nation(callback.from_user.id)
    if not nation:
        await callback.answer("Только для владельцев сборной.", show_alert=True)
        return
    await safe_edit(
        callback,
        f"📨 <b>Приглашение игрока в {escape(nation)}</b>\n\n"
        "Отправь <b>Roblox-ник</b>, <b>@username</b> или <b>ID</b> игрока "
        "одним сообщением:",
        club_cancel_keyboard(),
    )
    await state.update_data(club_nation=nation)
    await state.set_state(ClubInvite.target)
    await callback.answer()


@router.message(ClubInvite.target)
async def club_invite_target(message: Message, state: FSMContext, bot: Bot):
    if not is_private_chat(message):
        return
    data = await state.get_data()
    nation = data.get("club_nation")
    if not nation or not can_manage_nation(message.from_user.id, nation):
        await state.clear()
        await message.answer("❌ Нет прав.")
        return

    target_id, _ = find_player((message.text or "").strip())
    if not target_id:
        await message.answer(
            "❌ Игрок не найден. Попробуй ещё раз или нажми ⬅️ Отмена."
        )
        return

    ok, result = await create_invitation(
        bot, int(target_id), nation, message.from_user.id
    )
    await state.clear()
    await message.answer(
        ("✅ " if ok else "❌ ") + result,
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


@router.callback_query(F.data == "club:kick")
async def club_kick(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    nation = owner_nation(callback.from_user.id)
    if not nation:
        await callback.answer("Только для владельцев сборной.", show_alert=True)
        return
    await safe_edit(
        callback,
        f"🚫 <b>Исключение игрока из {escape(nation)}</b>\n\n"
        "Отправь <b>Roblox-ник</b>, <b>@username</b> или <b>ID</b> игрока:",
        club_cancel_keyboard(),
    )
    await state.update_data(club_nation=nation)
    await state.set_state(ClubKick.target)
    await callback.answer()


@router.message(ClubKick.target)
async def club_kick_target(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    data = await state.get_data()
    nation = data.get("club_nation")
    if not nation or not can_manage_nation(message.from_user.id, nation):
        await state.clear()
        await message.answer("❌ Нет прав.")
        return

    target_id, player = find_player((message.text or "").strip())
    if not target_id or not player:
        await message.answer("❌ Игрок не найден. Попробуй ещё раз.")
        return

    if player.get("nation") != nation:
        await message.answer("❌ Игрок не в этой сборной.")
        return

    was_owner = str(nation_owner_id(nation)) == str(target_id)

    STORE.core["nations"][nation]["players"] = [
        x for x in STORE.core["nations"][nation]["players"]
        if str(x) != str(target_id)
    ]
    player["nation"] = None

    if was_owner:
        STORE.core["nations"][nation]["owner_id"] = None

    add_history("player_kicked", target_id, nation,
                f"Исключён{f' (был владельцем)' if was_owner else ''}")
    await STORE.save_core()
    await state.clear()

    await message.answer(
        f"✅ Игрок <b>{escape(player['roblox_username'])}</b> исключён из "
        f"<b>{escape(nation)}</b>.",
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


@router.callback_query(F.data == "club:change_owner")
async def club_change_owner(
    callback: CallbackQuery, state: FSMContext, bot: Bot
):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    nation = owner_nation(callback.from_user.id)
    if not nation:
        await callback.answer("Только для владельцев сборной.", show_alert=True)
        return
    await safe_edit(
        callback,
        f"👑 <b>Смена владельца сборной {escape(nation)}</b>\n\n"
        "Отправь <b>Roblox-ник</b>, <b>@username</b> или <b>ID</b> нового владельца "
        "(он должен быть в этой сборной):",
        club_cancel_keyboard(),
    )
    await state.update_data(club_nation=nation)
    await state.set_state(ClubChangeOwner.target)
    await callback.answer()


@router.message(ClubChangeOwner.target)
async def club_change_owner_target(
    message: Message, state: FSMContext, bot: Bot
):
    if not is_private_chat(message):
        return
    data = await state.get_data()
    nation = data.get("club_nation")
    if not nation or not can_manage_nation(message.from_user.id, nation):
        await state.clear()
        await message.answer("❌ Нет прав.")
        return

    target_id, player = find_player((message.text or "").strip())
    if not target_id or not player:
        await message.answer("❌ Игрок не найден. Попробуй ещё раз.")
        return

    if player.get("nation") != nation:
        await message.answer("❌ Новый владелец должен быть в этой сборной.")
        return

    old_owner_id, old_owner = nation_owner_player(nation)
    old_nick = old_owner["roblox_username"] if old_owner else "—"

    STORE.core["nations"][nation]["owner_id"] = int(target_id)
    add_history("owner_change", target_id, nation,
                f"{old_owner_id} -> {target_id}")
    await STORE.save_core()

    text = template_owner_changed(nation, old_nick, player["roblox_username"])
    ok = await submit_moderation(bot, text, message.from_user.id, "owner_change")
    note = "\n\n⏳ Отправлено на модерацию." if ok else \
           "\n\n⚠️ Не удалось отправить в админ-группу."
    await state.clear()
    await message.answer(
        text + note,
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


# ============================================================
# FREE AGENT FSM (модерация)
# ============================================================

@router.message(FreeAgent.requirements)
async def free_agent_requirements(
    message: Message, state: FSMContext, bot: Bot
):
    if not is_private_chat(message):
        return
    requirements = (message.text or "").strip()
    if not requirements:
        await message.answer("❌ Требования не могут быть пустыми.")
        return

    player = get_player(message.from_user.id)
    if not player:
        await message.answer("Сначала зарегистрируйся.")
        await state.clear()
        return

    if not ADMIN_CHAT_ID:
        await message.answer(
            "⚠️ Модерация временно недоступна (ADMIN_CHAT_ID не задан). "
            "Попробуйте позже."
        )
        await state.clear()
        return

    expires = now() + timedelta(minutes=FREE_AGENT_COOLDOWN_MINUTES)
    contact = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else f"ID {message.from_user.id}"
    )

    STORE.free_agents["items"][str(message.from_user.id)] = {
        "user_id": message.from_user.id,
        "roblox_username": player["roblox_username"],
        "requirements": requirements,
        "position": player.get("position", "ALL"),
        "contact": contact,
        "created_at": iso(now()),
        "expires_at": iso(expires),
    }
    add_history("free_agent", message.from_user.id,
                details="Создана заявка свободного агента")
    await STORE.save_all()
    await state.clear()

    post_text = template_free_agent(
        player["roblox_username"],
        requirements,
        position_text(player),
        contact,
    )

    ok = await submit_moderation(
        bot, post_text, message.from_user.id, "free_agent"
    )
    note = (
        "\n\n⏳ Заявка отправлена в админ-группу на модерацию."
        if ok
        else "\n\n⚠️ Не удалось отправить заявку в админ-группу."
    )

    await message.answer(
        f"🌏 │ <b>СВОБОДНЫЙ АГЕНТ</b>\n\n"
        f"● Игрок — <b>{escape(player['roblox_username'])}</b>\n"
        f"● Требования — <b>{escape(requirements)}</b>\n"
        f"● Позиция — <b>{escape(position_text(player))}</b>\n"
        f"● Связь — <b>{escape(contact)}</b>\n\n"
        f"⏳ Следующая заявка возможна через "
        f"<b>{FREE_AGENT_COOLDOWN_MINUTES} мин.</b>"
        + note,
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


# ============================================================
# CAREER FSM (только ЛС)
# ============================================================

@router.message(CareerEnd.comment)
async def career_end_comment(message: Message, state: FSMContext, bot: Bot):
    if not is_private_chat(message):
        return
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("❌ Комментарий не может быть пустым.")
        return
    if len(comment) > 300:
        await message.answer("❌ Слишком длинный комментарий (макс. 300).")
        return

    player = get_player(message.from_user.id)
    if not player:
        await message.answer("Сначала зарегистрируйся.")
        await state.clear()
        return

    player["career_until"] = iso(now() + timedelta(days=CAREER_DAYS))
    add_history("career_end", message.from_user.id, player.get("nation"),
                f"30 дней | {comment}")
    await STORE.save_core()
    await state.clear()

    post_text = (
        "🥀 | <b>ЗАВЕРШИЛ КАРЬЕРУ</b>\n\n"
        f"● Игрок — <b>{escape(player['roblox_username'])}</b>\n"
        f"Завершил карьеру\n"
        f"● Комментарий: {escape(comment)}"
    )

    ok = await submit_moderation(bot, post_text, message.from_user.id, "career_end")
    note = "\n\n⏳ Отправлено на модерацию." if ok else \
           "\n\n⚠️ Не удалось отправить в админ-группу."

    await message.answer(
        post_text + note,
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


@router.message(CareerReturn.comment)
async def career_return_comment(message: Message, state: FSMContext, bot: Bot):
    if not is_private_chat(message):
        return
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("❌ Комментарий не может быть пустым.")
        return
    if len(comment) > 300:
        await message.answer("❌ Слишком длинный комментарий (макс. 300).")
        return

    player = get_player(message.from_user.id)
    if not player:
        await message.answer("Сначала зарегистрируйся.")
        await state.clear()
        return

    player["career_until"] = None
    add_history("career_return", message.from_user.id, player.get("nation"),
                comment)
    await STORE.save_core()
    await state.clear()

    post_text = (
        "⭐️ | <b>ВЕРНУЛ КАРЬЕРУ</b>\n\n"
        f"● Игрок — <b>{escape(player['roblox_username'])}</b>\n"
        f"Вернул карьеру\n"
        f"● Комментарий: {escape(comment)}"
    )

    ok = await submit_moderation(bot, post_text, message.from_user.id, "career_return")
    note = "\n\n⏳ Отправлено на модерацию." if ok else \
           "\n\n⚠️ Не удалось отправить в админ-группу."

    await message.answer(
        post_text + note,
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


# ============================================================
# TRANSFERS
# ============================================================

async def notify_user(bot, user_id, text, **kwargs):
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except Exception:
        return False


async def create_invitation(bot, target_id, nation, inviter_id):
    player = get_player(target_id)
    if not player:
        return False, "Игрок не найден."
    if nation not in NATIONS:
        return False, "Сборная не найдена."
    if nation_count(nation) >= MAX_PLAYERS:
        return False, "Сборная заполнена."
    if player.get("nation") == nation:
        return False, "Игрок уже в этой сборной."
    if ban_active(player):
        return False, "Игрок заблокирован."

    request_id = str(int(now().timestamp() * 1000))
    STORE.requests["items"][request_id] = {
        "id": request_id,
        "type": "invitation",
        "target_id": target_id,
        "from_nation": player.get("nation"),
        "to_nation": nation,
        "inviter_id": inviter_id,
        "status": "pending_player",
        "created_at": iso(now()),
    }
    await STORE.save_requests()

    keyboard = yes_no_keyboard("invite", request_id)
    ok = await notify_user(
        bot,
        target_id,
        "👔 Вас пригласили в сборную "
        f"<b>{escape(nation)}</b>.\n\n"
        "Вы хотите перейти в эту сборную?",
        reply_markup=keyboard,
    )
    if not ok:
        STORE.requests["items"][request_id]["status"] = "unreachable"
        await STORE.save_requests()
        return False, "Игрок должен сначала запустить бота."
    return True, "Приглашение отправлено игроку."


@router.callback_query(F.data.startswith("invite:"))
async def invitation_answer(callback: CallbackQuery, bot: Bot):
    if not is_private_chat(callback):
        await private_only_reply(callback, bot)
        return
    _, choice, request_id = callback.data.split(":")
    request = STORE.requests["items"].get(request_id)

    if not request:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if request.get("status") != "pending_player":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    if str(request["target_id"]) != str(callback.from_user.id):
        await callback.answer("Это не твоя заявка.", show_alert=True)
        return

    if choice == "no":
        request["status"] = "rejected_by_player"
        await STORE.save_requests()
        await notify_user(bot, int(request["inviter_id"]),
                          "Игрок отменил ваше приглашение ❌")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("❌ Ты отказался от приглашения.")
        await callback.answer()
        return

    request["status"] = "pending_admin"
    request["accepted_at"] = iso(now())
    await STORE.save_requests()

    player = get_player(callback.from_user.id)
    text = (
        "🔄 <b>НОВАЯ ЗАЯВКА НА ТРАНСФЕР</b>\n\n"
        f"👤 Игрок: <b>{escape(player['roblox_username'])}</b>\n"
        f"🆔 Telegram ID: <code>{player['telegram_id']}</code>\n"
        f"📤 Откуда: <b>{escape(request.get('from_nation') or 'Свободный агент')}</b>\n"
        f"📥 Куда: <b>{escape(request['to_nation'])}</b>\n"
        f"⚽️ Позиция: <b>{escape(position_text(player))}</b>"
    )
    keyboard = transfer_keyboard(request_id)

    sent = await send_to_admin_chat(bot, text, keyboard)
    if not sent:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            "⚠️ Не удалось отправить заявку в группу админов."
        )
        await callback.answer()
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "✅ Ты принял приглашение.\nЗаявка отправлена в группу администраторов."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("approve:"))
async def approve_transfer(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только администратор.", show_alert=True)
        return

    request_id = callback.data.split(":")[1]
    request = STORE.requests["items"].get(request_id)

    if not request:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if request.get("status") != "pending_admin":
        who = request.get("approved_by") or request.get("rejected_by")
        who_name = f"ID {who}" if who else "другим админом"
        if who:
            try:
                user = await bot.get_chat(who)
                who_name = (
                    f"@{user.username}"
                    if getattr(user, "username", None)
                    else user.full_name or f"ID {who}"
                )
            except Exception:
                pass
        await callback.answer(f"Уже обработано: {who_name}", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    target_id = int(request["target_id"])
    player = get_player(target_id)
    nation = request["to_nation"]

    if not player:
        await callback.answer("Игрок не найден.", show_alert=True)
        return
    if nation_count(nation) >= MAX_PLAYERS:
        await callback.answer("Сборная заполнена.", show_alert=True)
        return

    old_nation = player.get("nation")
    if old_nation in STORE.core["nations"]:
        STORE.core["nations"][old_nation]["players"] = [
            x for x in STORE.core["nations"][old_nation]["players"]
            if str(x) != str(target_id)
        ]
        if str(nation_owner_id(old_nation)) == str(target_id):
            STORE.core["nations"][old_nation]["owner_id"] = None

    if target_id not in STORE.core["nations"][nation]["players"]:
        STORE.core["nations"][nation]["players"].append(target_id)

    player["nation"] = nation

    moderator = callback.from_user
    moderator_name = (
        f"@{moderator.username}"
        if moderator.username
        else moderator.full_name or f"ID {moderator.id}"
    )

    request["status"] = "approved"
    request["approved_by"] = moderator.id
    request["approved_at"] = iso(now())

    add_history("transfer_approved", target_id, nation,
                f"{old_nation or 'Свободный агент'} -> {nation}")
    await STORE.save_all()

    transfer_text = template_transfer_done(
        player["roblox_username"],
        old_nation,
        nation,
        position_text(player),
    )

    await notify_user(bot, target_id, transfer_text)
    if request.get("inviter_id"):
        await notify_user(bot, int(request["inviter_id"]), transfer_text)

    await submit_moderation(bot, transfer_text, moderator.id, "transfer")

    try:
        await callback.message.edit_text(
            f"✅ <b>Одобрено</b> — {escape(moderator_name)}\n\n" + (
                callback.message.text or ""
            ),
            reply_markup=None,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await callback.answer("Трансфер одобрен.")


@router.callback_query(F.data.startswith("reject:"))
async def reject_transfer(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только администратор.", show_alert=True)
        return

    request_id = callback.data.split(":")[1]
    request = STORE.requests["items"].get(request_id)

    if not request:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if request.get("status") != "pending_admin":
        who = request.get("approved_by") or request.get("rejected_by")
        who_name = f"ID {who}" if who else "другим админом"
        if who:
            try:
                user = await bot.get_chat(who)
                who_name = (
                    f"@{user.username}"
                    if getattr(user, "username", None)
                    else user.full_name or f"ID {who}"
                )
            except Exception:
                pass
        await callback.answer(f"Уже обработано: {who_name}", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    moderator = callback.from_user
    moderator_name = (
        f"@{moderator.username}"
        if moderator.username
        else moderator.full_name or f"ID {moderator.id}"
    )

    request["status"] = "rejected_by_admin"
    request["rejected_by"] = moderator.id
    request["rejected_at"] = iso(now())
    await STORE.save_requests()

    await notify_user(
        bot,
        int(request["target_id"]),
        "❌ Трансфер в сборную "
        f"<b>{escape(request['to_nation'])}</b> "
        "отклонён администрацией.",
    )

    try:
        await callback.message.edit_text(
            f"❌ <b>Отклонено</b> — {escape(moderator_name)}\n\n" + (
                callback.message.text or ""
            ),
            reply_markup=None,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await callback.answer("Трансфер отклонён.")


# ============================================================
# ADMIN COMMANDS
# ============================================================

@router.message(Command("admin"))
async def admin_help(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return
    await message.answer(ADMIN_HELP_TEXT)


async def resolve_target(arg):
    if not arg:
        return None, None
    user_id, player = find_player(arg)
    if not user_id:
        return None, None
    return int(user_id), player


@router.message(Command("profile"))
async def profile_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    parts = message.text.split(maxsplit=1)
    target_id = message.from_user.id
    if len(parts) == 2:
        user_id, _ = find_player(parts[1])
        if not user_id:
            await message.answer("❌ Игрок не найден.")
            return
        target_id = int(user_id)
    player = get_player(target_id)
    if not player:
        await message.answer("❌ Игрок не найден.")
        return
    await message.answer(
        profile_text(player),
        reply_markup=main_inline_keyboard(message.from_user.id),
    )


@router.message(Command("transfer_n"))
async def transfer_n_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.answer("Использование: /transfer_n [id/ник] [сборная]")
        return

    target_id, _ = await resolve_target(args[0])
    nation = " ".join(args[1:]).strip()

    if not target_id:
        await message.answer("❌ Игрок не найден.")
        return
    if nation not in NATIONS:
        await message.answer("❌ Сборная не найдена.")
        return
    if not can_manage_nation(message.from_user.id, nation):
        await message.answer("❌ Нет прав управлять этой сборной.")
        return

    ok, result = await create_invitation(bot, target_id, nation,
                                         message.from_user.id)
    await message.answer(("✅ " if ok else "❌ ") + result)


@router.message(Command("delete_pl"))
async def delete_player_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.answer("Использование: /delete_pl [id/ник] [сборная]")
        return

    target_id, player = await resolve_target(args[0])
    nation = " ".join(args[1:]).strip()

    if not target_id or not player:
        await message.answer("❌ Игрок не найден.")
        return
    if nation not in NATIONS:
        await message.answer("❌ Сборная не найдена.")
        return
    if not can_manage_nation(message.from_user.id, nation):
        await message.answer("❌ Нет прав.")
        return
    if player.get("nation") != nation:
        await message.answer("❌ Игрок не находится в этой сборной.")
        return

    was_owner = str(nation_owner_id(nation)) == str(target_id)

    STORE.core["nations"][nation]["players"] = [
        x for x in STORE.core["nations"][nation]["players"]
        if str(x) != str(target_id)
    ]
    player["nation"] = None
    if was_owner:
        STORE.core["nations"][nation]["owner_id"] = None

    add_history("player_deleted", target_id, nation,
                f"Игрок исключён{f' (был владельцем)' if was_owner else ''}")
    await STORE.save_core()
    await message.answer("✅ Игрок исключён и стал свободным агентом.")


# -------------------- АДМИН: назначение/снятие владельца БЕЗ модерации

@router.message(Command("set_owner"))
async def set_owner_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split()[1:]
    if len(args) < 2:
        await message.answer("Использование: /set_owner [id/ник] [сборная]")
        return

    target_id, player = await resolve_target(args[0])
    nation = " ".join(args[1:]).strip()

    if not target_id or not player:
        await message.answer("❌ Игрок не найден.")
        return
    if nation not in NATIONS:
        await message.answer("❌ Сборная не найдена.")
        return

    if player.get("nation") != nation:
        if nation_count(nation) >= MAX_PLAYERS:
            await message.answer("❌ Сборная заполнена.")
            return
        old_nation = player.get("nation")
        if old_nation in STORE.core["nations"]:
            STORE.core["nations"][old_nation]["players"] = [
                x for x in STORE.core["nations"][old_nation]["players"]
                if str(x) != str(target_id)
            ]
            if str(nation_owner_id(old_nation)) == str(target_id):
                STORE.core["nations"][old_nation]["owner_id"] = None
        if target_id not in STORE.core["nations"][nation]["players"]:
            STORE.core["nations"][nation]["players"].append(target_id)
        player["nation"] = nation

    STORE.core["nations"][nation]["owner_id"] = target_id
    add_history("owner_set", target_id, nation)
    await STORE.save_core()

    text = template_owner_assigned(nation, player["roblox_username"])

    # Админ назначает владельца — публикуем сразу в канал
    published = await publish_to_channel(bot, text)

    if published:
        await message.answer(
            text + "\n\n✅ Опубликовано в канале.",
            reply_markup=main_inline_keyboard(message.from_user.id),
        )
    else:
        await message.answer(
            text + "\n\n⚠️ Владелец назначен, но не удалось опубликовать в канале. "
            "Проверьте TRANSFER_CHANNEL_ID.",
            reply_markup=main_inline_keyboard(message.from_user.id),
        )


@router.message(Command("remove_owner"))
async def remove_owner_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /remove_owner [сборная]")
        return

    nation = args[1].strip()
    if nation not in NATIONS:
        found = None
        for name in NATIONS:
            if name.lower() == nation.lower():
                found = name
                break
        if not found:
            await message.answer("❌ Сборная не найдена.")
            return
        nation = found

    old_owner_id, old_owner = nation_owner_player(nation)
    if not old_owner_id:
        await message.answer("❌ У сборной нет владельца.")
        return

    old_nick = old_owner["roblox_username"] if old_owner else "—"

    STORE.core["nations"][nation]["owner_id"] = None
    add_history("owner_removed", old_owner_id, nation,
                f"Снят с владельца: {old_nick}")
    await STORE.save_core()

    text = template_owner_removed(nation, old_nick)

    # Админ снимает владельца — публикуем сразу в канал
    published = await publish_to_channel(bot, text)

    if published:
        await message.answer(
            text + "\n\n✅ Опубликовано в канале.",
            reply_markup=main_inline_keyboard(message.from_user.id),
        )
    else:
        await message.answer(
            text + "\n\n⚠️ Владелец снят, но не удалось опубликовать в канале. "
            "Проверьте TRANSFER_CHANNEL_ID.",
            reply_markup=main_inline_keyboard(message.from_user.id),
        )


# -------------------- Смена владельца владельцем — через модерацию

@router.message(Command("transfer_vld"))
async def transfer_owner_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.answer("Использование: /transfer_vld [id/ник] [сборная]")
        return

    target_id, player = await resolve_target(args[0])
    nation = " ".join(args[1:]).strip()

    if not target_id or not player:
        await message.answer("❌ Игрок не найден.")
        return
    if nation not in NATIONS:
        await message.answer("❌ Сборная не найдена.")
        return
    if not can_manage_nation(message.from_user.id, nation):
        await message.answer("❌ Нет прав.")
        return
    if player.get("nation") != nation:
        await message.answer("❌ Новый владелец должен быть в этой сборной.")
        return

    old_owner_id, old_owner = nation_owner_player(nation)
    old_nick = old_owner["roblox_username"] if old_owner else "—"

    STORE.core["nations"][nation]["owner_id"] = target_id
    add_history("owner_change", target_id, nation,
                f"{old_owner_id} -> {target_id}")
    await STORE.save_core()

    text = template_owner_changed(nation, old_nick, player["roblox_username"])
    ok = await submit_moderation(bot, text, message.from_user.id, "owner_change")
    note = "\n\n⏳ Отправлено на модерацию." if ok else \
           "\n\n⚠️ Не удалось отправить в админ-группу."
    await message.answer(text + note)


# -------------------- Прочие команды

@router.message(Command("changenickname"))
async def change_nickname_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split(maxsplit=2)[1:]
    if len(args) < 2:
        await message.answer(
            "Использование: /changenickname [id/ник] [новый ник]"
        )
        return

    target_id, player = await resolve_target(args[0])
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    old_name = player["roblox_username"]
    new_name = args[1].strip()
    if not 3 <= len(new_name) <= 20:
        await message.answer("❌ Некорректная длина ника.")
        return

    player["roblox_username"] = new_name
    add_history("nickname_change", target_id, player.get("nation"),
                f"{old_name} -> {new_name}")
    await STORE.save_core()
    await message.answer(
        f"✅ Ник изменён: <code>{escape(old_name)}</code> → "
        f"<code>{escape(new_name)}</code>"
    )


@router.message(Command("ban"))
async def ban_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split(maxsplit=3)[1:]
    if len(args) < 2:
        await message.answer(
            "Использование: /ban [id/ник] [7d/30d/perm] [причина]"
        )
        return

    target_id, player = await resolve_target(args[0])
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    duration = args[1].lower()
    reason = args[2] if len(args) >= 3 else "Без причины"

    if duration == "perm":
        player["ban_permanent"] = True
        player["ban_until"] = None
    elif duration in ("7d", "30d"):
        days = int(duration[:-1])
        player["ban_permanent"] = False
        player["ban_until"] = iso(now() + timedelta(days=days))
    else:
        await message.answer("❌ Срок: 7d, 30d или perm.")
        return

    add_history("ban", target_id, player.get("nation"), f"{duration}: {reason}")
    await STORE.save_core()
    await message.answer(
        "⛔️ <b>Игрок забанен.</b>\n"
        f"Срок: <b>{escape(duration)}</b>\n"
        f"Причина: <b>{escape(reason)}</b>"
    )


@router.message(Command("unban"))
async def unban_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split()[1:]
    target_id, player = await resolve_target(args[0] if args else "")
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    player["ban_until"] = None
    player["ban_permanent"] = False
    add_history("unban", target_id, player.get("nation"))
    await STORE.save_core()
    await message.answer("✅ Бан снят.")


@router.message(Command("player_end"))
async def player_end_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split()[1:]
    target_id, player = await resolve_target(args[0] if args else "")
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    player["career_until"] = iso(now() + timedelta(days=CAREER_DAYS))
    add_history("admin_career_end", target_id, player.get("nation"))
    await STORE.save_core()
    await message.answer("🥀 Карьера игрока завершена на 30 дней.")


@router.message(Command("player_noend"))
async def player_noend_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split()[1:]
    target_id, player = await resolve_target(args[0] if args else "")
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    player["career_until"] = None
    add_history("admin_career_return", target_id, player.get("nation"))
    await STORE.save_core()
    await message.answer("🌹 Карьера игрока возвращена.")


@router.message(Command("add_admins"))
async def add_admin_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split()[1:]
    target_id, player = await resolve_target(args[0] if args else "")
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    if target_id not in get_admin_ids():
        STORE.admins["ids"].append(target_id)

    add_history("admin_add", target_id, player.get("nation"))
    await STORE.save_admins()
    await message.answer("✅ Игрок добавлен в администраторы.")


@router.message(Command("remove_admins"))
async def remove_admin_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split()[1:]
    target_id, player = await resolve_target(args[0] if args else "")
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    STORE.admins["ids"] = [
        x for x in STORE.admins["ids"] if str(x) != str(target_id)
    ]
    add_history("admin_remove", target_id, player.get("nation"))
    await STORE.save_admins()
    await message.answer("✅ Права администратора сняты.")


@router.message(Command("off_coldaun"))
async def cooldown_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split()[1:]
    target_id, player = await resolve_target(args[0] if args else "")
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    STORE.free_agents["items"].pop(str(target_id), None)
    add_history("cooldown_reset", target_id, player.get("nation"))
    await STORE.save_free_agents()
    await message.answer("✅ Ограничение свободного агента сброшено.")


# ============================================================
# HISTORY
# ============================================================

@router.message(Command("history_player"))
async def history_player_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    args = message.text.split()[1:]
    target_id, player = await resolve_target(args[0] if args else "")
    if not player:
        await message.answer("❌ Игрок не найден.")
        return

    events = [
        e for e in STORE.core["history"]
        if str(e.get("user_id")) == str(target_id)
    ][-30:]
    if not events:
        await message.answer("📜 История пуста.")
        return

    lines = ["📜 <b>ИСТОРИЯ ИГРОКА</b>\n"]
    for e in events:
        lines.append(
            f"• <code>{escape(e['time'])}</code> — "
            f"<b>{escape(e['action'])}</b>\n"
            f"  {escape(e.get('details') or '')}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("history_club"))
async def history_club_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    nation = message.text.partition(" ")[2].strip()
    if nation not in NATIONS:
        await message.answer("❌ Сборная не найдена.")
        return

    events = [
        e for e in STORE.core["history"] if e.get("nation") == nation
    ][-50:]
    if not events:
        await message.answer("📜 История пуста.")
        return

    lines = [f"📜 <b>ИСТОРИЯ: {escape(nation)}</b>\n"]
    for e in events:
        lines.append(
            f"• <code>{escape(e['time'])}</code> — "
            f"<b>{escape(e['action'])}</b>\n"
            f"  {escape(e.get('details') or '')}"
        )
    await message.answer("\n".join(lines))


# ============================================================
# POSTS (только ЛС)
# ============================================================

@router.message(Command("post_bot"))
async def post_bot_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("Использование: /post_bot [текст]")
        return

    sent = 0
    for user_id in list(STORE.core["players"]):
        if await notify_user(bot, int(user_id), text):
            sent += 1

    await message.answer(f"✅ Рассылка завершена. Доставлено: {sent}.")


@router.message(Command("post"))
async def post_channel_command(message: Message, bot: Bot):
    if not is_private_chat(message):
        await private_only_reply(message, bot)
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только администратор.")
        return

    if not TRANSFER_CHANNEL_ID:
        await message.answer("❌ TRANSFER_CHANNEL_ID не задан.")
        return

    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("Использование: /post [текст]")
        return

    ok = await submit_moderation(bot, text, message.from_user.id, "manual")
    if ok:
        await message.answer("⏳ Пост отправлен в админ-группу на модерацию.")
    else:
        await message.answer(
            "⚠️ Не удалось отправить в админ-группу. Проверьте ADMIN_CHAT_ID."
        )


# ============================================================
# CLEANUP
# ============================================================

async def cleanup_loop():
    while True:
        changed = False
        current = now()

        for user_id, request in list(STORE.free_agents["items"].items()):
            expires = parse_dt(request.get("expires_at"))
            if expires and current >= expires:
                STORE.free_agents["items"].pop(user_id, None)
                changed = True

        for player in STORE.core["players"].values():
            career_until = parse_dt(player.get("career_until"))
            if career_until and current >= career_until:
                player["career_until"] = None
                changed = True

            ban_until = parse_dt(player.get("ban_until"))
            if ban_until and current >= ban_until \
                    and not player.get("ban_permanent"):
                player["ban_until"] = None
                player["ban_permanent"] = False
                changed = True

        for request in STORE.requests["items"].values():
            created = parse_dt(request.get("created_at"))
            if (
                created
                and current - created > timedelta(hours=24)
                and request.get("status") in {"pending_player", "pending_admin"}
            ):
                request["status"] = "expired"
                changed = True

        for nation, data in STORE.core["nations"].items():
            owner_id = data.get("owner_id")
            if not owner_id:
                continue
            owner = STORE.core["players"].get(str(owner_id))
            if not owner or owner.get("nation") != nation:
                data["owner_id"] = None
                changed = True

        if changed:
            await STORE.save_all()

        await asyncio.sleep(60)


# ============================================================
# RUN
# ============================================================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Укажи BOT_TOKEN в переменных окружения.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    cleanup_task = asyncio.create_task(cleanup_loop())

    print("Bot started.")
    if ADMIN_CHAT_ID:
        try:
            chat_id = int(ADMIN_CHAT_ID)
            try:
                chat = await bot.get_chat(chat_id)
                print(f"ADMIN_CHAT_ID OK: {chat_id} ({chat.title or chat.type})")
            except Exception as error:
                print(f"ADMIN_CHAT_ID задан ({chat_id}), но недоступен: {error}")
        except ValueError:
            print(f"ADMIN_CHAT_ID некорректен: {ADMIN_CHAT_ID!r}")
    else:
        print("ADMIN_CHAT_ID НЕ ЗАДАН — модерация работать не будет!")
    print("TRANSFER_CHANNEL_ID:", TRANSFER_CHANNEL_ID or "(не задан)")

    try:
        await dispatcher.start_polling(bot)
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
