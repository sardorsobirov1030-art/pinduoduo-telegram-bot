import os
import re
import json
import html
import urllib.parse
import threading
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# SETTINGS
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://mobile.yangkeduo.com/",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}

BAD_WORDS = (
    "logo",
    "icon",
    "favicon",
    "avatar",
    "app_icon",
    "pdd_logo",
    "yangkeduo_logo",
    "default_avatar",
)

# =========================
# HELPERS
# =========================
def safe_url(url):
    try:
        p = urllib.parse.urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return str(url)[:200]


def clean_url(value):
    if not isinstance(value, str):
        return None

    value = html.unescape(value)
    value = value.replace("\\/", "/")
    value = value.replace("\\u002F", "/")
    value = value.strip().strip('"').strip("'")

    if value.startswith("//"):
        value = "https:" + value

    if value.startswith(("http://", "https://")):
        return value

    return None


def unique(items):
    result = []
    seen = set()

    for item in items:
        item = clean_url(item)

        if item and item not in seen:
            seen.add(item)
            result.append(item)

    return result


def is_image(url):
    return bool(
        re.search(
            r"\.(jpg|jpeg|png|webp)(\?.*)?$",
            url,
            re.I,
        )
    )


def is_video(url):
    return bool(
        re.search(
            r"\.(mp4)(\?.*)?$",
            url,
            re.I,
        )
    )


def looks_like_product_image(url):
    low = url.lower()

    if any(word in low for word in BAD_WORDS):
        return False

    return is_image(url)


def walk_json(obj, images, videos):
    if isinstance(obj, dict):

        for key, value in obj.items():

            key_lower = str(key).lower()

            if isinstance(value, str):

                url = clean_url(value)

                if not url:
                    continue

                image_keys = (
                    "gallery",
                    "banner",
                    "image",
                    "img",
                    "thumb",
                    "pic",
                    "detail",
                )

                video_keys = (
                    "video",
                    "play_url",
                    "video_url",
                    "mp4",
                )

                if any(x in key_lower for x in image_keys):
                    if looks_like_product_image(url):
                        images.append(url)

                if any(x in key_lower for x in video_keys):
                    if is_video(url):
                        videos.append(url)

                if "pddpic.com" in url.lower():

                    if is_video(url):
                        videos.append(url)

                    elif looks_like_product_image(url):
                        images.append(url)

            else:
                walk_json(value, images, videos)

    elif isinstance(obj, list):

        for item in obj:
            walk_json(item, images, videos)


# =========================
# HTML PARSER
# =========================
def extract_media_from_html(page):

    images = []
    videos = []

    patterns = [
        r'"(?:banner|gallery|image_url|hd_thumb_url|thumb_url)"\s*:\s*"([^"]+)"',
        r'"(?:url|src|play_url|video_url)"\s*:\s*"([^"]+)"',
    ]

    for pattern in patterns:

        for raw in re.findall(pattern, page, re.I):

            url = clean_url(raw)

            if not url:
                continue

            if is_video(url):
                videos.append(url)

            elif looks_like_product_image(url):
                images.append(url)

    decoded = (
        html.unescape(page)
        .replace("\\/", "/")
        .replace("\\u002F", "/")
    )

    media_urls = re.findall(
        r'https?://[^"\'\\\s<>]+?\.(?:jpg|jpeg|png|webp|mp4)'
        r'(?:\?[^"\'\\\s<>]*)?',
        decoded,
        re.I,
    )

    for raw in media_urls:

        url = clean_url(raw)

        if not url:
            continue

        if is_video(url):
            videos.append(url)

        elif looks_like_product_image(url):
            images.append(url)

    return unique(images), unique(videos)


