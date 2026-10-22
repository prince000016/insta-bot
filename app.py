import os
import re
import html
import base64
import json
import subprocess
import tempfile
import requests
from urllib.parse import quote_plus, urlparse
from flask import Flask, request, send_file
from supabase import create_client

app = Flask(__name__)

PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# Separate Instagram Login user token used ONLY for follow-status checks.
# Do not put a Facebook Page token in this variable.
FOLLOW_CHECK_IG_TOKEN = os.environ.get("FOLLOW_CHECK_IG_TOKEN", "").strip()
FOLLOW_CHECK_API_URL = "https://graph.instagram.com/v26.0"
FOLLOW_PAGE_URL = "https://www.instagram.com/find_myproduct52/"
# Pending product results are kept until the user presses Verify Follow.
# Note: in-memory pending requests are cleared by a server restart/redeploy.
PENDING_FOLLOW_PRODUCTS = {}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

API_URL = (
    "https://graph.instagram.com/v26.0/me/messages"
    f"?access_token={PAGE_ACCESS_TOKEN}"
)
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

SHOPPING_DOMAINS = (
    "amazon.", "amzn.to", "flipkart.com", "fkrt.it", "meesho.com",
    "myntra.com", "nykaa.com", "ajio.com", "snapdeal.com"
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@app.route("/", methods=["GET"])
def home():
    return "Instagram Bot is Running", 200


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


@app.route("/compare", methods=["GET"])
def compare_page():
    """Standalone premium product comparison landing page."""
    landing_file = os.path.join(os.path.dirname(__file__), "find_my_product_landing.html")
    if not os.path.isfile(landing_file):
        return "Comparison page is not installed yet.", 404
    return send_file(landing_file)


def _safe_event_shape(event):
    """Log only structural webhook metadata; never log message text, tokens, or full URLs."""
    message = event.get("message") or {}
    attachments = message.get("attachments") or []
    attachment_shapes = []
    for attachment in attachments[:5]:
        payload = attachment.get("payload") or {}
        attachment_shapes.append({
            "type": attachment.get("type"),
            "payload_keys": sorted(str(k) for k in payload.keys())[:30],
            "has_url": bool(payload.get("url") or payload.get("link")),
            "has_image_url": bool(
                payload.get("image_url") or payload.get("thumbnail_url")
                or payload.get("image") or payload.get("media_url")
            ),
            "has_video_url": bool(
                payload.get("video_url") or payload.get("video_uri")
                or payload.get("video_src") or payload.get("video")
            ),
        })
    return {
        "event_keys": sorted(str(k) for k in event.keys())[:30],
        "has_message": bool(event.get("message")),
        "has_text": bool(message.get("text")),
        "attachment_count": len(attachments),
        "attachments": attachment_shapes,
        "has_postback": bool(event.get("postback")),
    }


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge or "", 200, {"Content-Type": "text/plain"}
        return "Verification failed", 403

    data = request.get_json(silent=True) or {}
    print("Webhook received; object:", data.get("object"))
    entries = data.get("entry", [])
    print("Webhook entries received:", len(entries))

    processed_events = 0
    for entry in entries:
        for event in entry.get("messaging", []):
            print("Webhook event shape:", json.dumps(_safe_event_shape(event), ensure_ascii=True))
            try:
                process_message_event(event)
            except Exception as exc:
                print("Webhook event processing error:", type(exc).__name__, str(exc)[:250])
            processed_events += 1
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if change.get("field") == "messages":
                print("Webhook change shape:", json.dumps(_safe_event_shape(value), ensure_ascii=True))
                try:
                    process_message_event(value)
                except Exception as exc:
                    print("Webhook change processing error:", type(exc).__name__, str(exc)[:250])
                processed_events += 1

    if processed_events == 0:
        print("Webhook contained no messaging events. Entry keys:", [
            sorted(str(k) for k in entry.keys())[:20] for entry in entries[:5]
        ])
    return "OK", 200




def _find_direct_video_url(payload):
    """Find a direct video URL only from explicit video-related payload fields."""
    candidates = []

    def walk(value, key_hint=""):
        if isinstance(value, dict):
            for key, child in value.items():
                key_name = str(key).lower()
                if isinstance(child, str) and child.startswith(("https://", "http://")):
                    if key_name in {"video_url", "video_uri", "video_src", "video"}:
                        candidates.append(child)
                elif isinstance(child, (dict, list, tuple)):
                    walk(child, key_name)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, key_hint)

    walk(payload)
    for candidate in candidates:
        try:
            host = (urlparse(candidate).hostname or "").lower()
            if host.endswith((".cdninstagram.com", ".fbcdn.net", ".instagram.com")):
                return candidate
        except Exception:
            continue
    return None


