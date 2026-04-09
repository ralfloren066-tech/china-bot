import os
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, date

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
import gspread
from google.oauth2.service_account import Credentials

from parser import parse_product

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Env ───────────────────────────────────────────────────────────────────────
TOKEN       = os.environ["TELEGRAM_TOKEN"]
SHEET_ID    = os.environ["SHEET_ID"]
CREDS_JSON  = os.environ["GOOGLE_CREDS_JSON"]
ALLOWED_UID = int(os.environ.get("ALLOWED_USER_ID", "0"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── URL extraction ────────────────────────────────────────────────────────────
URL_DOMAINS = [
    "dewu.com", "poizon.com", "du.com",
    "taobao.com", "tmall.com",
    "1688.com",
    "95fen.com", "95.com",
]

def extract_url(update: Update) -> str | None:
    """Достаём URL из сообщения тремя способами."""
    msg = update.message

    # 1. Telegram entities — самый надёжный способ
    if msg.entities:
        for entity in msg.entities:
            if entity.type == "text_link" and entity.url:
                return entity.url.strip()
            if entity.type == "url":
                url = msg.text[entity.offset: entity.offset + entity.length]
                if url:
                    if not url.startswith("http"):
                        url = "https://" + url
                    return url.strip()

    text = (msg.text or "").strip()

    # 2. http/https в тексте
    m = re.search(r'https?://\S+', text)
    if m:
        return m.group(0).rstrip(".,)")

    # 3. Голый домен без схемы
    for domain in URL_DOMAINS:
        if domain in text.lower():
            m = re.search(r'(?:https?://)?' + re.escape(domain) + r'\S*', text, re.I)
            if m:
                url = m.group(0).rstrip(".,)")
                if not url.startswith("http"):
                    url = "https://" + url
                return url

    return None

# ── Google Sheets ─────────────────────────────────────────────────────────────
HEADERS = ["№", "Дата", "Источник", "Ссылка", "Название",
           "Цена (¥)", "Курс ¥→₽", "Цена (₽)", "Кол-во", "Размер", "Итого (₽)"]

def _get_ws():
    import json
    creds = Credentials.from_service_account_info(json.loads(CREDS_JSON), scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Товары")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Товары", rows=1000, cols=len(HEADERS))
        _init_sheet(ws)
    return ws

def _init_sheet(ws):
    ws.update("A1:K1", [HEADERS])
    ws.format("A1:K1", {
        "backgroundColor": {"red": 0.102, "green": 0.102, "blue": 0.102},
        "textFormat": {"bold": True,
                       "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                       "fontSize": 11},
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
    })
    ws.freeze(rows=1)

def sheet_append(data: dict, rate: float) -> int:
    ws = _get_ws()
    num = len(ws.get_all_values())
    price     = data["price"]
    qty       = data["qty"]
    price_rub = round(price * rate, 2)
    total_rub = round(price_rub * qty, 2)
    row = [
        num,
        datetime.now().strftime("%d.%m.%Y  %H:%M"),
        data.get("source", ""),
        data["url"],
        data.get("name", ""),
        price,
        rate,
        price_rub,
        qty,
        data.get("size", "—"),
        total_rub,
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return num

def sheet_last(n=5):
    ws = _get_ws()
    rows = ws.get_all_values()
    return rows[1:][-n:] if len(rows) > 1 else []

# ── Курс ЦБ РФ ────────────────────────────────────────────────────────────────
async def fetch_cbr_rate() -> float:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://www.cbr.ru/scripts/XML_daily.asp",
                            headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.text)
        for v in root.findall("Valute"):
            if v.find("CharCode").text == "CNY":
                val     = float(v.find("Value").text.replace(",", "."))
                nominal = int(v.find("Nominal").text)
                return round(val / nominal, 4)
    except Exception as e:
        log.warning(f"ЦБ недоступен: {e}")
    return 13.50

async def get_rate(ctx: ContextTypes.DEFAULT_TYPE) -> float:
    if ctx.bot_data.get("manual_rate"):
        return ctx.bot_data["manual_rate"]
    if ctx.bot_data.get("cbr_date") == date.today() and ctx.bot_data.get("cbr_rate"):
        return ctx.bot_data["cbr_rate"]
    rate = await fetch_cbr_rate()
    ctx.bot_data["cbr_rate"] = rate
    ctx.bot_data["cbr_date"] = date.today()
    return rate

# ── Auth ───────────────────────────────────────────────────────────────────────
def allowed(uid: int) -> bool:
    if ALLOWED_UID == 0:
        return True
    result = uid == ALLOWED_UID
    if not result:
        log.warning(f"Отклонён uid={uid}, разрешён только uid={ALLOWED_UID}")
    return result

# ── Состояния диалога ─────────────────────────────────────────────────────────
STATES: dict[int, dict] = {}

def st_get(uid):    return STATES.get(uid, {})
def st_set(uid, d): STATES[uid] = d
def st_del(uid):    STATES.pop(uid, None)

# ── Keyboards ─────────────────────────────────────────────────────────────────
def kb_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Сохранить в таблицу", callback_data="save")],
        [
            InlineKeyboardButton("✏️ Цена",   callback_data="edit_price"),
            InlineKeyboardButton("✏️ Кол-во", callback_data="edit_qty"),
            InlineKeyboardButton("✏️ Размер", callback_data="edit_size"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])

def kb_skip_size():
    return InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Пропустить", callback_data="skip_size")]])

# ── Карточка подтверждения ─────────────────────────────────────────────────────
def confirm_text(st: dict) -> str:
    rate      = st["rate"]
    price     = st["price"]
    qty       = st["qty"]
    size      = st.get("size") or "—"
    price_rub = round(price * rate, 2)
    total_rub = round(price_rub * qty, 2)
    name      = st.get("name") or "—"
    source    = st.get("source") or "—"
    short_url = st["url"][:55] + "…" if len(st["url"]) > 55 else st["url"]
    return (
        f"📋 *Проверь данные перед сохранением:*\n\n"
        f"🏪 Источник: *{source}*\n"
        f"📦 {name}\n"
        f"🔗 {short_url}\n\n"
        f"💰 Цена: *{price} ¥* = {price_rub} ₽\n"
        f"💱 Курс: {rate} ₽/¥\n"
        f"📦 Кол-во: *{qty} шт.*\n"
        f"📐 Размер: *{size}*\n"
        f"💵 Итого: *{total_rub} ₽*"
    )

# ── Команды ───────────────────────────────────────────────────────────────────
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(u.effective_user.id): return
    await u.message.reply_text(
        "👟 *Poizon Tracker Bot*\n\n"
        "Отправь ссылку на товар — бот попробует автоматически вытащить название и цену.\n"
        "Работает с: *Poizon, Taobao, 1688, 95*\n\n"
        "Команды:\n"
        "/rate — текущий курс ¥→₽\n"
        "/setrate 14.20 — задать свой курс\n"
        "/cbr — вернуть курс ЦБ РФ\n"
        "/list — последние 5 товаров\n"
        "/whoami — проверить доступ\n"
        "/cancel — отменить текущий ввод",
        parse_mode="Markdown",
    )

async def cmd_whoami(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    await u.message.reply_text(
        f"🪪 Твой Telegram ID: `{uid}`\n"
        f"ALLOWED\\_USER\\_ID в боте: `{ALLOWED_UID}`\n"
        f"Доступ: {'✅ разрешён' if allowed(uid) else '❌ запрещён — ID не совпадает'}",
        parse_mode="Markdown"
    )

async def cmd_rate(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(u.effective_user.id): return
    rate   = await get_rate(ctx)
    source = "задан вручную ✏️" if ctx.bot_data.get("manual_rate") else "ЦБ РФ 🏦"
    await u.message.reply_text(f"💱 *1 ¥ = {rate} ₽*\n_Источник: {source}_", parse_mode="Markdown")

async def cmd_setrate(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(u.effective_user.id): return
    try:
        rate = float(ctx.args[0].replace(",", "."))
        ctx.bot_data["manual_rate"] = rate
        await u.message.reply_text(f"✅ Курс: *1 ¥ = {rate} ₽*", parse_mode="Markdown")
    except (IndexError, ValueError):
        await u.message.reply_text("❌ Пример: /setrate 14.20")

async def cmd_cbr(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(u.effective_user.id): return
    ctx.bot_data.pop("manual_rate", None)
    ctx.bot_data.pop("cbr_rate", None)
    rate = await get_rate(ctx)
    await u.message.reply_text(f"🏦 Курс ЦБ РФ: *1 ¥ = {rate} ₽*", parse_mode="Markdown")

async def cmd_cancel(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(u.effective_user.id): return
    st_del(u.effective_user.id)
    await u.message.reply_text("❌ Отменено. Отправь новую ссылку.")

async def cmd_list(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(u.effective_user.id): return
    try:
        rows = sheet_last(5)
    except Exception as e:
        await u.message.reply_text(f"❌ Ошибка: {e}"); return
    if not rows:
        await u.message.reply_text("📭 Таблица пуста."); return
    lines = ["📋 *Последние товары:*\n"]
    for r in reversed(rows):
        num  = r[0]  if len(r) > 0  else "?"
        dt   = r[1]  if len(r) > 1  else ""
        src  = r[2]  if len(r) > 2  else ""
        name = r[4]  if len(r) > 4  else "—"
        prub = r[7]  if len(r) > 7  else "?"
        qty  = r[8]  if len(r) > 8  else "?"
        size = r[9]  if len(r) > 9  else "?"
        tot  = r[10] if len(r) > 10 else "?"
        name_short = name[:35] + "…" if len(name) > 35 else name
        lines.append(
            f"*#{num}* [{src}] {name_short}\n"
            f"💰 {prub} ₽ × {qty} шт. = *{tot} ₽*  📐 {size}\n"
            f"_{dt}_\n"
        )
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Главный обработчик сообщений ──────────────────────────────────────────────
async def on_message(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(u.effective_user.id): return
    uid  = u.effective_user.id
    text = (u.message.text or "").strip()
    st   = st_get(uid)

    # Ждём цену
    if st.get("step") == "price":
        raw = text.replace(",", ".").replace("¥", "").replace(" ", "")
        try:
            price = float(raw)
            if price <= 0: raise ValueError
        except ValueError:
            await u.message.reply_text("❌ Введи число, например *689*", parse_mode="Markdown")
            return
        st["price"] = price
        st["step"]  = "qty"
        st_set(uid, st)
        await u.message.reply_text("📦 Сколько штук? (например *1*)", parse_mode="Markdown")
        return

    # Ждём количество
    if st.get("step") == "qty":
        try:
            qty = int(text)
            if qty <= 0: raise ValueError
        except ValueError:
            await u.message.reply_text("❌ Введи целое число, например *1*", parse_mode="Markdown")
            return
        st["qty"]  = qty
        st["step"] = "size"
        st_set(uid, st)
        await u.message.reply_text(
            "📐 Введи размер (например: *42*, *L*, *One Size*)",
            parse_mode="Markdown",
            reply_markup=kb_skip_size(),
        )
        return

    # Ждём размер
    if st.get("step") == "size":
        st["size"] = text
        st["step"] = "confirm"
        st_set(uid, st)
        await u.message.reply_text(confirm_text(st), parse_mode="Markdown", reply_markup=kb_confirm())
        return

    # Новая ссылка
    url = extract_url(u)
    if url:
        rate = await get_rate(ctx)
        st_set(uid, {"step": "parsing", "url": url, "rate": rate})
        msg = await u.message.reply_text("🔍 Парсю страницу…")

        try:
            parsed = await parse_product(url)
        except Exception as e:
            parsed = {"name": "", "price": None, "image": "", "source": "?"}
            log.warning(f"parse_product exception: {e}")

        await msg.delete()

        state = {
            "url":    url,
            "rate":   rate,
            "name":   parsed.get("name", ""),
            "source": parsed.get("source", ""),
            "price":  parsed.get("price"),
            "qty":    1,
            "size":   "",
        }

        if parsed.get("price"):
            state["step"] = "qty"
            st_set(uid, state)
            name_str = f"\n📦 _{parsed['name']}_" if parsed["name"] else ""
            await u.message.reply_text(
                f"✅ *{parsed['source']}* — товар найден!{name_str}\n"
                f"💰 Цена: *{parsed['price']} ¥* = {round(parsed['price']*rate, 2)} ₽\n\n"
                f"Если цена неверная — нажми кнопку ниже.\n\n"
                f"📦 Сколько штук?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Изменить цену", callback_data="edit_price_inline")
                ]]),
            )
        else:
            state["step"] = "price"
            st_set(uid, state)
            name_str = f"\n📦 _{parsed['name']}_" if parsed["name"] else ""
            await u.message.reply_text(
                f"🔗 *{parsed['source']}*{name_str}\n\n"
                f"❓ Не удалось автоматически определить цену.\n"
                f"Введи цену в юанях (¥):",
                parse_mode="Markdown",
            )
        return

    await u.message.reply_text(
        "👟 Отправь ссылку на товар чтобы начать.\n"
        "Если ссылка не распознаётся — напиши /whoami для диагностики."
    )

# ── Callback кнопки ───────────────────────────────────────────────────────────
async def on_callback(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = u.callback_query
    uid = q.from_user.id
    await q.answer()
    if not allowed(uid): return

    action = q.data
    st = st_get(uid)

    if action == "save":
        if not st or not st.get("price"):
            await q.edit_message_text("❌ Данные устарели. Отправь ссылку заново."); return
        try:
            num = sheet_append(st, st["rate"])
        except Exception as e:
            await q.edit_message_text(f"❌ Ошибка при сохранении:\n{e}"); return
        st_del(uid)
        p_rub = round(st["price"] * st["rate"], 2)
        total = round(p_rub * st["qty"], 2)
        await q.edit_message_text(
            f"✅ *Товар #{num} сохранён!*\n\n"
            f"🏪 {st.get('source','')}  |  📐 {st.get('size') or '—'}\n"
            f"💰 {st['price']} ¥ × {st['qty']} шт. = *{total} ₽*",
            parse_mode="Markdown",
        )

    elif action == "cancel":
        st_del(uid)
        await q.edit_message_text("❌ Отменено.")

    elif action == "skip_size":
        st["size"] = "—"
        st["step"] = "confirm"
        st_set(uid, st)
        await q.edit_message_text("📐 Размер: *—*", parse_mode="Markdown")
        await ctx.bot.send_message(
            u.effective_chat.id, confirm_text(st),
            parse_mode="Markdown", reply_markup=kb_confirm(),
        )

    elif action in ("edit_price", "edit_price_inline"):
        st["step"] = "price"
        st_set(uid, st)
        await q.edit_message_text("✏️ Введи цену в юанях (¥):")

    elif action == "edit_qty":
        st["step"] = "qty"
        st_set(uid, st)
        await q.edit_message_text("✏️ Введи количество:")

    elif action == "edit_size":
        st["step"] = "size"
        st_set(uid, st)
        await q.edit_message_text("✏️ Введи размер:")

# ── Запуск ────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("whoami",  cmd_whoami))
    app.add_handler(CommandHandler("rate",    cmd_rate))
    app.add_handler(CommandHandler("setrate", cmd_setrate))
    app.add_handler(CommandHandler("cbr",     cmd_cbr))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("list",    cmd_list))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
