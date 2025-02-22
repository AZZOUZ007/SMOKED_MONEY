import logging
import sqlite3
from datetime import datetime, timedelta, date

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)

##############################################################################
#                             LOGGING SETUP                                  #
##############################################################################
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

##############################################################################
#                              DATABASE SETUP                                #
##############################################################################
db = sqlite3.connect("cigarettes.db", check_same_thread=False)
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    unit_price REAL,
    stock INTEGER,
    dashboard_message_id INTEGER,
    start_smoking_date TEXT
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    timestamp TEXT,
    quantity INTEGER,
    cost REAL
)
""")
db.commit()

# If 'start_smoking_date' column doesn't exist (older bots), try to add it:
try:
    cursor.execute("ALTER TABLE users ADD COLUMN start_smoking_date TEXT")
    db.commit()
except sqlite3.OperationalError:
    pass

##############################################################################
#                           CONVERSATION STATES                              #
##############################################################################
ASKING_UNIT_PRICE = 1
ASKING_START_DATE = 2

##############################################################################
#                          HELPER: GET CHAT & MSG ID                         #
##############################################################################
def get_chat_id(update: Update) -> int:
    """
    Returns the chat_id from either an Update with a message or a callback_query.
    """
    if update.message:
        return update.message.chat_id
    elif update.callback_query and update.callback_query.message:
        return update.callback_query.message.chat_id
    else:
        # fallback
        return update.effective_chat.id

def get_message_id(update: Update) -> int:
    """
    Returns the message_id if present (mostly relevant for callback_query).
    """
    if update.message:
        return update.message.message_id
    elif update.callback_query and update.callback_query.message:
        return update.callback_query.message.message_id
    else:
        return 0

##############################################################################
#                            DATABASE HELPERS                                #
##############################################################################
def get_user(user_id: int):
    """
    Return (unit_price, stock, dashboard_message_id, start_smoking_date) or None.
    """
    cursor.execute("""
        SELECT unit_price, stock, dashboard_message_id, start_smoking_date
          FROM users
         WHERE user_id = ?
    """, (user_id,))
    row = cursor.fetchone()
    return row if row else None

def create_user(user_id: int, unit_price: float):
    """
    Insert new user with stock=0, no dashboard_message_id, no start_smoking_date.
    """
    cursor.execute("""
        INSERT INTO users (user_id, unit_price, stock, dashboard_message_id, start_smoking_date)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, unit_price, 0, None, None))
    db.commit()

def update_dashboard_message_id(user_id: int, message_id: int):
    cursor.execute("""
        UPDATE users SET dashboard_message_id = ? WHERE user_id = ?
    """, (message_id, user_id))
    db.commit()

def update_user_price(user_id: int, new_price: float):
    cursor.execute("""
        UPDATE users SET unit_price = ? WHERE user_id = ?
    """, (new_price, user_id))
    db.commit()

def update_user_stock(user_id: int, new_stock: int):
    cursor.execute("""
        UPDATE users SET stock = ? WHERE user_id = ?
    """, (new_stock, user_id))
    db.commit()

def update_user_start_date(user_id: int, start_date_str: str):
    cursor.execute("""
        UPDATE users SET start_smoking_date = ? WHERE user_id = ?
    """, (start_date_str, user_id))
    db.commit()

def log_smoke_event(user_id: int, quantity: int, cost: float):
    timestamp = datetime.now().isoformat()
    cursor.execute("""
        INSERT INTO usage (user_id, timestamp, quantity, cost)
        VALUES (?, ?, ?, ?)
    """, (user_id, timestamp, quantity, cost))
    db.commit()

##############################################################################
#                           AGGREGATION HELPERS                              #
##############################################################################
def get_aggregates(user_id: int):
    """
    Returns a dict with daily, weekly, monthly, yearly usage & cost:
      {
        "daily": (qty, cost),
        "weekly": (qty, cost),
        "monthly": (qty, cost),
        "yearly": (qty, cost)
      }
    """
    now = datetime.now()
    daily_start   = now.replace(hour=0, minute=0, second=0, microsecond=0)
    weekly_start  = now - timedelta(days=7)
    monthly_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    yearly_start  = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    return {
        "daily":   _aggregate_usage(user_id, daily_start, now),
        "weekly":  _aggregate_usage(user_id, weekly_start, now),
        "monthly": _aggregate_usage(user_id, monthly_start, now),
        "yearly":  _aggregate_usage(user_id, yearly_start, now),
    }

