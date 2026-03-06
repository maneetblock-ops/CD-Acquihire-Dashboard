#!/usr/bin/env python3
"""Local server for corp dev pipeline dashboard."""

from __future__ import annotations

import argparse
import errno
import json
import re
from html import unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8610
MAX_PORT_ATTEMPTS = 25
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_BODY_BYTES = 500_000


def json_response(handler: SimpleHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def normalize_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"https://{url}"
    return url


def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=10) as resp:
        body = resp.read(MAX_BODY_BYTES)
    return body.decode("utf-8", errors="replace")


def extract_meta_description(html: str) -> str:
    patterns = [
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", unescape(match.group(1))).strip()
    return ""


def search_duckduckgo(query: str) -> list[str]:
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=12) as resp:
        html = resp.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")

    snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', html, flags=re.IGNORECASE | re.DOTALL)
    if not snippets:
        snippets = re.findall(r'<div[^>]*class="result__snippet"[^>]*>(.*?)</div>', html, flags=re.IGNORECASE | re.DOTALL)

    clean: list[str] = []
    for snippet in snippets:
        text = re.sub(r"<[^>]+>", " ", snippet)
        text = re.sub(r"\s+", " ", unescape(text)).strip()
        if text and text not in clean:
            clean.append(text)
        if len(clean) >= 4:
            break
    return clean


def normalize_text_for_similarity(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def is_low_quality_snippet(line: str) -> bool:
    text = normalize_text_for_similarity(line)
    bad_patterns = (
        "verified by gartner",
        "choose your business software",
        "software with confidence",
        "explore in depth",
        "real users",
        "reviews and insights",
        "compare",
        "best software",
        "read reviews",
        "g2",
        "capterra",
        "trustradius",
    )
    return any(pattern in text for pattern in bad_patterns)


def dedupe_similar(lines: list[str], threshold: float = 0.75) -> list[str]:
    out: list[str] = []
    seen_tokens: list[set[str]] = []
    for line in lines:
        cleaned = re.sub(r"\s+", " ", line).strip(" .")
        if not cleaned:
            continue
        tokens = set(normalize_text_for_similarity(cleaned).split())
        if not tokens:
            continue
        duplicate = False
        for existing in seen_tokens:
            overlap = len(tokens & existing) / max(1, len(tokens | existing))
            if overlap >= threshold:
                duplicate = True
                break
        if duplicate:
            continue
        out.append(cleaned)
        seen_tokens.append(tokens)
    return out


def compact_summary(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    summary = " ".join(sentences[:2]).strip()
    return summary[:440].rstrip()


def build_company_research(name: str, company_url: str) -> dict:
    website = normalize_url(company_url)
    parsed = urlparse(website)
    domain = parsed.netloc.lower().replace("www.", "")

    meta_description = ""
    website_error = ""
    try:
        page_html = fetch_html(website)
        meta_description = extract_meta_description(page_html)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        website_error = str(exc)

    search_query = f"{name} {domain} company product offerings"
    search_snippets: list[str] = []
    search_error = ""
    try:
        search_snippets = search_duckduckgo(search_query)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        search_error = str(exc)

    cleaned_snippets = [s for s in dedupe_similar(search_snippets, threshold=0.7) if not is_low_quality_snippet(s)]
    summary = ""
    if meta_description:
        summary = compact_summary(meta_description)
    elif cleaned_snippets:
        summary = compact_summary(cleaned_snippets[0])
    else:
        summary = f"{name} is under initial review. No reliable description was auto-fetched; please add manually."

    offerings: list[str] = []
    offering_terms = ("platform", "product", "api", "tool", "models", "software", "service", "agent")
    for line in dedupe_similar([meta_description, *cleaned_snippets], threshold=0.7):
        lower = line.lower()
        if any(term in lower for term in offering_terms) and not is_low_quality_snippet(line):
            offerings.append(compact_summary(line))
        if len(offerings) >= 3:
            break
    offerings = dedupe_similar(offerings, threshold=0.68)
    if not offerings and summary:
        offerings.append(summary)

    return {
        "normalized_url": website,
        "domain": domain,
        "summary": summary,
        "offerings": offerings,
        "sources": {
            "meta_description_found": bool(meta_description),
            "search_snippets": search_snippets,
            "website_error": website_error,
            "search_error": search_error,
        },
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/company-research":
            query = parse_qs(parsed.query)
            name = (query.get("name", [""])[0] or "").strip()
            company_url = (query.get("url", [""])[0] or "").strip()
            if not name or not company_url:
                return json_response(self, {"error": "Missing required query params: name and url"}, status=400)

            try:
                payload = build_company_research(name=name, company_url=company_url)
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": f"Research failed: {exc}"}, status=502)

            return json_response(self, payload)

        return super().do_GET()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serve corp dev dashboard locally.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Starting port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    server = None
    chosen_port = args.port
    for offset in range(MAX_PORT_ATTEMPTS):
        port = args.port + offset
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            chosen_port = port
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    if server is None:
        raise OSError(
            f"Could not find an open port in range {args.port}-{args.port + MAX_PORT_ATTEMPTS - 1}."
        )

    print(f"Serving corp dev dashboard at http://localhost:{chosen_port}/static/index.html")
    server.serve_forever()
