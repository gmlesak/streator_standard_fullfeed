#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response
import re
import threading
import time

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
    "Upgrade-Insecure-Requests": "1",
}

app = Flask(__name__)
cached_feed = None


def extract_article_html(html: str) -> str:
    """
    The container HTML does not include the JSON state or the expected
    blog-post-content wrapper, but it DOES include the full article as a large
    block of text with many <br> tags.

    Strategy:
    - Find all <div> elements that contain at least one <br>
    - Choose the largest such <div> (by character length)
    - If none found, fall back to a regex that grabs a big <br>-heavy block
    """
    page = BeautifulSoup(html, "html.parser")

    candidates = []
    for div in page.find_all("div"):
        if div.find("br"):
            candidates.append(str(div))

    if candidates:
        return max(candidates, key=len)

    # Fallback: any big block of text with lots of <br> tags
    br_blocks = re.findall(r"((?:[^<]*<br\s*/?>){5,}[^<]*)", html, re.DOTALL)
    if br_blocks:
        return br_blocks[0]

    return "Content not found."


def generate_feed():
    global cached_feed

    rss = requests.get(FEED_URL, headers=HEADERS, timeout=10).text
    soup = BeautifulSoup(rss, "xml")

    fg = FeedGenerator()
    fg.title("Streator Standard – Full Articles")
    fg.link(href="https://thestreatorstandard.com")
    fg.description("Full-text feed generated locally")

    for item in soup.find_all("item"):
        title = item.title.text
        link = item.link.text

        # Rewrite /f/<slug> → /post/<slug>
        full_link = re.sub(r"/f/(.*)$", r"/post/\1", link)

        try:
            html = requests.get(full_link, headers=HEADERS, timeout=10).text
            content_html = extract_article_html(html)
        except Exception as e:
            content_html = f"Content not found (fetch error: {e})"

        fe = fg.add_entry()
        fe.title(title)
        fe.link(href=full_link)
        fe.description(content_html)
        fe.guid(full_link)

    cached_feed = fg.rss_str(pretty=True)


def refresh_loop():
    while True:
        generate_feed()
        time.sleep(REFRESH_INTERVAL)


@app.route("/fullfeed.xml")
def fullfeed():
    return Response(cached_feed, mimetype="application/rss+xml")


if __name__ == "__main__":
    generate_feed()
    threading.Thread(target=refresh_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=9111)