def extract_video_frames(video_url, max_frames=5, max_bytes=25 * 1024 * 1024):
    """Extract sampled frames from an explicit direct Meta-hosted video URL.

    A normal Instagram permalink is never downloaded as a video. Requires
    imageio-ffmpeg; gracefully returns no frames if unavailable.
    """
    if not video_url:
        return []
    try:
        host = (urlparse(video_url).hostname or "").lower()
        if not host.endswith((".cdninstagram.com", ".fbcdn.net", ".instagram.com")):
            print("Video frame scan skipped: host not allowed.")
            return []

        with tempfile.TemporaryDirectory(prefix="product_frames_") as temp_dir:
            video_path = os.path.join(temp_dir, "source_video.mp4")
            response = requests.get(
                video_url, stream=True, timeout=(8, 20),
                headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True,
            )
            response.raise_for_status()
            final_host = (urlparse(response.url).hostname or "").lower()
            if not final_host.endswith((".cdninstagram.com", ".fbcdn.net", ".instagram.com")):
                response.close()
                print("Video frame scan blocked unexpected redirect host.")
                return []
            total = 0
            with open(video_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        response.close()
                        print("Video frame scan skipped: video exceeds size limit.")
                        return []
                    output.write(chunk)
            response.close()
            if total < 1024:
                return []

            try:
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError:
                print("Video frame scan unavailable: imageio-ffmpeg is not installed.")
                return []

            pattern = os.path.join(temp_dir, "frame_%02d.jpg")
            result = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-i", video_path,
                    "-vf", "fps=1/3,scale=640:-1:force_original_aspect_ratio=decrease",
                    "-frames:v", str(max_frames), "-q:v", "4", "-y", pattern,
                ],
                capture_output=True, timeout=35, check=False,
            )
            if result.returncode != 0:
                print("Video frame extraction failed:", result.stderr.decode("utf-8", "ignore")[:250])
                return []
            frames = []
            for name in sorted(os.listdir(temp_dir)):
                if re.fullmatch(r"frame_\d+\.jpg", name):
                    with open(os.path.join(temp_dir, name), "rb") as frame_file:
                        frames.append(frame_file.read())
            print("Video frames extracted:", len(frames))
            return frames[:max_frames]
    except Exception as exc:
        print("Video frame scan failed:", type(exc).__name__, str(exc)[:250])
        return []


def identify_products_from_image_bytes(image_bytes, mime_type="image/jpeg", max_products=3):
    """Run Gemini visual identification directly on sampled frame bytes."""
    if not image_bytes or not GEMINI_API_KEY:
        return []
    try:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        prompt = (
            "Identify distinct physical products visible in this image using only visual evidence. "
            "Do not use captions, hashtags, creator text, or outside post context. "
            "Return only a JSON array of up to " + str(max_products) +
            " concise product names. Include brand/model only if visually supported; otherwise use the visible product type."
        )
        body = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": encoded}},
            ]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 180,
                "responseMimeType": "application/json",
            },
        }
        response = requests.post(
            GEMINI_URL, params={"key": GEMINI_API_KEY}, json=body, timeout=25
        )
        print("Gemini frame API status:", response.status_code)
        if response.status_code != 200:
            print("Gemini frame API error:", response.text[:300])
            return []
        candidates = response.json().get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        raw = " ".join(part.get("text", "") for part in parts).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        names = []
        for item in parsed:
            if not isinstance(item, str):
                continue
            name = re.sub(r"\s+", " ", item).strip(" \t\r\n-•")[:75]
            if len(name) >= 3 and name.casefold() not in {n.casefold() for n in names}:
                names.append(name)
            if len(names) >= max_products:
                break
        return names
    except Exception as exc:
        print("Gemini frame analysis failed:", type(exc).__name__, str(exc)[:250])
        return []



def retrieve_instagram_media_visual(media_id):
    """Try official Instagram Graph API media fields for a shared media ID.

    This succeeds only if the configured token has permission to read that media.
    Never logs access tokens or full URLs.
    """
    if not media_id:
        return None, None
    tokens = []
    for token in (PAGE_ACCESS_TOKEN, FOLLOW_CHECK_IG_TOKEN):
        if token and token not in tokens:
            tokens.append(token)
    for token in tokens:
        try:
            response = requests.get(
                f"https://graph.instagram.com/v26.0/{quote_plus(str(media_id), safe='')}",
                params={
                    "fields": "media_url,thumbnail_url,media_type,permalink",
                    "access_token": token,
                },
                timeout=(5, 12),
            )
            print("Instagram media lookup:", {
                "status": response.status_code,
                "token_source": "page" if token == PAGE_ACCESS_TOKEN else "follow_check",
            })
            if response.status_code != 200:
                # Log only safe error metadata, never tokenized request URL.
                try:
                    err = (response.json().get("error") or {})
                    print("Instagram media lookup error:", {
                        "type": err.get("type"),
                        "code": err.get("code"),
                        "subcode": err.get("error_subcode"),
                        "message": str(err.get("message", ""))[:180],
                    })
                except Exception:
                    pass
                continue
            data = response.json()
            media_url = data.get("media_url") or data.get("thumbnail_url")
            if media_url and isinstance(media_url, str) and media_url.startswith("https://"):
                print("Instagram Graph API returned media visual:", {
                    "media_type": data.get("media_type"),
                    "has_media_url": bool(data.get("media_url")),
                    "has_thumbnail_url": bool(data.get("thumbnail_url")),
                })
                return media_url, data.get("media_type")
            print("Instagram Graph API media lookup succeeded but returned no visual URL.")
        except Exception as exc:
            print("Instagram media lookup failed:", type(exc).__name__, str(exc)[:180])
    return None, None


