
import logging
import os
import asyncio
import html
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

from telethon import TelegramClient, errors
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.types import MessageMediaWebPage

########################################
# Logging
########################################
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

########################################
# Konstanten / Globale Variablen
########################################
BOT_TOKEN = "7307210442:AAHHytxCGz1k_mRU0oAFCA7gUY_Q-w1aqr4"   # <-- Ersetze dies mit deinem echten Bot-Token
USER_STATE = {}               # user_id -> Zustand
USER_TAGGER_TASKS = {}        # user_id -> asyncio.Task

# Mögliche Zustände: 
#  "MAIN_MENU", "ENTER_API_ID", "ENTER_API_HASH",
#  "ENTER_PHONE", "WAITING_CODE", "WAITING_PASSWORD", 
#  "AUTHORIZED", "WAITING_INTERVAL"

########################################
# Keyboards
########################################
def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Weiter ▶️", callback_data="continue")]
    ])

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Konto verbinden 🔑", callback_data="connect_account")],
        [
            InlineKeyboardButton("Tagger starten 🚀", callback_data="launch_tagger"),
            InlineKeyboardButton("Tagger stoppen 🛑", callback_data="stop_tagger")
        ],
        [InlineKeyboardButton("Anleitung 📚", callback_data="instructions")],
    ])

def digit_keyboard(current_code=""):
    kb = [
        [
            InlineKeyboardButton("1", callback_data="digit_1"),
            InlineKeyboardButton("2", callback_data="digit_2"),
            InlineKeyboardButton("3", callback_data="digit_3")
        ],
        [
            InlineKeyboardButton("4", callback_data="digit_4"),
            InlineKeyboardButton("5", callback_data="digit_5"),
            InlineKeyboardButton("6", callback_data="digit_6")
        ],
        [
            InlineKeyboardButton("7", callback_data="digit_7"),
            InlineKeyboardButton("8", callback_data="digit_8"),
            InlineKeyboardButton("9", callback_data="digit_9")
        ],
        [
            InlineKeyboardButton("0", callback_data="digit_0"),
            InlineKeyboardButton("Löschen ⬅️", callback_data="digit_del"),
            InlineKeyboardButton("Senden ✅", callback_data="digit_submit")
        ]
    ]
    return InlineKeyboardMarkup(kb)

########################################
# /start
########################################
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    USER_STATE[user_id] = "MAIN_MENU"
    await update.message.reply_text(
        "Hallo! Drücke bitte 'Weiter', um das Menü zu öffnen.",
        reply_markup=start_keyboard()
    )

########################################
# Hilfsfunktion:
#  hole die letzten 4 «элемента» из 'Saved Messages':
#  • одиночное сообщение (без grouped_id)
#  • альбом (grouped_id)
########################################
async def get_last_4_items(client: TelegramClient):
    """
    Возвращает список "элементов" (каждый элемент — это list из 1 или нескольких сообщений с одинаковым grouped_id).
    Берём limit=20 последних сообщений из «Избранного» — чтобы наверняка нашлось 4 альбома/сообщения.
    Склеиваем одинаковые grouped_id в один элемент.
    Возвращаем максимум 4 последних (по хронологии).
    Порядок: самый новый элемент = первый, самый старый = последний в списке.
    """
    saved_entity = await client.get_entity("me")
    # Берём за раз 20 последних сообщений — это запас, чтобы точно выделить 4 "единицы" контента
    raw_msgs = await client.get_messages(saved_entity, limit=20)

    if not raw_msgs:
        return []

    # Идём с новейших к старым
    used_group_ids = set()
    items = []  # список элементов, где каждый элемент = [msg1, msg2, ...] (одиночка или альбом)

    for msg in raw_msgs:
        if msg.grouped_id:
            # Проверяем, не обрабатывали ли мы уже этот grouped_id
            if msg.grouped_id not in used_group_ids:
                # Собираем все сообщения этого альбома
                album_parts = [m for m in raw_msgs if m.grouped_id == msg.grouped_id]
                # Sort in ascending order (so inside the album, messages are in ascending id order)
                album_parts.sort(key=lambda x: x.id)
                items.append(album_parts)
                used_group_ids.add(msg.grouped_id)
        else:
            # Одиночное сообщение
            # Проверим, что нет grouped_id und wir haben es nicht bereits
            items.append([msg])

        # Если уже набралось 4 элемента, можно прерваться
        if len(items) >= 4:
            break

    # items сейчас в порядке «sowohl die neuesten Elemente zuerst»
    return items

