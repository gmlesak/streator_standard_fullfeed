#!/usr/bin/env python3
import json
import logging
import os
import sqlite3
import threading
import time
from html import escape

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response

FEED_URL = "https://thestreatorstandard.com/f.rss"
REFRESH_INTERVAL = 3600  # 60 minutes
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
session = requests.Session()
session.headers.update(HEADERS)

cached_feed = None
feed_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def init_database():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content_html TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_cached_article(url):
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT content_html FROM articles WHERE url = ?",
            (url,),
        ).fetchone()

    return row[0] if row else None


def save_article(url, title, content_html):
    with sqlite3.connect(DB_PATH) as connection:
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
    """Extract the article body from an Streator Standard page."""
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

    return "<p>Content not found.</p>"


def fetch_article_html(url):
    # Keep the original /f/... URL from the source RSS feed.
    response = session.get(url, timeout=15)
    response.raise_for_status()
    return extract_article_html(response.text)


def generate_feed():
    global cached_feed

    try:
        response = session.get(FEED_URL, timeout=15)
        response.raise_for_status()
    except requests.RequestException as error:
        logging.error("Could not download source RSS feed: %s", error)
        return

    source_feed = BeautifulSoup(response.content, "xml")

    feed = FeedGenerator()
    feed.id("streator-standard-full-feed")
    feed.title("Streator Standard – Full Articles")
    feed.link(href="https://thestreatorstandard.com/", rel="alternate")
    feed.description("Full-text feed generated locally")
    feed.language("en")

    items = source_feed.find_all("item")
    logging.info("Refreshing %d source feed entries.", len(items))

    for item in items:
        title_tag = item.find("title")
        link_tag = item.find("link")

        if not title_tag or not link_tag:
            continue

        title = title_tag.get_text(strip=True)
        full_link = link_tag.get_text(strip=True)

        content_html = get_cached_article(full_link)

        if content_html is None:
            try:
                content_html = fetch_article_html(full_link)
                save_article(full_link, title, content_html)
                logging.info("Downloaded and cached: %s", title)
            except requests.RequestException as error:
                logging.warning("Download failed for %s: %s", full_link, error)
                content_html = "<p>Article content could not be downloaded.</p>"
            except Exception:
                logging.exception("Extraction failed for %s", full_link)
                content_html = "<p>Article content could not be extracted.</p>"
        else:
            logging.info("Using cached article: %s", title)

        plain_description = BeautifulSoup(
            content_html,
            "html.parser",
        ).get_text(" ", strip=True)

        entry = feed.add_entry()
        entry.id(full_link)
        entry.guid(full_link, permalink=True)
        entry.title(title)
        entry.link(href=full_link)
        entry.description(plain_description)
        entry.content(content_html, type="html")

    with feed_lock:
        cached_feed = feed.rss_str(pretty=True)

    logging.info("Full-text feed refresh complete.")


def refresh_loop():
    while True:
        generate_feed()
        time.sleep(REFRESH_INTERVAL)


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