def process_message_event(event):
    sender = event.get("sender") or {}
    sender_id = sender.get("id")
    postback = event.get("postback") or {}

    # Keep the existing follow-verification callback intact.
    if sender_id and postback.get("payload") == "VERIFY_FOLLOW":
        verify_follow_and_unlock(sender_id)
        return

    message = event.get("message") or {}
    attachments = message.get("attachments") or []
    if not sender_id:
        print("No sender ID in event.")
        return

    # A user directly sending a retailer URL is a separate workflow, not AI
    # product identification. Do not use arbitrary text/captions as product names.
    text_content = message.get("text") or ""
    if text_content:
        direct_links = extract_shopping_links(text_content)
        if direct_links:
            send_links_as_buttons(sender_id, direct_links, "Shopping link from message")
            return

    if not attachments:
        if text_content and re.search(
            r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/",
            text_content,
            re.I,
        ):
            send_simple_message(
                sender_id,
                "Instagram link mil gaya, lekin uska product identify karne ke liye accessible image/thumbnail chahiye. "
                "Agar post ka visual access nahi hua, product ka clear screenshot/photo DM karo. "
                "Main caption se product ka naam guess nahi karunga.",
            )
        else:
            print("No attachments in this event.")
        return

    for attachment in attachments:
        attachment_type = attachment.get("type")
        payload = attachment.get("payload") or {}

        if attachment_type == "image":
            image_urls = find_image_urls(payload, limit=3)
            if not image_urls:
                candidate = (
                    payload.get("url")
                    or payload.get("image_url")
                    or payload.get("thumbnail_url")
                )
                if candidate:
                    image_urls = [candidate]

            if not image_urls:
                send_simple_message(
                    sender_id,
                    "Image mili, lekin accessible image URL nahi mila. Product ka clear screenshot/photo dobara DM karo.",
                )
                continue
            if not GEMINI_API_KEY:
                print("Image attachment received but GEMINI_API_KEY is missing.")
                send_simple_message(sender_id, "Visual detection ke liye GEMINI_API_KEY configured nahi hai.")
                continue

            visual_products = []
            for image_index, image_url in enumerate(image_urls, start=1):
                print(f"Visual-only DM image analysis {image_index}/{len(image_urls)}")
                for name in identify_products_from_image_url(image_url, max_products=3):
                    if name and name.casefold() not in {x.casefold() for x in visual_products}:
                        visual_products.append(name)
                    if len(visual_products) >= 3:
                        break
                if len(visual_products) >= 3:
                    break

            if not visual_products:
                # Image-only fallback. This still sends the image to Gemini; it
                # never substitutes caption, title, or message text as the name.
                for image_url in image_urls:
                    name = identify_product_from_image_url(image_url)
                    if name and name.casefold() not in {"unknown", "none", "not identifiable"}:
                        visual_products.append(name)
                        break

            if visual_products:
                print("VISUAL_ONLY_DM_PRODUCTS:", visual_products)
                product_data = make_multi_product_row(
                    f"image-{sender_id}-{__import__('time').time_ns()}",
                    visual_products,
                )
                send_product_buttons(sender_id, product_data)
            else:
                send_simple_message(
                    sender_id,
                    "Image mil gayi, lekin product ko image se confidently identify nahi kar paya. "
                    "Product ka clearer/closer screenshot bhejo. Caption ya text ko product name nahi banaya gaya.",
                )
            continue

        if attachment_type not in ("share", "ig_reel", "ig_post"):
            print("Ignoring unsupported attachment type:", attachment_type)
            continue

        post_url = payload.get("url") or payload.get("link")
        media_id = (
            payload.get("reel_video_id")
            or payload.get("ig_post_media_id")
            or payload.get("media_id")
        )
        if not media_id and post_url:
            media_id = extract_reel_id(post_url)

        if not media_id:
            # Some webhook formats carry a shared post without a parseable
            # permalink. Do not fall back to caption-based product guessing.
            print("Instagram post/Reel ID unavailable in webhook payload.")
            send_simple_message(
                sender_id,
                "Post/Reel mil gayi, lekin uska ID ya accessible visual nahi mila. "
                "Post ka clear screenshot/photo DM karo; main image se product identify karunga.",
            )
            continue

        print("Detected Instagram media ID:", media_id)

        # Cache only rows that contain names explicitly generated from visual
        # detection. Legacy caption searches and retailer-link labels are stale
        # for the new visual-only requirement and must be reprocessed.
        cached = get_products_from_db(media_id)
        if cached and has_product_links(cached):
            titles = [
                str(cached.get(f"product_{i}_title") or "").strip()
                for i in range(1, 4)
            ]
            is_visual_cache = any(
                title.lower().startswith("product:") for title in titles
            )
            if is_visual_cache:
                send_product_buttons(sender_id, cached)
                continue
            print("Ignoring legacy non-visual cache entry:", media_id)

        # Public-page lookup is used ONLY to obtain a thumbnail. Captions,
        # descriptions, embedded text, and retailer links are deliberately ignored.
        image_urls = find_image_urls(payload, limit=3)
        # The webhook often provides ig_post_media_id but no image URL.
        # Try the official Graph API before relying on anonymous HTML scraping.
        if not image_urls and payload.get("ig_post_media_id"):
            graph_visual_url, graph_media_type = retrieve_instagram_media_visual(
                payload.get("ig_post_media_id")
            )
            if graph_visual_url:
                image_urls.append(graph_visual_url)
        print("Instagram media payload shape:", {
            "attachment_type": attachment_type,
            "payload_keys": sorted(str(k) for k in payload.keys())[:30],
            "image_urls_found_in_payload": len(image_urls),
            "has_post_url": bool(post_url),
        })
        if post_url:
            public_thumbnail = discover_public_thumbnail(post_url)
            print("Public thumbnail retrieval result:", bool(public_thumbnail))
            if public_thumbnail and public_thumbnail not in image_urls:
                image_urls.append(public_thumbnail)
        image_urls = image_urls[:3]
        print("Total accessible image URLs:", len(image_urls))

        # If the webhook explicitly includes a direct video URL, scan sampled frames
        # first. A normal Instagram permalink is NOT treated as a video file.
        video_url = _find_direct_video_url(payload)
        frame_products = []
        if video_url and GEMINI_API_KEY:
            for frame_bytes in extract_video_frames(video_url, max_frames=5):
                for name in identify_products_from_image_bytes(frame_bytes, max_products=3):
                    if name and name.casefold() not in {x.casefold() for x in frame_products}:
                        frame_products.append(name)
                    if len(frame_products) >= 3:
                        break
                if len(frame_products) >= 3:
                    break

        if frame_products:
            print("VISUAL_VIDEO_FRAME_PRODUCTS:", len(frame_products), frame_products)
            product_data = make_multi_product_row(str(media_id), frame_products)
            save_products_to_db(product_data)
            send_product_buttons(sender_id, product_data)
            continue

        if not image_urls:
            print("No accessible visual for Instagram media:", media_id)
            send_simple_message(
                sender_id,
                "Is Instagram post/Reel ka accessible image/thumbnail nahi mila. "
                "Instagram share mein kabhi-kabhi sirf link aata hai, video frames nahi. "
                "Product ka clear screenshot/photo DM karo; main caption se naam guess nahi karunga.",
            )
            continue

        if not GEMINI_API_KEY:
            print("Visuals available but GEMINI_API_KEY is missing.")
            send_simple_message(sender_id, "Visual product detection ke liye GEMINI_API_KEY configured nahi hai.")
            continue

        visual_products = []
        for image_index, image_url in enumerate(image_urls, start=1):
            print(f"Visual-only post/Reel analysis {image_index}/{len(image_urls)}")
            detected = identify_products_from_image_url(image_url, max_products=3)
            for name in detected:
                if name and name.casefold() not in {x.casefold() for x in visual_products}:
                    visual_products.append(name)
                if len(visual_products) >= 3:
                    break
            if len(visual_products) >= 3:
                break

        if not visual_products:
            for image_url in image_urls:
                name = identify_product_from_image_url(image_url)
                if name and name.casefold() not in {"unknown", "none", "not identifiable"}:
                    visual_products.append(name)
                    break

        if visual_products:
            print("VISUAL_ONLY_PRODUCTS:", len(visual_products), visual_products)
            product_data = make_multi_product_row(str(media_id), visual_products)
            save_products_to_db(product_data)
            send_product_buttons(sender_id, product_data)
        else:
            print("Visual detector returned no confident product name.")
            send_simple_message(
                sender_id,
                "Image/thumbnail mil gaya, lekin product ko visually confidently identify nahi kar paya. "
                "Product ka closer screenshot/photo bhejo. Caption ko product name ke liye use nahi kiya gaya.",
            )


