import html
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Comment

FEED_URL = "https://zdopravy.cz/feed/"
OUTPUT_DIR = Path("public")
LIMIT = 30
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
</head>
<body>
{body}
</body>
</html>
"""


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    with httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (personal reader)"},
        follow_redirects=True,
        timeout=30,
    ) as client:
        items = parse_feed(client.get(FEED_URL).raise_for_status().text)[:LIMIT]
        entries = []
        for item in items:
            slug = item["url"].rstrip("/").rsplit("/", 1)[-1]
            print(f"Scraping {item['url']}", file=sys.stderr)
            try:
                body = scrape_article(client.get(item["url"]).raise_for_status().text)
            except Exception as e:
                print(f"  failed: {e}", file=sys.stderr)
                continue
            write_article(slug, item, body)
            entries.append((slug, item))
    write_index(entries)
    keep = {f"{slug}.html" for slug, _ in entries} | {"index.html"}
    for path in OUTPUT_DIR.glob("*.html"):
        if path.name not in keep:
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


def write_index(entries: list[tuple[str, dict]]) -> None:
    lines = [
        "<h1>Zdopravy.cz</h1>",
        f"<p><small>Aktualizováno {datetime.now(TZ):%-d. %-m. %Y %H:%M}</small></p>",
        "<ul>",
    ]
    for slug, item in entries:
        lines.append(
            f'<li><a href="{slug}.html">{html.escape(item["title"])}</a> '
            f"<small>{item['date']:%-d. %-m. %H:%M}</small></li>"
        )
    lines.append("</ul>")
    (OUTPUT_DIR / "index.html").write_text(
        PAGE.format(title="Zdopravy.cz", body="\n".join(lines))
    )
