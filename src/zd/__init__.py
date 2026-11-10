import html
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Comment

FEED_URL = "https://zdopravy.cz/feed/"
SITE_URL = "https://honzajavorek.github.io/zd/"
OUTPUT_DIR = Path("public")
CACHE_DIR = Path("cache")
LIMIT = 500
FEED_LIMIT = 100
REFRESH_AGE = timedelta(days=1)
TZ = ZoneInfo("Europe/Prague")

KEEP_ATTRS = {"a": {"href"}, "img": {"src", "alt"}}
DROP_SELECTORS = [
    ".ad-block",
    "script",
    "style",
    "iframe",
    "noscript",
    "form",
    "#tags",
    "#multireklama",
    "aside",
    "blockquote.wp-embedded-content",
    "a[href*='#comments']",
]

PAGE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<link rel="alternate" type="application/rss+xml" title="Zdopravy.cz" href="feed.xml">
</head>
<body>
{body}
</body>
</html>
"""


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    CACHE_DIR.mkdir(exist_ok=True)
    with httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (personal reader)"},
        follow_redirects=True,
        timeout=30,
    ) as client:
        items = []
        page = 1
        while len(items) < LIMIT:
            response = client.get(FEED_URL, params={"paged": page})
            if response.status_code == 404:
                break
            new_items = parse_feed(response.raise_for_status().text)
            if not new_items:
                break
            items.extend(new_items)
            page += 1
        items = list({item["url"]: item for item in items}.values())[:LIMIT]

        now = datetime.now(TZ)
        entries = []
        for item in items:
            slug = item["url"].rstrip("/").rsplit("/", 1)[-1]
            cache_path = CACHE_DIR / f"{slug}.html"
            if cache_path.exists() and now - item["date"] > REFRESH_AGE:
                body = cache_path.read_text()
            else:
                print(f"Scraping {item['url']}", file=sys.stderr)
                try:
                    page_html = client.get(item["url"]).raise_for_status().text
                    body = scrape_article(page_html)
                except Exception as e:
                    print(f"  failed: {e}", file=sys.stderr)
                    if not cache_path.exists():
                        continue
                    body = cache_path.read_text()
                else:
                    cache_path.write_text(body)
            write_article(slug, item, body)
            entries.append((slug, item, body))

    write_index(entries)
    write_feed(entries[:FEED_LIMIT])
    slugs = {slug for slug, _, _ in entries}
    for path in OUTPUT_DIR.glob("*.html"):
        if path.stem not in slugs | {"index"}:
            path.unlink()
    for path in CACHE_DIR.glob("*.html"):
        if path.stem not in slugs:
            path.unlink()


def parse_feed(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    return [
        {
            "title": item.findtext("title", "").strip(),
            "url": item.findtext("link", "").strip(),
            "date": parsedate_to_datetime(item.findtext("pubDate")).astimezone(TZ),
            "perex": item.findtext("description", "").strip(),
        }
        for item in root.iter("item")
    ]


def scrape_article(page: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    main = soup.select_one("main#main") or soup.select_one("article")
    for selector in DROP_SELECTORS:
        for el in main.select(selector):
            el.decompose()

    for el in main.find_all(string=lambda s: isinstance(s, Comment)):
        el.extract()
    for el in main.select("a[href*='new_gallery']"):
        el.unwrap()

    image = soup.select_one("article .post-image img")
    if image:
        main.insert(0, image)

    for el in main.find_all(True):
        if el.name in ("div", "span", "main", "section"):
            el.unwrap()
            continue
        allowed = KEEP_ATTRS.get(el.name, set())
        el.attrs = {k: v for k, v in el.attrs.items() if k in allowed}
        if el.name == "img" and el.parent.name != "p":
            el.wrap(soup.new_tag("p"))
        elif el.name == "p" and not el.find(True):
            el.string = " ".join(el.get_text().split())

    for el in main.find_all(["p", "a"]):
        if not el.get_text(strip=True) and not el.find("img"):
            el.decompose()

    text = main.decode_contents()
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def write_article(slug: str, item: dict, body: str) -> None:
    title = html.escape(item["title"])
    content = (
        f'<p><a href="index.html">← zpět</a></p>\n'
        f"<h1>{title}</h1>\n"
        f"<p><small>{item['date']:%-d. %-m. %Y %H:%M} · "
        f'<a href="{html.escape(item["url"])}">originál</a></small></p>\n'
        f"{body}\n"
        f'<p><a href="index.html">← zpět</a></p>'
    )
    (OUTPUT_DIR / f"{slug}.html").write_text(PAGE.format(title=title, body=content))


def write_index(entries: list[tuple[str, dict, str]]) -> None:
    lines = [
        "<h1>Zdopravy.cz</h1>",
        '<p><a href="feed.xml">RSS</a></p>',
        f"<p><small>Aktualizováno {datetime.now(TZ):%-d. %-m. %Y %H:%M}</small></p>",
        "<ul>",
    ]
    for slug, item, _ in entries:
        lines.append(
            f'<li><a href="{slug}.html">{html.escape(item["title"])}</a> '
            f"<small>{item['date']:%-d. %-m. %H:%M}</small></li>"
        )
    lines.append("</ul>")
    (OUTPUT_DIR / "index.html").write_text(
        PAGE.format(title="Zdopravy.cz", body="\n".join(lines))
    )


def write_feed(entries: list[tuple[str, dict, str]]) -> None:
    content_ns = "http://purl.org/rss/1.0/modules/content/"
    ET.register_namespace("content", content_ns)
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Zdopravy.cz"
    ET.SubElement(channel, "link").text = SITE_URL
    ET.SubElement(channel, "description").text = "Zdopravy.cz bez reklam"
    ET.SubElement(channel, "language").text = "cs"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(TZ))
    for slug, item, body in entries:
        el = ET.SubElement(channel, "item")
        ET.SubElement(el, "title").text = item["title"]
        ET.SubElement(el, "link").text = f"{SITE_URL}{slug}.html"
        ET.SubElement(el, "guid", isPermaLink="false").text = item["url"]
        ET.SubElement(el, "pubDate").text = format_datetime(item["date"])
        ET.SubElement(el, "description").text = item["perex"]
        ET.SubElement(el, f"{{{content_ns}}}encoded").text = (
            f'{body}\n<p><a href="{html.escape(item["url"])}">originál</a></p>'
        )
    ET.ElementTree(rss).write(
        OUTPUT_DIR / "feed.xml", encoding="utf-8", xml_declaration=True
    )
