import os
import re
import html
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def extract_media(url: str):
    r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    r.raise_for_status()
    page = r.text
    soup = BeautifulSoup(page, "html.parser")

    images, videos = [], []

    for tag in soup.find_all("meta"):
        prop = tag.get("property") or tag.get("name")
        content = tag.get("content")
        if not content:
            continue
        if prop in ("og:image", "twitter:image"):
            images.append(html.unescape(content))

    for tag in soup.find_all("img"):
        src = tag.get("src") or tag.get("data-src")
        if src and src.startswith(("http://", "https://")):
            images.append(html.unescape(src))

    for tag in soup.find_all("video"):
        src = tag.get("src")
        if src and src.startswith(("http://", "https://")):
            videos.append(html.unescape(src))
        for source in tag.find_all("source"):
            src = source.get("src")
            if src and src.startswith(("http://", "https://")):
                videos.append(html.unescape(src))

    patterns = [
        r'https?://[^"\'\\\s<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'\\\s<>]*)?',
        r'https?://[^"\'\\\s<>]+?\.(?:mp4|m3u8)(?:\?[^"\'\\\s<>]*)?',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, page, flags=re.I):
            value = html.unescape(match).replace("\\/", "/")
            if re.search(r'\.(?:mp4|m3u8)', value, re.I):
                videos.append(value)
            else:
                images.append(value)

    def unique(items):
        out, seen = [], set()
        for x in items:
            x = x.strip()
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return unique(images)[:10], unique(videos)[:5]


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if not ("pinduoduo.com" in text or "yangkeduo.com" in text):
        await update.message.reply_text(
            "Pinduoduo havolasini yuboring.\n\n"
            "Masalan: https://mobile.yangkeduo.com/goods.html?goods_id=..."
        )
        return

    await update.message.reply_text("⏳ Havola tekshirilmoqda...")

    try:
        images, videos = extract_media(text)

        if not images and not videos:
            await update.message.reply_text(
                "❌ Media topilmadi.\n"
                "Pinduoduo sahifasi media ma'lumotlarini yashirgan bo'lishi mumkin."
            )
            return

        caption = (
            "🛍 Pinduoduo mahsuloti\n\n"
            "📌 Manba: Pinduoduo\n"
            "🇺🇿 Telegram orqali yuborildi"
        )

        for i, image in enumerate(images):
            try:
                if i == 0:
                    await update.message.reply_photo(image, caption=caption)
                else:
                    await update.message.reply_photo(image)
            except Exception:
                continue

        for video in videos:
            if ".m3u8" in video.lower():
                continue
            try:
                await update.message.reply_video(video)
            except Exception:
                continue

        if CHANNEL_ID:
            if images:
                try:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID, photo=images[0], caption=caption
                    )
                except Exception:
                    pass

            for video in videos:
                if ".m3u8" in video.lower():
                    continue
                try:
                    await context.bot.send_video(chat_id=CHANNEL_ID, video=video)
                except Exception:
                    continue

    except Exception as e:
        await update.message.reply_text(
            "❌ Havolani ochishda xatolik yuz berdi.\n"
            "Boshqa Pinduoduo havolasini sinab ko‘ring."
        )
        print("ERROR:", repr(e))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Pinduoduo Telegram Bot is running.")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server listening on port {port}")
    server.serve_forever()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")

    # Render Web Service uchun port ochib turamiz.
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    print("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
            
