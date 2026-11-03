#!/usr/bin/env python3
"""Genera feeds Atom con el artículo completo a partir de feeds que solo traen extractos."""
import hashlib
import html as htmllib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import lxml.html
import requests
from feedgen.feed import FeedGenerator
from readability import Document

ROOT = Path(__file__).parent
OUT = ROOT / "docs"
CACHE_FILE = ROOT / "cache.json"
FEEDS_FILE = ROOT / "feeds.txt"

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
MAX_ENTRIES = 20        # artículos por feed
MIN_FULL_CHARS = 1500   # si el feed ya trae más texto que esto, no se descarga el artículo
MAX_TRIES = 3           # intentos de extracción por artículo
DELAY = 1.0             # segundos entre descargas (cortesía con los sitios)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def text_len(html: str) -> int:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return len(re.sub(r"\s+", " ", text).strip())


_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def clean(value):
    """Quita caracteres de control que XML no admite (algunos feeds los traen)."""
    return _ILLEGAL_XML.sub("", value or "")


def read_feed_list():
    urls = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def make_slug(url: str) -> str:
    host = re.sub(r"[^a-z0-9]+", "-", urlparse(url).netloc.lower()).strip("-")
    return f"{host}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"


def entry_dt(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def extract_full(session, url):
    """Descarga el artículo y devuelve el HTML limpio del cuerpo, o None si falla."""
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        if r.encoding is None or (r.encoding.lower() == "iso-8859-1" and "charset" not in ctype):
            r.encoding = r.apparent_encoding
        content = Document(r.text).summary(html_partial=True)
        frag = lxml.html.fragment_fromstring(content, create_parent="div")
        frag.make_links_absolute(url)
        inner = (frag.text or "") + "".join(
            lxml.html.tostring(c, encoding="unicode") for c in frag)
        return inner.strip() or None
    except Exception as exc:  # noqa: BLE001
        print(f"    ! no se pudo extraer {url}: {exc}")
        return None


def process_feed(url, session, cache, new_cache):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if not parsed.entries:
        raise RuntimeError("feed vacío o ilegible")

    title = clean(parsed.feed.get("title", url))
    site = clean(parsed.feed.get("link", url))
    slug = make_slug(url)
    now_iso = datetime.now(timezone.utc).isoformat()

    fg = FeedGenerator()
    fg.id(url)
    fg.title(f"{title} (texto completo)")
    fg.subtitle(f"Versión con artículo completo de {url}")
    fg.author({"name": title})
    fg.link(href=site, rel="alternate")
    fg.link(href=f"{BASE_URL}/{slug}.xml", rel="self")

    latest = None
    fetched = 0
    for e in parsed.entries[:MAX_ENTRIES]:
        link = e.get("link")
        if not link:
            continue
        prev = cache.get(link, {})
        seen = prev.get("seen") or now_iso

        original = ""
        if e.get("content"):
            original = e.content[0].get("value", "")
        if not original:
            original = e.get("summary", "")

        html = original
        stored = prev.get("html")
        tries = prev.get("tries", 0)

        if text_len(original) < MIN_FULL_CHARS:
            if not stored and tries < MAX_TRIES:
                stored = extract_full(session, link)
                tries += 1
                fetched += 1
                time.sleep(DELAY)
            if stored and text_len(stored) > text_len(original):
                html = stored

        new_cache[link] = {"seen": seen, "html": stored, "tries": tries, "feed": url}

        dt = entry_dt(e) or datetime.fromisoformat(seen)
        if latest is None or dt > latest:
            latest = dt

        fe = fg.add_entry()
        fe.id(clean(e.get("id") or link))
        fe.title(clean(e.get("title")) or "(sin título)")
        fe.link(href=clean(link))
        fe.published(dt)
        fe.updated(dt)
        fe.content(clean(html), type="html")
        if e.get("author"):
            fe.author({"name": clean(e.get("author"))})

    fg.updated(latest or datetime.now(timezone.utc))
    fg.atom_file(str(OUT / f"{slug}.xml"), pretty=True)
    print(f"  ok: {title} ({fetched} artículos descargados)")
    return {"slug": slug, "title": f"{title} (texto completo)", "site": site}


def write_index(results):
    items = "\n".join(
        f'<li><a href="{r["slug"]}.xml">{htmllib.escape(r["title"])}</a></li>' for r in results)
    (OUT / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Feeds de texto completo</title>"
        f"<h1>Feeds de texto completo</h1><p><a href='feeds.opml'>Descargar OPML</a></p><ul>{items}</ul>",
        encoding="utf-8")
    outlines = "\n".join(
        f'    <outline type="rss" text={json_attr(r["title"])} title={json_attr(r["title"])} '
        f'xmlUrl={json_attr(BASE_URL + "/" + r["slug"] + ".xml")} htmlUrl={json_attr(r["site"])}/>'
        for r in results)
    (OUT / "feeds.opml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">\n'
        '  <head><title>Feeds de texto completo</title></head>\n  <body>\n'
        f'{outlines}\n  </body>\n</opml>\n', encoding="utf-8")


def json_attr(value: str) -> str:
    return '"' + htmllib.escape(value, quote=True) + '"'


def main():
    OUT.mkdir(exist_ok=True)
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    new_cache = {}
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "es,en;q=0.8"})

    results = []
    for url in read_feed_list():
        print(f"Procesando {url}")
        try:
            results.append(process_feed(url, session, cache, new_cache))
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {exc}")
            # conserva la caché y el XML anteriores de ese feed para no perder trabajo
            for k, v in cache.items():
                if v.get("feed") == url:
                    new_cache[k] = v
            slug = make_slug(url)
            if (OUT / f"{slug}.xml").exists():
                results.append({"slug": slug, "title": url, "site": url})

    write_index(results)
    CACHE_FILE.write_text(json.dumps(new_cache, ensure_ascii=False, indent=0, sort_keys=True),
                          encoding="utf-8")


if __name__ == "__main__":
    main()
