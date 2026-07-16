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
    soup = BeautifulSoup(html, "html.parser")

    selectors = [
        "[data-ux='BlogContent']",
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
    logging.info("Playwright fetching: %s", url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Load page without waiting for full network idle
        page.goto(url, timeout=ARTICLE_TIMEOUT, wait_until="domcontentloaded")

        # Wait for article content to appear
        try:
            page.wait_for_selector("article, main, .entry-content", timeout=10000)
        except:
            logging.warning("Article selector not found, continuing anyway")

        html = page.content()
        browser.close()

    content_html = extract_article_html(html)

    if content_html == "<p>Content not found.</p>":
        raise ValueError("Article body was not found")

    return content_html


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

            except Exception as error:
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
