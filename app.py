import os
import re
import html
import requests
from urllib.parse import quote_plus, urlparse
from flask import Flask, request
from supabase import create_client

app = Flask(__name__)

PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "").strip()

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

API_URL = (
    "https://graph.instagram.com/v26.0/me/messages"
    f"?access_token={PAGE_ACCESS_TOKEN}"
)

# Only links on these shopping domains are treated as direct product links.
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
    message = event.get("message") or {}
    attachments = message.get("attachments") or []

    if not sender_id:
        print("No sender ID in event.")
        return

    # Some webhook events may contain plain text/caption-like content.
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

        if attachment_type not in ("share", "ig_reel", "ig_post"):
            continue

        reel_url = payload.get("url") or payload.get("link")
        reel_id = (
            payload.get("reel_video_id")
            or payload.get("ig_post_media_id")
            or payload.get("media_id")
        )

        # Check any caption/text actually supplied in the webhook payload.
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
                    "Reel mil gayi, lekin ID identify nahi hui. Is Reel ka link/caption text ke saath bhej do."
                )
            continue

        print("Detected Reel ID:", reel_id)

        # Keep existing behaviour first: known products from Supabase.
        product_data = get_products_from_db(reel_id)
        if product_data and has_product_links(product_data):
            send_product_buttons(sender_id, product_data)
            continue

        # Prefer a shopping URL if the webhook actually provided one.
        if direct_links:
            product_data = make_product_row(reel_id, direct_links, "Link from Reel caption/metadata")
            save_products_to_db(product_data)
            send_product_buttons(sender_id, product_data)
            continue

        # No caption/link was supplied. Use only text we actually have.
        search_terms = make_search_terms(caption_text or payload.get("title") or "")
        if search_terms:
            product_data = make_search_row(reel_id, search_terms)
            save_products_to_db(product_data)
            send_product_buttons(sender_id, product_data)
        else:
            print("No caption/product keywords available for Reel ID:", reel_id)
            send_simple_message(
                sender_id,
                "Reel mil gayi, lekin Instagram ne product caption nahi diya. "
                "Product ka naam ya caption text bhej do; phir main shopping-search links bana sakta hoon."
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


def extract_shopping_links(text):
    """Extract only URLs whose host looks like a known shopping domain."""
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
    """Use available text only; do not pretend to identify unseen video content."""
    cleaned = html.unescape(str(text or ""))
    cleaned = URL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"@\w+|#\w+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Avoid generic links from short/empty captions.
    if len(cleaned) < 4:
        return ""
    return cleaned[:180]


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

    # Keep the existing table and columns. Update existing reel row, otherwise insert.
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


def send_product_buttons(recipient_id, data):
    elements = []

    for index in range(1, 4):
        title = data.get(f"product_{index}_title")
        product_url = data.get(f"product_{index}_url")

        if title and product_url and str(product_url).startswith(("https://", "http://")):
            elements.append({
                "title": str(title)[:80],
                "buttons": [{
                    "type": "web_url",
                    "url": str(product_url),
                    "title": "Open Link" if "Search " in str(title) else "Buy Now"
                }]
            })

    if not elements:
        send_simple_message(recipient_id, "Is Reel ke product links abhi available nahi hain.")
        return

    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements
                }
            }
        }
    }
    send_api_request(payload)


def send_links_as_buttons(recipient_id, links, label):
    row = make_product_row("message-link", links, label)
    send_product_buttons(recipient_id, row)


def send_simple_message(recipient_id, text):
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    send_api_request(payload)


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
        