########################################
# Tagger-Funktion (по циклу 4 «элементов»), forward
########################################
async def run_tagger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Diese Funktion holt sich bis zu 4 "элемента" из 'Saved Messages' 
    (einzeln oder альбом) und forwardet sie по циклу (1->2->3->4->1->...) 
    in alle Gruppen, in denen der Benutzer Mitglied ist.
    """
    user_id = update.effective_user.id
    client = context.user_data.get('client')
    if not client:
        await update.effective_message.reply_text("Bitte zuerst das Konto verbinden! ❗")
        return

    interval = context.user_data.get('interval', 60.0)

    stop_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Tagger stoppen 🛑", callback_data="stop_tagger")]
    ])

    await update.effective_message.reply_text(
        f"🚀 Der Tagger wurde gestartet! Es werden nun reihum bis zu 4 letzte Nachrichten/Alben aus 'Gespeicherte Nachrichten' **weitergeleitet** (alle {interval} Sek.).",
        reply_markup=stop_keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

    # Берём список «элементов» (до 4). Jeder Element = список сообщений (einzeln oder альбом).
    items = await get_last_4_items(client)
    if not items:
        logger.info("Keine Nachrichten/Alben in den gespeicherten Nachrichten gefunden.")
        await update.effective_message.reply_text(
            "Keine Nachrichten/Alben in 'Gespeicherte Nachrichten' gefunden. Tagger wird beendet."
        )
        return

    total_count = len(items)
    current_index = 0

    try:
        while True:
            try:
                current_item = items[current_index]  # список сообщений (1 или несколько)
                current_index = (current_index + 1) % total_count

                # Alle Gruppen ermitteln
                dialogs = await client.get_dialogs(limit=100)
                target_chats = [d for d in dialogs if d.is_group]
                if not target_chats:
                    logger.info("Keine Gruppen gefunden, um Nachrichten zu senden.")
                    await asyncio.sleep(interval)
                    continue

                for chat in target_chats:
                    try:
                        # Пересылаем. Если несколько сообщений (альбом) — одним вызовом
                        msg_ids = [m.id for m in current_item]
                        await client.forward_messages(
                            chat,
                            msg_ids,
                            from_peer="me",  # или saved_entity
                        )
                        logger.info(f"Element (Album oder Nachricht) an {chat.name} forwarded.")
                    except FloodWaitError as e:
                        logger.warning(f"FloodWaitError beim Senden an {chat.name}: {e.seconds} Sek. Warte, dann skip.")
                        continue
                    except SessionPasswordNeededError:
                        logger.error("2FA benötigt!")
                        await update.effective_message.reply_text(
                            "Zwei-Faktor-Authentifizierung aktiviert. Bitte Passwort eingeben."
                        )
                        USER_STATE[user_id] = "ENTER_PASSWORD"
                        return
                    except Exception as e:
                        logger.error(f"Unerwarteter Fehler in Gruppe {chat.name}: {e}")
                        continue

                logger.info(f"Warten {interval} Sek. vor dem nächsten Senden...")
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                logger.info(f"Tagger für Benutzer {user_id} wurde gestoppt.")
                break
            except Exception as e:
                logger.error(f"Fehler in der Hauptschleife des Taggers: {e}")
                await asyncio.sleep(interval)

    finally:
        # Beim Abbrechen -> disconnect
        await client.disconnect()
        USER_TAGGER_TASKS.pop(user_id, None)
        USER_STATE[user_id] = "MAIN_MENU"
        await update.effective_message.reply_text(
            "🛑 Der Tagger wurde gestoppt.",
            reply_markup=main_menu_keyboard()
        )

########################################
# Callback-Handler für Buttons
########################################
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    await query.answer()

    if data == "continue":
        USER_STATE[user_id] = "MAIN_MENU"
        await query.edit_message_text("Hauptmenü:", reply_markup=main_menu_keyboard())

    elif data == "connect_account":
        USER_STATE[user_id] = "ENTER_API_ID"
        await query.edit_message_text(
            "Bitte geben Sie Ihre API ID ein (Zahl):"
        )

    elif data == "launch_tagger":
        # Nur wenn bereits AUTHORIZED
        if USER_STATE.get(user_id) == "AUTHORIZED":
            USER_STATE[user_id] = "WAITING_INTERVAL"
            await query.edit_message_text(
                "Bitte geben Sie das Weiterleitungs-Intervall in Sek. ein (z.B. 60):"
            )
        else:
            await query.edit_message_text(
                "Bitte zuerst ein Konto verbinden. ❗",
                reply_markup=main_menu_keyboard()
            )

    elif data == "stop_tagger":
        task = USER_TAGGER_TASKS.get(user_id)
        if task and not task.done():
            task.cancel()
        else:
            await query.edit_message_text(
                "Der Tagger läuft nicht.",
                reply_markup=main_menu_keyboard()
            )

    elif data == "instructions":
        await query.edit_message_text(
            "📚 Anleitung:\n"
            "- Konto verbinden (API ID, API Hash, Telefonnummer eingeben)\n"
            "- Tagger starten: Intervall angeben\n"
            "Der Bot nimmt dann reihum bis zu 4 letzte Nachrichten/Alben aus 'Gespeicherte Nachrichten' und leitet sie in deine Gruppen weiter. 💫",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard()
        )

    # Handling für Digit-Tasten
    elif data.startswith("digit_"):
        action = data.split("_")[1]
        current_code = context.user_data.get('code', '')

        if action.isdigit():
            if len(current_code) < 6:  # Telegram-Codes sind normalerweise 5-6 Ziffern
                current_code += action
                context.user_data['code'] = current_code
            else:
                await query.answer("Maximale Code-Länge erreicht.", show_alert=True)
        elif action == "del":
            current_code = current_code[:-1]
            context.user_data['code'] = current_code
        elif action == "submit":
            await confirm_code(update, context)
            return  # confirm_code wird die Nachricht bearbeiten

        # Aktualisiere die Nachricht, um den aktuellen Code-Status anzuzeigen
        masked_code = '*' * len(current_code) + '_' * (6 - len(current_code))
        await query.edit_message_text(
            f"Code: {masked_code}",
            reply_markup=digit_keyboard(current_code)
        )

########################################
# Text-Handler
########################################
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = USER_STATE.get(user_id, "")

    # API ID
    if state == "ENTER_API_ID":
        if not update.message.text.strip().isdigit():
            await update.message.reply_text("Bitte eine gültige API ID (Zahl) eingeben:")
            return
        context.user_data['api_id'] = int(update.message.text.strip())
        USER_STATE[user_id] = "ENTER_API_HASH"
        await update.message.reply_text("Perfekt! Bitte jetzt Ihren API Hash eingeben:")
        return

    # API Hash
    if state == "ENTER_API_HASH":
        context.user_data['api_hash'] = update.message.text.strip()
        USER_STATE[user_id] = "ENTER_PHONE"
        await update.message.reply_text("Gut! Bitte geben Sie Ihre Telefonnummer im Format +49... ein:")
        return

    # Telefonnummer
    if state == "ENTER_PHONE":
        phone_number = update.message.text.strip()
        if not phone_number.startswith('+') or not phone_number[1:].isdigit():
            await update.message.reply_text("Bitte eine gültige Telefonnummer im Format +49... eingeben:")
            return
        context.user_data['phone_number'] = phone_number
        USER_STATE[user_id] = "WAITING_CODE"
        await update.message.reply_text("Fordere Code bei Telegram an...")
        await create_telethon_client(update, context)
        return

    # Intervall
    if state == "WAITING_INTERVAL":
        user_input = update.message.text.strip()
        try:
            interval_value = float(user_input)
            if interval_value <= 0:
                raise ValueError("Intervall muss positiv sein.")
            context.user_data['interval'] = interval_value
            await update.message.reply_text(
                f"✅ Intervall eingestellt: {interval_value} Sek.\nStarte jetzt die Weiterleitung..."
            )
            USER_STATE[user_id] = "AUTHORIZED"
            task = asyncio.create_task(run_tagger(update, context))
            USER_TAGGER_TASKS[user_id] = task

        except ValueError:
            await update.message.reply_text(
                "Bitte eine positive Zahl eingeben. Beispiel: 60"
            )
        return

    # Passwort (Zwei-Faktor)
    if state == "WAITING_PASSWORD":
        password = update.message.text.strip()
        client = context.user_data.get('client')
        if not client:
            await update.message.reply_text("Kein Client vorhanden. Bitte erneut anfangen.")
            return
        try:
            await client.sign_in(password=password)
            USER_STATE[user_id] = "AUTHORIZED"
            await update.message.reply_text(
                "✔️ Authentifizierung erfolgreich! Starten Sie nun den Tagger.",
                reply_markup=main_menu_keyboard()
            )
        except errors.PasswordHashInvalidError:
            await update.message.reply_text("Falsches Passwort. Bitte erneut eingeben.")
        except FloodWaitError as e:
            logger.warning(f"FloodWaitError beim Passwort: {e.seconds} Sek.")
            await update.message.reply_text(
                f"Zu viele Versuche. Bitte {e.seconds} Sek. warten."
            )
            USER_STATE[user_id] = "MAIN_MENU"
        except Exception as e:
            await update.message.reply_text(f"Fehler beim Passwort: {e}")
        return

    # Wenn nichts von oben zutrifft
    await update.message.reply_text(
        "Bitte benutzen Sie das Menü oder warten Sie auf die passende Eingabe."
    )

########################################
# Codebestätigung (bei Ziffern-Buttons)
########################################
async def confirm_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diese Funktion wird aufgerufen, wenn die digitale Code-Eingabe beendet wurde."""
    user_id = update.effective_user.id
    code = context.user_data.get('code', '')

    if not code:
        await update.effective_message.reply_text("Der Code ist leer. Bitte erneut eingeben!")
        return

    client = context.user_data.get('client')
    if not client:
        await update.effective_message.reply_text("Kein Client vorhanden. Bitte neu starten.")
        return

    try:
        await client.sign_in(context.user_data['phone_number'], code)
    except SessionPasswordNeededError:
        await update.effective_message.reply_text("2FA aktiviert. Bitte Passwort eingeben.")
        USER_STATE[user_id] = "WAITING_PASSWORD"
        return
    except FloodWaitError as e:
        logger.warning(f"FloodWaitError beim Anmelden: {e.seconds} Sekunden.")
        await update.effective_message.reply_text(f"Zu viele Versuche. Bitte {e.seconds} Sek. warten.")
        return
    except errors.PhoneCodeInvalidError:
        await update.effective_message.reply_text("Der Code ist ungültig. Nochmal eingeben!")
        context.user_data['code'] = ""
        await update.effective_message.reply_text(
            "Bitte Code aus Telegram eingeben:",
            reply_markup=digit_keyboard()
        )
        USER_STATE[user_id] = "WAITING_CODE"
        return
    except Exception as e:
        await update.effective_message.reply_text(f"Fehler bei der Code-Eingabe: {e}")
        return

    USER_STATE[user_id] = "AUTHORIZED"
    await update.effective_message.reply_text(
        "✔️ Authentifizierung erfolgreich! Sie können nun den Tagger starten.",
        reply_markup=main_menu_keyboard()
    )