# =========================
# PINDUODUO EXTRACTOR
# =========================
def extract_media(url):

    log.info("========== PDD DEBUG START ==========")
    log.info("INPUT URL: %s", safe_url(url))

    session = requests.Session()

    try:

        response = session.get(
            url,
            headers=HEADERS,
            timeout=30,
            allow_redirects=True,
        )

        log.info("HTTP STATUS: %s", response.status_code)
        log.info("FINAL URL: %s", safe_url(response.url))
        log.info(
            "CONTENT TYPE: %s",
            response.headers.get("content-type"),
        )
        log.info("PAGE LENGTH: %s", len(response.text))

    except Exception as e:

        log.exception("INITIAL REQUEST ERROR")

        return [], []

    page = response.text
    final_url = response.url

    log.info(
        "MARKERS: rawData=%s initDataObj=%s gallery=%s banner=%s "
        "pddpic=%s video=%s",
        "rawData" in page,
        "initDataObj" in page,
        "gallery" in page,
        "banner" in page,
        "pddpic.com" in page,
        "video" in page.lower(),
    )

    images = []
    videos = []

    # -------------------------
    # 1. _oak_share_url
    # -------------------------
    for candidate in (url, final_url):

        try:

            parsed = urllib.parse.urlparse(candidate)
            query = urllib.parse.parse_qs(parsed.query)

            for key in (
                "_oak_share_url",
                "_oak_share_url_encoded",
            ):

                for value in query.get(key, []):

                    value = urllib.parse.unquote(value)
                    value = clean_url(value)

                    if value and looks_like_product_image(value):
                        images.append(value)

        except Exception:
            log.exception("SHARE PARAM ERROR")

    log.info(
        "AFTER SHARE PARAM: %s images",
        len(unique(images)),
    )

    # -------------------------
    # 2. JSON / scripts
    # -------------------------
    soup = BeautifulSoup(page, "html.parser")

    scripts = "\n".join(
        script.get_text(" ", strip=False)
        for script in soup.find_all("script")
        if script.get_text()
    )

    log.info("SCRIPT COUNT: %s", len(soup.find_all("script")))
    log.info("SCRIPT LENGTH: %s", len(scripts))

    json_candidates = []

    patterns = [
        r"window\.rawData\s*=\s*(\{.*?\})\s*;",
        r"rawData\s*=\s*(\{.*?\})\s*;",
        r"store\.initDataObj\s*=\s*(\{.*?\})\s*;",
    ]

    for pattern in patterns:

        found = re.findall(
            pattern,
            scripts,
            re.S,
        )

        log.info(
            "JSON PATTERN MATCHES: %s",
            len(found),
        )

        json_candidates.extend(found)

    for raw in json_candidates:

        try:

            data = json.loads(raw)
            walk_json(data, images, videos)

        except Exception:
            pass

    log.info(
        "AFTER JSON: %s images / %s videos",
        len(unique(images)),
        len(unique(videos)),
    )

    # -------------------------
    # 3. Direct PDD CDN
    # -------------------------
    pdd_urls = re.findall(
        r'https?://[^"\'\\\s<>]+?(?:pddpic\.com|yangkeduo\.com|pinduoduo\.com)'
        r'[^"\'\\\s<>]*',
        page,
        re.I,
    )

    log.info(
        "DIRECT PDD URL MATCHES: %s",
        len(pdd_urls),
    )

    for raw in pdd_urls:

        url2 = clean_url(raw)

        if not url2:
            continue

        if is_video(url2):
            videos.append(url2)

        elif looks_like_product_image(url2):
            images.append(url2)

    # -------------------------
    # 4. Generic media
    # -------------------------
    generic = re.findall(
        r'https?://[^"\'\\\s<>]+?\.(?:jpg|jpeg|png|webp|mp4)'
        r'(?:\?[^"\'\\\s<>]*)?',
        page,
        re.I,
    )

    log.info(
        "GENERIC MEDIA MATCHES: %s",
        len(generic),
    )

    for raw in generic:

        url2 = clean_url(raw)

        if not url2:
            continue

        if is_video(url2):
            videos.append(url2)

        elif looks_like_product_image(url2):
            images.append(url2)

    # -------------------------
    # 5. Direct goods fallback
    # -------------------------
    if not images:

        try:

            parsed = urllib.parse.urlparse(final_url)
            query = urllib.parse.parse_qs(parsed.query)

            goods_id = query.get(
                "goods_id",
                [None],
            )[0]

            log.info("GOODS ID: %s", goods_id)

            if goods_id:

                goods_urls = [
                    f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}",
                    f"https://mobile.yangkeduo.com/goods1.html?goods_id={goods_id}",
                ]

                for goods_url in goods_urls:

                    rr = session.get(
                        goods_url,
                        headers=HEADERS,
                        timeout=30,
                        allow_redirects=True,
                    )

                    log.info(
                        "DIRECT GOODS STATUS=%s FINAL=%s LENGTH=%s",
                        rr.status_code,
                        safe_url(rr.url),
                        len(rr.text),
                    )

                    i2, v2 = extract_media_from_html(
                        rr.text
                    )

                    log.info(
                        "DIRECT GOODS MEDIA=%s images / %s videos",
                        len(i2),
                        len(v2),
                    )

                    images.extend(i2)
                    videos.extend(v2)

                    if images or videos:
                        break

        except Exception:

            log.exception(
                "DIRECT GOODS FALLBACK ERROR"
            )

    # -------------------------
    # 6. Assurance fallback
    # -------------------------
    if not images:

        try:

            parsed = urllib.parse.urlparse(final_url)
            query = urllib.parse.parse_qs(parsed.query)

            goods_id = query.get(
                "goods_id",
                [None],
            )[0]

            if goods_id:

                share_page = (
                    "https://mobile.yangkeduo.com/"
                    "mall_quality_assurance.html"
                    "?_t_timestamp=comm_share_landing"
                    f"&goods_id={goods_id}"
                )

                rr = session.get(
                    share_page,
                    headers=HEADERS,
                    timeout=30,
                    allow_redirects=True,
                )

                log.info(
                    "SHARE FALLBACK STATUS=%s FINAL=%s LENGTH=%s",
                    rr.status_code,
                    safe_url(rr.url),
                    len(rr.text),
                )

                decoded = (
                    html.unescape(rr.text)
                    .replace("\\/", "/")
                    .replace("\\u002F", "/")
                )

                media = re.findall(
                    r'https?://[^"\'\\\s<>]+?pddpic\.com'
                    r'[^"\'\\\s<>]*',
                    decoded,
                    re.I,
                )

                log.info(
                    "SHARE FALLBACK PDD URLS=%s",
                    len(media),
                )

                for raw in media:

                    url2 = clean_url(raw)

                    if (
                        url2
                        and is_image(url2)
                        and looks_like_product_image(url2)
                    ):
                        images.append(url2)

        except Exception:

            log.exception(
                "SHARE FALLBACK ERROR"
            )

    images = unique(images)[:10]
    videos = unique(videos)[:5]

    images.sort(
        key=lambda x: (
            "pddpic.com" not in x.lower(),
            len(x),
        )
    )

    log.info(
        "FINAL MEDIA: %s images / %s videos",
        len(images),
        len(videos),
    )

    log.info("========== PDD DEBUG END ==========")

    return images, videos