def _aggregate_usage(user_id: int, start_dt: datetime, end_dt: datetime):
    start_iso = start_dt.isoformat()
    end_iso   = end_dt.isoformat()
    cursor.execute("""
        SELECT IFNULL(SUM(quantity), 0), IFNULL(SUM(cost), 0)
          FROM usage
         WHERE user_id = ?
           AND timestamp BETWEEN ? AND ?
    """, (user_id, start_iso, end_iso))
    row = cursor.fetchone()
    if row:
        return (int(row[0]), float(row[1]))
    return (0, 0.0)

def calculate_forecast(daily_cost: float):
    """
    If user spent X cost so far *today*, project cost for the rest of the year
    at the same daily rate.
    """
    if daily_cost <= 0:
        return 0.0
    today = date.today()
    end_of_year = date(today.year, 12, 31)
    remaining_days = (end_of_year - today).days
    return daily_cost * remaining_days

def theoretical_spent_since_start(user_id: int, start_date_str: str) -> float:
    """
    Theoretical total = (today's daily cost) * (days since start_date).
    """
    if not start_date_str:
        return 0.0
    try:
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except ValueError:
        return 0.0

    aggr       = get_aggregates(user_id)
    daily_cost = aggr["daily"][1]
    today_date = date.today()
    if start_dt > today_date:
        return 0.0
    days_diff = (today_date - start_dt).days + 1
    return daily_cost * days_diff

##############################################################################
#                            BOT COMMAND HANDLERS                            #
##############################################################################
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /start command:
      - If user not in DB, ask for unit price
      - Else, show the existing dashboard
    """
    user_id   = update.effective_user.id
    user_data = get_user(user_id)

    if user_data is None:
        # New user => ask for unit price
        await context.bot.send_message(
            chat_id=get_chat_id(update),
            text="Hello! Please tell me the *unit price* of a cigarette (e.g., 0.5)."
        )
        return ASKING_UNIT_PRICE
    else:
        # Existing user => show their dashboard
        await update_dashboard(user_id, update, context, is_new_message=True)
        return ConversationHandler.END

async def ask_unit_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Conversation state: ASKING_UNIT_PRICE
    """
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    try:
        price = float(text)
        if price <= 0:
            raise ValueError

        create_user(user_id, price)
        await context.bot.send_message(
            chat_id=get_chat_id(update),
            text=(
                f"Unit price set to {price:.2f}.\n"
                "Now, when did you start smoking? (YYYY-MM-DD) or 'skip'."
            )
        )
        return ASKING_START_DATE
    except ValueError:
        # Just re-ask
        await context.bot.send_message(
            chat_id=get_chat_id(update),
            text="Invalid number. Please enter a positive number (e.g. 1.2)."
        )
        return ASKING_UNIT_PRICE

