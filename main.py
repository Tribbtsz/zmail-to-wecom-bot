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

# Flask 应用初始化
app = Flask(__name__)

# 设置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s]: %(message)s'
)

# 配置环境变量
IMAP_SERVER = os.environ.get('IMAP_SERVER')
IMAP_PORT = int(os.environ.get('IMAP_PORT', 993))
IMAP_USERNAME = os.environ.get('IMAP_USERNAME')
IMAP_PASSWORD = os.environ.get('IMAP_PASSWORD')
WECHAT_WEBHOOK = os.environ.get('WECHAT_WEBHOOK')

# 邮件检查线程开关
stop_thread = False

def connect_imap():
    context = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(host=IMAP_SERVER, port=IMAP_PORT, ssl_context=context)
    conn.login(IMAP_USERNAME, IMAP_PASSWORD)
    conn.select('INBOX')
    return conn

def fetch_new_emails(conn, since_time):
    # 格式化最近两分钟的时间
    since_time_str = time.strftime('%d-%b-%Y', time.gmtime(since_time))
    # 通过 SINCE 参数检索指定时间之后的未读邮件
    status, data = conn.search(None, f'(SINCE "{since_time_str}" UNSEEN)')
    if status != 'OK':
        return []
    return data[0].split()

def parse_email(msg):
    content = {}
    # 获取发件人
    from_header = msg.get('From')
    from_name, from_email = email.utils.parseaddr(from_header)
    from_name, encoding = decode_header(from_name)[0]
    if isinstance(from_name, bytes):
        from_name = from_name.decode(encoding or 'utf-8')
    content['from'] = f"{from_name}<{from_email}>"
    
    # 获取主题
    subject, encoding = decode_header(msg.get('Subject'))[0]
    if isinstance(subject, bytes):
        subject = subject.decode(encoding or 'utf-8')
    content['subject'] = subject

    # 获取日期
    date_header = msg.get('Date')
    content['date'] = email.utils.parsedate_to_datetime(date_header).strftime('%Y-%m-%d %H:%M:%S')

    # 获取正文（仅支持文本部分）
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                charset = part.get_content_charset() or 'utf-8'
                body = part.get_payload(decode=True).decode(charset)
                break
    else:
        body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8')

    # 限制正文为 500 字符
    max_body_length = 500
    if len(body) > max_body_length:
        body = body[:max_body_length] + "\n[内容过长，已截断]"
    content['body'] = body
    return content

def send_to_wechat(content):
    message = {
        "msgtype": "text",
        "text": {
            "content": f"来自：{content['from']}\n主题：{content['subject']}\n时间：{content['date']}\n内容：{content['body']}"
        }
    }
    for attempt in range(3):  # 最多重试 3 次
        try:
            response = requests.post(WECHAT_WEBHOOK, json=message, timeout=10)
            if response.status_code == 200:
                logging.info("消息发送成功")
                return True
            else:
                logging.warning(f"消息发送失败，状态码: {response.status_code}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Webhook 请求失败: {e}")
        time.sleep(2)  # 重试间隔
    return False

def mark_as_read(conn, mail_id):
    try:
        conn.store(mail_id, '+FLAGS', '\\Seen')  # 标记为已读
        logging.info(f"邮件 {mail_id} 已标记为已读")
    except Exception as e:
        logging.error(f"标记邮件为已读时出错: {e}")

def email_check_worker():
    while not stop_thread:
        try:
            since_time = time.time() - 120  # 检查过去 2 分钟的邮件
            conn = connect_imap()
            mail_ids = fetch_new_emails(conn, since_time)
            if not mail_ids:
                logging.info("没有新的未读邮件")
            for mail_id in mail_ids:
                status, msg_data = conn.fetch(mail_id, '(RFC822)')
                if status != 'OK':
                    continue
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                content = parse_email(msg)
                if send_to_wechat(content):
                    mark_as_read(conn, mail_id)  # 发送成功后标记为已读
            conn.close()
            conn.logout()
        except Exception as e:
            logging.error(f"检查邮件时出错: {e}")
        time.sleep(60)  # 每分钟运行一次

# 启动邮件检查线程
def start_email_worker():
    worker = threading.Thread(target=email_check_worker, daemon=True)
    worker.start()

# 健康检查接口
@app.route('/')
def health_check():
    return jsonify({"status": "running", "message": "Email checker is active."})

# 应用启动时启动线程
if __name__ == '__main__':
    start_email_worker()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
