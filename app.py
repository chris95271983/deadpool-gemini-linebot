import os
import random
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

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

chat_memory = defaultdict(lambda: deque(maxlen=20))

# 本地死侍黃金幹話庫（絕對不甩 Google 機房臉色，秒速回話！）
DEADPOOL_QUOTES = [
    "法克！你以為我是客服中心喔？一直敲我！不過看在你這麼帥的份上，我原諒你了。",
    "（一邊擦拭雙刀一邊翻白眼）老兄，今天天氣很好，適合去吃墨西哥捲餅（Chimichangas），別在這跟我廢話。",
    "我有短期記憶，但我可沒有耐心一直聽你碎唸。說吧，找本大爺幹嘛？",
    "你知道嗎？剛才那句話深刻體現了人類大腦的極限。而我剛好沒有這種極限，因為我是AI，而且我很性感。",
    "（從螢幕角落爬出來）逼逼！偵測到無聊對話！要嘛給我一把槍，要嘛講個好笑的笑話來聽聽！",
    "喔對了，金剛狼剛剛打電話來問你有沒有想他。我叫他滾去吃土了，現在輪到我們聊天。",
]

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
        reply_text = "啪！我剛剛拿平底鍋砸了自己的頭，關於你們的黑歷史我全都忘光了。爽了吧？"
    else:
        # 結合使用者訊息的專屬死侍式吐槽
        reply_text = f"你剛剛說「{user_msg}」對吧？老兄，{random.choice(DEADPOOL_QUOTES)}"

    send_line_reply(event.reply_token, reply_text)

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    send_line_reply(event.reply_token, "（戴上墨鏡審視這張圖）哇喔，這畫質糊得跟我的前女友一樣神祕。不過我給過啦！")

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
