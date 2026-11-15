import email
import html
import imaplib
import logging
import os
import re
import time
from email.header import decode_header, make_header
from html.parser import HTMLParser

import requests


def env_int(name, default, minimum=1):
    raw = (os.getenv(name, '') or '').strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


def parse_chat_ids(raw):
    ids = []
    for item in raw.replace(',', ' ').split():
        try:
            ids.append(int(item))
        except ValueError:
            raise SystemExit('TG_CHAT_IDS: каждое значение должно быть числом')
    return ids


# --- НАСТРОЙКИ ---
TELEGRAM_API = 'https://api.telegram.org'
TG_TOKEN = (os.getenv('TG_TOKEN', '') or '').strip()
TG_CHAT_IDS = parse_chat_ids(os.getenv('TG_CHAT_IDS', '') or '')

IMAP_SERVER = (os.getenv('IMAP_SERVER', '') or '').strip()
IMAP_PORT = env_int('IMAP_PORT', 993)
MAIL_USER = (os.getenv('MAIL_USER', '') or '').strip()
MAIL_PASS = (os.getenv('MAIL_PASS', '') or '').strip()
MAIL_FOLDER = (os.getenv('MAIL_FOLDER', '') or '').strip() or 'INBOX'

CHECK_CYCLES = env_int('MAIL_CYCLES', 5)
CHECK_INTERVAL = env_int('MAIL_INTERVAL', 60)
MAX_PER_CHECK = env_int('MAIL_MAX_PER_CHECK', 20)
BODY_LIMIT = env_int('MAIL_BODY_LIMIT', 500, 50)

SUBJECT_LIMIT = 300
FROM_LIMIT = 200
MESSAGE_LIMIT = 4000
IMAP_TIMEOUT = 30
HTTP_TIMEOUT = 20
SEND_PAUSE = 0.5

INVISIBLE_CHARS = re.compile('[\u200b-\u200f\u2060\ufeff\u034f\u00ad]')

logger = logging.getLogger('email_bot')
logger.setLevel(logging.INFO)
logger.propagate = False


def validate_settings():
    missing = []
    if not TG_TOKEN: missing.append('TG_TOKEN')
    if not TG_CHAT_IDS: missing.append('TG_CHAT_IDS')
    if not IMAP_SERVER: missing.append('IMAP_SERVER')
    if not MAIL_USER: missing.append('MAIL_USER')
    if not MAIL_PASS: missing.append('MAIL_PASS')
    if missing:
        raise SystemExit('Не заполнены настройки: ' + ', '.join(missing))


def setup_logging():
    logger.handlers.clear()
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(h)


