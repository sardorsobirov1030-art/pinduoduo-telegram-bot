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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Connection": "keep-alive",
}

BAD_WORDS = (
    "logo", "icon", "favicon", "avatar", "app_icon",
    "pdd_logo", "yangkeduo_logo", "default_avatar"
)

def safe_url(url):
    try:
        p = urllib.parse.urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return str(url)[:200]

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

def extract_media_from_html(html_text):
    images, videos = [], []

    patterns = [
        r'"(?:banner|gallery|image_url|hd_thumb_url|thumb_url)"\s*:\s*"([^"]+)"',
        r'"(?:url|src|play_url|video_url)"\s*:\s*"([^"]+)"',
    ]

    for pattern in patterns:
        for raw in re.findall(pattern, html_text, re.I):
            u = clean_url(raw)
            if not u:
                continue
            if is_video(u):
                videos.append(u)
            elif looks_like_product_image(u):
                images.append(u)

    decoded = html.unescape(html_text).replace("\\/", "/").replace("\\u002F", "/")
    for raw in re.findall(
        r'https?://[^"\'\\\s<>]+?\.(?:jpg|jpeg|png|webp|mp4)(?:\?[^"\'\\\s<>]*)?',
        decoded, re.I
    ):
        u = clean_url(raw)
        if not u:
            continue
        if is_video(u):
            videos.append(u)
        elif looks_like_product_image(u):
            images.append(u)

    return unique(images), unique(videos)

