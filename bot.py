import os
import re
import json
import html
import urllib.parse
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

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
    "Referer": "https://mobile.yangkeduo.com/",
}

BAD_WORDS = (
    "logo", "icon", "favicon", "avatar", "app_icon",
    "pdd_logo", "yangkeduo_logo", "default_avatar"
)

def clean_url(value):
    if not isinstance(value, str):
        return None
    value = html.unescape(value).replace("\\/", "/")
    value = value.replace("\\u002F", "/")
    value = value.strip().strip('"').strip("'")
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith(("http://", "https://")):
        return value
    return None

def is_image(url):
    return bool(re.search(r"\.(?:jpg|jpeg|png|webp)(?:[?#].*)?$", url, re.I))

def is_video(url):
    return bool(re.search(r"\.(?:mp4)(?:[?#].*)?$", url, re.I))

def looks_like_product_image(url):
    low = url.lower()
    if any(word in low for word in BAD_WORDS):
        return False
    return is_image(url)

def unique(items):
    out, seen = [], set()
    for x in items:
        x = clean_url(x)
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

def walk_json(obj, images, videos):
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key).lower()

            if isinstance(value, str):
                u = clean_url(value)
                if u:
                    if any(x in k for x in (
                        "gallery", "banner", "image", "img", "thumb", "pic", "detail"
                    )) and looks_like_product_image(u):
                        images.append(u)
                    elif any(x in k for x in (
                        "video", "play_url", "video_url", "mp4"
                    )) and is_video(u):
                        videos.append(u)

                    # PDD image/video URLs are often stored under generic "url".
                    if "pddpic.com" in u.lower():
                        if is_video(u):
                            videos.append(u)
                        elif looks_like_product_image(u):
                            images.append(u)

            else:
                walk_json(value, images, videos)

    elif isinstance(obj, list):
        for item in obj:
            walk_json(item, images, videos)

def extract_media(url):
    session = requests.Session()
    response = session.get(
        url, headers=HEADERS, timeout=30, allow_redirects=True
    )
    response.raise_for_status()

    page = response.text
    final_url = response.url

    images, videos = [], []

    # 1) Pinduoduo share redirect can expose the original product image
    # through _oak_share_url.
    for candidate_url in (url, final_url):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(candidate_url).query)
            for key in ("_oak_share_url", "_oak_share_url_encoded"):
                for value in qs.get(key, []):
                    value = urllib.parse.unquote(value)
                    value = clean_url(value)
                    if value and looks_like_product_image(value):
                        images.append(value)
        except Exception:
            pass

    # 2) Parse inline rawData / initDataObj JSON.
    soup = BeautifulSoup(page, "html.parser")
    scripts = "\n".join(
        s.get_text(" ", strip=False)
        for s in soup.find_all("script")
        if s.get_text()
    )

    json_candidates = []

    patterns = [
        r"window\.rawData\s*=\s*(\{.*?\})\s*;",
        r"rawData\s*=\s*(\{.*?\})\s*;",
        r"store\.initDataObj\s*=\s*(\{.*?\})\s*;",
    ]

    for pattern in patterns:
        for match in re.findall(pattern, scripts, re.S):
            json_candidates.append(match)

    for raw in json_candidates:
        try:
            data = json.loads(raw)
            walk_json(data, images, videos)
        except Exception:
            # If the object is embedded in JS and not strict JSON,
            # fall back to URL extraction below.
            pass

    # 3) Direct PDD CDN URLs anywhere in the HTML/JS.
    pdd_urls = re.findall(
        r'https?://[^"\'\\\s<>]+?(?:pddpic\.com|yangkeduo\.com|pinduoduo\.com)[^"\'\\\s<>]*',
        page,
        re.I
    )
    for raw in pdd_urls:
        u = clean_url(raw)
        if not u:
            continue
        if is_video(u):
            videos.append(u)
        elif looks_like_product_image(u):
            images.append(u)

    # 4) Generic image/video URLs as a final fallback, but NEVER use
    # og:image blindly because it may be the Pinduoduo logo.
    generic = re.findall(
        r'https?://[^"\'\\\s<>]+?\.(?:jpg|jpeg|png|webp|mp4)(?:\?[^"\'\\\s<>]*)?',
        page,
        re.I
    )
    for raw in generic:
        u = clean_url(raw)
        if not u:
            continue
        if is_video(u):
            videos.append(u)
        elif looks_like_product_image(u):
            images.append(u)

    images = unique(images)[:10]
    videos = unique(videos)[:5]

    # Prefer actual product/CDN images.
    images.sort(key=lambda x: ("pddpic.com" not in x.lower(), len(x)))

    return images, videos

def download_file(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BytesIO(r.content)

async def send_media(update, context, images, videos, caption):
    sent_any = False

    for i, image in enumerate(images):
        try:
            data = download_file(image)
            data.name = f"product_{i+1}.jpg"
            await update.message.reply_photo(
                photo=data,
                caption=caption if i == 0 else None
            )
            sent_any = True
        except Exception as e:
            print("IMAGE ERROR:", repr(e))

    for video in videos:
        try:
            data = download_file(video)
            data.name = "product.mp4"
            await update.message.reply_video(video=data)
            sent_any = True
        except Exception as e:
            print("VIDEO ERROR:", repr(e))

    return sent_any

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if "pinduoduo.com" not in text and "yangkeduo.com" not in text:
        await update.message.reply_text(
            "Pinduoduo havolasini yuboring."
        )
        return

    status = await update.message.reply_text("⏳ Mahsulot rasmlari olinmoqda...")

    try:
        images, videos = extract_media(text)

        if not images and not videos:
            await status.edit_text(
                "❌ Mahsulot rasmi topilmadi.\n"
                "Pinduoduo sahifasi media ma'lumotlarini yashirgan bo'lishi mumkin."
            )
            return

        caption = "🛍 Pinduoduo mahsuloti\n📌 Manba: Pinduoduo"

        sent = await send_media(update, context, images, videos, caption)

        if not sent:
            await status.edit_text("❌ Rasmni yuklab bo'lmadi.")
            return

        try:
            await status.delete()
        except Exception:
            pass

        # Send the first product image to the configured channel.
        if CHANNEL_ID and images:
            try:
                data = download_file(images[0])
                data.name = "product.jpg"
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=data,
                    caption=caption
                )
            except Exception as e:
                print("CHANNEL IMAGE ERROR:", repr(e))

        if CHANNEL_ID:
            for video in videos:
                try:
                    data = download_file(video)
                    data.name = "product.mp4"
                    await context.bot.send_video(
                        chat_id=CHANNEL_ID,
                        video=data
                    )
                except Exception as e:
                    print("CHANNEL VIDEO ERROR:", repr(e))

    except Exception as e:
        print("ERROR:", repr(e))
        await status.edit_text(
            "❌ Havolani ochishda xatolik.\n"
            "Iltimos, yana bir marta yuboring."
        )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )

    print("Bot ishga tushdi...")
    app.run_polling()

if __name__ == "__main__":
    main()
