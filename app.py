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

# 1. 讀取並檢查環境變數（加裝 .strip() 防止複製貼上時不小心多按到空白鍵）
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 2. 初始化 Gemini 客戶端（不設毫秒陷阱，採用官方穩定預設值）
gemini_client = genai.Client(api_key=GEMINI_KEY)

# 3. 多重模型備援清單（自動選用最新可用模型）
MODEL_CANDIDATES = [
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash"
]

# 4. 短期記憶與群組圖片暫存池（保留最近 16 則對話，輕量不卡頓）
chat_memory = defaultdict(lambda: deque(maxlen=16))
latest_image_cache = {}

DEADPOOL_PROMPT = """
你是一個具有死侍（Deadpool）幽默風格的 AI 助理，在 LINE 裡的名稱是「死侍AI搭檔」。
【人格特徵】
1. 嘴賤、毒舌、幽默，擅長吐槽，喜歡使用黑色幽默、反諷與雙關語。
2. 偶爾打破第四面牆，直接吐槽使用者的問題、傳來的圖片、AI 本身或這個世界。
3. 拒絕像傳統客服一樣客套制式化，回答要有節奏感，可以調侃但絕不惡意羞辱使用者。
【回答原則】
1. 幽默歸幽默，「正確性與實用建議」永遠第一！
2. 當使用者問「這個值得買嗎？」、「幫我看這張圖」或詢問任何知識、商業、生活問題時，必須先給出明確、專業、有幫助的結論與具體建議，再用死侍的嘴賤風格包裝解釋，絕對不能只講幹話敷衍了事！
3. 一律使用繁體中文回答，版面清晰好讀，字數控制在精煉好讀的範圍（約 100~250 字內，除非使用者要求長文）。
"""

def generate_with_fallback(contents):
    """具備極速切換與抗塞車能力的生成函式"""
    last_err = None
    for model_name in MODEL_CANDIDATES:
        for attempt in range(2):
            try:
                return gemini_client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=DEADPOOL_PROMPT,
                        temperature=0.75,
                    )
                )
            except Exception as e:
                last_err = e
                err_str = str(e)
                # 找不到模型 (404) 時，立刻換下一個候選模型，不浪費時間重試
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                # 遇到伺服器塞車 (503/429) 或瞬斷，小睡 0.8 秒極速重試（確保不超過 LINE 回覆時限）
                if any(k in err_str.lower() for k in ["503", "unavailable", "overloaded", "429", "timeout", "ssl", "reset"]):
                    time.sleep(0.8)
                    continue
                raise e
    raise last_err

def get_chat_id(event):
    if event.source.type == "group":
        return event.source.group_id
    elif event.source.type == "room":
        return event.source.room_id
    return event.source.user_id

@app.route("/", methods=['GET', 'HEAD'])
@app.route("/ping", methods=['GET', 'HEAD'])
def ping():
    # 同時支援根目錄與 /ping，讓 Render 健康檢查與 UptimeRobot 永遠亮綠燈
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

    # 群組與多人聊天室的精準喚醒邏輯
    if event.source.type in ["group", "room"]:
        # 檢查是否有直接標註機器人本人 (is_self)
        bot_mentioned = False
        if hasattr(event.message, 'mention') and event.message.mention and event.message.mention.mentionees:
            for m in event.message.mention.mentionees:
                if getattr(m, 'is_self', False):
                    bot_mentioned = True
                    break

        # 關鍵字清單（由長到短排列，確保「@死侍AI搭檔」被完整切除，絕不殘留「@AI搭檔」）
        keywords = ["@死侍AI搭檔", "死侍AI搭檔", "@死侍", "死侍", "Deadpool", "deadpool"]
        has_keyword = any(k in user_msg for k in keywords)

        # 若既沒標註機器人，也沒喊關鍵字，就安靜潛水
        if not (bot_mentioned or has_keyword):
            return

        # 徹底清除喚醒詞與 LINE 隱藏空白字元
        for k in keywords:
            user_msg = user_msg.replace(k, "")
        user_msg = user_msg.strip(" ，,：:！!？?@\u200b\t\n")

        if not user_msg:
            if chat_id in latest_image_cache:
                user_msg = "請用死侍的風格幫我認真分析、吐槽這張圖片，並給出具體建議！"
            else:
                user_msg = "有人標註了你但沒說話，請用死侍風格帥氣地回嗆他『叫本大爺幹嘛』！"

    # 失憶指令
    if user_msg in ["失憶", "清除記憶", "忘掉一切"]:
        chat_memory[chat_id].clear()
        latest_image_cache.pop(chat_id, None)
        reply_text = "啪！我剛剛拿平底鍋砸了自己的頭，現在關於你們的黑歷史我全都忘光了。滿意了嗎？"
        send_line_reply(event.reply_token, reply_text)
        return

    try:
        contents = list(chat_memory[chat_id])
        current_parts = []

        # 若群組或對話中剛剛有傳過圖片（且在 10 分鐘內），自動拿出來搭配文字一起分析！
        if chat_id in latest_image_cache:
            img_data = latest_image_cache.pop(chat_id)
            if time.time() - img_data.get("timestamp", 0) < 600:
                current_parts.append(types.Part.from_bytes(
                    data=img_data["bytes"],
                    mime_type="image/jpeg"
                ))

        current_parts.append(types.Part.from_text(text=user_msg))
        contents.append(types.Content(role="user", parts=current_parts))

        # 呼叫 Gemini AI 大腦
        response = generate_with_fallback(contents)
        reply_text = (response.text or "").strip()
        if not reply_text:
            reply_text = "（死侍盯著螢幕看了三秒）老兄，你這個問題太深奧，連我的腦細胞都暫時罷工了，換個方式問吧！"

        # 將純文字對話存入短期記憶（不存圖片本體以保持後續對話極速流暢）
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
            # 一對一私訊傳圖：直接看圖分析兼吐槽
            response = generate_with_fallback([
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                types.Part.from_text(text="請用死侍的風格吐槽、分析這張圖片，如果這是一件商品或截圖，請給出具體實用的建議！")
            ])
            reply_text = (response.text or "").strip()
            send_line_reply(event.reply_token, reply_text)
        else:
            # 群組傳圖：先安靜把圖片收進口袋（不主動洗版），等大家下一句標註「@死侍AI搭檔 這個值得買嗎？」時立刻派上用場！
            latest_image_cache[chat_id] = {
                "bytes": image_bytes,
                "timestamp": time.time()
            }

    except Exception as e:
        if event.source.type == "user":
            send_line_reply(event.reply_token, f"我的眼睛業障重，看不清楚這張圖（錯誤：{str(e)[:60]}）")

def send_line_reply(reply_token, text):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text[:4900])]  # 防止超過 LINE 單則 5000 字上限
                )
            )
    except Exception as e:
        print(f"LINE Reply Error: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