def extract_media(url):
    print("\n========== PDD DEBUG START ==========")
    print("INPUT:", safe_url(url))

    session = requests.Session()

    response = session.get(
        url, headers=HEADERS, timeout=30, allow_redirects=True
    )

    print("HTTP STATUS:", response.status_code)
    print("FINAL URL:", safe_url(response.url))
    print("CONTENT TYPE:", response.headers.get("content-type"))
    print("PAGE LENGTH:", len(response.text))

    page = response.text
    final_url = response.url

    print(
        "MARKERS:",
        "rawData=", "rawData" in page,
        "initDataObj=", "initDataObj" in page,
        "gallery=", "gallery" in page,
        "banner=", "banner" in page,
        "pddpic.com=", "pddpic.com" in page,
        "video=", "video" in page.lower(),
    )

    decoded_page = page
    for _ in range(3):
        decoded_page = html.unescape(decoded_page)
        decoded_page = decoded_page.replace("\\\\/", "/").replace("\\/", "/")
        decoded_page = decoded_page.replace("\\\\u002F", "/").replace("\\u002F", "/")
        decoded_page = urllib.parse.unquote(decoded_page)

    images, videos = [], []

    # 1) Share URL can expose original product image.
    for candidate_url in (url, final_url):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(candidate_url).query)
            for key in ("_oak_share_url", "_oak_share_url_encoded"):
                for value in qs.get(key, []):
                    value = urllib.parse.unquote(value)
                    value = clean_url(value)
                    if value and looks_like_product_image(value):
                        images.append(value)
        except Exception as e:
            print("SHARE PARAM ERROR:", repr(e))

    print("AFTER SHARE PARAM:", len(images), "images")

    # 2) Inline JSON.
    soup = BeautifulSoup(page, "html.parser")
    scripts = "\n".join(
        s.get_text(" ", strip=False)
        for s in soup.find_all("script")
        if s.get_text()
    )

    print("SCRIPT LENGTH:", len(scripts))
    print("SCRIPT COUNT:", len(soup.find_all("script")))

    json_candidates = []
    patterns = [
        r"window\.rawData\s*=\s*(\{.*?\})\s*;",
        r"rawData\s*=\s*(\{.*?\})\s*;",
        r"store\.initDataObj\s*=\s*(\{.*?\})\s*;",
    ]

    for pattern in patterns:
        found = re.findall(pattern, scripts, re.S)
        print("JSON PATTERN MATCHES:", len(found))
        json_candidates.extend(found)

    for raw in json_candidates:
        try:
            data = json.loads(raw)
            walk_json(data, images, videos)
        except Exception:
            pass

    print("AFTER JSON:", len(images), "images", len(videos), "videos")

    # 3) Direct CDN URLs.
    pdd_urls = re.findall(
        r'https?://[^"\'\\\s<>]+?(?:pddpic\.com|yangkeduo\.com|pinduoduo\.com)[^"\'\\\s<>]*',
        page, re.I
    )

    print("DIRECT PDD URL MATCHES:", len(pdd_urls))

    for raw in pdd_urls:
        u = clean_url(raw)
        if not u:
            continue
        if is_video(u):
            videos.append(u)
        elif looks_like_product_image(u):
            images.append(u)

    # 4) Generic media.
    generic = re.findall(
        r'https?://[^"\'\\\s<>]+?\.(?:jpg|jpeg|png|webp|mp4)(?:\?[^"\'\\\s<>]*)?',
        page, re.I
    )

    print("GENERIC MEDIA MATCHES:", len(generic))

    for raw in generic:
        u = clean_url(raw)
        if not u:
            continue
        if is_video(u):
            videos.append(u)
        elif looks_like_product_image(u):
            images.append(u)

    # 5) Direct goods fallback.
    if not images:
        try:
            parsed = urllib.parse.urlparse(final_url)
            qs = urllib.parse.parse_qs(parsed.query)
            goods_id = qs.get("goods_id", [None])[0]

            print("GOODS ID:", goods_id)

            if goods_id:
                for goods_url in (
                    f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}",
                    f"https://mobile.yangkeduo.com/goods1.html?goods_id={goods_id}",
                ):
                    rr = session.get(
                        goods_url, headers=HEADERS, timeout=30, allow_redirects=True
                    )

                    print(
                        "DIRECT GOODS:",
                        safe_url(goods_url),
                        "STATUS=", rr.status_code,
                        "FINAL=", safe_url(rr.url),
                        "LENGTH=", len(rr.text),
                    )

                    i2, v2 = extract_media_from_html(rr.text)
                    print("DIRECT GOODS MEDIA:", len(i2), "images", len(v2), "videos")

                    images.extend(i2)
                    videos.extend(v2)

                    if images or videos:
                        break
        except Exception as e:
            print("DIRECT GOODS FALLBACK ERROR:", repr(e))

    # 6) Assurance/share fallback.
    if not images:
        try:
            parsed = urllib.parse.urlparse(final_url)
            qs = urllib.parse.parse_qs(parsed.query)
            goods_id = qs.get("goods_id", [None])[0]

            if goods_id:
                share_page = (
                    "https://mobile.yangkeduo.com/"
                    "mall_quality_assurance.html?_t_timestamp=comm_share_landing"
                    f"&goods_id={goods_id}"
                )

                r2 = session.get(
                    share_page, headers=HEADERS, timeout=30, allow_redirects=True
                )

                print(
                    "SHARE FALLBACK:",
                    r2.status_code,
                    safe_url(r2.url),
                    "LENGTH=", len(r2.text),
                )

                p2 = r2.text
                d2 = urllib.parse.unquote(
                    html.unescape(p2).replace("\\\\/", "/").replace("\\/", "/")
                )

                for raw in re.findall(
                    r'https?://[^"\\\'\s<>]+?pddpic\.com[^"\\\'\s<>]*',
                    d2, re.I
                ):
                    u = clean_url(raw)
                    if u and is_image(u) and looks_like_product_image(u):
                        images.append(u)

                for raw in re.findall(
                    r'https?://[^"\\\'\s<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\\\'\s<>]*)?',
                    d2, re.I
                ):
                    u = clean_url(raw)
                    if u and looks_like_product_image(u):
                        images.append(u)

                print(
                    "SHARE FALLBACK MEDIA:",
                    len(unique(images)),
                    "images"
                )

        except Exception as e:
            print("SHARE FALLBACK ERROR:", repr(e))

    images = unique(images)[:10]
    videos = unique(videos)[:5]
    images.sort(key=lambda x: ("pddpic.com" not in x.lower(), len(x)))

    print("FINAL MEDIA:", len(images), "images", len(videos), "videos")
    print("========== PDD DEBUG END ==========\n")

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

    print("\nTELEGRAM INPUT RECEIVED")
    print("CHAT ID:", update.effective_chat.id if update.effective_chat else None)
    print("MESSAGE LENGTH:", len(text))

    if "pinduoduo.com" not in text and "yangkeduo.com" not in text:
        await update.message.reply_text("Pinduoduo havolasini yuboring.")
        return

    status = await update.message.reply_text(
        "⏳ Mahsulot rasmlari olinmoqda..."
    )

    try:
        images, videos = extract_media(text)

        if not images and not videos:
            await status.edit_text(
                "❌ Mahsulot rasmi topilmadi.\n"
                "Pinduoduo sahifasi media ma'lumotlarini yashirgan bo'lishi mumkin."
            )
            return

        caption = "🛍 Pinduoduo mahsuloti\n📌 Manba: Pinduoduo"

        sent = await send_media(
            update, context, images, videos, caption
        )

        if not sent:
            await status.edit_text("❌ Rasmni yuklab bo'lmadi.")
            return

        try:
            await status.delete()
        except Exception:
            pass

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
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link
        )
    )

    print("Bot ishga tushdi...")
    app.run_polling()

if __name__ == "__main__":
    main()
