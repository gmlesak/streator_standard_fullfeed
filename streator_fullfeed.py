#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response
import re
import json
import threading
import time

FEED_URL = "https://thestreatorstandard.com/f.rss"
REFRESH_INTERVAL = 3600  # 60 minutes

# Browser-like headers so GoDaddy treats us as a real client
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
    GoDaddy Websites+Marketing injects blog content via a JS state object
    (window.__INITIAL_STATE__). The raw HTML contains a <script> tag with that
    object; the DOM that the browser sees is built from it.

    We:
    - find the <script> containing "__INITIAL_STATE__"
    - regex out the JSON object
    - parse it
    - pull articleBody from blog.posts[*]
    """
    page = BeautifulSoup(html, "html.parser")

    # Find the script tag that contains __INITIAL_STATE__
    script = page.find("script", string=re.compile("__INITIAL_STATE__"))
    if not script or not script.string:
        return "Content not found."

    # Extract JSON object assigned to window.__INITIAL_STATE__
    match = re.search(r"__INITIAL_STATE__\s*=\s*(\{.*\});", script.string, re.DOTALL)
    if not match:
        return "Content not found."

    try:
        state = json.loads(match.group(1))
    except Exception as e:
        return f"Content not found (JSON parse error: {e})"

    # Navigate into the blog posts structure
    blog = state.get("blog", {})
    posts = blog.get("posts", {})

    if not posts:
        return "Content not found."

    # There should be exactly one post on a /post/<slug> page
    post = list(posts.values())[0]
    content_html = post.get("articleBody") or post.get("description")

    if not content_html:
        return "Content not found."

    return content_html


def generate_feed():
    global cached_feed

    # Fetch RSS with browser-like headers
    rss = requests.get(FEED_URL, headers=HEADERS, timeout=10).text
    soup = BeautifulSoup(rss, "xml")

    fg = FeedGenerator()
    fg.title("Streator Standard – Full Articles")
    fg.link(href="https://thestreatorstandard.com")
    fg.description("Full-text feed generated locally")

    for item in soup.find_all("item"):
        title = item.title.text
        link = item.link.text

        # Rewrite /f/<slug> → /post/<slug> to hit the full article page
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
