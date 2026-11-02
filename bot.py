import asyncio
import logging
import os
import platform
import threading
import time
import urllib.request
from datetime import datetime

from flask import Flask
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("quest")

# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN")

# Кому присылать оценки. Можно несколько через запятую: "123456789,987654321".
ADMIN_CHAT_IDS = [
    chunk.strip() for chunk in os.getenv("ADMIN_CHAT_ID", "").split(",") if chunk.strip()
]

# ------------------------------------------------------------
# Маленький веб-сервер для Render
# ------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def home():
    return "Петербургский квест работает! 🟡"


@app.route("/health")
def health():
    # Render дёргает этот адрес для проверки, что сервис жив
    return f"ok admin={len(ADMIN_CHAT_IDS)}", 200


def run_web():
    port = int(os.getenv("PORT", "10000"))
    log.info("Веб-сервер слушает порт %s", port)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# ============================================================
# КАРТЫ
# ============================================================

def map_url(lat, lon):
    """
    Открывает Яндекс Карты с точкой назначения.
    Пешеходный режим.
    Старт маршрута можно выбрать непосредственно в Картах.
    """
    return (
        f"https://yandex.ru/maps/?"
        f"rtext=~{lat},{lon}"
        f"&rtt=pd"
    )


# Координаты следующих точек
MAPS = {
    "point2": map_url(59.926400, 30.343100),   # Пять углов
    "point3": map_url(59.926900, 30.318200),   # Сенная площадь
    "point4": map_url(59.923265, 30.308780),   # Дом с совами
    "point5": map_url(59.920902, 30.298423),   # Пикалов мост
    "point6": map_url(59.926915, 30.301403),   # Львиный мост
    "point7": map_url(59.931664, 30.308926),   # Синий мост
    "point8": map_url(59.933484, 30.307805),   # Тайная дверца
    "point9": map_url(59.937988, 30.308788),   # Адмиралтейство
    "finish": map_url(59.936041, 30.314251),   # Адмиралтейская
}


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def buttons(items):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data)] for text, data in items]
    )


# Последний заданный вопрос в каждом чате: chat_id -> (текст, клавиатура).
# Нужен, чтобы на неверный ответ повторять только вопрос, а не весь блок целиком.
LAST_QUESTION = {}

# Когда игру начали: chat_id -> datetime. Нужно для времени прохождения.
START_TIME = {}

# Флаг «сейчас мы просто повторяем вопрос», а не задаём новый.
REPEAT_MODE = [False]


def question_only(text):
    """Оставляет от текста блока только строку вопроса (всё, что начинается с ❓)."""
    idx = text.find("❓")
    return text[idx:].strip() if idx >= 0 else text.strip()