def extract_reel_id(url):
    try:
        parts = url.split("/")
        for index, part in enumerate(parts):
            if part.lower() in ("reel", "reels", "p"):
                if index + 1 < len(parts):
                    return parts[index + 1].split("?")[0]
    except Exception as exc:
        print("Reel ID extraction error:", str(exc))
    return None



def discover_public_thumbnail(reel_url):
    """Best-effort thumbnail extraction from public Instagram HTML metadata."""
    if not reel_url or not re.match(r"^https://(?:www\.)?instagram\.com/(?:reel|reels|p)/", reel_url):
        return None
    try:
        response = requests.get(
            reel_url, timeout=(6, 15),
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            allow_redirects=True,
        )
        final_host = (urlparse(response.url).hostname or "").lower()
        content_type = response.headers.get("content-type", "").split(";")[0].lower()
        print("Public media page response:", {
            "status": response.status_code,
            "final_host": final_host,
            "content_type": content_type,
            "bytes": len(response.content or b""),
        })
        if response.status_code != 200 or "html" not in content_type:
            print("Thumbnail unavailable: response is not successful HTML.")
            return None

        page = response.text[:4_000_000]
        tags = re.findall(r"<meta\b[^>]*>", page, flags=re.I)
        candidates = []

        # Parse each meta tag independent of attribute order or quote style.
        for tag in tags:
            attrs = {}
            for match in re.finditer(
                r"""([a-zA-Z_:][-a-zA-Z0-9_:]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
                tag,
            ):
                attrs[match.group(1).lower()] = next(
                    group for group in match.groups()[1:] if group is not None
                )
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            value = attrs.get("content") or ""
            if key in {"og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
                candidates.append(value)

        # Instagram sometimes embeds image URLs in JSON rather than OG tags.
        for pattern in (
            r'"(?:thumbnail_url|display_url|image_url)"\s*:\s*"((?:https?:\\?/\\?/)[^"]+)"',
            r'"og:image"\s*:\s*"([^"]+)"',
        ):
            candidates.extend(match.group(1) for match in re.finditer(pattern, page, flags=re.I))

        for candidate in candidates:
            candidate = html.unescape(candidate)
            candidate = candidate.replace(r"\u0026", "&").replace(r"\/", "/").replace("&amp;", "&")
            candidate = candidate.replace(r"\u003D", "=").replace(r"\u003d", "=")
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            if candidate.startswith("https://") and not candidate.startswith("https://www.instagram.com/"):
                print("Public thumbnail metadata found.")
                return candidate

        # Diagnostic only: distinguish a login/challenge shell from a page with no OG image.
        lower_page = page.lower()
        if "login" in lower_page and ("accounts/login" in lower_page or "loginrequired" in lower_page):
            print("Instagram returned a login-required page; public thumbnail is not accessible.")
        elif "challenge" in lower_page or "consent" in lower_page:
            print("Instagram returned a challenge/consent page; public thumbnail is not accessible.")
        else:
            print("Instagram HTML had no recognized public image metadata; meta tags:", len(tags))
    except Exception as exc:
        print("Public thumbnail lookup failed:", type(exc).__name__, str(exc)[:250])
    return None



def inspect_public_reel_page(reel_url):
    """Best-effort second-stage scan of public Reel HTML for captions and linked products.

    Instagram may block anonymous requests or omit the creator's original product link.
    Only inspect the public Instagram Reel URL; never treat generated store-search URLs as originals.
    Returns (text_clues, original_shopping_links, thumbnail_url).
    """
    if not reel_url or not re.match(r"^https://(?:www\.)?instagram\.com/(?:reel|reels|p)/", reel_url):
        return "", [], None
    try:
        response = requests.get(
            reel_url, timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            allow_redirects=True,
        )
        print("Public Reel detector stage status:", response.status_code)
        if response.status_code != 200:
            return "", [], None
        page = response.text[:3_000_000]
        clues = []
        thumbnail = None
        # Parse Open Graph metadata regardless of attribute order.
        for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.I):
            prop = re.search(r"(?:property|name)\s*=\s*[\"']([^\"']+)[\"']", tag, flags=re.I)
            content = re.search(r"content\s*=\s*[\"']([^\"']*)[\"']", tag, flags=re.I)
            if not prop or not content:
                continue
            key = prop.group(1).lower()
            value = html.unescape(content.group(1)).replace("&amp;", "&").strip()
            if key in ("og:title", "og:description", "twitter:title", "twitter:description") and value:
                clues.append(value)
            if key in ("og:image", "twitter:image") and value.startswith("https://") and not thumbnail:
                thumbnail = value
        # Public HTML may include outbound retailer URLs in metadata/embedded JSON.
        links = extract_shopping_links(html.unescape(page).replace(r"\u0026", "&").replace(r"\/", "/"))
        text_clues = " ".join(dict.fromkeys(clues))[:1200]
        print("Public Reel metadata clues found:", bool(text_clues), "public retailer links found:", len(links), "thumbnail found:", bool(thumbnail))
        return text_clues, links, thumbnail
    except (requests.RequestException, ValueError, TypeError) as exc:
        print("Public Reel detector stage failed:", str(exc))
        return "", [], None

def find_image_urls(payload, limit=4):
    """Collect accessible image/thumbnail URLs included in the webhook payload.

    This intentionally does not treat a Reel permalink or an MP4 URL as an image.
    Instagram often sends only a permalink; in that case public-page thumbnail
    discovery remains the fallback and may fail if Instagram blocks anonymous access.
    """
    found = []
    image_keys = {
        "thumbnail_url", "image_url", "image", "media_url", "thumbnail",
        "display_url", "cover_url", "preview_url", "image_src"
    }
    def walk(value, key_hint=""):
        if len(found) >= limit:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, str(key).lower())
                if len(found) >= limit:
                    return
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, key_hint)
                if len(found) >= limit:
                    return
        elif isinstance(value, str) and value.startswith("https://"):
            path = urlparse(value).path.lower()
            image_extension = path.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif"))
            if key_hint in image_keys or image_extension:
                if value not in found:
                    found.append(value)

    walk(payload)
    return found


def find_image_url(payload):
    """Compatibility wrapper returning the first image URL, if any."""
    urls = find_image_urls(payload, limit=1)
    return urls[0] if urls else None


def identify_product_from_image_url(image_url):
    """Download a publicly accessible image and ask Gemini for a concise product query."""
    if not GEMINI_API_KEY:
        return ""
    try:
        image_response = requests.get(
            image_url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        image_response.raise_for_status()
        content_type = image_response.headers.get("Content-Type", "").split(";")[0].lower()
        if not content_type.startswith("image/"):
            print("Visual analysis skipped: URL did not return an image.")
            return ""
        if len(image_response.content) > 8 * 1024 * 1024:
            print("Visual analysis skipped: image is larger than 8 MB.")
            return ""

        mime_type = content_type if content_type in ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif") else "image/jpeg"
        encoded = base64.b64encode(image_response.content).decode("ascii")
        body = {
            "contents": [{
                "parts": [
                    {"text": (
                        "Identify the physical product shown in this image using visual evidence only. "
                        "Do not use or assume any caption, hashtags, creator text, or post context. "
                        "Return visible brand/model and product type only when supported by the image; "
                        "otherwise return UNKNOWN. Return one concise product name/search query only."
                    )},
                    {"inline_data": {"mime_type": mime_type, "data": encoded}}
                ]
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 80}
        }
        response = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=body,
            timeout=25
        )
        print("Gemini API status:", response.status_code)
        if response.status_code != 200:
            print("Gemini API error:", response.text[:800])
            return ""
        result = response.json()
        candidates = result.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        text_value = " ".join(p.get("text", "") for p in parts).strip()
        text_value = re.sub(r"\s+", " ", text_value)
        if text_value and text_value.lower() not in ("unknown", "none", "not identifiable"):
            print("Gemini visual product query:", text_value[:200])
            return text_value[:180]
    except Exception as exc:
        print("Gemini visual analysis failed:", str(exc))
    return ""



def identify_products_from_image_url(image_url, max_products=3):
    """Identify distinct visible products from a Reel thumbnail using Gemini.

    Returns a list of concise product search names. A thumbnail is not a full
    video, so this intentionally reports only products visibly supported by it.
    """
    if not GEMINI_API_KEY or not image_url:
        return []
    try:
        image_response = requests.get(
            image_url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        image_response.raise_for_status()
        content_type = image_response.headers.get("Content-Type", "").split(";")[0].lower()
        if not content_type.startswith("image/") or len(image_response.content) > 8 * 1024 * 1024:
            print("Multi-product analysis skipped: URL is not a supported image or exceeds 8 MB.")
            return []

        mime_type = content_type if content_type in (
            "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"
        ) else "image/jpeg"
        encoded = base64.b64encode(image_response.content).decode("ascii")
        prompt = (
            "Identify each distinct physical product/item visibly shown in this image, not just the overall category. "
            "Use only pixels, visible logos, and readable labels in this image; do not use caption, hashtags, creator text, or post context. "
            "Return ONLY a JSON array of 1 to " + str(max_products) + " short product search names. "
            "Include brand/model only when readable or clearly recognizable; do not invent brands. "
            "For a lineup/display containing several different bottles/items, return each distinct visible item separately. "
            "Read visible product labels/logos when legible, but do not invent text or brands. "
            "Do not return people, captions, hashtags, generic marketing phrases, or duplicate variants. "
            "If the image does not show a clearly identifiable product, return []. "
            'Example: ["Beardo Godfather perfume", "Beardo Daddy perfume", "Beardo Toxic perfume"]'
        )
        body = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": encoded}}
            ]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 180,
                "responseMimeType": "application/json"
            }
        }
        response = requests.post(
            GEMINI_URL, params={"key": GEMINI_API_KEY},
            json=body, timeout=25
        )
        print("Gemini multi-product API status:", response.status_code)
        if response.status_code != 200:
            print("Gemini multi-product API error:", response.text[:800])
            return []
        result = response.json()
        candidates = result.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        raw = " ".join(part.get("text", "") for part in parts).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        cleaned = []
        for item in parsed:
            if not isinstance(item, str):
                continue
                name = re.sub(r"\s+", " ", item).strip(" \t\r\n-•")
            if len(name) < 3:
                continue
            if name.lower() not in {x.lower() for x in cleaned}:
                cleaned.append(name[:75])
            if len(cleaned) >= max_products:
                break
        print("Gemini identified product count:", len(cleaned))
        if cleaned:
            print("Gemini identified product names:", " | ".join(cleaned))
        return cleaned
    except (requests.RequestException, ValueError, TypeError) as exc:
        print("Gemini multi-product analysis failed:", str(exc))
        return []


def build_single_product_compare_url(title):
    """Create the website URL for one product's eight store-search options."""
    from urllib.parse import urlencode
    base_url = (
        os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        or request.url_root.rstrip("/")
    )
    return base_url + "/compare?" + urlencode({
        "title": str(title)[:160],
        "search_all": "1",
        "description": "Choose a store to search for this product."
    })


def make_multi_product_row(reel_id, product_names):
    """Map up to three detected products to the existing Supabase schema."""
    row = {"reel_id": str(reel_id)}
    for index, name in enumerate(product_names[:3], start=1):
        row[f"product_{index}_title"] = "Product: " + str(name)[:75]
        row[f"product_{index}_url"] = build_single_product_compare_url(name)
    return row

def extract_shopping_links(text):
    found = []
    for raw_url in URL_RE.findall(text or ""):
        url = raw_url.rstrip(".,);]")
        try:
            host = (urlparse(url).hostname or "").lower()
            if host and any(domain in host for domain in SHOPPING_DOMAINS):
                if url not in found:
                    found.append(url)
        except Exception:
            continue
    return found[:3]


def make_search_terms(text):
    """Turn noisy Reel captions into a short, usable product search query."""
    cleaned = html.unescape(str(text or ""))
    cleaned = URL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"@\w+|#[\w.]+", " ", cleaned)
    # Remove common creator/engagement/CTA phrases that are not product details.
    noise = re.compile(
        r"\b(link in bio|follow for more|follow me|comment below|save this|share this|"
        r"dm me|dm for link|want to buy this look|shop this look|how to style|"
        r"viral|trending now|must have|must-have|check my bio|use code|limited offer|"
        r"tag someone|which one|rate this|part \d+)\b", re.I
    )
    cleaned = noise.sub(" ", cleaned)
    cleaned = re.sub(r"[🔥😍🥰✨💯❤️💕🤍🛍️👇👉]+", " ", cleaned)
    cleaned = re.sub(r"[|•·]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|,.;:! ")
    if len(cleaned) < 4:
        return ""
    # A caption with only generic words is not a reliable product identifier.
    if cleaned.lower() in {"this look", "outfit", "fashion", "shopping", "reel", "instagram"}:
        return ""
    return cleaned[:60]


