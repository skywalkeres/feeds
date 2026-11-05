#!/usr/bin/env python3
"""Genera feeds Atom con el artículo completo a partir de feeds que solo traen extractos."""
import copy
import hashlib
import html as htmllib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import lxml.etree
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


EMBED_HOSTS = ("youtube.com", "youtube-nocookie.com", "youtu.be", "vimeo.com",
               "spotify.com", "bandcamp.com", "soundcloud.com", "dailymotion.com")
YT_ID = re.compile(r"(?:embed/|v=|youtu\.be/)([\w-]{11})")


def _host_ok(src):
    host = urlparse(src).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in EMBED_HOSTS)


def _stem(u):
    """Nombre base de una imagen, sin extensiones (incluso dobles como '.jpg.avif') ni sufijos de
    tamaño ('-610x352'), para reconocer la misma foto aunque venga en versiones distintas."""
    name = os.path.basename(urlparse(u).path)
    name = re.sub(r"(\.(jpe?g|png|gif|webp|avif|bmp|svg))+$", "", name, flags=re.I)
    return re.sub(r"-\d+x\d+$", "", name).lower()


def fix_noscript(tree):
    """Muchas webs ponen la imagen real dentro de <noscript> junto a un marcador de carga
    diferida. Nos quedamos con la imagen real y quitamos el marcador y el <noscript>."""
    for ns in list(tree.iter("noscript")):
        parent = ns.getparent()
        imgs = ns.xpath(".//img")
        if parent is None:
            continue
        if imgs:
            prev = ns.getprevious()
            if prev is not None and prev.tag == "img":
                parent.remove(prev)
            ns.addprevious(imgs[0])
        ns.drop_tree()


def fix_lazy(tree):
    """Convierte los atributos de carga diferida (data-src...) en src/srcset reales."""
    for el in tree.iter("img", "iframe", "source"):
        if el.tag in ("img", "iframe"):
            for attr in ("data-src", "data-lazy-src", "data-original", "data-orig-file"):
                v = el.get(attr)
                if v and not v.startswith("data:"):
                    el.set("src", v)
                    break
        for attr in ("data-srcset", "data-lazy-srcset"):
            v = el.get(attr)
            if v:
                el.set("srcset", v)
                break


def header_images(tree, frag, base_url):
    """Imágenes situadas entre el título y el primer párrafo del artículo, que el extractor
    suele descartar por no llevar texto."""
    first_p = None
    for p in frag.iter("p"):
        t = " ".join(p.text_content().split())
        if len(t) > 100:
            first_p = t[:60]
            break
    if not first_p:
        return []
    found = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag in ("h1", "h2", "h3"):
            found = []  # solo valen las imágenes posteriores al último título anterior al texto
        elif el.tag == "p" and " ".join(el.text_content().split()).startswith(first_p):
            break
        elif el.tag == "img":
            src = el.get("src") or ""
            if not src or src.startswith("data:"):
                continue
            src = urljoin(base_url, src)
            w = el.get("width") or ""
            small = w.isdigit() and int(w) < 150
            if not small and not re.search(r"logo|avatar|gravatar|icon|sprite|pixel|banner", src, re.I):
                found.append(src)
    else:
        return []  # no se localizó el primer párrafo: mejor no añadir nada
    return found[:2]


def find_embeds(tree, base_url):
    """Busca vídeos/audios incrustados (YouTube, Vimeo, Spotify...) dentro del artículo."""
    found, seen = [], set()
    for box in (tree.xpath("//article") or [tree]):
        for el in box.iter():
            if not isinstance(el.tag, str) or el.tag not in ("iframe", "div", "span"):
                continue
            src = el.get("src") or ""
            if not src or src.startswith(("about:", "data:")):
                src = el.get("data-src") or ""
            if not src:
                continue
            src = urljoin(base_url, src)
            if _host_ok(src) and src not in seen:
                seen.add(src)
                found.append(src)
    return found[:3]


def embed_html(src):
    m = YT_ID.search(src)
    if m and "youtu" in src:
        vid = m.group(1)
        embed = f"https://www.youtube.com/embed/{vid}"
        watch = f"https://www.youtube.com/watch?v={vid}"
        label = "▶ Ver vídeo en YouTube"
    else:
        embed, watch, label = src, src, "▶ Abrir contenido incrustado"
    return (f'<p><iframe src="{htmllib.escape(embed, quote=True)}" width="560" height="315" '
            f'allowfullscreen></iframe></p>'
            f'<p><a href="{htmllib.escape(watch, quote=True)}">{label}</a></p>')