# =========================
# DOWNLOAD
# =========================
def download_file(url):

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )

    response.raise_for_status()

    data = BytesIO(response.content)

    return data


# =========================
# TELEGRAM
# =========================
async def send_media(
    update,
    images,
    videos,
    caption,
):

    sent = False

    for index, image in enumerate(images):

        try:

            data = download_file(image)
            data.name = f"product_{index + 1}.jpg"

            await update.message.reply_photo(
                photo=data,
                caption=caption if index == 0 else None,
            )

            sent = True

        except Exception:

            log.exception("IMAGE SEND ERROR")

    for video in videos:

        try:

            data = download_file(video)
            data.name = "product.mp4"

            await update.message.reply_video(
                video=data,
            )

            sent = True

        except Exception:

            log.exception("VIDEO SEND ERROR")

    return sent


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        update.message.text
        if update.message
        else ""
    ).strip()

    log.info("========== TELEGRAM MESSAGE ==========")

    log.info(
        "CHAT ID: %s",
        update.effective_chat.id
        if update.effective_chat
        else None,
    )

    log.info(
        "TEXT LENGTH: %s",
        len(text),
    )

    if text.upper() == "TEST":

        log.info("TEST MESSAGE RECEIVED")

        await update.message.reply_text(
            "✅ BOT IS WORKING!\n"
            "Telegram connection OK."
        )

        return

    if (
        "pinduoduo.com" not in text
        and "yangkeduo.com" not in text
    ):

        await update.message.reply_text(
            "Pinduoduo havolasini yuboring."
        )

        return

    status = await update.message.reply_text(
        "⏳ Mahsulot rasmlari olinmoqda..."
    )

    try:

        images, videos = extract_media(text)

        if not images and not videos:

            await status.edit_text(
                "❌ Mahsulot rasmi topilmadi.\n"
                "Logs orqali sababini aniqlaymiz."
            )

            return

        caption = (
            "🛍 Pinduoduo mahsuloti\n"
            "📌 Manba: Pinduoduo"
        )

        sent = await send_media(
            update,
            images,
            videos,
            caption,
        )

        if not sent:

            await status.edit_text(
                "❌ Rasmni yuklab bo'lmadi."
            )

            return

        try:
            await status.delete()
        except Exception:
            pass

        # CHANNEL
        if CHANNEL_ID and images:

            try:

                data = download_file(
                    images[0]
                )

                data.name = "product.jpg"

                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=data,
                    caption=caption,
                )

                log.info(
                    "CHANNEL IMAGE SENT"
                )

            except Exception:

                log.exception(
                    "CHANNEL IMAGE ERROR"
                )

        if CHANNEL_ID:

            for video in videos:

                try:

                    data = download_file(
                        video
                    )

                    data.name = "product.mp4"

                    await context.bot.send_video(
                        chat_id=CHANNEL_ID,
                        video=data,
                    )

                    log.info(
                        "CHANNEL VIDEO SENT"
                    )

                except Exception:

                    log.exception(
                        "CHANNEL VIDEO ERROR"
                    )

    except Exception:

        log.exception(
            "HANDLE MESSAGE ERROR"
        )

        try:

            await status.edit_text(
                "❌ Xatolik yuz berdi."
            )

        except Exception:
            pass


async def error_handler(
    update,
    context,
):

    log.exception(
        "TELEGRAM ERROR",
        exc_info=context.error,
    )


# =========================
# RENDER HEALTH SERVER
# =========================
class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain",
        )
        self.end_headers()

        self.wfile.write(
            b"OK"
        )

    def log_message(
        self,
        format,
        *args,
    ):
        return


def start_health_server():

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    server = HTTPServer(
        (
            "0.0.0.0",
            port,
        ),
        HealthHandler,
    )

    log.info(
        "HEALTH SERVER STARTED ON PORT %s",
        port,
    )

    server.serve_forever()


# =========================
# MAIN
# =========================
def main():

    log.info("==============================")
    log.info("STARTING PINDUODUO TELEGRAM BOT")
    log.info("==============================")

    if not BOT_TOKEN:

        log.error(
            "BOT_TOKEN IS MISSING!"
        )

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing"
        )

    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(
        error_handler
    )

    log.info(
        "BOT APPLICATION CREATED"
    )

    log.info(
        "BOT POLLING STARTING..."
    )

    app.run_polling(
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
    
