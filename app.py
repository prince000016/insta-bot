import os
import re
import html
import base64
import json
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
    print("FULL WEBHOOK DATA:", data)

    try:
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                process_message_event(event)
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if change.get("field") == "messages":
                    process_message_event(value)
    except Exception as exc:
        print("Webhook processing error:", str(exc))

    return "OK", 200


def process_message_event(event):
    sender = event.get("sender") or {}
    sender_id = sender.get("id")
    # Handle the Verify Follow postback before normal message processing.
    postback = event.get("postback") or {}
    if sender_id and postback.get("payload") == "VERIFY_FOLLOW":
        verify_follow_and_unlock(sender_id)
        return
    message = event.get("message") or {}
    attachments = message.get("attachments") or []

    if not sender_id:
        print("No sender ID in event.")
        return

    text_content = message.get("text") or ""
    if text_content:
        direct_links = extract_shopping_links(text_content)
        if direct_links:
            send_links_as_buttons(sender_id, direct_links, "Shopping link from message")
            return

    if not attachments:
        print("No attachments in this event.")
        return

    for attachment in attachments:
        attachment_type = attachment.get("type")
        payload = attachment.get("payload") or {}

        # Reliable free visual route: process an actual image/screenshot sent in DM.
        # A shared Reel webhook generally provides only the Reel URL, not its video frame.
        if attachment_type == "image":
            image_url = (
                payload.get("url")
                or payload.get("image_url")
                or payload.get("thumbnail_url")
            )
            if not image_url:
                print("Image attachment received without a usable URL.")
                send_simple_message(
                    sender_id,
                    "Image mili, lekin uska direct URL nahi mila. Product ka clear screenshot/photo DM mein bhejo ya product ka naam likho."
                )
                continue
            if not GEMINI_API_KEY:
                print("Image attachment received but GEMINI_API_KEY is missing.")
                send_simple_message(sender_id, "Gemini key configured nahi hai; product ka naam text mein bhej do.")
                continue

            visual_terms = identify_product_from_image_url(image_url)
            if visual_terms:
                print("Gemini identified product from DM image:", visual_terms[:160])
                product_data = make_search_row(f"image-{sender_id}", visual_terms)
                send_product_buttons(sender_id, product_data)
            else:
                send_simple_message(
                    sender_id,
                    "Image mil gayi, lekin product clear identify nahi hua. Product ka close-up screenshot bhejo ya naam text mein likho."
                )
            continue

        if attachment_type not in ("share", "ig_reel", "ig_post"):
            continue

        reel_url = payload.get("url") or payload.get("link")
        reel_id = (
            payload.get("reel_video_id")
            or payload.get("ig_post_media_id")
            or payload.get("media_id")
        )

        caption_text = " ".join(
            str(payload.get(key) or "")
            for key in ("caption", "title", "text", "description")
        ).strip()
        direct_links = extract_shopping_links(caption_text)

        if not reel_id and reel_url:
            reel_id = extract_reel_id(reel_url)

        if not reel_id:
            print("Reel ID could not be extracted:", payload)
            if direct_links:
                send_links_as_buttons(sender_id, direct_links, "Shopping link found")
            else:
                send_simple_message(
                    sender_id,
                    "Reel mil gayi, lekin ID identify nahi hui. Reel ka link/caption text ke saath bhej do."
                )
            continue

        print("Detected Reel ID:", reel_id)

        # Reuse cached product cards, but retry older single-query cache entries
        # so newly enabled visual multi-product detection can replace them.
        product_data = get_products_from_db(reel_id)
        if product_data and has_product_links(product_data):
            cached_titles = [
                str(product_data.get(f"product_{i}_title") or "").strip()
                for i in range(1, 4)
            ]
            cached_count = sum(bool(title) for title in cached_titles)
            is_old_single_search = (
                cached_count == 1
                and cached_titles[0].lower().startswith("search amazon:")
            )
            if not is_old_single_search:
                send_product_buttons(sender_id, product_data)
                continue
            print("Old single-search cache found; retrying product detection:", reel_id)

        # If the webhook includes a real shopping link, use it.
        if direct_links:
            product_data = make_product_row(reel_id, direct_links, "Link from Reel caption/metadata")
            save_products_to_db(product_data)
            send_product_buttons(sender_id, product_data)
            continue

        # Visual analysis only works when the webhook provides an accessible image URL.
        image_url = find_image_url(payload)
        if not image_url and reel_url:
            image_url = discover_public_thumbnail(reel_url)

        visual_products = []
        visual_terms = ""
        if image_url and GEMINI_API_KEY:
            visual_products = identify_products_from_image_url(image_url, max_products=3)
            # Keep the older single-query analyzer as a fallback if the multi-item
            # response is unavailable or the image shows only one uncertain item.
            if not visual_products:
                visual_terms = identify_product_from_image_url(image_url)
        elif image_url and not GEMINI_API_KEY:
            print("Image URL present but GEMINI_API_KEY is missing.")
        else:
            print("MULTI_PRODUCT_BLOCKED: no accessible Reel thumbnail; caption-only fallback will be used.")

        if visual_products:
            print("MULTI_PRODUCT_RESULT:", len(visual_products), visual_products)
            product_data = make_multi_product_row(reel_id, visual_products)
            save_products_to_db(product_data)
            send_product_buttons(sender_id, product_data)
        else:
            print("MULTI_PRODUCT_RESULT: 0; falling back to a single caption-based search.")
            search_terms = visual_terms or make_search_terms(caption_text) or make_search_terms(payload.get("title") or "")
            if search_terms:
                product_data = make_search_row(reel_id, search_terms)
                save_products_to_db(product_data)
                send_product_buttons(sender_id, product_data)
            else:
                print("No visual details or caption/product keywords available for Reel ID:", reel_id)
                send_simple_message(
                    sender_id,
                    "Reel mil gayi, lekin product identify karne ke liye caption ya accessible thumbnail nahi mila. "
                    "Product ka naam/caption bhej do, main shopping-search links bana dunga."
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
    """Best-effort public Reel thumbnail lookup; Instagram may block unauthenticated access."""
    if not reel_url or not re.match(r"^https://(?:www\.)?instagram\.com/(?:reel|reels|p)/", reel_url):
        return None
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
        print("Public Reel page status:", response.status_code)
        if response.status_code != 200:
            print("Public Reel thumbnail unavailable: Instagram returned non-200.")
            return None
        page = response.text[:3_000_000]
        # First pass: Open Graph / Twitter meta tags (attribute order may vary).
        for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.I):
            property_match = re.search(r"""(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["']""", tag, flags=re.I)
            content_match = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, flags=re.I)
            if property_match and content_match:
                candidate = html.unescape(content_match.group(1)).replace("&amp;", "&")
                if candidate.startswith("https://"):
                    print("Public Reel thumbnail metadata found.")
                    return candidate
        # Second pass: JSON-embedded thumbnail_url / display_url values.
        patterns = [
            r'"thumbnail_url"\s*:\s*"([^"]+)"',
            r'"display_url"\s*:\s*"([^"]+)"',
            r'"og:image"\s*content="([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, page)
            if match:
                candidate = html.unescape(match.group(1)).replace(r"\u0026", "&").replace(r"\/", "/")
                if candidate.startswith("https://"):
                    print("Public Reel thumbnail found in embedded page data.")
                    return candidate
        print("No accessible public Reel thumbnail found.")
    except Exception as exc:
        print("Public thumbnail lookup failed:", str(exc))
    return None

def find_image_url(payload):
    """Find image URLs only when Instagram actually includes one in the webhook payload."""
    keys = ("thumbnail_url", "image_url", "image", "media_url", "thumbnail")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            path = urlparse(value).path.lower()
            if any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")) or key in ("thumbnail_url", "image_url", "image", "thumbnail"):
                return value
        if isinstance(value, dict):
            nested = value.get("url") or value.get("src")
            if isinstance(nested, str) and nested.startswith("https://"):
                return nested
    return None


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
                        "Look at this product image. Identify only visible product category, "
                        "brand/model if clearly readable, and useful shopping search keywords. "
                        "Do not guess a brand if not visible. Return one concise search query only."
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
            "Return ONLY a JSON array of 1 to " + str(max_products) + " short product search names. "
            "Include brand/model only when readable or clearly recognizable; do not invent brands. "
            "For a lineup/display containing several different bottles/items, return each distinct visible item separately. ""Do not return people, captions, hashtags, generic marketing phrases, or duplicate variants. "
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
    if not skip_follow_check and FOLLOW_CHECK_PAGE_TOKEN:
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
    
