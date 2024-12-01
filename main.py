import os
import time
import imaplib
import ssl
import email
from email.header import decode_header
import requests
import locale
import logging

# 设置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s]: %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()  # 同时打印到控制台
    ]
)

# 环境变量
IMAP_SERVER = os.environ.get('IMAP_SERVER')
IMAP_PORT = int(os.environ.get('IMAP_PORT', 993))
IMAP_USERNAME = os.environ.get('IMAP_USERNAME')
IMAP_PASSWORD = os.environ.get('IMAP_PASSWORD')
WECHAT_WEBHOOK = os.environ.get('WECHAT_WEBHOOK')

# 设置 locale
locale.setlocale(locale.LC_TIME, 'C')

def connect_imap():
    context = ssl.create_default_context()
    try:
        conn = imaplib.IMAP4_SSL(host=IMAP_SERVER, port=IMAP_PORT, ssl_context=context)
        conn.login(IMAP_USERNAME, IMAP_PASSWORD)
        conn.select('INBOX')
        logging.info("IMAP连接成功")
        return conn
    except Exception as e:
        logging.error(f"IMAP连接失败: {e}")
        return None

def fetch_new_emails(conn, since_time):
    try:
        since_time_str = time.strftime('%d-%b-%Y', time.gmtime(since_time))
        status, data = conn.search(None, f'(SINCE "{since_time_str}")')
        if status != 'OK':
            logging.warning("未找到新邮件")
            return []
        mail_ids = data[0].split()
        if not mail_ids:
            logging.warning("没有新邮件")
            return []
        return mail_ids
    except Exception as e:
        logging.error(f"获取新邮件时发生错误: {e}")
        return []

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
    # 获取邮件正文，截取 500 字符并忽略附件
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" or content_type == "text/html":
                body_bytes = part.get_payload(decode=True)
                charset = part.get_charset()
                if charset:
                    body = body_bytes.decode(charset)
                else:
                    body = body_bytes.decode()
                break
    else:
        body_bytes = msg.get_payload(decode=True)
        charset = msg.get_charset()
        if charset:
            body = body_bytes.decode(charset)
        else:
            body = body_bytes.decode()

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
            "content": f"来自：{content['from']}\n主题：{content['subject']}\n内容：{content['body']}\n日期：{content['date']}"
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
        time.sleep(2)  # 延迟重试
    return False

def main():
    while True:
        try:
            # 获取过去两分钟的时间戳
            two_minutes_ago = time.time() - 120
            conn = connect_imap()
            if not conn:
                time.sleep(60)
                continue
            # 获取新邮件
            mail_ids = fetch_new_emails(conn, two_minutes_ago)
            # 每次处理最多 10 封邮件
            batch_size = 10
            for i in range(0, len(mail_ids), batch_size):
                batch = mail_ids[i:i + batch_size]
                for mail_id in batch:
                    try:
                        status, msg_data = conn.fetch(mail_id, '(RFC822)')
                        if status != 'OK':
                            logging.warning(f"无法获取邮件 {mail_id}")
                            continue
                        raw_email = msg_data[0][1]
                        msg = email.message_from_bytes(raw_email)
                        content = parse_email(msg)
                        if send_to_wechat(content):
                            # 标记邮件为已读
                            conn.store(mail_id, '+FLAGS', '\\Seen')
                            logging.info(f"邮件 {mail_id} 已标记为已读")
                    except Exception as e:
                        logging.error(f"处理邮件 {mail_id} 时出错: {e}")
            # Expunge changes
            conn.expunge()
            # Close connection
            conn.close()
            conn.logout()
        except Exception as e:
            logging.error(f"主程序发生错误: {e}")
        # 每分钟检查一次
        time.sleep(60)

if __name__ == "__main__":
    main()
