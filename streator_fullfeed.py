#!/usr/bin/env python3
import json
import logging
import os
import re
import sqlite3
import threading
import time
from html import escape

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response

from playwright.sync_api import sync_playwright

FEED_URL = "https://thestreatorstandard.com/f.rss"

REFRESH_INTERVAL = 3600
RETRY_INTERVAL = 300

RSS_TIMEOUT = 10
ARTICLE_TIMEOUT = 15000
ARTICLE_DELAY = 1.0

DB_PATH = os.environ.get("DB_PATH", "/data/feed_cache.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://thestreatorstandard.com/",
}

app = Flask(__name__)
cached_feed = None
feed_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_database():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content_html TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_cached_article(url):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT content_html FROM articles WHERE url = ?",
            (url,),
        ).fetchone()

    return row[0] if row else None


def save_article(url, title, content_html):
    with get_connection() as connection:
        connection.execute("""
            INSERT INTO articles (url, title, content_html)
            VALUES (?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                content_html = excluded.content_html
        """, (url, title, content_html))


# ------------------------------------------------------------
# REMOVE FOOTER/SIDEBAR JUNK
# ------------------------------------------------------------
GENERIC_FOOTERS = [
    "Join my email list",
    "District 44 May Seek Forensic Audit Proposals",
    "The Streator Standard serves as a reliable news source",
    "Do you have a story idea for us",
    "This site is protected by reCAPTCHA",
    "We use cookies to analyze website traffic",
]


def strip_generic_footers(html):
    for footer in GENERIC_FOOTERS:
        html = html.replace(footer, "")
    return html


# ------------------------------------------------------------
# PARSE _BLOG_DATA JSON
# ------------------------------------------------------------
def extract_blog_data_json(soup):
    """
    Extract the JSON inside window._BLOG_DATA = {...}
    """
    for script in soup.find_all("script"):
        if not script.string:
            continue
        if "window._BLOG_DATA" in script.string:
            try:
                # Extract JSON substring
                match = re.search(r"window\._BLOG_DATA\s*=\s*(\{.*\});", script.string, re.DOTALL)
                if not match:
                    continue
                json_text = match.group(1)

                data = json.loads(json_text)
                return data
            except Exception as e:
                logging.warning(f"Could not parse _BLOG_DATA JSON: {e}")
                return None

    return None


# ------------------------------------------------------------
# CONVERT DraftJS fullContent → HTML
# ------------------------------------------------------------
def draftjs_to_html(fullContent):
    """
    Convert DraftJS blocks into proper HTML paragraphs + images,
    preserving internal paragraph breaks exactly as they appear
    on thestreatorstandard.com.
    """
    try:
        data = json.loads(fullContent)
    except Exception as e:
        logging.warning(f"Could not parse DraftJS fullContent: {e}")
        return None

    blocks = data.get("blocks", [])
    entityMap = data.get("entityMap", {})

    html_parts = []

    for block in blocks:
        block_type = block.get("type")
        text = block.get("text", "")

        if block_type == "atomic":
            for entity_range in block.get("entityRanges", []):
                key = entity_range.get("key")
                entity = entityMap.get(str(key))
                if entity and entity.get("type") == "IMAGE":
                    src = entity["data"].get("src")
                    if src:
                        html_parts.append(f'<img src="{src}" style="max-width:100%;">')
            continue

        # TEXT BLOCK — preserve paragraph breaks
        if text.strip():
            # Split on double newlines → paragraphs
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

            for para in paragraphs:
                # Replace single newlines with <br>
                para = escape(para).replace("\n", "<br>")
                html_parts.append(f"<p>{para}</p>")

    return "\n".join(html_parts)

def normalize_paragraphs(html_fragment):
    """
    Convert arbitrary HTML (static article body) into clean paragraphs.
    Handles:
    - <p>
    - <br>
    - DraftJS-style <div> blocks
    - Inline spans
    - Raw text nodes
    """

    soup = BeautifulSoup(html_fragment, "html.parser")

    paragraphs = []

    # 1. Real <p> tags
    p_tags = soup.find_all("p")
    if p_tags:
        for p in p_tags:
            text = p.get_text(strip=True)
            if text:
                paragraphs.append(f"<p>{text}</p>")
        return "\n".join(paragraphs)

    # 2. DraftJS-style <div> blocks
    div_blocks = soup.find_all("div")
    div_texts = [d.get_text(strip=True) for d in div_blocks if d.get_text(strip=True)]
    if div_texts:
        return "\n".join(f"<p>{t}</p>" for t in div_texts)

    # 3. Split on <br>
    html_str = str(soup)
    parts = re.split(r"<br\s*/?>", html_str)
    cleaned = [BeautifulSoup(p, "html.parser").get_text(strip=True) for p in parts]
    cleaned = [c for c in cleaned if c]
    if cleaned:
        return "\n".join(f"<p>{c}</p>" for c in cleaned)

    # 4. Fallback: raw text
    raw = soup.get_text("\n", strip=True)
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    return "\n".join(f"<p>{l}</p>" for l in lines)