def make_search_row(reel_id, terms):
    query = quote_plus(terms)
    return {
        "reel_id": str(reel_id),
        "product_1_title": f"Search Amazon: {terms}"[:80],
        "product_1_url": f"https://www.amazon.in/s?k={query}",
        "product_2_title": f"Search Flipkart: {terms}"[:80],
        "product_2_url": f"https://www.flipkart.com/search?q={query}",
        "product_3_title": f"Search Google Shopping: {terms}"[:80],
        "product_3_url": f"https://www.google.com/search?tbm=shop&q={query}",
    }


def make_product_row(reel_id, links, label):
    row = {"reel_id": str(reel_id)}
    for index, link in enumerate(links[:3], start=1):
        row[f"product_{index}_title"] = f"{label} {index}"[:80]
        row[f"product_{index}_url"] = link
    return row


def get_products_from_db(reel_id):
    if supabase is None:
        print("Supabase is not configured.")
        return None
    try:
        response = (
            supabase.table("reels_products")
            .select("*")
            .eq("reel_id", str(reel_id))
            .execute()
        )
        if response.data:
            return response.data[0]
    except Exception as exc:
        print("Supabase query error:", str(exc))
    return None


def has_product_links(data):
    return any(
        data.get(f"product_{index}_title") and data.get(f"product_{index}_url")
        for index in range(1, 4)
    )


