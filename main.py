from flask import Flask, jsonify
import threading
import os
import time
import imaplib
import ssl
import email
from email.header import decode_header
import requests
import logging
from openai import OpenAI
from bs4 import BeautifulSoup

# Flask 应用初始化
app = Flask(__name__)

# 设置日志记录
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s"
)

# 配置环境变量
IMAP_SERVER = os.environ.get("IMAP_SERVER")
IMAP_PORT = int(os.environ.get("IMAP_PORT", 993))
IMAP_USERNAME = os.environ.get("IMAP_USERNAME")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")
WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK")
API_KEY = os.environ.get("API_KEY")
API_BASE_URL = os.environ.get("API_BASE_URL")
AI_MODEL = os.environ.get("AI_MODEL")


# 邮件检查线程开关
stop_thread = False

# 初始化 OpenAI 客户端
client = OpenAI(api_key=API_KEY, base_url=API_BASE_URL)

# 缓存过期时间（秒）
CACHE_EXPIRATION = 3600  # 1小时

# 定义缓存数据结构
cache = {}


# 从HTML中提取纯文本
def extract_text_from_html(html_content):
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        return soup.get_text(separator="\n", strip=True)
    except Exception as e:
        logging.error(f"HTML 解析失败: {e}")
        return html_content


# AI 总结函数
def summarize_text_with_retry(content, retries=2):
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=AI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个邮件总结助手。请用中文总结以下内容（链接用：详细链接请进入邮箱查看 表示），要求：简洁突出主要信息，限制在150字以内。最终格式：主题：xxx\n内容：xxx\n。",
                    },
                    {"role": "user", "content": content},
                ],
                timeout=10,
            )
            return response.choices[0].message.content
        except Exception as e:
            logging.error(f"AI 总结失败 (尝试 {attempt + 1}/{retries}): {e}")
    return None


# IMAP连接
def connect_imap():
    context = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(host=IMAP_SERVER, port=IMAP_PORT, ssl_context=context)
    conn.login(IMAP_USERNAME, IMAP_PASSWORD)
    conn.select("INBOX")
    return conn


# 获取新邮件


def fetch_new_emails(conn, since_time):
    since_time_str = time.strftime("%d-%b-%Y", time.gmtime(since_time))
    status, data = conn.search(None, f'(SINCE "{since_time_str}" UNSEEN)')
    if status != "OK":
        return []
    return data[0].split()


# 清理缓存
def clear_expired_cache():
    current_time = time.time()
    expired_keys = [
        key
        for key, (timestamp) in cache.items()
        if current_time - timestamp > CACHE_EXPIRATION
    ]
    for key in expired_keys:
        del cache[key]


# 解析邮件


def parse_email(msg):
    content = {}
    from_header = msg.get("From")
    from_name, from_email = email.utils.parseaddr(from_header)
    from_name, encoding = decode_header(from_name)[0]
    if isinstance(from_name, bytes):
        from_name = from_name.decode(encoding or "utf-8")
    content["from"] = f"{from_name}<{from_email}>"

    content["message_id"] = msg.get("Message-ID", "")

    date_header = msg.get("Date")
    content["date"] = email.utils.parsedate_to_datetime(date_header).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" or content_type == "text/html":
                try:
                    charset = part.get_content_charset() or "utf-8"
                    body_content = part.get_payload(decode=True).decode(charset)
                    if content_type == "text/html":
                        body_content = extract_text_from_html(body_content)
                    if body_content.strip():
                        body = body_content
                        break
                except Exception as e:
                    logging.error(f"解析邮件内容失败: {e}")
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8"
            )
            if msg.get_content_type() == "text/html":
                body = extract_text_from_html(body)
        except Exception as e:
            logging.error(f"解析邮件内容失败: {e}")

    content["body"] = body
    return content


# 发送到 Webhook
def send_to_wechat(message):
    for i in range(2):
        try:
            response = requests.post(
                WECHAT_WEBHOOK,
                json={"msgtype": "text", "text": {"content": message}},
                timeout=10,
            )
            if response.status_code == 200:
                logging.info("Webhook 发送成功")
                return True
            else:
                logging.warning(f"Webhook 发送失败，状态码: {response.status_code}")
        except requests.RequestException as e:
            logging.error(f"Webhook 请求失败: {e}")
            if i == 0:  # 第一次失败时继续重试
                continue
    return False


# 处理批次邮件
def process_email_batch(conn, mail_ids):
    clear_expired_cache()
    summaries = []
    failures = 0

    for mail_id in mail_ids:
        try:
            status, msg_data = conn.fetch(mail_id, "(RFC822)")
            if status != "OK":
                failures += 1
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            email_content = parse_email(msg)

            cache_key = hash(f"{email_content['message_id']}_{email_content['date']}")

            if cache_key in cache:
                summary = cache[cache_key][0]
            else:
                summary = summarize_text_with_retry(email_content["body"])
                if summary:
                    cache[cache_key] = (summary, time.time())

            if summary:
                summaries.append(
                    f"来自：{email_content['from']}\n{summary}\n日期：{email_content['date']}\n---"
                )
                # 将邮件标记为已读
                conn.store(mail_id, "+FLAGS", "(\Seen)")
            else:
                failures += 1
        except Exception as e:
            logging.error(f"处理邮件失败: {e}")
            failures += 1

    message = f"【Zmail】：共{len(mail_ids)}封邮件：\n" + "\n".join(summaries)
    if failures:
        message += f"\n失败：{failures}封"

    send_to_wechat(message)


# 邮件检查线程
def email_check_worker():
    while not stop_thread:
        try:
            since_time = time.time() - 120  # 2分钟
            conn = connect_imap()
            mail_ids = fetch_new_emails(conn, since_time)
            if mail_ids:
                logging.info(f"发现 {len(mail_ids)} 封新邮件")
                for i in range(0, len(mail_ids), 10):
                    process_email_batch(conn, mail_ids[i : i + 10])
            else:
                logging.info("没有新邮件")
            conn.close()
            conn.logout()
        except Exception as e:
            logging.error(f"邮件检查出错: {e}")
        time.sleep(60)


# 健康检查接口
@app.route("/")
def health_check():
    return jsonify({"status": "running", "message": "Email checker is active."})


# 启动线程
if __name__ == "__main__":
    worker = threading.Thread(target=email_check_worker, daemon=True)
    worker.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
