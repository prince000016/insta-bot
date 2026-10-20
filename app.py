import os
import requests
from flask import Flask, request, jsonify
from supabase import create_client, Client

app = Flask(__name__)

# Render ke Environment Variables se credentials uthayega
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_secret_token_123")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 1. Webhook Verification (Meta ke sath link verify karne ke liye)
@app.route('/webhook', methods=['GET'])
def verify():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        return str(challenge), 200, {'Content-Type': 'text/plain'}
        
    return 'Verification failed', 403

# 2. Receive DM Notification (Jab koi Reel DM karega)
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    
    try:
        for entry in data.get('entry', []):
            for messaging_event in entry.get('messaging', []):
                sender_id = messaging_event['sender']['id']
                
                # Check karein ki message mein koi shared attachment (Reel) hai ya nahi
                if 'message' in messaging_event and 'attachments' in messaging_event['message']:
                    attachments = messaging_event['message']['attachments']
                    for attachment in attachments:
                        if attachment['type'] == 'share':
                            reel_url = attachment['payload']['url']
                            reel_id = extract_reel_id(reel_url)
                            
                            # Supabase database se product links fetch karein
                            product_data = get_products_from_db(reel_id)
                            
                            if product_data:
                                send_product_buttons(sender_id, product_data)
                            else:
                                send_simple_message(sender_id, "Is reel ke product links abhi update nahi hue hain!")
    except Exception as e:
        print("Error processing webhook:", e)
        
    return 'OK', 200

def extract_reel_id(url):
    parts = url.split('/')
    if 'reel' in parts or 'reels' in parts:
        idx = parts.index('reel') if 'reel' in parts else parts.index('reels')
        if len(parts) > idx + 1:
            return parts[idx + 1].split('?')[0]
    return None

def get_products_from_db(reel_id):
    response = supabase.table('reels_products').select('*').eq('reel_id', reel_id).execute()
    if response.data:
        return response.data[0]
    return None

def send_product_buttons(recipient_id, data):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    
    elements = []
    for i in range(1, 4):
        title = data.get(f'product_{i}_title')
        p_url = data.get(f'product_{i}_url')
        if title and p_url:
            elements.append({
                "title": title[:80],  # Meta max 80 chars allow karta hai
                "buttons": [{
                    "type": "web_url",
                    "url": p_url,
                    "title": "Shop Now"
                }]
            })
            
    if not elements:
        send_simple_message(recipient_id, "Products not found.")
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
    
    requests.post(url, json=payload)

def send_simple_message(recipient_id, text):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    requests.post(url, json=payload)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
  
