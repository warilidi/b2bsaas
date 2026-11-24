"""Telegram-бот напоминаний о записи для салона красоты.

Как это работает:
1. Администратор салона пишет боту: /new 25.11 15:00 Анна | маникюр
2. Бот создаёт запись и выдаёт ссылку для клиента.
3. Клиент открывает ссылку и нажимает «Старт» (Telegram не позволяет ботам
   писать первыми, поэтому этот шаг обязателен).
4. За 24 часа до визита бот присылает напоминание с кнопками «Приду / Не смогу».
5. Ответ клиента приходит администратору.
"""
import asyncio
import csv
import hashlib
import io
import logging
import os
import re
import secrets
import sqlite3
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

TZ = ZoneInfo("Europe/Kyiv")
REMIND_BEFORE = timedelta(hours=24)
CHECK_EVERY_SECONDS = 60
SHEET_EVERY_SECONDS = 120

log = logging.getLogger("salonbot")

# Эти значения заполняются в main() из .env
DB_PATH = "bookings.db"
ADMINS: set[int] = set()
SALON_NAME = "салон"
SHEET_SOURCE = ""  # ссылка на опубликованную CSV-таблицу (или путь к CSV-файлу)


# ---------------------------------------------------------------- настройки
def load_env(path: str = ".env") -> None:
    """Простой загрузчик .env без внешних библиотек."""
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ------------------------------------------------------------ база данных
def run(sql: str, params: tuple = (), *, fetch: bool = False):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        last_id = cur.lastrowid
    return rows if fetch else last_id