# ------------------------------------------------------------
# MAIN ARTICLE EXTRACTOR
# ------------------------------------------------------------
def extract_article_html(html):
    soup = BeautifulSoup(html, "html.parser")

    # 1. Try extracting from _BLOG_DATA first (dynamic articles)
    blog_data = extract_blog_data_json(soup)
    if blog_data:
        post = blog_data.get("post", {})
        fullContent = post.get("fullContent")
        if fullContent:
            html_from_json = draftjs_to_html(fullContent)
            if html_from_json:
                logging.info("Extracted article from _BLOG_DATA JSON")
                return strip_generic_footers(html_from_json)

    # 2. Fallback: static HTML extraction
    selectors = [
        "[data-ux='BlogContent']",
        "[data-aid='BlogContent']",
        "[itemprop='articleBody']",
        ".article-body",
        ".article-content",
        ".post-content",
        ".entry-content",
        "article",
        "main",
    ]

    for selector in selectors:
        candidates = soup.select(selector)
        if not candidates:
            continue

        content = max(
            candidates,
            key=lambda tag: len(tag.get_text(" ", strip=True)),
        )

        logging.info(f"Matched selector: {selector}")

        if len(content.get_text(" ", strip=True)) > 300:
            normalized = normalize_paragraphs(str(content))
            return strip_generic_footers(normalized)

    return "<p>Content not found.</p>"


# ------------------------------------------------------------
# PLAYWRIGHT FETCHER
# ------------------------------------------------------------
def fetch_article_html(url):
    logging.info("Playwright fetching: %s", url)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )

        page = context.new_page()
        page.goto(url, timeout=ARTICLE_TIMEOUT, wait_until="domcontentloaded")

        html = page.content()
        browser.close()

    content_html = extract_article_html(html)

    if content_html == "<p>Content not found.</p>":
        raise ValueError("Article body was not found")

    return content_html


# ------------------------------------------------------------
# FEED GENERATION
# ------------------------------------------------------------
def generate_feed():
    global cached_feed

    try:
        response = requests.get(
            FEED_URL,
            headers=HEADERS,
            timeout=RSS_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        logging.error("Could not download source RSS feed: %s", error)
        return False

    source_feed = BeautifulSoup(response.content, "xml")
    items = list(reversed(source_feed.find_all("item")))

    feed = FeedGenerator()
    feed.id("streator-standard-full-feed")
    feed.title("Streator Standard – Full Articles")
    feed.link(href="https://thestreatorstandard.com/", rel="alternate")
    feed.description("Full-text feed generated locally")
    feed.language("en")

    logging.info("Refreshing %d source-feed entries.", len(items))

    for item in items:
        title_tag = item.find("title")
        link_tag = item.find("link")

        if not title_tag or not link_tag:
            continue

        title = title_tag.get_text(strip=True)
        article_url = link_tag.get_text(strip=True)

        content_html = get_cached_article(article_url)

        if content_html is None:
            try:
                content_html = fetch_article_html(article_url)
                save_article(article_url, title, content_html)
                logging.info("Downloaded and cached: %s", title)
                time.sleep(ARTICLE_DELAY)

            except Exception:
                logging.exception("Could not extract article: %s", article_url)
                content_html = (
                    "<p>Full article content could not be extracted yet. "
                    "It will be retried during the next refresh.</p>"
                )

        plain_description = BeautifulSoup(
            content_html,
            "html.parser",
        ).get_text(" ", strip=True)

        entry = feed.add_entry()
        entry.id(article_url)
        entry.guid(article_url, permalink=True)
        entry.title(title)
        entry.link(href=article_url)
        entry.description(plain_description)
        entry.content(content_html, type="html")

    with feed_lock:
        cached_feed = feed.rss_str(pretty=True)

    logging.info("Full-text feed refresh complete.")
    return True


def refresh_loop():
    while True:
        succeeded = generate_feed()

        if succeeded:
            time.sleep(REFRESH_INTERVAL)
        else:
            logging.info(
                "Source feed refresh failed; retrying in %d seconds.",
                RETRY_INTERVAL,
            )
            time.sleep(RETRY_INTERVAL)


@app.route("/fullfeed.xml")
def fullfeed():
    with feed_lock:
        feed = cached_feed

    if feed is None:
        return Response(
            "Feed is building. Please try again shortly.",
            status=503,
            mimetype="text/plain",
        )

    return Response(feed, mimetype="application/rss+xml")


if __name__ == "__main__":
    init_database()

    threading.Thread(
        target=refresh_loop,
        daemon=True,
        name="feed-refresh",
    ).start()

    app.run(host="0.0.0.0", port=9111)