def save_products_to_db(data):
    if supabase is None:
        print("Supabase is not configured; cannot save product links.")
        return False
    try:
        existing = (
            supabase.table("reels_products")
            .select("id")
            .eq("reel_id", str(data["reel_id"]))
            .execute()
        )
        if existing.data:
            row_id = existing.data[0].get("id")
            if row_id is not None:
                supabase.table("reels_products").update(data).eq("id", row_id).execute()
            else:
                supabase.table("reels_products").update(data).eq("reel_id", str(data["reel_id"])).execute()
        else:
            supabase.table("reels_products").insert(data).execute()
        print("Supabase product links saved for Reel ID:", data["reel_id"])
        return True
    except Exception as exc:
        print("Supabase save error:", str(exc))
        return False


def get_follow_status(sender_id):
    """Return True/False only when Instagram Login API confirms status; None means unavailable."""
    if not FOLLOW_CHECK_IG_TOKEN:
        print(
            "FOLLOW_CHECK_IG_TOKEN is missing. Follow verification needs an Instagram Login "
            "user token with instagram_business_manage_messages; Page tokens are not valid here."
        )
        return None
    try:
        response = requests.get(
            f"{FOLLOW_CHECK_API_URL}/{sender_id}",
            params={
                "fields": "is_user_follow_business",
                "access_token": FOLLOW_CHECK_IG_TOKEN,
            },
            timeout=12,
        )
        print("Follow-check API status:", response.status_code)
        if response.status_code != 200:
            print("Follow-check API error:", response.text[:1000])
            return None
        result = response.json()
        value = result.get("is_user_follow_business")
        if isinstance(value, bool):
            print("Follow-check result returned a boolean.")
            return value
        print("Follow-check API did not return a boolean is_user_follow_business field.")
    except (requests.RequestException, ValueError) as exc:
        print("Follow-check API request failed:", str(exc))
    return None