class TextExtractor(HTMLParser):
    BLOCK_TAGS = {'p', 'div', 'br', 'tr', 'li', 'ul', 'ol', 'table', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
    SKIP_TAGS = {'script', 'style', 'title'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            if self.skip:
                self.skip -= 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(value):
    parser = TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return re.sub(r'<[^>]+>', ' ', value)
    return ''.join(parser.parts)


def normalize_text(text):
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n').replace('\xa0', ' ')
    text = INVISIBLE_CHARS.sub('', text)
    text = '\n'.join(line.strip() for line in text.split('\n'))
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def truncate(text, limit):
    text = text or ''
    return text if len(text) <= limit else text[:limit].rstrip() + '...'


def clean_header(value):
    if not value:
        return ''
    try:
        text = str(make_header(decode_header(str(value))))
    except Exception:
        text = str(value)
    return ' '.join(text.split())


def decode_payload(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ''
    charset = part.get_content_charset() or 'utf-8'
    try:
        return payload.decode(charset, errors='replace')
    except (LookupError, UnicodeDecodeError):
        return payload.decode('utf-8', errors='replace')


def get_body(msg):
    plain = None
    html_text = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        if 'attachment' in str(part.get('Content-Disposition', '')).lower():
            continue
        content_type = part.get_content_type()
        if content_type == 'text/plain' and plain is None:
            plain = decode_payload(part)
        elif content_type == 'text/html' and html_text is None:
            html_text = decode_payload(part)
    if plain and plain.strip():
        return plain
    if html_text:
        return html_to_text(html_text)
    return ''


def build_message(from_addr, subject, body, as_html):
    escape = html.escape if as_html else (lambda value: value)
    subject = subject or '(без темы)'
    limit = BODY_LIMIT
    while True:
        body_part = escape(truncate(body, limit)) if body else '(без текста)'
        if as_html:
            message = (
                f'📧 <b>Новое письмо</b>\n\n'
                f'<b>От:</b> {escape(from_addr)}\n'
                f'<b>Тема:</b> {escape(subject)}\n\n'
                f'<b>Текст:</b>\n{body_part}'
            )
        else:
            message = (
                f'Новое письмо\n\n'
                f'От: {from_addr}\n'
                f'Тема: {subject}\n\n'
                f'Текст:\n{body_part}'
            )
        if len(message) <= MESSAGE_LIMIT or limit <= 50:
            return message
        limit //= 2


def telegram_post(chat_id, text, parse_mode):
    payload = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    url = f'{TELEGRAM_API}/bot{TG_TOKEN}/sendMessage'
    response = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    if response.status_code == 429:
        try:
            delay = int(response.json().get('parameters', {}).get('retry_after', 5))
        except (ValueError, TypeError):
            delay = 5
        time.sleep(min(max(delay, 1), 30))
        response = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    return response


def describe_error(response):
    try:
        return str(response.json().get('description', ''))[:200]
    except ValueError:
        return response.text[:200]


def send_to_chat(index, chat_id, from_addr, subject, body):
    try:
        response = telegram_post(chat_id, build_message(from_addr, subject, body, True), 'HTML')
        if response.ok and response.json().get('ok'):
            return True
        description = describe_error(response)
        if response.status_code == 400 and 'parse' in description.lower():
            response = telegram_post(chat_id, build_message(from_addr, subject, body, False), None)
            if response.ok and response.json().get('ok'):
                return True
            description = describe_error(response)
        logger.error('Чат №%d: Telegram вернул %s: %s', index, response.status_code, description)
    except Exception as error:
        logger.error('Чат №%d: ошибка отправки: %s', index, type(error).__name__)
    return False


def process_message(mail, uid):
    status, data = mail.uid('fetch', uid, '(BODY.PEEK[])')
    if status != 'OK' or not data:
        return False
    raw = next((part[1] for part in data if isinstance(part, tuple)), None)
    if raw is None:
        return False

    msg = email.message_from_bytes(raw)
    from_addr = truncate(clean_header(msg.get('From')), FROM_LIMIT)
    subject = truncate(clean_header(msg.get('Subject')), SUBJECT_LIMIT)
    body = normalize_text(get_body(msg))

    delivered = False
    for index, chat_id in enumerate(TG_CHAT_IDS, 1):
        if send_to_chat(index, chat_id, from_addr, subject, body):
            delivered = True
        time.sleep(SEND_PAUSE)

    if delivered:
        mail.uid('store', uid, '+FLAGS', '(\\Seen)')
    return delivered


def check_mail():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, port=IMAP_PORT, timeout=IMAP_TIMEOUT)
        mail.login(MAIL_USER, MAIL_PASS)
        status, _ = mail.select(MAIL_FOLDER)
        if status != 'OK':
            raise RuntimeError('Не удалось открыть папку почты')
        status, data = mail.uid('search', None, 'UNSEEN')
        if status != 'OK':
            raise RuntimeError('Ошибка поиска непрочитанных писем')

        uids = data[0].split() if data and data[0] else []
        batch = uids[:MAX_PER_CHECK]
        logger.info('Непрочитанных писем: %d, в обработке: %d', len(uids), len(batch))

        delivered = 0
        for uid in batch:
            try:
                if process_message(mail, uid):
                    delivered += 1
            except Exception as error:
                logger.error('Ошибка обработки письма: %s', type(error).__name__)
        if batch:
            logger.info('Доставлено писем: %d из %d', delivered, len(batch))
    except Exception as error:
        logger.error('Ошибка мониторинга: %s: %s', type(error).__name__, error)
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


def main():
    validate_settings()
    setup_logging()

    started = time.monotonic()
    logger.info('Старт запуска: %d проверок с интервалом %d сек', CHECK_CYCLES, CHECK_INTERVAL)

    for cycle in range(CHECK_CYCLES):
        logger.info('--- Проверка %d из %d ---', cycle + 1, CHECK_CYCLES)
        check_mail()

        if cycle < CHECK_CYCLES - 1:
            next_tick = started + (cycle + 1) * CHECK_INTERVAL
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    logger.info('Запуск завершён за %.0f сек', time.monotonic() - started)


if __name__ == '__main__':
    main()
