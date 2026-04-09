import yt_dlp
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes

BOT_TOKEN = "8638795351:AAE6hgfjl9hhQNvWqqytGzR69OvdP6sAgos"

user_data = {}
last_update = {}

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a YouTube URL 🎬")

# ---------------- HANDLE URL ----------------
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    
    ydl_opts = {'quiet': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title")
    duration = info.get("duration")

    user_data[update.message.chat_id] = {"url": url}

    keyboard = [
        [InlineKeyboardButton("MP4 720p 🎥", callback_data="mp4_720")],
        [InlineKeyboardButton("MP4 360p 📱", callback_data="mp4_360")],
        [InlineKeyboardButton("MP3 🎵", callback_data="mp3")],
        [InlineKeyboardButton("Cancel ❌", callback_data="cancel")]
    ]

    await update.message.reply_text(
        f"📄 *{title}*\n⏱ Duration: {duration}s\n\nChoose format:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ---------------- PROGRESS HOOK ----------------
def progress_hook(d, context, chat_id, message_id):
    now = time.time()

    if chat_id in last_update and now - last_update[chat_id] < 2:
        return  # limit updates

    last_update[chat_id] = now

    if d['status'] == 'downloading':
        percent = d.get('_percent_str', '0%')
        speed = d.get('_speed_str', 'N/A')
        downloaded = d.get('_downloaded_bytes_str', '0')

        # progress bar
        try:
            p = float(percent.replace('%', '').strip())
        except:
            p = 0

        bars = int(p // 5)
        progress_bar = "█" * bars + "░" * (20 - bars)

        text = (
            f"⏳ Downloading...\n\n"
            f"📊 [{progress_bar}] {percent}\n"
            f"🚀 Speed: {speed}\n"
            f"📦 {downloaded}"
        )

        try:
            context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text
            )
        except:
            pass

# ---------------- BUTTON CLICK ----------------
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled")
        return

    url = user_data[chat_id]["url"]

    msg = await query.edit_message_text("⏳ Starting download...")

    # ---------------- OPTIONS ----------------
    if query.data == "mp3":
        ydl_opts = {
            'format': 'bestaudio',
            'outtmpl': 'audio.%(ext)s',
            'progress_hooks': [
                lambda d: progress_hook(d, context, chat_id, msg.message_id)
            ],
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3'
            }]
        }

    elif query.data == "mp4_720":
        ydl_opts = {
            'format': 'bestvideo[height<=720]+bestaudio',
            'outtmpl': 'video.mp4',
            'progress_hooks': [
                lambda d: progress_hook(d, context, chat_id, msg.message_id)
            ]
        }

    elif query.data == "mp4_360":
        ydl_opts = {
            'format': 'bestvideo[height<=360]+bestaudio',
            'outtmpl': 'video.mp4',
            'progress_hooks': [
                lambda d: progress_hook(d, context, chat_id, msg.message_id)
            ]
        }

    # ---------------- DOWNLOAD ----------------
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Error: {e}")
        return

    # ---------------- SEND FILE ----------------
    try:
        if query.data == "mp3":
            await context.bot.send_audio(chat_id, open("audio.mp3", "rb"))
        else:
            await context.bot.send_video(chat_id, open("video.mp4", "rb"))

        await context.bot.send_message(chat_id, "✅ Done!")
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Send failed: {e}")

# ---------------- MAIN ----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_click))

    app.run_polling()

if __name__ == "__main__":
    main()