########################################
# Telethon-Client erstellen (Code anfordern)
########################################
async def create_telethon_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api_id = context.user_data.get('api_id')
    api_hash = context.user_data.get('api_hash')
    phone_number = context.user_data.get('phone_number')

    if not api_id or not api_hash or not phone_number:
        await update.message.reply_text("Fehlende API-Daten. Bitte den Vorgang neu starten.")
        USER_STATE.pop(user_id, None)
        return

    session_name = f"session_{user_id}"
    
    if 'client' in context.user_data:
        client = context.user_data['client']
        if not client.is_connected():
            await client.connect()
    else:
        client = TelegramClient(session_name, api_id, api_hash)
        context.user_data['client'] = client
        await client.connect()

    try:
        is_authorized = await client.is_user_authorized()
        if not is_authorized:
            try:
                await client.send_code_request(phone_number)
                context.user_data['code'] = ""
                USER_STATE[user_id] = "WAITING_CODE"
                await update.message.reply_text(
                    "Bitte Code aus Telegram eingeben:",
                    reply_markup=digit_keyboard()
                )
            except FloodWaitError as e:
                logger.warning(f"FloodWaitError bei Code-Anforderung: {e.seconds} Sekunden.")
                await update.message.reply_text(f"Zu viele Versuche. Bitte {e.seconds} Sek. warten.")
                USER_STATE.pop(user_id, None)
            except Exception as e:
                await update.message.reply_text(f"Fehler bei der Code-Anforderung: {e}")
                USER_STATE.pop(user_id, None)
        else:
            USER_STATE[user_id] = "AUTHORIZED"
            await update.message.reply_text(
                "✔️ Bereits angemeldet! Sie können jetzt den Tagger starten.",
                reply_markup=main_menu_keyboard()
            )
    except FloodWaitError as e:
        logger.warning(f"FloodWaitError beim Verbinden: {e.seconds} Sekunden.")
        await update.message.reply_text(f"Zu viele Versuche. Bitte {e.seconds} Sek. warten.")
        USER_STATE.pop(user_id, None)
    except Exception as e:
        await update.message.reply_text(f"Fehler beim Verbinden: {e}")
        USER_STATE.pop(user_id, None)

########################################
# Main
########################################
if __name__ == "__main__":
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    application.run_polling()

