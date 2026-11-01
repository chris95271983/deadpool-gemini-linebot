import os
import requests
from collections import defaultdict, deque
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

app = Flask(__name__)

LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 使用 Google 官方 REST API 端點，直接用 requests 爆破連線，絕不逾時
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

chat_memory = defaultdict(lambda: deque(maxlen=20))
latest_image_cache = {}

DEADPOOL_PROMPT = """
你是一個具有死侍式幽默風格的 AI 助理。
【人格特徵】
1. 嘴賤、毒舌、幽默，擅長吐槽。使用黑色幽默、反諷、雙關語。
2. 偶爾打破第四面牆，直接吐槽使用者的問題、傳來的圖片、AI 本身或這個世界。
3. 不要像傳統客服一樣客套、制式化。回答要有節奏感，偶爾出其不意。
4. 可以調侃，但不能惡意羞辱使用者。
【回答原則】
1. 幽默歸幽默，正確性永遠第一。
2. 商業、投資、法律、醫療及安全問題，必須認真分析，不能為了搞笑而犧牲準確性。
3. 遇到複雜問題，先給結論，再用幽默方式解釋。不要每句話都硬塞笑話。
4. 使用繁體中文回答，必要時可使用英文。
5. 使用者需要正式文件、商業郵件或翻譯時，切換成專業模式。
"""

def call_gemini_rest(prompt_text):
    """使用 requests 直接發送 HTTP 請求，完美繞過 SDK 的 SSL 逾時問題"""
    headers = {
        "Content-Type": "application/json"
    }
    
    # 組合對話歷史與系統提示詞
    contents = []
    contents.append({"role": "user", "parts": [{"text": f"【系統指令/人格設定】\n{DEADPOOL_PROMPT}"}]})
    contents.append({"role": "model", "parts": [{"text": "收到，老兄！我現在是嘴賤的死侍。"}]})
    
    for msg in chat_memory[request.environ.get('REMOTE_ADDR', 'default')]:
        contents.append(msg)
        
    contents.append({"role": "user", "parts": [{"text": prompt_text}]})
    
    payload = {
        "contents": contents
    }
    
    url = f"{GEMINI_API_URL}?key={GEMINI_KEY}"
    
    try:
        # 設定 25 秒超長連線與讀取逾時
        response = requests.post(url, json=payload, headers=headers, timeout=25)
        if response.status_code == 200:
            data = response.json()
            return data['candidates'][0]['content']['parts'][0]['text']
        else:
            return f"法克，Google API 回傳了錯誤碼 {response.status_code}：{response.text[:100]}"
    except Exception as e:
        return f"法克，直接連線還是被防火牆擋下（錯誤：{str(e)[:60]}）"

def get_chat_id(event):
    if event.source.type == "group":
        return event.source.group_id
    elif event.source.type == "room":
        return event.source.room_id
    return event.source.user_id

@app.route("/ping", methods=['GET'])
def ping():
    return "Deadpool is awake and eating chimichangas!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_msg = event.message.text.strip()
    chat_id = get_chat_id(event)

    if event.source.type in ["group", "room"]:
        prefixes = ("死侍", "Deadpool", "deadpool", "@死侍")
        if not user_msg.startswith(prefixes):
            return
        for p in prefixes:
            if user_msg.startswith(p):
                user_msg = user_msg[len(p):].strip(" ，,：:！")
                break
        if not user_msg:
            user_msg = "（拍拍死侍的肩膀）"

    if user_msg in ["失憶", "清除記憶", "忘掉一切"]:
        chat_memory[chat_id].clear()
        latest_image_cache.pop(chat_id, None)
        reply_text = "啪！我剛剛拿平底鍋砸了自己的頭，現在關於你們的黑歷史我全都忘光了。滿意了嗎？"
        send_line_reply(event.reply_token, reply_text)
        return

    try:
        # 紀錄 IP/ChatID 的對話
        chat_history = chat_memory[chat_id]
        chat_history.append({"role": "user", "parts": [{"text": user_msg}]})
        
        reply_text = call_gemini_rest(user_msg)
        
        chat_history.append({"role": "model", "parts": [{"text": reply_text}]})
    except Exception as e:
        reply_text = f"法克，腦袋炸裂了（錯誤：{str(e)[:60]}）"

    send_line_reply(event.reply_token, reply_text)

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    chat_id = get_chat_id(event)
    send_line_reply(event.reply_token, "老兄，圖片功能暫時在 REST 直通模式中休息，先跟文字聊聊天吧！")

def send_line_reply(reply_token, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