async def ask_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Conversation state: ASKING_START_DATE
    """
    user_id = update.effective_user.id
    text    = update.message.text.strip().lower()

    if text == "skip":
        pass
    else:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d").date()
            iso_str = dt.isoformat()
            update_user_start_date(user_id, iso_str)
        except ValueError:
            # re-ask
            await context.bot.send_message(
                chat_id=get_chat_id(update),
                text="Invalid date. Please try again or type 'skip'."
            )
            return ASKING_START_DATE

    # Show the dashboard
    await update_dashboard(user_id, update, context, is_new_message=True)
    return ConversationHandler.END

##############################################################################
#                           DASHBOARD UPDATE LOGIC                           #
##############################################################################
async def update_dashboard(
    user_id: int,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    is_new_message: bool
):
    """
    Edits or sends a new "dashboard" message:
      - daily/weekly/monthly/yearly usage
      - stock, unit_price
      - theoretical cost since start
      - forecast for the rest of the year
    """
    row = get_user(user_id)
    if not row:
        return

    unit_price, stock, dashboard_msg_id, start_date_str = row

    # Calculate usage aggregates
    aggr = get_aggregates(user_id)
    daily_qty, daily_cost     = aggr["daily"]
    weekly_qty, weekly_cost   = aggr["weekly"]
    monthly_qty, monthly_cost = aggr["monthly"]
    yearly_qty, yearly_cost   = aggr["yearly"]

    forecast_val      = calculate_forecast(daily_cost)
    theoretical_total = theoretical_spent_since_start(user_id, start_date_str)

    text = (
        "📊 **Your Smoking Dashboard**\n\n"
        f"**Daily**:       {daily_qty} cigs, cost = {daily_cost:.2f}\n"
        f"**Weekly**:   {weekly_qty} cigs, cost = {weekly_cost:.2f}\n"
        f"**Monthly**: {monthly_qty} cigs, cost = {monthly_cost:.2f}\n"
        f"**Yearly**:     {yearly_qty} cigs, cost = {yearly_cost:.2f}\n\n"
        f"**Stock**: {stock} cigs\n"
        f"**Unit Price**: {unit_price:.2f}\n\n"
    )
    if start_date_str:
        text += (
            f"**You started smoking**: {start_date_str}\n"
            f"**(Theoretical) total spent since start**: {theoretical_total:.2f}\n\n"
        )
    else:
        text += "(No start date recorded.)\n\n"

    if forecast_val > 0:
        text += (
            f"**Forecast**: If you keep smoking at today's rate, you'll spend "
            f"~ {forecast_val:.2f} more by year-end.\n"
        )

    # Inline keyboard
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Pack (20)", callback_data="ADD_PACK"),
            InlineKeyboardButton("➕ Add Cigarette", callback_data="ADD_CIG"),
        ],
        [
            InlineKeyboardButton("🚬 Smoke One",     callback_data="SMOKE_ONE"),
            InlineKeyboardButton("🚬 Smoked More",  callback_data="SMOKE_MORE"),
        ],
        [
            InlineKeyboardButton("💲 Change Price", callback_data="CHANGE_PRICE"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    chat_id = get_chat_id(update)

    if is_new_message or dashboard_msg_id is None:
        # Send a new dashboard message
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        update_dashboard_message_id(user_id, msg.message_id)
    else:
        # Attempt to edit existing dashboard
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=dashboard_msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.warning(f"Failed to edit message: {e}")
            # if it fails, send a new one
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            update_dashboard_message_id(user_id, msg.message_id)

##############################################################################
#                         INLINE BUTTON CALLBACKS                            #
##############################################################################
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles inline button presses: Add Pack, Add Cig, Smoke One, Smoked More, Change Price
    No ephemeral messages are sent; we just update DB, then refresh the inline keyboard text.
    """
    query   = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    await query.answer()  # Acknowledge

    user_row = get_user(user_id)
    if not user_row:
        # If no user data, do nothing
        return

    unit_price, stock, _, _ = user_row
    choice = query.data

    if choice == "ADD_PACK":
        new_stock = stock + 20
        update_user_stock(user_id, new_stock)

    elif choice == "ADD_CIG":
        new_stock = stock + 1
        update_user_stock(user_id, new_stock)

    elif choice == "SMOKE_ONE":
        if stock > 0:
            new_stock = stock - 1
            update_user_stock(user_id, new_stock)
            log_smoke_event(user_id, 1, unit_price)
        # If stock = 0, do nothing

    elif choice == "SMOKE_MORE":
        # We'll set a conversation flag so next text is # cigs
        context.user_data[f"awaiting_smoked_more_{user_id}"] = True
        # No message is sent. The user can type a number, or do nothing.
        return

    elif choice == "CHANGE_PRICE":
        context.user_data[f"awaiting_new_price_{user_id}"] = True
        # No message is sent. The user can type a price, or do nothing.
        return

    # Now refresh the same dashboard
    await update_dashboard(user_id, update, context, is_new_message=False)

##############################################################################
#                    MESSAGE HANDLER (TEXT) FOR STATES                       #
##############################################################################
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    If we're awaiting a new price or a "smoked more" quantity, handle it.
    Otherwise, do nothing (no ephemeral messages).
    """
    user_id = update.effective_user.id
    text    = update.message.text.strip().lower()

    # 1) New Price?
    if context.user_data.get(f"awaiting_new_price_{user_id}"):
        try:
            new_price = float(text)
            if new_price > 0:
                update_user_price(user_id, new_price)
        except ValueError:
            pass  # ignore or do nothing
        # Clear flag & refresh
        context.user_data.pop(f"awaiting_new_price_{user_id}", None)
        await update_dashboard(user_id, update, context, is_new_message=False)
        return

    # 2) Smoked More?
    if context.user_data.get(f"awaiting_smoked_more_{user_id}"):
        user_row = get_user(user_id)
        if not user_row:
            return
        unit_price, stock, _, _ = user_row
        try:
            qty = int(text)
            if qty > 0 and qty <= stock:
                update_user_stock(user_id, stock - qty)
                total_cost = unit_price * qty
                log_smoke_event(user_id, qty, total_cost)
        except ValueError:
            pass  # do nothing
        # Clear & refresh
        context.user_data.pop(f"awaiting_smoked_more_{user_id}", None)
        await update_dashboard(user_id, update, context, is_new_message=False)
        return

    # Otherwise, do nothing

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cancel command: just clears any flags, no ephemeral messages or responses.
    """
    user_id = update.effective_user.id
    context.user_data.pop(f"awaiting_new_price_{user_id}", None)
    context.user_data.pop(f"awaiting_smoked_more_{user_id}", None)
    return ConversationHandler.END

##############################################################################
#                                MAIN ENTRY                                  #
##############################################################################
def main():
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASKING_UNIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_unit_price)],
            ASKING_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_start_date)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(menu_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    application.run_polling()

if __name__ == "__main__":
    main()
