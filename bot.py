import os
import logging
import json
import websocket
import threading
import time
import numpy as np
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ---------------------------
# CONFIGURATION
# ---------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DERIV_API_TOKEN = os.environ.get("DERIV_API_TOKEN")
PRICE_HISTORY_LENGTH = 50  # store last 50 prices
SIGNAL_THRESHOLD = 0.0005  # minimum price change to trigger new alert

# Supported pairs
SUPPORTED_PAIRS = ["frxEURUSD", "frxGBPUSD"]

# ---------------------------
# LOGGING
# ---------------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# ---------------------------
# DERIV WEBSOCKET
# ---------------------------
prices = {pair: [] for pair in SUPPORTED_PAIRS}

def deriv_ws():
    def on_message(ws, message):
        data = json.loads(message)
        if "tick" in data:
            symbol = data["tick"]["symbol"]
            quote = data["tick"]["quote"]
            prices[symbol].append(quote)
            if len(prices[symbol]) > PRICE_HISTORY_LENGTH:
                prices[symbol].pop(0)

    def on_open(ws):
        for pair in SUPPORTED_PAIRS:
            ws.send(json.dumps({"ticks": pair, "subscribe": 1, "passthrough": {"token": DERIV_API_TOKEN}}))

    ws = websocket.WebSocketApp(
        "wss://ws.binaryws.com/websockets/v3?app_id=1089",
        on_message=on_message,
        on_open=on_open
    )
    ws.run_forever()

threading.Thread(target=deriv_ws, daemon=True).start()

# ---------------------------
# INDICATORS
# ---------------------------
def calculate_rsi(prices_list, period=14):
    if len(prices_list) < period:
        return None
    deltas = np.diff(prices_list)
    ups = deltas[deltas > 0].sum() / period
    downs = -deltas[deltas < 0].sum() / period
    if downs == 0:
        return 100
    rs = ups / downs
    return 100 - (100 / (1 + rs))

def calculate_ema(prices_list, period=14):
    if len(prices_list) < period:
        return None
    prices_array = np.array(prices_list)
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    ema = np.convolve(prices_array[-period:], weights, mode='valid')[0]
    return ema

def get_signal(symbol):
    price_list = prices.get(symbol, [])
    if len(price_list) < 14:
        return None, None, None, None
    rsi = calculate_rsi(price_list)
    ema = calculate_ema(price_list)
    current_price = price_list[-1]
    if rsi is None or ema is None:
        return None, None, None, None
    if rsi < 30 and current_price > ema:
        signal = "BUY"
        desc = f"💹 Strong Buy for {symbol}\nRSI={rsi:.2f}, EMA={ema:.5f}"
        img = "buy.png"
    elif rsi > 70 and current_price < ema:
        signal = "SELL"
        desc = f"📉 Strong Sell for {symbol}\nRSI={rsi:.2f}, EMA={ema:.5f}"
        img = "sell.png"
    else:
        signal = "LOWER_RISK"
        desc = f"⚪ Lower-Risk Trade for {symbol}\nRSI={rsi:.2f}, EMA={ema:.5f}"
        img = "buy.png" if rsi < 50 else "sell.png"
    return signal, desc, img, current_price

# ---------------------------
# USER SUBSCRIPTIONS
# ---------------------------
subscriptions = {}  # {chat_id: {pair: interval}}
last_sent = {}      # {chat_id: {pair: last_signal}}
last_price = {}     # {chat_id: {pair: last_price}}

# ---------------------------
# TELEGRAM BOT
# ---------------------------
bot = Bot(token=TELEGRAM_TOKEN)

def alert_pair(chat_id, pair, interval):
    if chat_id not in last_sent:
        last_sent[chat_id] = {}
    if chat_id not in last_price:
        last_price[chat_id] = {}
    while subscriptions.get(chat_id, {}).get(pair, 0) > 0:
        signal, desc, img, price = get_signal(pair)
        send_alert = False

        # Trigger alert if signal changed
        if signal and last_sent[chat_id].get(pair) != signal:
            send_alert = True
        # Trigger alert if price moved significantly
        elif pair in last_price[chat_id]:
            if abs(price - last_price[chat_id][pair]) >= SIGNAL_THRESHOLD:
                send_alert = True

        if send_alert:
            last_sent[chat_id][pair] = signal
            last_price[chat_id][pair] = price
            if img:
                with open(img, "rb") as photo:
                    bot.send_photo(chat_id=chat_id, photo=photo, caption=desc)
            else:
                bot.send_message(chat_id=chat_id, text=desc)

        time.sleep(interval)

# ---------------------------
# COMMANDS
# ---------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"{pair} Signal", callback_data=pair) for pair in SUPPORTED_PAIRS],
        [InlineKeyboardButton("Refresh", callback_data="refresh")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome to SYC Final Pro Forex Bot!\nSubscribe to pairs for alerts:",
        reply_markup=reply_markup
    )

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /subscribe PAIR INTERVAL_SECONDS")
        return
    pair = context.args[0].upper()
    interval = int(context.args[1])
    if pair not in SUPPORTED_PAIRS:
        await update.message.reply_text(f"Unsupported pair. Supported: {SUPPORTED_PAIRS}")
        return
    if chat_id not in subscriptions:
        subscriptions[chat_id] = {}
    subscriptions[chat_id][pair] = interval
    threading.Thread(target=alert_pair, args=(chat_id, pair, interval), daemon=True).start()
    await update.message.reply_text(f"Subscribed to {pair} with interval {interval}s!")

async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /unsubscribe PAIR")
        return
    pair = context.args[0].upper()
    if chat_id in subscriptions and pair in subscriptions[chat_id]:
        subscriptions[chat_id][pair] = 0
        await update.message.reply_text(f"Unsubscribed from {pair}")
    else:
        await update.message.reply_text(f"You were not subscribed to {pair}")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in subscriptions or not subscriptions[chat_id]:
        await update.message.reply_text("No active subscriptions.")
    else:
        msg = "Active subscriptions:\n"
        for pair, interval in subscriptions[chat_id].items():
            msg += f"{pair} → {interval}s\n"
        await update.message.reply_text(msg)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in subscriptions or not subscriptions[chat_id]:
        await update.message.reply_text("No active subscriptions. Use /subscribe PAIR INTERVAL")
        return
    messages = []
    for pair in subscriptions[chat_id]:
        _, desc, _, _ = get_signal(pair)
        messages.append(desc)
    await update.message.reply_text("\n\n".join(messages))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    if data in SUPPORTED_PAIRS:
        _, desc, img, _ = get_signal(data)
        if img:
            with open(img, "rb") as photo:
                await query.message.reply_photo(photo=photo, caption=desc)
        else:
            await query.message.reply_text(desc)
    elif data == "refresh":
        await query.message.reply_text("Prices refreshed. Click a pair button to get latest signal.")

# ---------------------------
# MAIN
# ---------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("SYC Final Pro Multi-User Forex Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()