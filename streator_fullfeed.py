#!/usr/bin/env python3
import json
import logging
import threading
import time
from html import escape

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response

FEED_URL = "https://thestreatorstandard.com/f.rss"
REFRESH_INTERVAL = 3600  # 60 minutes

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


def extract_json_ld_body(soup):
    """Return an articleBody value from Schema.org JSON-LD, if present."""
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


def remove_page_furniture(element):
    """Remove navigation, scripts, social buttons, and similar non-article items."""
    for unwanted in element.select(
        "script, style, noscript, nav, header, footer, form, aside, "
        ".share, .social, .advertisement, .ad, .comments, .cookie"
    ):
        unwanted.decompose()


def extract_article_html(html):
    """Extract full article HTML from an Streator Standard article page."""
    soup = BeautifulSoup(html, "html.parser")

    # First try article-specific containers.
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

    # Next try structured article metadata.
    json_ld_body = extract_json_ld_body(soup)
    if json_ld_body:
        return json_ld_body

    # Fallback: build article HTML from meaningful paragraphs.
    paragraphs = []
    for paragraph in soup.find_all("p"):
        text = paragraph.get_text(" ", strip=True)
        if len(text) >= 40:
            paragraphs.append(f"<p>{escape(text)}</p>")

    if len(paragraphs) >= 3:
        return "".join(paragraphs)

    return "<p>Content not found.</p>"


def fetch_article_html(url):
    """
    Download the article.

    Important: Streator Standard's real article URLs use /f/... .
    Do NOT rewrite them to /post/... .
    """
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return extract_article_html(response.text)


def generate_feed():
    global cached_feed

    try:
        response = session.get(FEED_URL, timeout=45)
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
    logging.info("Refreshing %d feed entries.", len(items))

    for item in items:
        title_tag = item.find("title")
        link_tag = item.find("link")

        if not title_tag or not link_tag:
            continue

        title = title_tag.get_text(strip=True)

        # Keep the original /f/... article URL from the source RSS feed.
        full_link = link_tag.get_text(strip=True)

        try:
            content_html = fetch_article_html(full_link)
            logging.info("Extracted: %s", title)
        except requests.RequestException as error:
            logging.warning("Download failed for %s: %s", full_link, error)
            content_html = "<p>Article content could not be downloaded.</p>"
        except Exception:
            logging.exception("Extraction failed for %s", full_link)
            content_html = "<p>Article content could not be extracted.</p>"

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

    new_feed = feed.rss_str(pretty=True)

    with feed_lock:
        cached_feed = new_feed

    logging.info("Full-text feed refresh complete.")


def refresh_loop():
    while True:
        generate_feed()
        time.sleep(REFRESH_INTERVAL)


@app.route("/fullfeed.xml")
def fullfeed():
    global cached_feed

    with feed_lock:
        feed = cached_feed

    if feed is None:
        generate_feed()

        with feed_lock:
            feed = cached_feed

    if feed is None:
        return Response(
            "Feed is not available yet. Please try again shortly.",
            status=503,
            mimetype="text/plain",
        )

    return Response(feed, mimetype="application/rss+xml")


if __name__ == "__main__":
    generate_feed()

    threading.Thread(
        target=refresh_loop,
        daemon=True,
        name="feed-refresh",
    ).start()

    app.run(host="0.0.0.0", port=9111)