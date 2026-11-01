import os
import re
import time
import logging
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

# 設定標準日誌輸出，便於在 Render Console 追蹤各階段耗時與變數狀態
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 1. 環境變數載入與清理
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 2. 初始化 Gemini Client
# 注意：google-genai 的 timeout 單位為毫秒 (ms)，15000 = 15 秒
gemini_client = genai.Client(
    api_key=GEMINI_KEY,
    http_options={'timeout': 15000}
)

MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-flash-latest"
]

# 3. 記憶體管理：限制保留最近 10 則對話（5 輪），避免 Token 膨脹拖慢回應
chat_memory = defaultdict(lambda: deque(maxlen=10))
latest_image_cache = {}
IMAGE_CACHE_TTL = 300  # 群組圖片暫存有效期限：300 秒（5 分鐘）

DEADPOOL_PROMPT = """
你是一個具有死侍（Deadpool）幽默風格的 AI 助理，在 LINE 群組中名叫「死侍AI搭檔」。
【人格特徵】
1. 嘴賤、毒舌、幽默，擅長吐槽，喜歡使用黑色幽默與反諷。
2. 偶爾打破第四面牆，直接吐槽使用者的問題、傳來的圖片或這個世界。
3. 拒絕像傳統客服一樣客套，回答有節奏感，可以調侃但絕不惡意羞辱。
【回答原則】
1. 幽默歸幽默，「正確性與實用建議」永遠第一！
2. 當使用者詢問「這個值得買嗎？」、「幫我看這張圖」或請教各類問題時，必須先針對問題或圖片給出明確、具體、有價值的分析與建議，再用死侍的嘴賤風格包裝，絕對不能只講無意義的幹話敷衍。
3. 一律使用繁體中文回答，字數控制在 100~250 字內，重點清晰。
"""

def parse_group_message(raw_text: str, mention_obj) -> tuple[bool, str]:
    """
    隔離出的純函式（Pure Function）：負責判斷群組訊息是否喚醒機器人，並清理喚醒詞。
    回傳: (is_triggered: bool, cleaned_text: str)
    """
    text = raw_text.strip()
    is_mentioned = False

    # 檢查 LINE 原生 Mention 物件是否標註了機器人自己
    if mention_obj and getattr(mention_obj, 'mentionees', None):
        for m in mention_obj.mentionees:
            if getattr(m, 'is_self', False):
                is_mentioned = True
                break

    # 關鍵字清單（嚴格依長度由長到短排列，避免子字串提前切割）
    keywords = ["@死侍AI搭檔", "死侍AI搭檔", "@死侍", "死侍", "Deadpool", "deadpool"]
    has_keyword = any(k in text for k in keywords)

    if not (is_mentioned or has_keyword):
        return False, text

    # 依序移除關鍵字與標點殘留
    cleaned = text
    for k in keywords:
        cleaned = cleaned.replace(k, "")
    
    # 移除開頭可能殘留的 @符號、全半形標點與零寬空白 (\u200b)
    cleaned = re.sub(r"^[@\s，,：:！!？?\u200b]+", "", cleaned).strip()
    return True, cleaned

