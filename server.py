#!/usr/bin/env python3
"""Local server for corp dev pipeline dashboard."""

from __future__ import annotations

import argparse
import base64
import errno
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
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
XLSX_MAX_BYTES = 8_000_000


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


def parse_xlsx_talent_rows(xlsx_bytes: bytes) -> list[dict]:
    """Parse key talent fields from an XLSX census-style workbook."""
    if len(xlsx_bytes) > XLSX_MAX_BYTES:
        raise ValueError("Excel file is too large.")

    ns_main = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    ns_rel = {"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    rel_type_sheet = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"

    def col_from_ref(cell_ref: str) -> str:
        return "".join(ch for ch in cell_ref if ch.isalpha())

    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    def clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "")).strip()

    def safe_value(cells: dict, key: str) -> str:
        return clean_text(cells.get(key, ""))

    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
        names = set(zf.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("Invalid XLSX: missing workbook.xml")

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("a:si", ns_main):
                text = "".join((t.text or "") for t in si.findall(".//a:t", ns_main))
                shared_strings.append(text)

        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        wb_rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map: dict[str, tuple[str, str]] = {}
        for rel in wb_rels:
            rel_map[rel.attrib.get("Id", "")] = (rel.attrib.get("Target", ""), rel.attrib.get("Type", ""))

        first_sheet_target = ""
        for sheet in wb.findall("a:sheets/a:sheet", {**ns_main, **ns_rel}):
            rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            target, rel_type = rel_map.get(rel_id, ("", ""))
            if rel_type == rel_type_sheet or target:
                first_sheet_target = target
                break
        if not first_sheet_target:
            raise ValueError("Invalid XLSX: no worksheet found")

        if not first_sheet_target.startswith("xl/"):
            sheet_path = f"xl/{first_sheet_target.lstrip('/')}"
        else:
            sheet_path = first_sheet_target
        sheet_path = str(Path(sheet_path))
        if sheet_path not in names:
            raise ValueError("Invalid XLSX: worksheet file missing")

        sheet_rels_path = ""
        if "/" in sheet_path:
            parent, filename = sheet_path.rsplit("/", 1)
            sheet_rels_path = f"{parent}/_rels/{filename}.rels"
        hyperlink_targets: dict[str, str] = {}
        if sheet_rels_path in names:
            rels_root = ET.fromstring(zf.read(sheet_rels_path))
            for rel in rels_root:
                rid = rel.attrib.get("Id", "")
                target = rel.attrib.get("Target", "")
                if rid and target:
                    hyperlink_targets[rid] = target

        sheet_root = ET.fromstring(zf.read(sheet_path))
        cell_hyperlink_map: dict[str, str] = {}
        for hl in sheet_root.findall(".//a:hyperlinks/a:hyperlink", {**ns_main, **ns_rel}):
            cell_ref = hl.attrib.get("ref", "")
            if not cell_ref:
                continue
            rid = hl.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            location = hl.attrib.get("location", "")
            target = hyperlink_targets.get(rid, "") if rid else ""
            if not target and location:
                target = location
            if target:
                cell_hyperlink_map[cell_ref] = target

        row_cells: list[dict[str, str]] = []
        row_links: list[dict[str, str]] = []
        for row in sheet_root.findall(".//a:sheetData/a:row", ns_main):
            cells: dict[str, str] = {}
            links: dict[str, str] = {}
            for cell in row.findall("a:c", ns_main):
                cell_ref = cell.attrib.get("r", "")
                col = col_from_ref(cell_ref)
                if not col:
                    continue

                value = ""
                cell_type = cell.attrib.get("t", "")
                if cell_type == "inlineStr":
                    value = "".join((t.text or "") for t in cell.findall(".//a:t", ns_main))
                else:
                    v = cell.find("a:v", ns_main)
                    if v is not None and v.text is not None:
                        value = v.text
                if cell_type == "s" and value.isdigit():
                    idx = int(value)
                    if 0 <= idx < len(shared_strings):
                        value = shared_strings[idx]
                value = clean_text(unescape(value))
                cells[col] = value
                if cell_ref in cell_hyperlink_map:
                    links[col] = cell_hyperlink_map[cell_ref]

            if any(v for v in cells.values()):
                row_cells.append(cells)
                row_links.append(links)

        if not row_cells:
            return []

        expected_headers = {
            "first_name": {"preferredfirstname", "legalfirstname", "firstname"},
            "last_name": {"preferredlastname", "legallastname", "lastname"},
            "title": {"currentbusinesstitle", "title", "currenttitle"},
            "location_city": {"currentworklocationcity", "city", "locationcity"},
            "location_state": {"currentworklocationstate", "state", "locationstate"},
            "location_country": {"currentworklocationcountry", "country", "locationcountry"},
            "linkedin": {"linkedinprofilelinkifavailable", "linkedinprofilelink", "linkedin", "linkedinurl"},
        }

        header_idx = -1
        header_map: dict[str, str] = {}
        for i, cells in enumerate(row_cells):
            normalized = {norm(v): col for col, v in cells.items() if v}
            candidate_map: dict[str, str] = {}
            for key, aliases in expected_headers.items():
                col = next((normalized[a] for a in aliases if a in normalized), "")
                if col:
                    candidate_map[key] = col
            if {"first_name", "last_name", "title"}.issubset(set(candidate_map.keys())):
                header_idx = i
                header_map = candidate_map
                break

        if header_idx < 0:
            return []

        out: list[dict] = []
        for i in range(header_idx + 1, len(row_cells)):
            cells = row_cells[i]
            links = row_links[i] if i < len(row_links) else {}
            first = safe_value(cells, header_map.get("first_name", ""))
            last = safe_value(cells, header_map.get("last_name", ""))
            name = clean_text(f"{first} {last}")
            title = safe_value(cells, header_map.get("title", ""))

            city = safe_value(cells, header_map.get("location_city", ""))
            state = safe_value(cells, header_map.get("location_state", ""))
            country = safe_value(cells, header_map.get("location_country", ""))
            location_parts = [part for part in (city, state, country) if part]
            location = ", ".join(location_parts)

            linkedin_col = header_map.get("linkedin", "")
            linkedin_raw = safe_value(cells, linkedin_col)
            linkedin_href = clean_text(links.get(linkedin_col, ""))
            linkedin = linkedin_href or linkedin_raw
            if linkedin and linkedin.lower() == "link" and not linkedin_href:
                linkedin = ""
            if linkedin and not re.match(r"^https?://", linkedin, re.IGNORECASE):
                if "linkedin.com" in linkedin.lower():
                    linkedin = f"https://{linkedin.lstrip('/')}"

            # Skip obvious instructional/template rows.
            if not name and not title:
                continue
            if norm(name) in {"janedoe", "doejane"} and norm(title) == norm("Director, Product Management"):
                continue
            if any(token in norm(name) for token in ("idnumber", "username")):
                continue

            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "title": title,
                    "location": location,
                    "linkedin": linkedin,
                }
            )

        return out


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

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/parse-talent-sheet":
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            if content_length <= 0 or content_length > XLSX_MAX_BYTES * 2:
                return json_response(self, {"error": "Invalid request size."}, status=400)

            raw = self.rfile.read(content_length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return json_response(self, {"error": "Invalid JSON body."}, status=400)

            filename = str(payload.get("filename", "")).strip()
            data_b64 = str(payload.get("data", "")).strip()
            if not data_b64:
                return json_response(self, {"error": "Missing file data."}, status=400)
            if filename and not filename.lower().endswith(".xlsx"):
                return json_response(
                    self,
                    {"error": "Unsupported Excel format. Please upload .xlsx (not legacy .xls)."},
                    status=400,
                )

            try:
                file_bytes = base64.b64decode(data_b64, validate=True)
                rows = parse_xlsx_talent_rows(file_bytes)
            except (ValueError, zipfile.BadZipFile) as exc:
                return json_response(self, {"error": f"Excel parse failed: {exc}"}, status=400)
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": f"Excel parse failed: {exc}"}, status=502)

            return json_response(
                self,
                {
                    "rows": rows,
                    "count": len(rows),
                },
            )

        return json_response(self, {"error": "Not found"}, status=404)


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
