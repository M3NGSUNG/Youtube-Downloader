import yt_dlp
import os
import time
import asyncio
import shutil
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from pyrogram import Client

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
DOWNLOAD_PATH = "downloads"
TIKWM_BASE = "https://www.tikwm.com"
SPEED_THREADS = 8

user_data = {}
last_update = {}
last_upload_update = {}
cancel_flags = {}

# ---------------- PYROGRAM CLIENT ----------------
app_client = Client("uploader_session", api_id=API_ID, api_hash=API_HASH)

# ---------------- DETECT PLATFORM ----------------
def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u or "vm.tiktok.com" in u or "vt.tiktok.com" in u:
        return "tiktok"
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "facebook"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return "other"

# ---------------- SPEED BOOST: parallel chunk downloader ----------------
def _download_chunk(url: str, start: int, end: int, session: requests.Session) -> tuple[int, bytes]:
    headers = {"Range": f"bytes={start}-{end}"}
    with session.get(url, headers=headers, timeout=60) as r:
        r.raise_for_status()
        return (start, r.content)

def parallel_download(url: str, out_path: str, threads: int = SPEED_THREADS):
    session = requests.Session()
    head = session.head(url, timeout=15, allow_redirects=True)
    content_length = int(head.headers.get("Content-Length", 0))
    accepts_ranges = head.headers.get("Accept-Ranges", "none").lower() == "bytes"

    if not accepts_ranges or content_length == 0:
        with session.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
        return

    chunk_size = max(content_length // threads, 1)
    ranges = []
    for i in range(threads):
        start = i * chunk_size
        end = (start + chunk_size - 1) if i < threads - 1 else (content_length - 1)
        ranges.append((start, end))

    results = {}
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(_download_chunk, url, s, e, session): (s, e)
            for s, e in ranges
        }
        for future in as_completed(futures):
            offset, data = future.result()
            results[offset] = data

    with open(out_path, "wb") as f:
        for offset in sorted(results.keys()):
            f.write(results[offset])

# ---------------- TIKTOK via tikwm API ----------------
def fetch_tiktok_info(url: str) -> dict:
    r = requests.post(
        f"{TIKWM_BASE}/api/",
        data={"url": url, "count": 1, "cursor": 0, "web": 1, "hd": 1},
        timeout=15
    )
    data = r.json()
    if data.get("code") != 0:
        raise Exception(f"TikTok API error: {data.get('msg', 'Unknown error')}")
    return data["data"]

def download_tiktok_file(tiktok_data: dict, folder: str, as_mp3: bool = False) -> str:
    if as_mp3:
        primary = TIKWM_BASE + tiktok_data["music"]
        fallback = None
        ext = "mp3"
    else:
        primary = TIKWM_BASE + tiktok_data["play"]
        # CDN fallback: try hdplay if play fails
        hdplay = tiktok_data.get("hdplay")
        fallback = (TIKWM_BASE + hdplay) if hdplay else None
        ext = "mp4"

    out_path = os.path.join(folder, f"video.{ext}")

    urls = [u for u in [primary, fallback] if u]
    last_err = None
    for url in urls:
        try:
            parallel_download(url, out_path, threads=SPEED_THREADS)
            return out_path
        except Exception as e:
            last_err = e
            continue

    raise last_err or Exception("All TikTok download URLs failed")

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a video URL 🎬\n\n"
        "Supported platforms:\n"
        "▶️ YouTube\n"
        "🎵 TikTok\n"
        "📘 Facebook"
    )