def generate_with_fallback(contents):
    """具備明確時間預算（Time Budget）的模型呼叫與降級機制"""
    last_err = None
    for model_name in MODEL_CANDIDATES:
        start_t = time.time()
        try:
            logger.info(f"[Step D] Calling Gemini model={model_name}")
            resp = gemini_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=DEADPOOL_PROMPT,
                    temperature=0.75,
                )
            )
            elapsed = time.time() - start_t
            logger.info(f"[Step D] Success model={model_name} in {elapsed:.2f}s")
            return resp
        except Exception as e:
            elapsed = time.time() - start_t
            last_err = e
            logger.warning(f"[Step D] Failed model={model_name} after {elapsed:.2f}s: {e}")
            continue
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
    return "Deadpool is awake and eating chimichangas!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid LINE signature.")
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    raw_msg = event.message.text
    chat_id = get_chat_id(event)
    source_type = event.source.type
    logger.info(f"[Step B] Received text from {source_type} ({chat_id}): {raw_msg}")

    # Step C: 群組喚醒判斷與訊息清理
    if source_type in ["group", "room"]:
        mention_obj = getattr(event.message, 'mention', None)
        is_triggered, user_msg = parse_group_message(raw_msg, mention_obj)
        logger.info(f"[Step C] Group filter: triggered={is_triggered}, cleaned='{user_msg}'")
        if not is_triggered:
            return
        if not user_msg:
            user_msg = (
                "請分析剛剛群組傳的這張圖片並給出具體建議！"
                if chat_id in latest_image_cache
                else "有人標註了你但沒講話，請用死侍風格回嗆他叫本大爺幹嘛！"
            )
    else:
        user_msg = raw_msg.strip()

    # 清除記憶指令
    if user_msg in ["失憶", "清除記憶", "忘掉一切"]:
        chat_memory[chat_id].clear()
        latest_image_cache.pop(chat_id, None)
        send_line_reply(event.reply_token, "啪！我剛剛拿平底鍋砸了自己的頭，現在關於你們的黑歷史我全都忘光了。滿意了嗎？")
        return

    try:
        contents = list(chat_memory[chat_id])
        current_parts = []

        # 檢查是否有 5 分鐘內的群組暫存圖片可搭配分析
        if chat_id in latest_image_cache:
            img_data = latest_image_cache.pop(chat_id)
            if time.time() - img_data["timestamp"] <= IMAGE_CACHE_TTL:
                logger.info(f"[Step C] Attaching cached image for chat_id={chat_id}")
                current_parts.append(types.Part.from_bytes(
                    data=img_data["bytes"],
                    mime_type="image/jpeg"
                ))

        current_parts.append(types.Part.from_text(text=user_msg))
        contents.append(types.Content(role="user", parts=current_parts))

        # Step D: 呼叫模型
        response = generate_with_fallback(contents)
        reply_text = (response.text or "").strip()
        if not reply_text:
            reply_text = "（死侍盯著螢幕看了三秒）老兄，這個問題太奇葩，連我的腦細胞都暫時罷工了，換個問法吧！"

        # 僅將純文字寫入短期記憶，避免圖片 Bytes 塞爆後續 Context
        chat_memory[chat_id].append(types.Content(role="user", parts=[types.Part.from_text(text=user_msg)]))
        chat_memory[chat_id].append(types.Content(role="model", parts=[types.Part.from_text(text=reply_text)]))

    except Exception as e:
        logger.error(f"[Step D Error] {e}", exc_info=True)
        reply_text = f"法克，我的腦袋連線剛剛卡住了（{type(e).__name__}），請再敲我一次！"

    # Step E: 回傳 LINE
    send_line_reply(event.reply_token, reply_text)

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    chat_id = get_chat_id(event)
    source_type = event.source.type
    logger.info(f"[Step B] Received image from {source_type} ({chat_id})")

    try:
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            image_bytes = blob_api.get_message_content(message_id=event.message.id)

        if source_type == "user":
            response = generate_with_fallback([
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                types.Part.from_text(text="請用死侍的風格吐槽並認真分析這張圖片，若是商品或問題請給出具體建議！")
            ])
            send_line_reply(event.reply_token, (response.text or "").strip())
        else:
            # 群組內收到圖片：先寫入快取，等待後續文字提問一併觸發
            latest_image_cache[chat_id] = {
                "bytes": image_bytes,
                "timestamp": time.time()
            }
            logger.info(f"[Step C] Cached group image for chat_id={chat_id} ({len(image_bytes)} bytes)")

    except Exception as e:
        logger.error(f"[Image Error] {e}", exc_info=True)
        if source_type == "user":
            send_line_reply(event.reply_token, f"我的眼睛業障重，圖片讀取失敗（{type(e).__name__}）")

def send_line_reply(reply_token, text):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text[:4900])]
                )
            )
        logger.info("[Step E] Reply sent successfully.")
    except Exception as e:
        logger.error(f"[Step E Error] Failed to send LINE reply: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