def elapsed_text(chat_id):
    """Сколько идёт прогулка: '3 ч 12 мин' или '' если время неизвестно."""
    started = START_TIME.get(chat_id)
    if not started:
        return ""
    minutes = int((datetime.now() - started).total_seconds() // 60)
    if minutes < 1:
        return "меньше минуты"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


async def ask(query, text, reply_markup):
    """Отправляет вопрос и запоминает его на случай неверного ответа."""
    if REPEAT_MODE[0]:
        await query.message.reply_text(question_only(text), reply_markup=reply_markup)
        return
    LAST_QUESTION[query.message.chat_id] = (text, reply_markup)
    await query.message.reply_text(text, reply_markup=reply_markup)


async def ask_again(query, data):
    """
    Повторяет только вопрос (без вступления и правил) с теми же кнопками.

    Обычно берёт последний заданный вопрос из памяти. Если бота перезапустили
    и память пуста, восстанавливает вопрос по нажатой кнопке.
    """
    saved = LAST_QUESTION.get(query.message.chat_id)
    if saved:
        text, reply_markup = saved
        await query.message.reply_text(question_only(text), reply_markup=reply_markup)
        return

    step = RETRY_BY_DATA.get(data)
    if not step:
        return
    REPEAT_MODE[0] = True
    try:
        await step(query)
    finally:
        REPEAT_MODE[0] = False


async def notify_admins(context, query, stars):
    """Отправляет оценку всем, кто указан в ADMIN_CHAT_ID."""
    user = query.from_user
    who = f"@{user.username}" if user.username else user.full_name
    passed = elapsed_text(query.message.chat_id)

    lines = [
        f"⭐ Новая оценка квеста: {stars} из 5",
        f"Кто: {who} (id {user.id})",
        f"Когда: {datetime.now():%d.%m.%Y %H:%M}",
    ]
    if passed:
        lines.append(f"Время прохождения: {passed}")
    text = "\n".join(lines)

    if not ADMIN_CHAT_IDS:
        log.info("Оценка %s/5 от %s. ADMIN_CHAT_ID не задан, отправлять некому.", stars, user.id)
        return

    for chat_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не смог отправить оценку в %s: %s", chat_id, exc)


async def send_route(query, text, map_key, arrived_callback):
    keyboard = [
        [InlineKeyboardButton("🗺 Открыть маршрут", url=MAPS[map_key])],
        [InlineKeyboardButton("📍 Я на месте", callback_data=arrived_callback)],
    ]

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # сбрасываем прошлую попытку и засекаем время
    LAST_QUESTION.pop(chat_id, None)
    START_TIME[chat_id] = datetime.now()

    text = """
Привет! 👋

Сегодня типичный Питер с серыми домами покажет свои пасхалки: жёлтую дорогу, пять углов, сов на крыше, мосты, тайную дверцу и подозрительно идеальные линии.

Маршрут пешеходный, бесплатный и займёт около 4 часов с перекусом.

Смотрите по сторонам, выбирайте ответы и не спорьте с домофонами.

Готовы искать то, что СПб спрятал на самом видном месте?

Правило квеста:
не лезем за ограждения, не заходим туда, куда нельзя, и не рискуем ради правильного ответа. Петербург никуда не убежит. 😉
"""

    keyboard = [
        [InlineKeyboardButton("🟡 В путь по жёлтой дороге", callback_data="point1")]
    ]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
Как играть 📖

1. Нажмите /start и кнопку «🟡 В путь по жёлтой дороге».
2. Бот ведёт от точки к точке: сначала маршрут, потом кнопка «📍 Я на месте».
3. На каждой точке один вопрос. Отвечайте кнопками.
4. Ошиблись? Придёт подсказка и вопрос повторится. Можно пробовать сколько угодно.
5. В конце можно поставить оценку.

Что ещё умею:
/start или /restart: начать заново с первой точки
/help: эта памятка

Если бот молчит, подождите минуту: бесплатный сервер иногда просыпается.
"""
    await update.message.reply_text(text)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ответ на обычный текст: подсказываем, что тут только кнопки."""
    await update.message.reply_text(
        "Понимаю только кнопки 🙂\n\n"
        "Выберите вариант в последнем сообщении с вопросом.\n\n"
        "Начать заново: /start\n"
        "Памятка: /help"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает chat_id: его нужно вписать в ADMIN_CHAT_ID на Render."""
    await update.message.reply_text(
        f"Ваш chat_id: {update.effective_chat.id}\n\n"
        "Скопируйте это число и вставьте в переменную ADMIN_CHAT_ID на Render."
    )


async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка, что оценки доходят до админа."""
    if not ADMIN_CHAT_IDS:
        await update.message.reply_text(
            "ADMIN_CHAT_ID не задан, поэтому оценки пока никуда не приходят.\n"
            "Впишите его в переменные окружения на Render и перезапустите сервис."
        )
        return

    sent, failed = [], []
    for chat_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text="✅ Проверка связи. Сюда будут приходить оценки квеста.",
            )
            sent.append(chat_id)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{chat_id} ({exc})")

    lines = []
    if sent:
        lines.append("Отправлено: " + ", ".join(sent))
    if failed:
        lines.append("Не получилось: " + ", ".join(failed))
        lines.append(
            "Обычно причина в том, что этот аккаунт ещё не нажимал /start у бота."
        )
    await update.message.reply_text("\n\n".join(lines))


# ============================================================
# ТОЧКА 1
# ============================================================

async def point1(query):
    text = """
📍 Улица Правды, дом 8

Где-то здесь Петербург спрятал Изумрудный город.

Как и положено волшебному месту, вход иногда существует,
а иногда притворяется домофоном.

Двор жилой: не звоним в квартиры, не идём хвостиком за жильцами
и не применяем заклинание «А вы придержите».
"""

    await ask(query, text, reply_markup=buttons([
            ("🟢 Двор открыт", "yard_open"),
            ("🔒 Магия не сработала", "yard_closed"),
        ]),
    )


async def yard_open(query):
    text = """
Отлично!

Начинайте у дома № 8 и двигайтесь по доступным дворам к дому № 2.

Смотрите под ноги: сегодня серый Петербург взял выходной.

❓ Какого цвета дорожка решила заменить вам навигатор?
"""

    await ask(query, text, reply_markup=buttons([
            ("🟡 Жёлтая, всё по канону", "yellow"),
            ("🟢 Зелёная, мы же в Изумрудном", "green"),
            ("🩶 Серая, это всё-таки Петербург", "grey"),
        ]),
    )


async def yard_closed(query):
    text = """
Домофон оказался сильнее Гудвина.

Ничего страшного: двор закрылся, а квест нет.

Остаёмся снаружи. Без звонков жильцам, тайных проникновений
и операции «проскочить за бабушкой».

❓ Найдите номер дома, от которого правильнее начинать путь
по Изумрудному городу в сторону дома № 2.
"""

    await ask(query, text, reply_markup=buttons([
            ("🏠 № 2: начать с финиша", "house2"),
            ("🏠 № 6: ворваться с середины", "house6"),
            ("🏠 № 8: идти по сюжету", "house8"),
            ("👀 Не вижу номер", "house_unknown"),
        ]),
    )


async def go_point2(query):
    await send_route(
        query,
        """
🟡 Отлично!

Следующая остановка: Пять углов.

Ищите Загородный проспект, дом 11.
Пешком примерно 15-20 минут.

Сначала доберитесь до места, затем нажмите «📍 Я на месте».
""",
        "point2",
        "point2_arrived",
    )


# ============================================================
# ТОЧКА 2: ПЯТЬ УГЛОВ
# ============================================================

async def point2(query):
    text = """
📍 Пять углов
Загородный проспект, 11

Вы пришли туда, где у перекрёстка углов больше, чем положено
по технике безопасности.

Найдите дом с башней. Это доходный дом купца Ш. З. Иоффа,
главный ориентир Пяти углов.

❓ Сколько улиц и проспектов сходятся здесь?
Не доверяйте названию.
"""

    await ask(query, text, reply_markup=buttons([
            ("3️⃣ Мало", "five_3"),
            ("4️⃣ Похоже", "five_4"),
            ("5️⃣ Логично", "five_5"),
        ]),
    )


# ============================================================
# ТОЧКА 3: СЕННАЯ
# ============================================================

async def go_point3(query):
    await send_route(
        query,
        """
🥐 Перекус!

От Пяти углов идите к Сенной площади.
Пешком примерно 15-20 минут.

Можно открыть карту и спокойно идти по маршруту.

Когда придёте, нажмите «📍 Я на месте».
""",
        "point3",
        "point3_arrived",
    )


async def point3(query):
    text = """
📍 Сенная площадь

По графику 15 минут на перекус.

Можно зайти в «Цех85» на набережной канала Грибоедова,
65-67 (если открыт) или выбрать любое кафе рядом.

Главное: перекусить, а не переехать сюда жить. 😄

После перекуса:

Под Сенной встретились сразу три станции метро.
Почти клуб на букву «С», но одну участницу мы добавили сами.

❓ Какая станция находится не на Сенной площади?
"""

    await ask(query, text, reply_markup=buttons([
            ("Сенная", "metro_sennaya"),
            ("Садовая", "metro_sadovaya"),
            ("Спасская", "metro_spasskaya"),
            ("Спортивная", "metro_sportivnaya"),
        ]),
    )


# ============================================================
# ТОЧКА 4: ДОМ С СОВАМИ
# ============================================================

async def go_point4(query):
    await send_route(
        query,
        """
🦉 Следующая точка: Дом с совами.

От Сенной площади идите по Садовой улице
в сторону Вознесенского проспекта.

Нужный дом:
Садовая, 55-57.

Пешком около 10-12 минут.

Нажмите «📍 Я на месте», когда доберётесь.
""",
        "point4",
        "point4_arrived",
    )


async def point4(query):
    text = """
📍 Дом городских учреждений

Перед вами Дом городских учреждений, он же Дом с совами.

Видимо, чиновникам решили добавить мудрости хотя бы на уровне крыши.

Встаньте у перекрёстка и внимательно осмотрите верхнюю часть
обоих фасадов: по Садовой и Вознесенскому.

❓ Сколько больших сов охраняют дом?
"""

    await ask(query, text, reply_markup=buttons([
            ("2 🦉", "owls2"),
            ("3 🦉", "owls3"),
            ("4 🦉", "owls4"),
        ]),
    )


# ============================================================
# ТОЧКА 5: ПИКАЛОВ МОСТ
# ============================================================

async def go_point5(query):
    await send_route(
        query,
        """
🌉 Следующая точка: Пикалов мост.

От Дома городских учреждений продолжайте идти прямо
по Садовой улице.

Дойдите до Крюкова канала и у Старо-Никольского моста
поверните направо на набережную.

Пикалов мост будет совсем рядом.
Пешком около 10 минут.

Нажмите «📍 Я на месте».
""",
        "point5",
        "point5_arrived",
    )


async def point5(query):
    text = """
📍 Пикалов мост

Встаньте примерно посередине моста так,
чтобы Никольский собор оказался слева.

Медленно поворачивайтесь и считайте мосты.

Только не забудьте про тот, который сейчас держит вас над водой.

❓ Сколько мостов вы насчитали вместе с Пикаловым?
"""

    await ask(query, text, reply_markup=buttons([
            ("6️⃣", "bridges6"),
            ("7️⃣", "bridges7"),
            ("8️⃣", "bridges8"),
            ("Не вижу", "bridges_unknown"),
        ]),
    )


# ============================================================
# ТОЧКА 6: ЛЬВИНЫЙ МОСТ
# ============================================================

async def go_point6(query):
    await send_route(
        query,
        """
🦁 Следующая точка: Львиный мост.

С Пикалова моста спуститесь на набережную
канала Грибоедова и идите вдоль воды в сторону центра.

Примерно через 10 минут увидите небольшой пешеходный мост
с белыми львами.

Нажмите «📍 Я на месте».
""",
        "point6",
        "point6_arrived",
    )


async def point6(query):
    text = """
📍 Львиный мост

Этим летом мосту исполнилось 200 лет.

Он пережил императоров, революции и миллионы фотографий
«я держу льва за голову».

Мост открыли 1 июля 1826 года,
а львы не просто украшают его:
внутри них спрятаны опоры цепей.

❓ Посчитайте белых охранников моста.
Сколько здесь львов?
"""

    await ask(query, text, reply_markup=buttons([
            ("2 🦁", "lions2"),
            ("4 🦁", "lions4"),
            ("6 🦁", "lions6"),
        ]),
    )


# ============================================================
# ТОЧКА 7: СИНИЙ МОСТ
# ============================================================

async def go_point7(query):
    await send_route(
        query,
        """
🌊 Следующая точка: Синий мост.

От Львиного моста идите вдоль канала Грибоедова
до Вознесенского проспекта, затем по нему
прямо до Исаакиевской площади.

Ориентир: Мариинский дворец.

Пешком около 15-20 минут.

Нажмите «📍 Я на месте».
""",
        "point7",
        "point7_arrived",
    )


async def point7(query):
    text = """
📍 Синий мост

Синий мост считается одним из самых широких мостов Петербурга
и одновременно самый незаметный:
он выглядит как продолжение Исаакиевской площади.

Если кажется, что никакого моста здесь нет,
поздравляем: скорее всего, вы уже на нём.

У набережной найдите гранитный столб с трезубцем.

Это шкала Нептуна. На ней отмечены уровни
крупнейших наводнений.

❓ Каким годом подписана самая высокая отметка?
"""

    await ask(query, text, reply_markup=buttons([
            ("1824", "flood1824"),
            ("1924", "flood1924"),
            ("1967", "flood1967"),
        ]),
    )


# ============================================================
# ТОЧКА 8: ТАЙНАЯ ДВЕРЦА
# ============================================================

async def go_point8(query):
    await send_route(
        query,
        """
🚪 Следующая точка: тайная дверца.

От Синего моста пройдите через Исаакиевскую площадь
к собору, затем двигайтесь в сторону Александровского сада.

Ищите дерево со стороны Исаакиевского собора.

Координаты:
59.933484, 30.307805

Пешком около 5-7 минут.

Нажмите «📍 Я на месте».
""",
        "point8",
        "point8_arrived",
    )


async def point8(query):
    text = """
📍 Дверца на дереве

Здесь у дерева появилась собственная входная дверь.

Квартира маленькая, зато район хороший. 😄

Это одна из сказочных дверок проекта FILLART.
Уличные арт-объекты иногда снимают или переносят,
поэтому сначала проверим, на месте ли наш тайный подъезд.

❓ Что вы видите?
"""

    await ask(query, text, reply_markup=buttons([
            ("🚪 Нашёл", "door_found"),
            ("🌳 Исчезла", "door_gone"),
        ]),
    )


async def door_found(query):
    text = """
Рассмотрите два маленьких окошка.
Из одного за прохожими наблюдают местные жильцы.

❓ Кто выглядывает из окошка?
"""

    await ask(query, text, reply_markup=buttons([
            ("Гном и кот", "gnome_cat"),
            ("Фея и пёс", "fairy_dog"),
            ("Две мыши", "two_mice"),
        ]),
    )


async def door_gone(query):
    text = """
Похоже, жильцы сменили адрес без объявления.

Такое с уличными арт-объектами бывает:
дерево остаётся, недвижимость уезжает.

Запасной вопрос:

❓ Посмотрите на вершину главного купола Исаакиевского собора.
Что находится наверху?
"""

    await ask(query, text, reply_markup=buttons([
            ("Крест", "cross"),
            ("Кораблик", "ship"),
            ("Филин", "owl"),
        ]),
    )


# ============================================================
# ТОЧКА 9: АДМИРАЛТЕЙСТВО
# ============================================================

async def go_point9(query):
    await send_route(
        query,
        """
🔱 Финальная точка: Адмиралтейство.

От тайной дверцы пройдите через Александровский сад
к Адмиралтейству.

Ориентир: золотой шпиль с корабликом.
Пешком около 5 минут.

Нажмите «📍 Я на месте».
""",
        "point9",
        "point9_arrived",
    )


async def point9(query):
    text = """
📍 Адмиралтейство

От золотого шпиля расходятся три городских луча:
Невский проспект, Гороховая улица и Вознесенский проспект.

Вместе они образуют Адмиралтейский трезубец.

Три идеально ровные линии.

Масоны? Иллюминаты?
Официально: градостроительный план.

Неофициально: мы всё поняли. 😏

❓ Встаньте спиной к Адмиралтейству и посмотрите
на три луча.

Какая улица проходит по центру трезубца?
"""

    await ask(query, text, reply_markup=buttons([
            ("Невский", "trident_nevsky"),
            ("Гороховая", "trident_goroh"),
            ("Вознесенский", "trident_vos"),
        ]),
    )


# ============================================================
# ФИНИШ
# ============================================================

async def finish(query):
    passed = elapsed_text(query.message.chat_id)
    time_line = f"Вы прошли маршрут за {passed}.\n\n" if passed else ""

    text = f"""
🏁 ФИНИШ

До метро «Адмиралтейская» несколько минут пешком.

{time_line}Вы нашли жёлтую дорогу, пересчитали углы, сов, мосты и львов,
проверили уровень Нептуна, заглянули к гному
и раскрыли городскую схему.

СПб пытался всё спрятать в серых домах.

Не получилось.

Ну что, как вам прогулка?

Оцените бота.
Он не обидится, просто запомнит. 😉
"""

    await ask(query, text, reply_markup=buttons([
            ("⭐", "rating1"),
            ("⭐⭐", "rating2"),
            ("⭐⭐⭐", "rating3"),
            ("⭐⭐⭐⭐", "rating4"),
            ("⭐⭐⭐⭐⭐", "rating5"),
        ]),
    )


# ============================================================
# ОБРАБОТКА КНОПОК
# ============================================================

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # -------- Точка 1 --------

    if data == "point1":
        await point1(query)

    elif data == "yard_open":
        await yard_open(query)

    elif data == "yard_closed":
        await yard_closed(query)

    elif data == "yellow":
        await query.message.reply_text(
            """
Точно! Элли одобряет, Тотошка уже убежал вперёд,
а навигатор нервно курит в стороне.
"""
        )
        await go_point2(query)

    elif data == "green":
        await query.message.reply_text(
            "Почти логично, но зелёный здесь город, а не дорожная разметка. Посмотрите под ноги ещё раз."
        )
        await ask_again(query, data)

    elif data == "grey":
        await query.message.reply_text(
            "Очень по-петербургски, но сегодня город решил удивить. Ищите цвет повеселее."
        )
        await ask_again(query, data)

    elif data == "house2":
        await query.message.reply_text(
            "Это конец маршрута по двору. Начинать с конца можно, но тогда Элли запутается раньше вас."
        )
        await ask_again(query, data)

    elif data == "house6":
        await query.message.reply_text(
            "Это середина. Вы почти телепортировались, но сегодня идём без чит-кодов."
        )
        await ask_again(query, data)

    elif data == "house8":
        await query.message.reply_text(
            "Верно! Путь начинается у дома № 8. Ворота победили доступ, но не вашу дедукцию."
        )
        await go_point2(query)

    elif data == "house_unknown":
        await query.message.reply_text(
            "Посмотрите на фасады вдоль улицы Правды. Нужный дом: № 8. Нашли или нет, двигаемся дальше: волшебные ворота не должны задерживать путешественников."
        )
        await go_point2(query)

    # -------- Точка 2 --------

    elif data == "point2_arrived":
        await point2(query)

    elif data == "five_4":
        await query.message.reply_text(
            """
Верно!

Четыре дороги, пять углов.
Геометрия посмотрела на правила и пошла своей дорогой.
"""
        )
        await go_point3(query)

    elif data == "five_3":
        await query.message.reply_text(
            "Три уже трезубец. Не торопитесь: тайные линии и заговоры будут ближе к финалу."
        )
        await ask_again(query, data)

    elif data == "five_5":
        await query.message.reply_text(
            "Название вас подловило: углы и дороги здесь не совпадают. Считайте не углы, а улицы и проспекты, которые сходятся у башни."
        )
        await ask_again(query, data)

    # -------- Точка 3 --------

    elif data == "point3_arrived":
        await point3(query)

    elif data in ["metro_sennaya", "metro_sadovaya", "metro_spasskaya"]:
        messages = {
            "metro_sennaya": "Она здесь главная: площадь даже носит её имя. Выгонять хозяйку невежливо.",
            "metro_sadovaya": "«Садовая» на месте. Спряталась под землёй, но из списка не исчезла.",
            "metro_spasskaya": "«Спасская» тоже здесь. Возможно, спасает тех, кто слишком долго выбирал перекус.",
        }
        await query.message.reply_text(messages[data])
        await ask_again(query, data)

    elif data == "metro_sportivnaya":
        await query.message.reply_text(
            "Верно! «Спортивная» пришла на встречу не на ту площадь. Бывает: буква «С» вскружила голову."
        )
        await go_point4(query)

    # -------- Точка 4 --------

    elif data == "point4_arrived":
        await point4(query)

    elif data == "owls3":
        await query.message.reply_text(
            """
Верно!

Три совы: одна смотрит на Садовую, две на Вознесенский.

Наблюдение ведётся, документы приняты.
"""
        )
        await go_point5(query)

    elif data == "owls2":
        await query.message.reply_text(
            "Одну сову вы недосчитали. Посмотрите на дом с обеих сторон: они работают посменно, но сегодня все на месте."
        )
        await ask_again(query, data)

    elif data == "owls4":
        await query.message.reply_text(
            "Четвёртая, возможно, ушла согласовывать перепланировку. Осмотрите оба фасада (по Садовой и по Вознесенскому) и посчитайте ещё раз."
        )
        await ask_again(query, data)

    # -------- Точка 5 --------

    elif data == "point5_arrived":
        await point5(query)

    elif data == "bridges7":
        await query.message.reply_text(
            """
Верно!

Шесть мостов видно вокруг, седьмой прямо под ногами.

По легенде, теперь можно загадать желание.
Бот ничего не гарантирует, но у мостов могут быть свои связи.
"""
        )
        await go_point6(query)

    elif data == "bridges6":
        await query.message.reply_text(
            "Вы забыли Пикалов мост. Он скромно лежит под ногами и делает вид, что не участвует."
        )
        await ask_again(query, data)

    elif data == "bridges8":
        await query.message.reply_text(
            "Восемь, это щедро. Похоже, один мост получил второй голос. Повернитесь ещё раз, но помедленнее."
        )
        await ask_again(query, data)

    elif data == "bridges_unknown":
        await query.message.reply_text(
            "Ничего страшного: погода, деревья и кораблики тоже любят вмешиваться в квест. Считаем шесть мостов вокруг и Пикалов под ногами. Всего семь."
        )
        await go_point6(query)

    # -------- Точка 6 --------

    elif data == "point6_arrived":
        await point6(query)

    elif data == "lions4":
        await query.message.reply_text(
            "Верно! Четыре льва, двести лет службы и ни одного перерыва на обед."
        )
        await go_point7(query)

    elif data == "lions2":
        await query.message.reply_text(
            "Вы посчитали только одну сторону. Львы работают по обоим берегам. Обойдите мост взглядом ещё раз."
        )
        await ask_again(query, data)

    elif data == "lions6":
        await query.message.reply_text(
            "Шесть многовато. Это уже расширенный штат охраны. Обойдите мост и посчитайте львов на обоих берегах."
        )
        await ask_again(query, data)

    # -------- Точка 7 --------

    elif data == "point7_arrived":
        await point7(query)

    elif data == "flood1824":
        await query.message.reply_text(
            "Верно! В 1824 году вода поднялась выше четырёх метров. Нептун явно перестарался."
        )
        await go_point8(query)

    elif data == "flood1924":
        await query.message.reply_text(
            "Почти: наводнение было огромным, но рекорд устоял. Ищите отметку выше."
        )
        await ask_again(query, data)

    elif data == "flood1967":
        await query.message.reply_text(
            "В этот раз Нептун действовал скромнее. Посмотрите на самый верх шкалы."
        )
        await ask_again(query, data)

    # -------- Точка 8 --------

    elif data == "point8_arrived":
        await point8(query)

    elif data == "door_found":
        await door_found(query)

    elif data == "door_gone":
        await door_gone(query)

    elif data == "gnome_cat":
        await query.message.reply_text(
            "Верно! Гном дома, кот контролирует двор. Всё как в обычном доме, только собрания жильцов короче."
        )
        await go_point9(query)

    elif data == "fairy_dog":
        await query.message.reply_text(
            "Нет, здесь более серьёзная управляющая компания. Присмотритесь к окошку ещё раз."
        )
        await ask_again(query, data)

    elif data == "two_mice":
        await query.message.reply_text(
            "Мыши решили остаться за кулисами. Присмотритесь ещё раз: один жилец явно сказочный, второй куда более пушистый."
        )
        await ask_again(query, data)

    elif data == "cross":
        await query.message.reply_text(
            "Верно! Дверца скрылась, но внимательность осталась. Направляемся к трём подозрительно ровным линиям."
        )
        await go_point9(query)

    elif data == "ship":
        await query.message.reply_text(
            "Кораблик будет на Адмиралтействе. Не торопите финал."
        )
        await ask_again(query, data)

    elif data == "owl":
        await query.message.reply_text(
            "Совы остались на крыше городских учреждений. Эта сопровождает нас мысленно."
        )
        await ask_again(query, data)

    # -------- Точка 9 --------

    elif data == "point9_arrived":
        await point9(query)

    elif data == "trident_goroh":
        await query.message.reply_text(
            "Верно! Гороховая и есть центральный луч. Тайный знак прочитан, иллюминаты поставлены в известность. 🔱"
        )
        await finish(query)

    elif data == "trident_nevsky":
        await query.message.reply_text(
            "Невский решил перетянуть всё внимание на себя, но он боковой луч. Центр ищите прямо перед собой."
        )
        await ask_again(query, data)

    elif data == "trident_vos":
        await query.message.reply_text(
            "Вознесенский тоже участвует в заговоре, но держится сбоку. А центральный луч другой."
        )
        await ask_again(query, data)

    # -------- ОЦЕНКА --------

    elif data.startswith("rating"):
        messages = {
            "rating1": "Одна звезда. Хорошо, что львы на мосту не умеют ставить оценки.",
            "rating2": "Две звезды. Бот задумался и ушёл советоваться с совами.",
            "rating3": "Три звезды, как три луча Адмиралтейства. Совпадение? Неофициально мы всё поняли.",
            "rating4": "Четыре. Отлично. Пятую, видимо, унесли иллюминаты.",
            "rating5": "Пять звёзд! Кажется, масоны всё-таки одобрили маршрут. Спасибо! 🔱",
        }

        await query.message.reply_text(
            messages[data] +
            "\n\nСпасибо! Ваш ответ поможет сделать маршрут лучше и смешнее."
        )

        await notify_admins(context, query, int(data[-1]))

        await query.message.reply_text(
            "До новых городских заговоров! 👋"
        )


# Какая кнопка к какому вопросу относится. Нужно, если бота перезапустили
# и память о последнем вопросе потерялась: вопрос восстановим по нажатию.
RETRY_BY_DATA = {
    "yellow": yard_open,
    "green": yard_open,
    "grey": yard_open,
    "house2": yard_closed,
    "house6": yard_closed,
    "house8": yard_closed,
    "house_unknown": yard_closed,
    "five_3": point2,
    "five_4": point2,
    "five_5": point2,
    "metro_sennaya": point3,
    "metro_sadovaya": point3,
    "metro_spasskaya": point3,
    "metro_sportivnaya": point3,
    "owls2": point4,
    "owls3": point4,
    "owls4": point4,
    "bridges6": point5,
    "bridges7": point5,
    "bridges8": point5,
    "bridges_unknown": point5,
    "lions2": point6,
    "lions4": point6,
    "lions6": point6,
    "flood1824": point7,
    "flood1924": point7,
    "flood1967": point7,
    "gnome_cat": door_found,
    "fairy_dog": door_found,
    "two_mice": door_found,
    "cross": door_gone,
    "ship": door_gone,
    "owl": door_gone,
    "trident_nevsky": point9,
    "trident_goroh": point9,
    "trident_vos": point9,
}


# ============================================================
# ЗАПУСК
# ============================================================

async def post_init(application):
    """Меню команд в кнопке «/» у клиента Telegram."""
    await application.bot.set_my_commands([
        BotCommand("start", "Начать квест заново"),
        BotCommand("help", "Как играть"),
    ])


def build_telegram_app():
    application = (
        Application.builder().token(TOKEN).post_init(post_init).build()
    )
    application.add_handler(CommandHandler(["start", "restart"], start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("id", whoami))
    application.add_handler(CommandHandler("checkadmin", check_admin))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)
    )
    return application