def send_follow_gate(recipient_id):
    """Show Follow and Verify buttons; never unlock links without API confirmation."""
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": [{
                        "title": "Follow to unlock product links",
                        "subtitle": "Follow @find_myproduct52, then tap Verify Follow.",
                        "buttons": [
                            {"type": "web_url", "url": FOLLOW_PAGE_URL, "title": "Follow Page"},
                            {"type": "postback", "title": "Verify Follow", "payload": "VERIFY_FOLLOW"},
                        ],
                    }],
                },
            }
        },
    }
    send_api_request(payload)


def verify_follow_and_unlock(sender_id):
    status = get_follow_status(sender_id)
    if status is None:
        send_simple_message(
            sender_id,
            "Follow verification abhi available nahi hai. Admin ko Instagram Login token (FOLLOW_CHECK_IG_TOKEN) aur instagram_business_manage_messages permission configure karni hogi. Product links abhi unlock nahi kiye gaye.",
        )
        return
    if not status:
        send_simple_message(sender_id, "Abhi follow confirm nahi hua. @find_myproduct52 ko follow karke Verify Follow dobara dabao.")
        send_follow_gate(sender_id)
        return
    product_data = PENDING_FOLLOW_PRODUCTS.pop(str(sender_id), None)
    if product_data:
        send_simple_message(sender_id, "Follow verified! Ye rahe tumhare product links 👇")
        send_product_buttons(sender_id, product_data, skip_follow_check=True)
    else:
        send_simple_message(sender_id, "Follow verified! Ab Reel dobara share karo, main product links bhej dunga.")


