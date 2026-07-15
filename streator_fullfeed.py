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
    The article body appears directly in the HTML as a large block of text
    separated by <br><br> sequences. We extract the largest such block.
    """

    # Find all blocks with repeated <br><br>
    blocks = re.findall(
        r"((?:[^<]*<br\s*/?><br\s*/?>){3,}[^<]*)",
        html,
        re.DOTALL
    )

    if blocks:
        return max(blocks, key=len)

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
