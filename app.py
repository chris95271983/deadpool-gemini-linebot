import os
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

# 讀取環境變數中的三把鑰匙
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)
gemini_client = genai.Client(api_key=GEMINI_KEY)

# 【升級 1：短期記憶庫】每個聊天室記住最近 10 輪對話（20 句話）
chat_memory = defaultdict(lambda: deque(maxlen=20))

# 【升級 2：圖片暫存區】記住每個聊天室剛剛傳的最後一張圖片
latest_image_cache = {}

# 死侍專屬人格設定
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

def get_chat_id(event):
    """取得聊天室的唯一 ID（區分不同群組與個人私訊）"""
    if event.source.type == "group":
        return event.source.group_id
    elif event.source.type == "room":
        return event.source.room_id
    return event.source.user_id

# 【升級 3：防休眠專屬通道】給 UptimeRobot 定時喚醒用
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

# 處理「文字訊息」
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_msg = event.message.text.strip()
    chat_id = get_chat_id(event)

    # 群組防吵機制：在群組裡開頭必須叫「死侍」才觸發（私訊則有問必答）
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

    # 特殊指令：清除記憶與圖片暫存
    if user_msg in ["失憶", "清除記憶", "忘掉一切"]:
        chat_memory[chat_id].clear()
        latest_image_cache.pop(chat_id, None)
        reply_text = "啪！我剛剛拿平底鍋砸了自己的頭，現在關於你們的黑歷史我全都忘光了。滿意了嗎？"
        send_line_reply(event.reply_token, reply_text)
        return

    try:
        # 載入該聊天室的歷史對話紀錄
        contents = list(chat_memory[chat_id])
        current_parts = []

        # 檢查該聊天室有沒有剛傳過來的圖片
        if chat_id in latest_image_cache:
            img = latest_image_cache.pop(chat_id)
            current_parts.append(types.Part.from_bytes(
                data=img["bytes"],
                mime_type="image/jpeg"
            ))
        
        current_parts.append(types.Part.from_text(text=user_msg))
        contents.append(types.Content(role="user", parts=current_parts))

        # 呼叫 Gemini 2.5 Flash
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=DEADPOOL_PROMPT,
                temperature=0.8,
            )
        )
        reply_text = response.text

        # 將本輪對話存入短期記憶庫（僅存文字以節省記憶體）
        chat_memory[chat_id].append(types.Content(role="user", parts=[types.Part.from_text(text=user_msg)]))
        chat_memory[chat_id].append(types.Content(role="model", parts=[types.Part.from_text(text=reply_text)]))

    except Exception as e:
        reply_text = f"法克，我的腦袋剛剛短路了（API 錯誤：{str(e)}），去叫工程師出來挨揍！"

    send_line_reply(event.reply_token, reply_text)

# 處理「圖片訊息」
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    chat_id = get_chat_id(event)

    try:
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            image_bytes = blob_api.get_message_content(message_id=event.message.id)

        # 個人私訊：傳圖直接吐槽
        if event.source.type == "user":
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    "請用死侍的風格吐槽、分析或評論這張圖片！"
                ],
                config=types.GenerateContentConfig(
                    system_instruction=DEADPOOL_PROMPT,
                    temperature=0.8,
                )
            )
            send_line_reply(event.reply_token, response.text)
        else:
            # 群組聊天：先把圖片暫存起來，等有人喊「死侍」再連圖帶字一起處理
            latest_image_cache[chat_id] = {"bytes": image_bytes}

    except Exception as e:
        if event.source.type == "user":
            send_line_reply(event.reply_token, f"我的眼睛業障重，看不清楚這張圖（錯誤：{str(e)}）")

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