def decode_html(r):
    """Decodifica la página probando primero la cabecera, luego UTF-8 y luego la etiqueta meta.
    Evita adivinar la codificación, que a veces confunde UTF-8 con otras (p. ej. 'así' -> 'as√≠')."""
    content = r.content
    candidates = []
    m = re.search(r"charset=([\w.-]+)", r.headers.get("content-type", ""), re.I)
    if m:
        candidates.append(m.group(1))
    candidates.append("utf-8")
    m2 = re.search(rb'<meta[^>]+charset=["\']?\s*([\w.-]+)', content[:4096], re.I)
    if m2:
        candidates.append(m2.group(1).decode("ascii", "ignore"))
    for enc in candidates:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode(r.apparent_encoding or "latin-1", errors="replace")


_EMBED_TAGS = ("iframe", "embed", "div", "span", "lite-youtube", "amp-youtube")
_URL_ATTRS = ("src", "data-src", "data-lazy-src", "data-url", "data-embed", "data-video-url")
_YT_ID_ATTRS = ("videoid", "data-youtube-id", "data-yt-id")


def _norm(text):
    return " ".join(text.split())


def embed_src(el, base_url):
    """URL de vídeo/audio incrustado (YouTube, Vimeo, Spotify...) que representa este elemento."""
    if not isinstance(el.tag, str) or el.tag not in _EMBED_TAGS:
        return None
    for attr in _URL_ATTRS:
        v = (el.get(attr) or "").strip()
        if v and not v.startswith(("about:", "data:", "javascript:")):
            v = urljoin(base_url, v)
            if _host_ok(v):
                return v
    for attr in _YT_ID_ATTRS:
        v = (el.get(attr) or "").strip()
        if re.fullmatch(r"[\w-]{11}", v):
            return f"https://www.youtube.com/embed/{v}"
    return None


def _embed_key(src):
    m = YT_ID.search(src)
    return m.group(1) if (m and "youtu" in src) else src


def _paragraph_map(frag):
    paras = {}
    for p in frag.iter("p"):
        k = _norm(p.text_content())[:60]
        if len(k) >= 40:
            paras.setdefault(k, p)
    return paras


def collect_embeds(tree, frag, base_url):
    """Vídeos/audios incrustados situados dentro del artículo, con el párrafo al que siguen.
    Devuelve None si no se pudo localizar el artículo en la página original."""
    paras = _paragraph_map(frag)
    if not paras:
        return None
    last_key = list(paras)[-1]
    pending, found, seen = [], [], set()
    started, anchor = False, None
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag in ("h1", "h2", "h3"):
            if started and anchor == last_key:
                break  # ya pasamos el último párrafo del artículo
            if not started:
                pending = []  # solo valen los incrustados posteriores al último título previo al texto
            continue
        if el.tag == "p":
            k = _norm(el.text_content())[:60]
            if k in paras:
                if not started:
                    started = True
                    found.extend((None, s) for s in pending)
                anchor = k
                continue
        src = embed_src(el, base_url)
        if src:
            (found if started else pending).append((anchor, src) if started else src)
    if not started:
        return None
    out = []
    for a, s in found:
        key = _embed_key(s)
        if key not in seen:
            seen.add(key)
            out.append((a, s))
    return out[:6]


def insert_embeds(frag, embeds):
    """Inserta cada incrustado justo después del párrafo que lo precedía en la web original."""
    paras = _paragraph_map(frag)
    last, top = {}, []
    for anchor, src in embeds:
        wrapper = lxml.html.fragment_fromstring(embed_html(src), create_parent="div")
        els = list(wrapper)
        if anchor is None or anchor not in paras:
            top.extend(els)
            continue
        ref = last.get(anchor, paras[anchor])
        for e in els:
            ref.addnext(e)
            ref = e
        last[anchor] = ref
    for i, e in enumerate(top):
        frag.insert(i, e)