# ---------------- HANDLE URL ----------------
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    chat_id = update.message.chat_id
    platform = detect_platform(url)

    try:
        if platform == "tiktok":
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: fetch_tiktok_info(url))
            title = info.get("title", "TikTok Video")
            duration = info.get("duration")
            thumbnail = info.get("origin_cover") or info.get("cover")
            tiktok_data = info
            available_qualities = []
        else:
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
            title = info.get("title", "Unknown")
            duration = info.get("duration")
            thumbnail = info.get("thumbnail")
            tiktok_data = None
            heights = {
                f.get("height") for f in info.get("formats", [])
                if f.get("height")
            }
            available_qualities = [q for q in [1080, 720, 360] if any(h >= q for h in heights)]
    except Exception as e:
        await update.message.reply_text(f"❌ Could not fetch video info: {e}")
        return

    user_data[chat_id] = {
        "url": url,
        "title": title,
        "platform": platform,
        "tiktok_data": tiktok_data
    }
    cancel_flags[chat_id] = False

    if duration:
        mins, secs = divmod(int(duration), 60)
        duration_str = f"{mins}:{secs:02d}"
    else:
        duration_str = "N/A"

    if platform == "tiktok":
        qualities_str = "🎬 Video  •  🎵 MP3"
        icon = "🎵 TikTok"
    elif platform == "facebook":
        qualities_str = "🎬 Best Quality  •  🎵 MP3"
        icon = "📘 Facebook"
    else:
        qualities_str = "  •  ".join([f"📺 {q}p" for q in available_qualities]) + "  •  🎵 MP3"
        icon = "▶️ YouTube"

    caption = (
        f"*{title}*\n"
        f"{'─' * 28}\n"
        f"🕐 *Duration:* {duration_str}\n"
        f"🌐 *Source:* {icon}\n"
        f"{'─' * 28}\n"
        f"📥 *Available formats:*\n{qualities_str}\n\n"
        f"Choose a format to download:"
    )

    if platform == "tiktok":
        keyboard = [
            [InlineKeyboardButton("🎬 Video", callback_data="tiktok_video")],
            [InlineKeyboardButton("🎵 MP3", callback_data="mp3")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
    elif platform == "facebook":
        keyboard = [
            [InlineKeyboardButton("🎬 Best Quality", callback_data="fb_video")],
            [InlineKeyboardButton("🎵 MP3", callback_data="mp3")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
    else:
        keyboard = []
        for q in available_qualities:
            keyboard.append([InlineKeyboardButton(f"🎥 MP4 {q}p", callback_data=f"mp4_{q}")])
        keyboard.append([InlineKeyboardButton("🎵 MP3", callback_data="mp3")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    markup = InlineKeyboardMarkup(keyboard)

    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption,
                parse_mode="Markdown"
            )
            await update.message.reply_text(
                "👇 Select a format:",
                reply_markup=markup
            )
            return
        except Exception:
            pass

    await update.message.reply_text(
        caption,
        reply_markup=markup,
        parse_mode="Markdown"
    )

# ---------------- DOWNLOAD PROGRESS ----------------
def progress_hook(d, context, chat_id, message_id, loop):
    if cancel_flags.get(chat_id):
        raise Exception("Download cancelled by user")

    now = time.time()
    if chat_id in last_update and now - last_update[chat_id] < 2:
        return
    last_update[chat_id] = now

    if d['status'] == 'downloading':
        percent = d.get('_percent_str', '0%')
        speed = d.get('_speed_str', 'N/A')
        downloaded = d.get('_downloaded_bytes_str', '0')

        try:
            p = float(percent.replace('%', '').strip())
        except:
            p = 0

        bars = int(p // 5)
        progress_bar = "█" * bars + "░" * (20 - bars)

        text = (
            f"⚡ Speed Boost Active\n"
            f"📥 Downloading...\n\n"
            f"📊 [{progress_bar}] {percent}\n"
            f"🚀 Speed: {speed}\n"
            f"📦 {downloaded}"
        )

        try:
            asyncio.run_coroutine_threadsafe(
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text
                ),
                loop
            )
        except:
            pass

# ---------------- UPLOAD PROGRESS ----------------
async def upload_progress(current, total, upload_msg):
    chat_id = upload_msg.chat_id
    now = time.time()
    if chat_id in last_upload_update and now - last_upload_update[chat_id] < 2:
        return
    last_upload_update[chat_id] = now

    percent = current * 100 / total
    bars = int(percent // 5)
    bar = "█" * bars + "░" * (20 - bars)

    try:
        await upload_msg.edit_text(
            f"⚡ Speed Boost Active\n"
            f"📤 Uploading...\n\n"
            f"[{bar}] {percent:.1f}%"
        )
    except:
        pass

# ---------------- GET FILE ----------------
def get_file(folder):
    for f in os.listdir(folder):
        return os.path.join(folder, f)
    return None

# ---------------- BUTTON CLICK ----------------
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id

    if query.data == "cancel":
        cancel_flags[chat_id] = True
        await query.edit_message_text("❌ Cancelled")
        return

    cancel_flags[chat_id] = False
    url = user_data[chat_id]["url"]
    title = user_data[chat_id].get("title", "video")
    platform = user_data[chat_id].get("platform", "other")
    tiktok_data = user_data[chat_id].get("tiktok_data")

    folder = f"{DOWNLOAD_PATH}/{chat_id}"
    os.makedirs(folder, exist_ok=True)

    msg = await query.edit_message_text("⚡ Speed Boost Active\n⏳ Starting download...")
    loop = asyncio.get_event_loop()

    file_path = None

    # ---------------- TIKTOK: parallel chunk download + CDN fallback ----------------
    if platform == "tiktok":
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg.message_id,
                text="⚡ Speed Boost Active\n📥 Downloading from TikTok..."
            )
            as_mp3 = query.data == "mp3"
            file_path = await loop.run_in_executor(
                None, lambda: download_tiktok_file(tiktok_data, folder, as_mp3)
            )
        except Exception as e:
            await context.bot.send_message(chat_id, f"❌ {e}")
            shutil.rmtree(folder, ignore_errors=True)
            return

    # ---------------- OTHER PLATFORMS: yt-dlp with speed boost opts ----------------
    else:
        hooks = [lambda d: progress_hook(d, context, chat_id, msg.message_id, loop)]

        speed_opts = {
            'concurrent_fragment_downloads': 16,
            'http_chunk_size': 10 * 1024 * 1024,
            'retries': 10,
            'fragment_retries': 10,
            'socket_timeout': 30,
        }

        if query.data == "mp3":
            ydl_opts = {
                **speed_opts,
                'format': 'bestaudio',
                'outtmpl': f'{folder}/audio.%(ext)s',
                'progress_hooks': hooks,
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}]
            }
        elif query.data == "fb_video":
            ydl_opts = {
                **speed_opts,
                'format': 'bestvideo+bestaudio/best',
                'merge_output_format': 'mp4',
                'outtmpl': f'{folder}/video.%(ext)s',
                'progress_hooks': hooks
            }
        else:
            if "1080" in query.data:
                quality = "1080"
            elif "720" in query.data:
                quality = "720"
            else:
                quality = "360"

            ydl_opts = {
                **speed_opts,
                'format': f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]',
                'merge_output_format': 'mp4',
                'outtmpl': f'{folder}/video.%(ext)s',
                'progress_hooks': hooks
            }

        try:
            def run_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

            await loop.run_in_executor(None, run_download)
        except Exception as e:
            await context.bot.send_message(chat_id, f"❌ {e}")
            shutil.rmtree(folder, ignore_errors=True)
            return

        file_path = get_file(folder)

    if not file_path or not os.path.exists(file_path):
        await context.bot.send_message(chat_id, "❌ File not found after download.")
        shutil.rmtree(folder, ignore_errors=True)
        return

    # ---------------- UPLOAD ----------------
    try:
        upload_msg = await context.bot.send_message(chat_id, "⚡ Speed Boost Active\n📤 Uploading...")

        if not app_client.is_connected:
            await app_client.start()

        if file_path.endswith(".mp3"):
            await app_client.send_audio(
                chat_id, file_path,
                title=title[:64],
                caption=title[:1024],
                file_name=f"{title[:80]}.mp3",
                progress=lambda c, t: asyncio.run_coroutine_threadsafe(
                    upload_progress(c, t, upload_msg), loop
                )
            )
        else:
            await app_client.send_video(
                chat_id, file_path,
                caption=title[:1024],
                file_name=f"{title[:80]}.mp4",
                progress=lambda c, t: asyncio.run_coroutine_threadsafe(
                    upload_progress(c, t, upload_msg), loop
                )
            )

        await upload_msg.edit_text("✅ Done! Download complete.")

    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Upload error: {e}")

    # ---------------- CLEANUP ----------------
    shutil.rmtree(folder, ignore_errors=True)

# ---------------- MAIN ----------------
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set.")
    if not API_ID or not API_HASH:
        raise ValueError("API_ID and API_HASH are not set.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(button_click))

    app.run_polling()

if __name__ == "__main__":
    main()
