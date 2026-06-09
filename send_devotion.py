"""
Daily Devotion -> Telegram Bot

Fetches the current devotion from intouchglobal.org/read/daily-devotions
and posts it to a Telegram group via the Bot API.

Environment variables (set as GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN  - token from @BotFather
  TELEGRAM_CHAT_ID    - numeric group chat ID (negative number for groups)
"""

import html
import os
import sys
import re
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta

DEVOTION_URL = "https://www.intouchglobal.org/read/daily-devotions"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
# Telegram's hard cap is 4096 chars per message
MAX_MSG = 4000


def fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


class DevotionParser(HTMLParser):
    """
    Extracts:
      - title (first <h1>)
      - subtitle (first <h2> inside article area)
      - date (first <time> or element whose class contains 'date')
      - body paragraphs (all <p> inside <article>)
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.subtitle = ""
        self.date = ""
        self.paragraphs = []

        self._in_h1 = False
        self._in_article = False
        self._in_p = False
        self._in_time = False
        self._in_h2_article = False
        self._h2_seen_article = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "article":
            self._in_article = True
        if tag == "h1" and not self.title:
            self._in_h1 = True
            self._buf = []
        if tag == "h2" and self._in_article and not self._h2_seen_article:
            self._in_h2_article = True
            self._buf = []
        if tag == "p" and self._in_article:
            self._in_p = True
            self._buf = []
        if tag == "time" and not self.date:
            self._in_time = True
            self._buf = []
        # date via class
        cls = attrs_d.get("class", "")
        if not self.date and "date" in cls.lower() and tag in ("span", "div", "p"):
            self._in_time = True
            self._buf = []

    def handle_endtag(self, tag):
        # Only act / clear the buffer when closing the tag that opened the capture.
        # Nested closing tags (strong, span, em, a, etc.) inside a <p> must NOT
        # clear the buffer — otherwise we lose text that came before them.
        if tag == "h1" and self._in_h1:
            self.title = "".join(self._buf).strip()
            self._in_h1 = False
            self._buf = []
        elif tag == "h2" and self._in_h2_article:
            self.subtitle = "".join(self._buf).strip()
            self._in_h2_article = False
            self._h2_seen_article = True
            self._buf = []
        elif tag == "p" and self._in_p:
            text = "".join(self._buf).strip()
            if text:
                self.paragraphs.append(text)
            self._in_p = False
            self._buf = []
        elif tag == "time" and self._in_time:
            self.date = "".join(self._buf).strip()
            self._in_time = False
            self._buf = []
        elif tag in ("span", "div") and self._in_time and not self.date:
            text = "".join(self._buf).strip()
            if text:
                self.date = text
            self._in_time = False
            self._buf = []
        if tag == "article":
            self._in_article = False
            # don't clear _buf here; any open capture handles its own reset

    def handle_data(self, data):
        if (
            self._in_h1
            or self._in_p
            or self._in_time
            or self._in_h2_article
        ):
            self._buf.append(data)


def clean_paragraphs(paragraphs):
    """Remove boilerplate like the NASB copyright footer and Bible-in-One-Year notes if desired."""
    cleaned = []
    for p in paragraphs:
        p = re.sub(r"\s+", " ", p).strip()
        if not p:
            continue
        low = p.lower()
        if "copyright" in low and "lockman" in low:
            continue
        if "for permission to quote" in low:
            continue
        cleaned.append(p)
    return cleaned


def today_sgt_string() -> str:
    """Today's date formatted for the message header, in Singapore time (UTC+8).

    We intentionally use *today* in SGT rather than the date the page displays
    — InTouch sometimes re-features older devotions, so the page's <time>
    reflects the original publication date, not when it was featured.
    """
    sgt = timezone(timedelta(hours=8))
    return datetime.now(sgt).strftime("%A, %B %-d, %Y")


def build_message(title, subtitle, date, paragraphs) -> str:
    """Build an HTML-formatted Telegram message.

    The `date` argument is ignored in favor of today's SGT date — see
    today_sgt_string for why.
    """
    parts = []
    if title:
        parts.append(f"<b>📖 {html.escape(title)}</b>")
    parts.append(f"<i>{html.escape(today_sgt_string())}</i>")
    if subtitle:
        parts.append(html.escape(subtitle))
    if parts:
        parts.append("")  # blank line
    for p in paragraphs:
        parts.append(html.escape(p))
    parts.append("")
    parts.append('🔗 <a href="https://www.intouchglobal.org/read/daily-devotions">Read on InTouch</a>')
    msg = "\n\n".join(parts)
    if len(msg) > MAX_MSG:
        # Truncate body, keeping header/footer
        footer = '\n\n… <a href="https://www.intouchglobal.org/read/daily-devotions">(continue on InTouch)</a>'
        msg = msg[: MAX_MSG - len(footer)] + footer
    return msg


def send_telegram(token: str, chat_id: str, text: str) -> dict:
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
    ).encode()
    req = urllib.request.Request(TELEGRAM_API.format(token=token), data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    import json as _json
    return _json.loads(body)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids_raw = os.environ.get("TELEGRAM_CHAT_IDS") or os.environ.get("TELEGRAM_CHAT_ID") or ""
    chat_ids = [cid.strip() for cid in chat_ids_raw.split(",") if cid.strip()]
    if not token or not chat_ids:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS (or TELEGRAM_CHAT_ID) env vars are required.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching {DEVOTION_URL} …")
    html_src = fetch_html(DEVOTION_URL)

    parser = DevotionParser()
    parser.feed(html_src)

    paragraphs = clean_paragraphs(parser.paragraphs)
    if not paragraphs:
        print("ERROR: Could not extract devotion paragraphs.", file=sys.stderr)
        sys.exit(2)

    message = build_message(parser.title, parser.subtitle, parser.date, paragraphs)
    print(f"Built message, length={len(message)} chars. Title={parser.title!r} Date={parser.date!r}")
    print("---- PREVIEW ----")
    print(message[:500])
    print("-----------------")

    errors = 0
    for chat_id in chat_ids:
        print(f"\nSending to chat_id={chat_id} …")
        try:
            result = send_telegram(token, chat_id, message)
            if not result.get("ok"):
                print(f"ERROR sending to {chat_id}:", result, file=sys.stderr)
                errors += 1
            else:
                print(f"Sent OK to {chat_id}. message_id =", result["result"].get("message_id"))
        except Exception as e:
            print(f"ERROR sending to {chat_id}: {e}", file=sys.stderr)
            errors += 1

    if errors == len(chat_ids):
        print("ERROR: Failed to send to ALL groups.", file=sys.stderr)
        sys.exit(3)
    print(f"\nDone. Sent to {len(chat_ids) - errors}/{len(chat_ids)} groups.")


if __name__ == "__main__":
    main()