def normalize_kept_embeds(frag, base_url):
    """Sustituye los vídeos que el extractor conservó (con restos como el texto 'VIDEO') por
    nuestro bloque estándar: iframe + enlace de respaldo. Devuelve sus claves."""
    keys = set()
    for el in list(frag.iter("iframe", "embed")):
        src = embed_src(el, base_url)
        if not src:
            continue
        keys.add(_embed_key(src))
        els = list(lxml.html.fragment_fromstring(embed_html(src), create_parent="div"))
        parent = el.getparent()
        anchor = parent if (parent is not None and parent.tag == "p") else el
        ref = anchor
        for e in els:
            ref.addnext(e)
            ref = e
        el.drop_tree()
    return keys


def _inner(frag):
    return ((frag.text or "") + "".join(
        lxml.html.tostring(c, encoding="unicode") for c in frag)).strip()


def _has_class(name):
    return f'contains(concat(" ", normalize-space(@class), " "), " {name} ")'


def fix_abandomoviez(frag, base_url):
    """Abandomoviez: cada estreno es un bloque 'top-card' con la imagen, un botón 'TRÁILER' que
    abre el vídeo con JavaScript, la fecha y el título. Sin JavaScript el botón queda como el texto
    suelto 'TRÁILER12 NOVAllen (2026)'. Lo convertimos en imagen enlazada + título + enlace."""
    for card in frag.xpath(f'.//*[{_has_class("top-card")}]'):
        links = card.xpath('.//a[@href][.//img]')
        href = urljoin(base_url, links[0].get("href")) if links else None
        title = _norm(" ".join(card.xpath(f'.//*[{_has_class("top-titulo")}]//text()')))
        date = _norm(" ".join(card.xpath(f'.//*[{_has_class("top-numero")}]//text()')))
        new = lxml.html.Element("div")
        imgs = card.xpath(".//img")
        if imgs:
            p1 = lxml.etree.SubElement(new, "p")
            holder = lxml.etree.SubElement(p1, "a", href=href) if href else p1
            holder.append(copy.deepcopy(imgs[0]))
        p2 = lxml.etree.SubElement(new, "p")
        label = " · ".join(x for x in (date, title) if x)
        if label:
            strong = lxml.etree.SubElement(p2, "strong")
            strong.text = label
        if href:
            if label:
                strong.tail = " — "
            a = lxml.etree.SubElement(p2, "a", href=href)
            a.text = "▶ Ver ficha y tráiler"
        card.addprevious(new)
        card.drop_tree()


SITE_RULES = {"abandomoviez.net": fix_abandomoviez}


def apply_site_rules(frag, url):
    host = urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    rule = SITE_RULES.get(host)
    if rule:
        rule(frag, url)


def extract_full(session, url):
    """Descarga el artículo y devuelve el HTML limpio del cuerpo, o None si falla."""
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        try:
            tree = lxml.html.fromstring(decode_html(r))
        except ValueError:
            tree = lxml.html.fromstring(r.content)
        fix_noscript(tree)
        fix_lazy(tree)
        page_html = lxml.html.tostring(tree, encoding="unicode")

        content = Document(page_html).summary(html_partial=True)
        frag = lxml.html.fragment_fromstring(content, create_parent="div")
        apply_site_rules(frag, url)
        frag.make_links_absolute(url)
        inner = _inner(frag)
        if not inner:
            return None

        extras = ""
        have = {_stem(i) for i in frag.xpath(".//img/@src")}
        # imágenes de cabecera (entre el título y el primer párrafo)
        for src in header_images(tree, frag, url):
            if _stem(src) not in have:
                have.add(_stem(src))
                extras += f'<p><img src="{htmllib.escape(src, quote=True)}"></p>'
        # imagen destacada (suele estar fuera del cuerpo del artículo)
        meta = (tree.xpath('//meta[@property="og:image"]/@content')
                or tree.xpath('//meta[@name="og:image"]/@content')
                or tree.xpath('//meta[@name="twitter:image"]/@content'))
        if meta:
            og_url = urljoin(url, meta[0].strip())
            stem = _stem(og_url)
            if stem and stem not in have and "logo" not in stem:
                extras += f'<p><img src="{htmllib.escape(og_url, quote=True)}"></p>'
        # vídeos / audios incrustados que el extractor suele descartar, en su sitio
        already = normalize_kept_embeds(frag, url)
        embeds = collect_embeds(tree, frag, url)
        if embeds is None:
            embeds = [(None, s) for s in find_embeds(tree, url)]
        embeds = [(a, s) for a, s in embeds if _embed_key(s) not in already]
        insert_embeds(frag, embeds)
        return (extras + _inner(frag)).strip()
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
