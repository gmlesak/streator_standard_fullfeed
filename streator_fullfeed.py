#!/usr/bin/env python3
import json
import logging
import re
import threading
import time
from html import escape

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response

FEED_URL = "https://thestreatorstandard.com/f.rss"
REFRESH_INTERVAL = 3600  # Refresh every 60 minutes

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


def extract_json_ld_article_body(soup: BeautifulSoup) -> str | None:
    """Look for full text exposed in Schema.org JSON-LD metadata."""
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

                article_body = candidate.get("articleBody")
                if article_body and len(article_body.strip()) > 100:
                    paragraphs = [
                        f"<p>{escape(paragraph.strip())}</p>"
                        for paragraph in article_body.splitlines()
                        if paragraph.strip()
                    ]
                    return "".join(paragraphs)

    return None


def extract_article_html(html: str) -> str:
    """Extract the article body and return safe article HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Check common article-content containers first.
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

        # Prefer the largest matching element.
        content = max(
            candidates,
            key=lambda tag: len(tag.get_text(" ", strip=True)),
        )

        # Ignore a candidate that clearly is not an article.
        if len(content.get_text(" ", strip=True)) < 200:
            continue

        # Remove page furniture and non-article content.
        for unwanted in content.select(
            "script, style, noscript, nav, header, footer, form, "
            "aside, .share, .social, .advertisement, .ad, .comments"
        ):
            unwanted.decompose()

        if len(content.get_text(" ", strip=True)) >= 200:
            return str(content)

    # Some sites store the body in structured metadata instead of visible markup.
    json_ld_body = extract_json_ld_article_body(soup)
    if json_ld_body:
        return json_ld_body

    # Final fallback: retain meaningful paragraph text.
    paragraphs = []
    for paragraph in soup.find_all("p"):
        text = paragraph.get_text(" ", strip=True)
        if len(text) >= 40:
            paragraphs.append(f"<p>{escape(text)}</p>")

    if paragraphs:
        return "".join(paragraphs)

    return "<p>Content not found.</p>"


def get_full_article_url(feed_url: str) -> str:
    """Convert the site's shortened feed URL to its full post URL."""
    return re.sub(r"/f/(.+)$", r"/post/\1", feed_url)


def fetch_article_html(url: str) -> str:
    response = session.get(url, timeout=20)
    response.raise_for_status()
    return extract_article_html(response.text)


def generate_feed() -> None:
    global cached_feed

    try:
        response = session.get(FEED_URL, timeout=20)
        response.raise_for_status()
    except requests.RequestException as error:
        logging.error("Could not download source RSS feed: %s", error)
        return

    source_feed = BeautifulSoup(response.content, "xml")

    feed = FeedGenerator()
    feed.id("https://thestreatorstandard.com/fullfeed.xml")
    feed.title("Streator Standard – Full Articles")
    feed.link(href="https://thestreatorstandard.com/", rel="alternate")
    feed.link(href="https://thestreatorstandard.com/fullfeed.xml", rel="self")
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
        source_link = link_tag.get_text(strip=True)
        full_link = get_full_article_url(source_link)

        try:
            content_html = fetch_article_html(full_link)
            logging.info("Extracted article: %s", title)
        except requests.RequestException as error:
            logging.warning("Could not fetch %s: %s", full_link, error)
            content_html = "<p>Article content could not be downloaded.</p>"
        except Exception:
            logging.exception("Could not extract article: %s", full_link)
            content_html = "<p>Article content could not be extracted.</p>"

        plain_description = BeautifulSoup(
            content_html, "html.parser"
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

    logging.info("Full-text feed refreshed successfully.")


def refresh_loop() -> None:
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