def build_comparison_url(data):
    """Build the comparison page URL; search rows populate all supported stores."""
    title = ""
    stores = {}
    domain_to_store = (
        ("amazon", ("amazon.", "amzn.to")),
        ("flipkart", ("flipkart.com", "fkrt.it")),
        ("meesho", ("meesho.com",)),
        ("myntra", ("myntra.com",)),
        ("ajio", ("ajio.com",)),
        ("google", ("google.com", "google.co.in")),
        ("nykaa", ("nykaa.com",)),
        ("snapdeal", ("snapdeal.com",)),
    )
    labels = []
    urls = []
    for index in range(1, 4):
        label = str(data.get(f"product_{index}_title") or "").strip()
        url = str(data.get(f"product_{index}_url") or "").strip()
        if label:
            labels.append(label)
        if url.startswith(("https://", "http://")):
            urls.append((label, url))
    # Search rows are generated by this bot and should create searches for every store.
    search_label = next((x for x in labels if x.lower().startswith("search amazon:")), "")
    if search_label:
        title = re.sub(r"^Search Amazon:\s*", "", search_label, flags=re.I).strip()
        q = quote_plus(title)
        stores = {
            "amazon_url": f"https://www.amazon.in/s?k={q}",
            "flipkart_url": f"https://www.flipkart.com/search?q={q}",
            "meesho_url": f"https://www.meesho.com/search?q={q}",
            "myntra_url": f"https://www.myntra.com/{q.replace('+', '-')}",
            "ajio_url": f"https://www.ajio.com/search/?text={q}",
            "google_url": f"https://www.google.com/search?tbm=shop&q={q}",
            "nykaa_url": f"https://www.nykaa.com/search/result/?q={q}",
            "snapdeal_url": f"https://www.snapdeal.com/search?keyword={q}",
        }
    else:
        for label, url in urls:
            if not title and label:
                title = re.sub(r"^(Link from Reel caption/metadata|Shopping link from message|Shopping link found)\s*", "", label, flags=re.I).strip()
            host = (urlparse(url).hostname or "").lower()
            matched = False
            for store, domains in domain_to_store:
                if any(domain in host for domain in domains):
                    stores.setdefault(store + "_url", url)
                    matched = True
                    break
            if not matched:
                stores.setdefault("extra_url", url)
        if not title:
            title = "Product from Instagram"
    base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")
    from urllib.parse import urlencode
    return base_url + "/compare?" + urlencode({"title": title[:180], **stores})


def send_product_buttons(recipient_id, data, skip_follow_check=False):
    """Send named, individual product cards; keep existing follow gate intact."""
    if not skip_follow_check and FOLLOW_CHECK_IG_TOKEN:
        status = get_follow_status(recipient_id)
        if status is not True:
            PENDING_FOLLOW_PRODUCTS[str(recipient_id)] = data
            if status is False:
                send_follow_gate(recipient_id)
            else:
                send_simple_message(
                    recipient_id,
                    "Product links bhejne se pehle follow status verify karna zaroori hai, lekin Instagram follow-check API abhi fail ho rahi hai. Admin ko Instagram Login token (FOLLOW_CHECK_IG_TOKEN) aur permissions check karni hongi. Links unlock nahi kiye gaye.",
                )
            return

    # New multi-product rows: each product has its own named card and its own
    # comparison URL. The URL opens the eight-store chooser for that product.
    card_elements = []
    for index in range(1, 4):
        raw_title = str(data.get(f"product_{index}_title") or "").strip()
        url = str(data.get(f"product_{index}_url") or "").strip()
        if raw_title.lower().startswith("product:") and url.startswith(("https://", "http://")):
            title = raw_title.split(":", 1)[1].strip()[:80]
            if not title:
                continue
            card_elements.append({
                "title": title,
                "subtitle": "Choose a store to shop this product",
                "buttons": [{
                    "type": "web_url",
                    "url": url,
                    "title": "Shop Now"
                }]
            })

    if card_elements:
        # Instagram generic templates support a bounded number of elements.
        # We currently save up to three product names in the existing schema.
        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "generic",
                        "elements": card_elements
                    }
                }
            }
        }
        send_api_request(payload)
        return

    # Existing single-search rows should open the website instead of dumping
    # eight long URLs into Instagram DM.
    search_title = next(
        (str(data.get(f"product_{i}_title") or "") for i in range(1, 4)
         if str(data.get(f"product_{i}_title") or "").lower().startswith("search ")),
        "",
    )
    if search_title and ":" in search_title:
        terms = search_title.split(":", 1)[1].strip()[:60]
        url = build_single_product_compare_url(terms)
        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "generic",
                        "elements": [{
                            "title": terms[:80],
                            "subtitle": "Choose a store to shop this product",
                            "buttons": [{
                                "type": "web_url",
                                "url": url,
                                "title": "Shop Now"
                            }]
                        }]
                    }
                }
            }
        }
        send_api_request(payload)
        return

    # Preserve legacy direct shopping-link handling for messages containing
    # retailer URLs; these are not fabricated into AI product names.
    lines = ["🛍️ Product links", ""]
    count = 0
    for index in range(1, 4):
        title = str(data.get(f"product_{index}_title") or "").strip()
        url = str(data.get(f"product_{index}_url") or "").strip()
        if title and url.startswith(("https://", "http://")):
            count += 1
            lines.append(f"{count}. {title[:55]}: {url}")
    if count:
        _send_text_in_chunks(recipient_id, "\n".join(lines), max_chars=850)
    else:
        send_simple_message(recipient_id, "Is Reel ke product links abhi available nahi hain.")

def _send_text_in_chunks(recipient_id, text, max_chars=850):
    """Send line-by-line DM text in chunks below Instagram's 1000-char limit."""
    chunks = []
    current = ""
    for line in str(text or "").splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
        # Extremely long individual URLs/lines are split defensively.
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            current = current[max_chars:]
    if current:
        chunks.append(current)
    for chunk in chunks:
        send_simple_message(recipient_id, chunk[:950])


def send_links_as_buttons(recipient_id, links, label):
    row = make_product_row("message-link", links, label)
    send_product_buttons(recipient_id, row)


def send_simple_message(recipient_id, text):
    send_api_request({
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    })


def send_api_request(payload):
    if not PAGE_ACCESS_TOKEN:
        print("ERROR: PAGE_ACCESS_TOKEN is missing.")
        return
    try:
        response = requests.post(API_URL, json=payload, timeout=15)
        print("Instagram API status:", response.status_code)
        print("Instagram API response:", response.text[:1500])
    except requests.RequestException as exc:
        print("Instagram API request failed:", str(exc))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
    
