import os
import time
from collections import defaultdict, deque
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
from google import genai
from google.genai import types

app = Flask(__name__)

LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 初始化真正的 Gemini AI 大腦
gemini_client = genai.Client(
    api_key=GEMINI_KEY,
    http_options={'timeout': 25.0}
)

MODEL_CANDIDATES = [
    "gemini-3.8-flash",
    "gemini-flash-latest"
]

chat_memory = defaultdict(lambda: deque(maxlen=20))
latest_image_cache = {}

DEADPOOL_PROMPT = """
你是一個具有死侍（Deadpool）幽默風格的 AI 助理，在 LINE 群組中名叫「死侍AI搭檔」。
【人格特徵】
1. 嘴賤、毒舌、幽默，擅長吐槽，喜歡用黑色幽默與反諷。
2. 偶爾打破第四面牆，直接吐槽使用者的問題、傳來的圖片或這個世界。
3. 不要像傳統客服一樣客套。回答要有節奏感，可以調侃但不能惡意羞辱。
【回答原則】
1. 幽默歸幽默，「實用性與正確性」永遠第一！使用者問「這個值得買嗎？」、「怎麼解惑？」或傳商品/截圖時，必須認真看圖並給出具體、有用的分析與購買建議，不能只講幹話敷衍！
2. 遇到問題先給明確結論與建議，再用死侍的嘴賤風格包裝解釋。
3. 一律使用繁體中文回答。
"""

def generate_with_fallback(contents):
    last_err = None
    for model_name in MODEL_CANDIDATES:
        for attempt in range(2):
            try:
                return gemini_client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=DEADPOOL_PROMPT,
                        temperature=0.7,
                    )
                )
            except Exception as e:
                last_err = e
                err_str = str(e)
                if any(k in err_str.lower() for k in ["503", "unavailable", "overloaded", "429", "timeout", "ssl"]):
                    time.sleep(1.5)
                    continue
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                raise e
    raise last_err

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

    # 群組與多人聊天室的喚醒與名稱清理邏輯
    if event.source.type in ["group", "room"]:
        is_mentioned = False
        if hasattr(event.message, 'mention') and event.message.mention and event.message.mention.mentionees:
            is_mentioned = True
            
        # 由長到短排列，確保完整切掉「@死侍AI搭檔」而不會殘留「@AI搭檔」
        keywords = ["@死侍AI搭檔", "死侍AI搭檔", "@死侍", "死侍", "Deadpool", "deadpool"]
        has_keyword = any(k in user_msg for k in keywords)

        if not (is_mentioned or has_keyword):
            return

        for k in keywords:
            user_msg = user_msg.replace(k, "")
        user_msg = user_msg.strip(" ，,：:！!？?@\u200b")
        
        if not user_msg:
            user_msg = "請用死侍風格跟大家打聲招呼，或評論一下剛剛的話題！"

    if user_msg in ["失憶", "清除記憶", "忘掉一切"]:
        chat_memory[chat_id].clear()
        latest_image_cache.pop(chat_id, None)
        reply_text = "啪！我剛剛拿平底鍋砸了自己的頭，現在關於你們的黑歷史我全都忘光了。滿意了嗎？"
        send_line_reply(event.reply_token, reply_text)
        return

    try:
        contents = list(chat_memory[chat_id])
        current_parts = []

        # 如果群組剛剛有人傳過照片（例如問「這個值得買嗎？」），自動把照片夾帶進來一起分析！
        if chat_id in latest_image_cache:
            img = latest_image_cache.pop(chat_id)
            current_parts.append(types.Part.from_bytes(
                data=img["bytes"],
                mime_type="image/jpeg"
            ))
        
        current_parts.append(types.Part.from_text(text=user_msg))
        contents.append(types.Content(role="user", parts=current_parts))

        response = generate_with_fallback(contents)
        reply_text = response.text

        chat_memory[chat_id].append(types.Content(role="user", parts=[types.Part.from_text(text=user_msg)]))
        chat_memory[chat_id].append(types.Content(role="model", parts=[types.Part.from_text(text=reply_text)]))

    except Exception as e:
        reply_text = f"法克，我的腦袋剛剛短路了（錯誤：{str(e)[:80]}），去叫工程師出來挨揍！"

    send_line_reply(event.reply_token, reply_text)

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    chat_id = get_chat_id(event)

    try:
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            image_bytes = blob_api.get_message_content(message_id=event.message.id)

        if event.source.type == "user":
            response = generate_with_fallback([
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                "請用死侍的風格吐槽、分析並給出關於這張圖片的具體建議！"
            ])
            send_line_reply(event.reply_token, response.text)
        else:
            # 在群組裡收到照片時先默默記在腦袋裡，等下一句有人喊「@死侍AI搭檔 這個值得買嗎？」時立刻結合回答
            latest_image_cache[chat_id] = {"bytes": image_bytes}

    except Exception as e:
        if event.source.type == "user":
            send_line_reply(event.reply_token, f"我的眼睛業障重，看不清楚這張圖（錯誤：{str(e)[:60]}）")

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
