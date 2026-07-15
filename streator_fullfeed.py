#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response
import re
import threading
import time

FEED_URL = "https://thestreatorstandard.com/f.rss"
ARTICLE_SELECTOR = 'div.blog-post-content'   # Correct selector
REFRESH_INTERVAL = 3600  # 60 minutes

# REQUIRED: GoDaddy blocks non-browser clients unless we send a real User-Agent
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

app = Flask(__name__)
cached_feed = None

def generate_feed():
    global cached_feed

    # Fetch RSS with browser headers
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

        # Fetch article HTML with browser headers
        html = requests.get(full_link, headers=HEADERS, timeout=10).text
        page = BeautifulSoup(html, "html.parser")

        # Extract article content
        content_div = page.select_one(ARTICLE_SELECTOR)
        content_html = str(content_div) if content_div else "Content not found."

        # Build RSS entry
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
