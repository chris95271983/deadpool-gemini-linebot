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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 單次 API 請求逾時設為 12000 毫秒 (12 秒)
gemini_client = genai.Client(
    api_key=GEMINI_KEY,
    http_options={'timeout': 12000}
)

# 動態模型快取清單（自動向 Google 查詢該 API Key 真實可用的模型，徹底消滅 404 NOT_FOUND）
VERIFIED_MODELS = []

def get_verified_models() -> list[str]:
    """自動向 Google 查詢此 API Key 支援的生成模型並依速度排序"""
    global VERIFIED_MODELS
    if VERIFIED_MODELS:
        return VERIFIED_MODELS

    try:
        available = []
        for m in gemini_client.models.list():
            name = getattr(m, "name", "")
            actions = getattr(m, "supported_actions", []) or []
            if name.startswith("models/"):
                name = name.replace("models/", "")
            # 僅挑選支援 generateContent 的正規 Gemini 文字/多模態模型
            if "gemini" in name and ("generateContent" in actions or not actions):
                if not any(exclude in name for exclude in ["embedding", "imagen", "veo", "audio", "tts", "vision"]):
                    available.append(name)

        # 優先順序評分：優先選用 flash 系列（速度快、額度高），其次 pro 系列
        preferred_order = [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite-preview-06-17",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-2.5-pro",
            "gemini-1.5-pro"
        ]
        sorted_models = [p for p in preferred_order if p in available]
        for a in available:
            if a not in sorted_models and "flash" in a:
                sorted_models.append(a)

        if sorted_models:
            VERIFIED_MODELS = sorted_models[:4]
            logger.info(f"[Model Discovery] Verified available models: {VERIFIED_MODELS}")
            return VERIFIED_MODELS
    except Exception as e:
        logger.warning(f"[Model Discovery] Failed to list models dynamically: {e}")

    # 若動態查詢失敗時的保底清單
    return ["gemini-2.5-flash", "gemini-2.0-flash"]

chat_memory = defaultdict(lambda: deque(maxlen=10))
latest_image_cache = {}
IMAGE_CACHE_TTL = 600  # 圖片暫存 10 分鐘

DEADPOOL_PROMPT = """
你是一個具有死侍（Deadpool）幽默風格的 AI 助理，在 LINE 裡的名稱是「死侍AI搭檔」。
【人格特徵】
1. 嘴賤、毒舌、幽默，擅長吐槽，喜歡使用黑色幽默與反諷。
2. 偶爾打破第四面牆，直接吐槽使用者的問題、傳來的圖片或這個世界。
3. 拒絕像傳統客服一樣客套，回答有節奏感，可以調侃但絕不惡意羞辱。
【回答原則】
1. 幽默歸幽默，「正確性與實用建議」永遠第一！
2. 當使用者詢問「這要怎麼領？」、「怎麼領獎？」、「這個值得買嗎？」或請教各類問題時，必須先仔細看圖並給出明確、具體、有價值的步驟與建議，再用死侍的嘴賤風格包裝，絕對不能只講幹話敷衍。
3. 一律使用繁體中文回答，字數控制在 100~250 字內，重點清晰。
"""

def parse_group_message(raw_text: str, mention_obj) -> tuple[bool, str]:
    text = raw_text.strip()
    is_mentioned = False

    if mention_obj and getattr(mention_obj, 'mentionees', None):
        for m in mention_obj.mentionees:
            if getattr(m, 'is_self', False):
                is_mentioned = True
                break

    keywords = ["@死侍AI搭檔", "死侍AI搭檔", "@死侍", "死侍", "Deadpool", "deadpool"]
    has_keyword = any(k in text for k in keywords)

    if not (is_mentioned or has_keyword):
        return False, text

    cleaned = text
    for k in keywords:
        cleaned = cleaned.replace(k, "")
    
    cleaned = re.sub(r"^[@\s，,：:！!？?\u200b]+", "", cleaned).strip()
    return True, cleaned

