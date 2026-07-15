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

FEED_URL = "https://thestreatorstandard.com/f.rss"

REFRESH_INTERVAL = 3600  # 60 minutes after a successful refresh
RETRY_INTERVAL = 300     # Retry source-feed failure after 5 minutes

RSS_TIMEOUT = 10
ARTICLE_TIMEOUT = 15
ARTICLE_DELAY = 1.0      # One second between article downloads during backfill

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


def remove_page_furniture(element):
    for unwanted in element.select(
        "script, style, noscript, nav, header, footer, form, aside, "
        ".share, .social, .advertisement, .ad, .comments, .cookie"
    ):
        unwanted.decompose()


def extract_json_ld_body(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        records = data if isinstance(data, list) else [data]

        for record in records:
            if not isinstance(record, dict):
                continue

            candidates = [record]
            if isinstance(record.get("@graph"), list):
                candidates.extend(record["@graph"])

            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue

                body = candidate.get("articleBody")
                if body and len(body.strip()) > 200:
                    return "".join(
                        f"<p>{escape(paragraph.strip())}</p>"
                        for paragraph in body.splitlines()
                        if paragraph.strip()
                    )

    return None


def extract_article_html(html):
    """Extract the full article body from a Streator Standard article page."""
    soup = BeautifulSoup(html, "html.parser")

    selectors = [
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

        remove_page_furniture(content)

        if len(content.get_text(" ", strip=True)) > 300:
            return str(content)

    json_ld_body = extract_json_ld_body(soup)
    if json_ld_body:
        return json_ld_body

    paragraphs = []
    for paragraph in soup.find_all("p"):
        text = paragraph.get_text(" ", strip=True)
        if len(text) >= 40:
            paragraphs.append(f"<p>{escape(text)}</p>")

    if len(paragraphs) >= 3:
        return "".join(paragraphs)

    # Fallback for pages using repeated <br> tags instead of <p> tags.
    blocks = re.findall(
        r"((?:[^<]*<br\s*/?>\s*){3,}[^<]*)",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if blocks:
        return max(blocks, key=len)

    return "<p>Content not found.</p>"


def fetch_article_html(url):
    # Keep the original /f/... URL from the source RSS feed.
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=ARTICLE_TIMEOUT,
    )
    response.raise_for_status()

    return extract_article_html(response.text)


def generate_feed():
    """Create the full-text RSS feed and cache uncached article bodies."""
    global cached_feed

    try:
        # Uses the same request pattern as the original working script.
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
    items = source_feed.find_all("item")

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

       