def init_db() -> None:
    run(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            client_name TEXT NOT NULL,
            service TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            chat_id INTEGER,
            status TEXT NOT NULL DEFAULT 'booked',  -- booked / confirmed / cancelled
            reminded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual'  -- manual / sheet
        )
        """
    )
    try:  # для баз, созданных до появления таблиц
        run("ALTER TABLE appointments ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    except sqlite3.OperationalError:
        pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def create_appointment(
    name: str,
    service: str,
    start_local: datetime,
    *,
    code: str | None = None,
    source: str = "manual",
) -> tuple[int, str]:
    code = code or secrets.token_urlsafe(6)
    app_id = run(
        "INSERT INTO appointments (code, client_name, service, start_utc, created_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            code,
            name,
            service,
            start_local.astimezone(timezone.utc).isoformat(),
            now_utc().isoformat(),
            source,
        ),
    )
    return app_id, code


def get_by_code(code: str):
    rows = run("SELECT * FROM appointments WHERE code = ?", (code,), fetch=True)
    return rows[0] if rows else None


def get_by_id(app_id: int):
    rows = run("SELECT * FROM appointments WHERE id = ?", (app_id,), fetch=True)
    return rows[0] if rows else None


def due_reminders(now: datetime | None = None):
    """Записи, которым пора отправить напоминание."""
    now = now or now_utc()
    return run(
        "SELECT * FROM appointments "
        "WHERE status = 'booked' AND reminded = 0 AND chat_id IS NOT NULL "
        "AND start_utc > ? AND start_utc <= ?",
        (now.isoformat(), (now + REMIND_BEFORE).isoformat()),
        fetch=True,
    )


def upcoming(now: datetime | None = None):
    now = now or now_utc()
    return run(
        "SELECT * FROM appointments WHERE status != 'cancelled' AND start_utc > ? "
        "ORDER BY start_utc",
        (now.isoformat(),),
        fetch=True,
    )


# ------------------------------------------------- синхронизация с таблицей
COLUMN_NAMES = {
    "date": ("дата", "date"),
    "time": ("час", "время", "time"),
    "name": ("ім'я", "ім’я", "імя", "имя", "клієнт", "клиент", "name"),
    "service": ("послуга", "услуга", "service"),
}


def fetch_sheet(source: str) -> str:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "salonbot"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8-sig")
    return Path(source).read_text(encoding="utf-8-sig")


def _parse_date(value: str, now_local: datetime):
    value = value.strip()
    for pattern in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", value)
    if match:  # «25.11» без года
        candidate = datetime(now_local.year, int(match.group(2)), int(match.group(1))).date()
        if candidate < now_local.date() - timedelta(days=1):
            candidate = candidate.replace(year=now_local.year + 1)
        return candidate
    raise ValueError(value)


def parse_sheet(text: str, now: datetime | None = None):
    """CSV -> ([(имя, услуга, datetime)], [номера строк с ошибками])."""
    now_local = (now or now_utc()).astimezone(TZ)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("Таблиця порожня.")
    header = [cell.strip().lower() for cell in rows[0]]
    index: dict[str, int] = {}
    for key, names in COLUMN_NAMES.items():
        for position, title in enumerate(header):
            if title in names:
                index[key] = position
                break
        else:
            raise ValueError(
                f"У таблиці немає стовпця «{names[0].capitalize()}». "
                "Перший рядок має бути: Дата, Час, Ім'я, Послуга."
            )
    result, errors = [], []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        try:
            day = _parse_date(row[index["date"]], now_local)
            time_match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", row[index["time"]].strip())
            if not time_match:
                raise ValueError("time")
            name, service = row[index["name"]].strip(), row[index["service"]].strip()
            if not name or not service:
                raise ValueError("empty")
            start = datetime(
                day.year, day.month, day.day,
                int(time_match.group(1)), int(time_match.group(2)), tzinfo=TZ,
            )
            result.append((name, service, start))
        except (ValueError, IndexError):
            errors.append(line_no)
    return result, errors


def make_code(start: datetime, name: str, service: str) -> str:
    raw = f"{start.astimezone(timezone.utc).isoformat()}|{name.casefold()}|{service.casefold()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def apply_sheet(rows, now: datetime | None = None):
    """Добавляет новые записи из таблицы и отменяет те, что пропали из неё."""
    now = now or now_utc()
    seen, added = set(), 0
    for name, service, start in rows:
        if start.astimezone(timezone.utc) <= now:
            continue
        code = make_code(start, name, service)
        seen.add(code)
        if not get_by_code(code):
            create_appointment(name, service, start, code=code, source="sheet")
            added += 1
    gone = []
    for row in run(
        "SELECT * FROM appointments WHERE source = 'sheet' AND status != 'cancelled' "
        "AND start_utc > ?",
        (now.isoformat(),),
        fetch=True,
    ):
        if row["code"] not in seen:
            run("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (row["id"],))
            gone.append(row)
    return added, gone


# ---------------------------------------------------------------- утилиты
NEW_RE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+?)\s*\|\s*(.+)$"
)


def parse_new(args: str, now: datetime | None = None):
    """'25.11 15:00 Анна | маникюр' -> (имя, услуга, datetime в Киеве)."""
    match = NEW_RE.match((args or "").strip())
    if not match:
        raise ValueError("format")
    day, month, hour, minute, name, service = match.groups()
    now_local = (now or now_utc()).astimezone(TZ)
    year = now_local.year
    start = datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=TZ)
    # если дата уже прошла больше чем на сутки, значит, речь про следующий год
    if start < now_local - timedelta(days=1):
        start = start.replace(year=year + 1)
    return name.strip(), service.strip(), start


def fmt(start_utc: str) -> str:
    local = datetime.fromisoformat(start_utc).astimezone(TZ)
    return local.strftime("%d.%m о %H:%M")


STATUS_LABEL = {"booked": "чекає", "confirmed": "підтвердив(ла)", "cancelled": "скасовано"}


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMINS)


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            log.warning("Не удалось написать администратору %s", admin_id)


def reminder_keyboard(app_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Прийду", callback_data=f"ok:{app_id}"),
                InlineKeyboardButton(text="❌ Не зможу", callback_data=f"no:{app_id}"),
            ]
        ]
    )


# ----------------------------------------------------------- обработчики
dp = Dispatcher()

HELP_ADMIN = (
    "Команди адміністратора:\n"
    "/new 25.11 15:00 Анна | манікюр — створити запис\n"
    "/list — найближчі записи\n"
    "/cancel 3 — скасувати запис №3\n"
    "/links — посилання для клієнтів, які ще не підключили бота\n"
    "/sync — прочитати таблицю просто зараз\n"
    "/myid — показати ваш Telegram ID\n\n"
    "Якщо підключено таблицю, записи беруться з неї: додайте рядок "
    "(Дата, Час, Ім'я, Послуга), і за пару хвилин запис з'явиться."
)


@dp.message(CommandStart(deep_link=True))
async def start_with_code(message: Message, command: CommandObject, bot: Bot):
    appointment = get_by_code(command.args or "")
    if not appointment:
        await message.answer("Не знайшов такого запису. Попросіть у салоні нове посилання.")
        return
    if appointment["status"] == "cancelled":
        await message.answer("Цей запис скасовано. За потреби зв'яжіться із салоном.")
        return
    if appointment["chat_id"] and appointment["chat_id"] != message.chat.id:
        await message.answer("Цей запис уже прив'язано до іншого акаунта.")
        return
    run("UPDATE appointments SET chat_id = ? WHERE id = ?", (message.chat.id, appointment["id"]))
    await message.answer(
        f"Вітаю, {appointment['client_name']}! Ви записані до {SALON_NAME}: "
        f"{fmt(appointment['start_utc'])}, {appointment['service']}.\n"
        "За добу до візиту я надішлю нагадування."
    )
    await notify_admins(
        bot, f"Клієнт {appointment['client_name']} підключив нагадування (запис №{appointment['id']})."
    )


@dp.message(CommandStart())
async def start_plain(message: Message):
    if is_admin(message):
        await message.answer(f"Привіт! Я бот нагадувань для «{SALON_NAME}».\n\n{HELP_ADMIN}")
    else:
        await message.answer(
            "Я надсилаю нагадування про запис до салону. "
            "Відкрийте посилання, яке вам дав адміністратор."
        )


@dp.message(Command("myid"))
async def my_id(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(Command("help"))
async def help_cmd(message: Message):
    if is_admin(message):
        await message.answer(HELP_ADMIN)
    else:
        await message.answer("Я надсилаю нагадування про запис. Відкрийте посилання від салону.")


@dp.message(Command("new"))
async def new_cmd(message: Message, command: CommandObject, bot: Bot):
    if not is_admin(message):
        return
    try:
        name, service, start = parse_new(command.args or "")
    except ValueError:
        await message.answer(
            "Не зрозумів формат. Приклад:\n/new 25.11 15:00 Анна | манікюр\n"
            "(день.місяць години:хвилини Ім'я | послуга)"
        )
        return
    if start <= datetime.now(TZ):
        await message.answer("Цей час уже минув. Перевірте дату.")
        return
    app_id, code = create_appointment(name, service, start)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    await message.answer(
        f"Запис №{app_id} створено: {name}, {service}, {start.strftime('%d.%m о %H:%M')}.\n\n"
        f"Надішліть клієнту це повідомлення:\n\n"
        f"Вітаю! Щоб отримати нагадування про запис до {SALON_NAME}, "
        f"відкрийте посилання та натисніть «Старт»: {link}"
    )


@dp.message(Command("list"))
async def list_cmd(message: Message):
    if not is_admin(message):
        return
    rows = upcoming()
    if not rows:
        await message.answer("Найближчих записів немає.")
        return
    lines = []
    for row in rows[:30]:
        linked = "" if row["chat_id"] else " (клієнт ще не підключив бота)"
        lines.append(
            f"№{row['id']} · {fmt(row['start_utc'])} · {row['client_name']} · "
            f"{row['service']} · {STATUS_LABEL[row['status']]}{linked}"
        )
    await message.answer("\n".join(lines))


@dp.message(Command("links"))
async def links_cmd(message: Message, bot: Bot):
    if not is_admin(message):
        return
    rows = [row for row in upcoming() if not row["chat_id"]]
    if not rows:
        await message.answer("Усі клієнти вже підключили бота.")
        return
    me = await bot.get_me()
    blocks = [
        f"{fmt(row['start_utc'])} · {row['client_name']} · {row['service']}\n"
        f"https://t.me/{me.username}?start={row['code']}"
        for row in rows[:15]
    ]
    await message.answer(
        "Надішліть кожному клієнту його посилання (потрібно натиснути «Старт»):\n\n" + "\n\n".join(blocks)
    )


@dp.message(Command("sync"))
async def sync_cmd(message: Message, bot: Bot):
    if not is_admin(message):
        return
    if not SHEET_SOURCE:
        await message.answer("Таблицю не підключено. Додайте SHEET_CSV_URL у файл .env.")
        return
    try:
        report = await sync_from_sheet(bot)
    except ValueError as error:
        await message.answer(str(error))
        return
    except Exception:
        log.exception("Не удалось прочитать таблицу")
        await message.answer("Не вдалося прочитати таблицю. Перевірте посилання в SHEET_CSV_URL.")
        return
    await message.answer(report)


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, command: CommandObject, bot: Bot):
    if not is_admin(message):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Вкажіть номер запису: /cancel 3")
        return
    appointment = get_by_id(int(command.args.strip()))
    if not appointment:
        await message.answer("Запис із таким номером не знайдено.")
        return
    run("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment["id"],))
    await message.answer(f"Запис №{appointment['id']} скасовано.")
    if appointment["chat_id"]:
        try:
            await bot.send_message(
                appointment["chat_id"],
                f"Ваш запис до {SALON_NAME} на {fmt(appointment['start_utc'])} скасовано салоном.",
            )
        except TelegramAPIError:
            pass


@dp.callback_query(F.data.regexp(r"^(ok|no):\d+$"))
async def answer_callback(callback: CallbackQuery, bot: Bot):
    action, raw_id = callback.data.split(":")
    appointment = get_by_id(int(raw_id))
    if not appointment or appointment["chat_id"] != callback.message.chat.id:
        await callback.answer("Запис не знайдено.", show_alert=True)
        return
    if appointment["status"] == "cancelled":
        await callback.answer("Запис уже скасовано.", show_alert=True)
        return

    when = fmt(appointment["start_utc"])
    if action == "ok":
        run("UPDATE appointments SET status = 'confirmed' WHERE id = ?", (appointment["id"],))
        await callback.message.edit_text(f"Дякуємо! Чекаємо на вас {when} 💜")
        await notify_admins(
            bot, f"✅ {appointment['client_name']} підтвердив(ла) запис №{appointment['id']} на {when}."
        )
    else:
        run("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment["id"],))
        await callback.message.edit_text(
            f"Запис на {when} скасовано. Щоб обрати інший час, зв'яжіться із салоном."
        )
        await notify_admins(
            bot,
            f"❌ {appointment['client_name']} скасував(ла) запис №{appointment['id']} на {when}. "
            "Час звільнився.",
        )
    await callback.answer()


# ------------------------------------------------------- фоновые напоминания
async def send_due_reminders(bot: Bot) -> None:
    for row in due_reminders():
        # помечаем сразу, чтобы не отправить дважды при сбое
        run("UPDATE appointments SET reminded = 1 WHERE id = ?", (row["id"],))
        text = (
            f"Вітаю, {row['client_name']}! Нагадуємо про запис до {SALON_NAME}: "
            f"{fmt(row['start_utc'])}, {row['service']}.\nВи прийдете?"
        )
        try:
            await bot.send_message(row["chat_id"], text, reply_markup=reminder_keyboard(row["id"]))
        except TelegramForbiddenError:
            await notify_admins(
                bot,
                f"Не вдалося надіслати нагадування клієнту {row['client_name']} "
                f"(запис №{row['id']}): бот заблокований. Зателефонуйте йому.",
            )
        except TelegramAPIError:
            log.exception("Ошибка при отправке напоминания по записи %s", row["id"])


_last_problem = ""


async def sync_from_sheet(bot: Bot) -> str:
    """Читает таблицу, обновляет записи, возвращает короткий отчёт."""
    global _last_problem
    text = await asyncio.to_thread(fetch_sheet, SHEET_SOURCE)
    rows, errors = parse_sheet(text)
    added, gone = apply_sheet(rows)

    for row in gone:
        extra = " Клієнт уже підключав бота, попередьте його." if row["chat_id"] else ""
        await notify_admins(
            bot,
            f"Запис №{row['id']} ({row['client_name']}, {fmt(row['start_utc'])}) зник "
            f"з таблиці та скасований.{extra}",
        )

    problem = f"Не вдалося прочитати рядки таблиці: {', '.join(map(str, errors))}. " \
              "Перевірте дати (25.11.2026) та час (15:00)." if errors else ""
    if problem and problem != _last_problem:
        await notify_admins(bot, problem)
    _last_problem = problem

    return f"З таблиці: додано {added}, скасовано {len(gone)}, рядків із помилками {len(errors)}."


async def sheet_loop(bot: Bot) -> None:
    global _last_problem
    while True:
        try:
            await sync_from_sheet(bot)
        except ValueError as error:  # неверная шапка таблицы
            if str(error) != _last_problem:
                _last_problem = str(error)
                await notify_admins(bot, str(error))
        except Exception as error:
            log.warning("Не удалось прочитать таблицу: %s", error)
        await asyncio.sleep(SHEET_EVERY_SECONDS)


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            await send_due_reminders(bot)
        except Exception:
            log.exception("Сбой в цикле напоминаний")
        await asyncio.sleep(CHECK_EVERY_SECONDS)


# -------------------------------------------------------------------- запуск
async def main() -> None:
    global DB_PATH, ADMINS, SALON_NAME, SHEET_SOURCE
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")
    DB_PATH = os.environ.get("DB_PATH", DB_PATH)
    SALON_NAME = os.environ.get("SALON_NAME", SALON_NAME)
    ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
    if not ADMINS:
        log.warning("ADMIN_IDS пуст: создавать записи пока некому. Узнайте ID командой /myid.")

    SHEET_SOURCE = os.environ.get("SHEET_CSV_URL", "").strip()

    init_db()
    bot = Bot(token)
    tasks = [asyncio.create_task(reminder_loop(bot))]
    if SHEET_SOURCE:
        tasks.append(asyncio.create_task(sheet_loop(bot)))
        log.info("Таблица подключена, читаю каждые %s сек.", SHEET_EVERY_SECONDS)
    try:
        await bot.delete_webhook()  # на случай, если у бота остался старый webhook
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())