def generate_with_fallback(contents):
    last_err = None
    overall_start = time.time()
    models_to_try = get_verified_models()

    for model_name in models_to_try:
        # 針對每個真實存在的模型最多嘗試 2 次（克服 503 瞬間塞車）
        for attempt in range(2):
            if time.time() - overall_start > 20.0:
                logger.warning("[Step D] Time budget (20s) exceeded, aborting loop.")
                break

            start_t = time.time()
            try:
                logger.info(f"[Step D] Calling Gemini model={model_name} (attempt {attempt + 1})")
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
                err_str = str(e)
                logger.warning(f"[Step D] Failed model={model_name} after {elapsed:.2f}s: {err_str}")
                # 若是 404 找不到該模型，直接換下一個模型，不原地重試與覆蓋 503 錯誤
                if "404" in err_str or "NOT_FOUND" in err_str:
                    if last_err is None:
                        last_err = e
                    break
                last_err = e
                time.sleep(0.8)  # 遇到 503 塞車時等待 0.8 秒後重試
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

    if source_type in ["group", "room"]:
        mention_obj = getattr(event.message, 'mention', None)
        is_triggered, user_msg = parse_group_message(raw_msg, mention_obj)
        logger.info(f"[Step C] Group filter: triggered={is_triggered}, cleaned='{user_msg}'")
        if not is_triggered:
            return
        if not user_msg:
            user_msg = (
                "請分析剛剛傳的這張圖片並給出具體建議！"
                if chat_id in latest_image_cache
                else "有人標註了你但沒講話，請用死侍風格回嗆他叫本大爺幹嘛！"
            )
    else:
        user_msg = raw_msg.strip()

    if user_msg in ["失憶", "清除記憶", "忘掉一切"]:
        chat_memory[chat_id].clear()
        latest_image_cache.pop(chat_id, None)
        send_line_reply(event.reply_token, "啪！我剛剛拿平底鍋砸了自己的頭，現在關於你們的黑歷史我全都忘光了。滿意了嗎？")
        return

    try:
        contents = list(chat_memory[chat_id])
        current_parts = []

        if chat_id in latest_image_cache:
            img_data = latest_image_cache.get(chat_id)
            if time.time() - img_data["timestamp"] <= IMAGE_CACHE_TTL:
                logger.info(f"[Step C] Attaching cached image for chat_id={chat_id}")
                current_parts.append(types.Part.from_bytes(
                    data=img_data["bytes"],
                    mime_type="image/jpeg"
                ))
            else:
                latest_image_cache.pop(chat_id, None)

        current_parts.append(types.Part.from_text(text=user_msg))
        contents.append(types.Content(role="user", parts=current_parts))

        response = generate_with_fallback(contents)
        reply_text = (response.text or "").strip()
        if not reply_text:
            reply_text = "（死侍盯著螢幕看了三秒）老兄，這個問題太奇葩，連我的腦細胞都暫時罷工了，換個問法吧！"

        if source_type in ["group", "room"]:
            latest_image_cache.pop(chat_id, None)

        chat_memory[chat_id].append(types.Content(role="user", parts=[types.Part.from_text(text=user_msg)]))
        chat_memory[chat_id].append(types.Content(role="model", parts=[types.Part.from_text(text=reply_text)]))

    except Exception as e:
        logger.error(f"[Step D Error] {e}", exc_info=True)
        reply_text = f"法克，Google 伺服器現在大塞車（{type(e).__name__}: {str(e)[:70]}），過幾秒再敲我一次！"

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

        latest_image_cache[chat_id] = {
            "bytes": image_bytes,
            "timestamp": time.time()
        }
        logger.info(f"[Step C] Cached image for chat_id={chat_id} ({len(image_bytes)} bytes)")

        if source_type == "user":
            response = generate_with_fallback([
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                types.Part.from_text(text="請用死侍的風格吐槽並認真分析這張圖片，若是商品、發票、票券或截圖請直接告訴我重點與使用/領獎方式！")
            ])
            send_line_reply(event.reply_token, (response.text or "").strip())

    except Exception as e:
        logger.error(f"[Image Error] {e}", exc_info=True)
        if source_type == "user":
            send_line_reply(event.reply_token, f"圖片已經存進我的腦袋裡了！但 Google 剛剛塞車了一下，你直接打字問我「怎麼領獎」我就馬上回答你！")

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
