import os
import requests
from flask import Flask, request
from supabase import create_client

app = Flask(__name__)

PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "").strip()

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

API_URL = (
    "https://graph.instagram.com/v26.0/me/messages"
    f"?access_token={PAGE_ACCESS_TOKEN}"
)


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
            # Messaging webhook events
            for event in entry.get("messaging", []):
                process_message_event(event)

            # Changes-format events, including test payloads
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if change.get("field") == "messages":
                    process_message_event(value)

    except Exception as exc:
        print("Webhook processing error:", str(exc))

    # Return 200 so Meta does not repeatedly retry this delivery.
    return "OK", 200


def process_message_event(event):
    sender = event.get("sender") or {}
    sender_id = sender.get("id")

    message = event.get("message") or {}
    attachments = message.get("attachments") or []

    if not sender_id or not attachments:
        print("No sender ID or attachments in this event.")
        return

    for attachment in attachments:
        if attachment.get("type") != "share":
            continue

        payload = attachment.get("payload") or {}
        reel_url = payload.get("url")

        if not reel_url:
            print("Share attachment had no URL.")
            continue

        reel_id = extract_reel_id(reel_url)
        if not reel_id:
            send_simple_message(
                sender_id,
                "Reel link mil gaya, lekin uska ID identify nahi hua."
            )
            continue

        product_data = get_products_from_db(reel_id)

        if product_data:
            send_product_buttons(sender_id, product_data)
        else:
            send_simple_message(
                sender_id,
                "Is reel ke product links abhi database mein nahi mile."
            )


def extract_reel_id(url):
    try:
        parts = url.split("/")
        for index, part in enumerate(parts):
            if part.lower() in ("reel", "reels"):
                if index + 1 < len(parts):
                    return parts[index + 1].split("?")[0]
    except Exception as exc:
        print("Reel ID extraction error:", str(exc))

    return None


def get_products_from_db(reel_id):
    try:
        response = (
            supabase.table("reels_products")
            .select("*")
            .eq("reel_id", reel_id)
            .execute()
        )
        if response.data:
            return response.data[0]
    except Exception as exc:
        print("Supabase query error:", str(exc))

    return None


def send_product_buttons(recipient_id, data):
    elements = []

    for index in range(1, 4):
        title = data.get(f"product_{index}_title")
        product_url = data.get(f"product_{index}_url")

        if title and product_url:
            elements.append({
                "title": str(title)[:80],
                "buttons": [{
                    "type": "web_url",
                    "url": product_url,
                    "title": "Buy Now"
                }]
            })

    if not elements:
        send_simple_message(
            recipient_id,
            "Is reel ke product links abhi available nahi hain."
        )
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