def run_bot():
    # На Python 3.14 asyncio больше не создаёт цикл событий сам,
    # и python-telegram-bot падает с «There is no current event loop».
    # Создаём цикл вручную, чтобы работать на любой версии Python.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    application = build_telegram_app()
    log.info("Бот запущен на Python %s, event loop %s", platform.python_version(), loop)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        # не разгребает старое, если бот лежал: иначе после пробуждения
        # прилетает пачка старых нажатий
        drop_pending_updates=True,
    )


def keep_alive():
    """
    Раз в 10 минут дёргаем свой же публичный адрес.
    Render усыпляет бесплатный сервис после 15 минут без входящих
    запросов, вот эти запросы его и бодрят.
    """
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        log.info("RENDER_EXTERNAL_URL не задан, авто-пинг выключен")
        return

    target = url.rstrip("/") + "/health"
    log.info("Авто-пинг включён: %s", target)

    while True:
        try:
            with urllib.request.urlopen(target, timeout=30) as resp:
                log.info("Авто-пинг: %s", resp.status)
        except Exception as exc:  # noqa: BLE001
            log.warning("Авто-пинг не удался: %s", exc)
        time.sleep(600)


def main():
    log.info("Старт бота, файл %s", __file__)

    # Веб-сервер запускаем отдельным потоком
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    run_bot()


if __name__ == "__main__":
    main()
