import argparse
import csv
import json
import os
import re
import ssl
import urllib.request
from collections import Counter, defaultdict
from io import BytesIO
from typing import Dict, List

import config

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

def extract_sheet_id(link: str) -> str:
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", link)
    if not match:
        raise ValueError("Could not find sheet ID in link")
    return match.group(1)


def build_export_url(link: str, file_format: str = "csv") -> str:
    sheet_id = extract_sheet_id(link)
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format={file_format}"


def download_sheet(link: str, dest_dir: str) -> str:
    export_url = build_export_url(link, "csv")
    # Some environments lack system certificates; fall back to an unverified context.
    context = ssl._create_unverified_context()

    def _write(to_dir: str) -> str:
        os.makedirs(to_dir, exist_ok=True)
        dest_file = os.path.join(to_dir, "sheet.csv")
        with urllib.request.urlopen(export_url, context=context) as response, open(dest_file, "wb") as handle:
            handle.write(response.read())
        return dest_file

    try:
        return _write(dest_dir)
    except PermissionError:
        # If the target volume is blocked, fall back to a writable local folder.
        fallback_dir = os.path.join(os.getcwd(), "data")
        print(f"Permission denied writing to {dest_dir}; saving instead to {fallback_dir}")
        return _write(fallback_dir)


def clean_header(header: str) -> str:
    # Collapse newlines, drop trailing parentheticals, and compress whitespace.
    header = header.replace("\n", " ").strip()
    header = re.sub(r"\s*\(.*?\)$", "", header).strip()
    header = re.sub(r"\s+", " ", header)
    return header or "column"


def parse_summary_counts(cell: str) -> Dict[str, int]:
    counts = {}
    for line in cell.splitlines():
        line = line.strip()
        m = re.match(r"(\d+)\s+(.+)", line)
        if m:
            counts[m.group(2)] = int(m.group(1))
    return counts


def load_csv_cleaned(path: str):
    with open(path, newline="") as file:
        reader = csv.reader(file)
        try:
            raw_headers = next(reader)
        except StopIteration:
            return [], []

        headers = [clean_header(h) for h in raw_headers]
        rows = []
        for raw_row in reader:
            row = {}
            for idx, header in enumerate(headers):
                value = raw_row[idx] if idx < len(raw_row) else ""
                row[header] = value.strip()
            rows.append(row)
    return headers, rows


def split_sections(headers: List[str], raw_rows: List[dict]):
    """Split the sheet into era sections and track rows.

    The sheet uses rows where the Era cell contains counts like "13 OG File(s)"
    to denote a new era. Those rows act as section headers and should not be
    treated as tracks.
    """
    sections: List[dict] = []
    tracks: List[dict] = []
    current_era = "Unknown Era"

    for idx, row in enumerate(raw_rows, start=2):  # account for header row
        era_cell = (row.get("Era") or "").strip()
        name_cell = (row.get("Name") or "").strip()
        notes_cell = (row.get("Notes") or "").strip()

        if "OG File(s)" in era_cell:
            era_name = (name_cell.splitlines()[0] if name_cell else current_era).strip() or current_era
            sections.append(
                {
                    "era": era_name,
                    "counts": parse_summary_counts(era_cell),
                    "description": notes_cell,
                    "row": idx,
                }
            )
            current_era = era_name
            continue

        # For normal tracks, inherit the latest era header.
        row = dict(row)
        row["Era"] = row.get("Era") or current_era
        row["_row"] = idx
        tracks.append(row)

    # Attach track counts to sections for quick display.
    era_track_counts = Counter(t["Era"] for t in tracks)
    for section in sections:
        section["track_count"] = era_track_counts.get(section["era"], 0)

    # If there were tracks before the first header, add a synthetic section.
    if current_era == "Unknown Era" or era_track_counts.get("Unknown Era"):
        sections.insert(
            0,
            {
                "era": current_era,
                "counts": {},
                "description": "",
                "row": 2,
                "track_count": era_track_counts.get(current_era, 0),
            },
        )

    return sections, tracks


def analyze_rows(headers, rows, sample_rows: int = 5):
    row_count = len(rows)
    samples = rows[:sample_rows]
    stats = defaultdict(lambda: {"non_empty": 0, "numeric_like": 0, "examples": []})

    for row in rows:
        for header in headers:
            value = (row.get(header) or "").strip()
            if not value:
                continue

            column_stats = stats[header]
            column_stats["non_empty"] += 1
            if len(column_stats["examples"]) < 3 and value not in column_stats["examples"]:
                column_stats["examples"].append(value)

            try:
                float(value.replace(",", ""))
            except ValueError:
                continue
            else:
                column_stats["numeric_like"] += 1

    return {
        "headers": headers,
        "row_count": row_count,
        "stats": stats,
        "samples": samples,
    }


def save_json(payload, dest_dir: str, filename: str = "sheet.json") -> str:
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, filename)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def top_counts(rows, field: str, top_n: int = 15):
    counter = Counter()
    for row in rows:
        value = (row.get(field) or "").strip()
        if value:
            counter[value] += 1
    return counter.most_common(top_n)


def _era_slug(era: str) -> str:
    """Slugify era name for artwork filenames (must match player._era_slug)."""
    s = re.sub(r"[^\w\s-]", "", (era or "").strip()).lower()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:80] or "unknown"


def download_section_artwork(data_dir: str, era: str, url: str) -> bool:
    """
    Download section artwork from URL to data_dir/artwork/_era_{slug}.jpg.
    Returns True if saved, False on failure. No URL is stored in JSON.
    """
    if not url or not url.startswith("http"):
        return False
    slug = _era_slug(era)
    artwork_dir = os.path.join(data_dir, "artwork")
    os.makedirs(artwork_dir, exist_ok=True)
    path = os.path.join(artwork_dir, f"_era_{slug}.jpg")
    if not HAS_PIL:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Yzyfi-Sheet/1.0"})
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = resp.read()
        img = Image.open(BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((300, 300), Image.Resampling.LANCZOS)
        img.save(path, "JPEG", quality=85)
        return True
    except Exception:
        return False


def _fetch_html(url: str, timeout: int = 30) -> str:
    """Fetch URL and return response body as string."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"},
    )
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean_image_url(url: str) -> str:
    """Remove size params (e.g. =w102-h101) from Google image URL to get full size."""
    if not url:
        return url
    return re.sub(r"=w\d+-h\d+", "", url)


def scrape_section_artwork_from_html(sheet_link: str, sheet_gid: str = "0") -> Dict[str, str]:
    """
    Scrape section row images from the sheet's htmlview/sheet HTML (unpublished).
    URL format: .../spreadsheets/u/0/d/{sheet_id}/htmlview/sheet?headers=true&gid={gid}
    Row structure: th (row #), td Era (OG File(s)), td Name (era title), td Notes, td Image, td Description.
    Removes =w102-h101 from image URLs to request full size.
    """
    if not HAS_BS4:
        return {}
    sheet_id = extract_sheet_id(sheet_link)
    # Extract gid from link if present: ...&gid=199908479
    gid_match = re.search(r"[?&]gid=(\d+)", sheet_link, re.IGNORECASE)
    gid = (gid_match.group(1) if gid_match else sheet_gid or "0").strip()
    url = (
        f"https://docs.google.com/spreadsheets/u/0/d/{sheet_id}/htmlview/sheet"
        f"?headers=true&gid={gid}"
    )
    try:
        html = _fetch_html(url)
    except Exception as e:
        print(f"Error fetching HTML: {e}")
        return {}
    if not html or "OG File(s)" not in html:
        print("No HTML found")
        return {}
    print(f"HTML length: {len(html)}")
    soup = BeautifulSoup(html, "html.parser")
    era_to_url: Dict[str, str] = {}

    def extract_era_from_text(s: str) -> str:
        first_line = (s or "").split("\n")[0].strip()
        return first_line.split("(")[0].strip() or first_line

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 5:
                continue
            text = " ".join(c.get_text(separator=" ", strip=True) for c in cells)
            if "OG File(s)" not in text:
                continue
            # cells[0]=th, [1]=Era, [2]=Name (era title), [3]=Notes, [4]=Image, [5]=Description
            era_name = extract_era_from_text(cells[2].get_text(separator=" ", strip=True))
            if not era_name:
                continue
            img = cells[4].find("img") if len(cells) > 4 else None
            if not img:
                img = tr.find("img")
            if img and img.get("src"):
                src = _clean_image_url((img.get("src") or "").strip())
                if src.startswith("http") and era_name:
                    era_to_url[era_name] = src

    # Fallback: any tag containing "OG File(s)", walk up to tr, then era from cells[2], img from row
    if not era_to_url:
        for tag in soup.find_all(True):
            if "OG File(s)" not in (tag.get_text() or ""):
                continue
            tr = tag
            for _ in range(25):
                if tr is None:
                    break
                if getattr(tr, "name", None) == "tr":
                    cells = tr.find_all(["td", "th"])
                    if len(cells) >= 3:
                        era_name = extract_era_from_text(cells[2].get_text(separator=" ", strip=True))
                        img = tr.find("img")
                        if era_name and img and img.get("src"):
                            src = _clean_image_url((img.get("src") or "").strip())
                            if src.startswith("http"):
                                era_to_url[era_name] = src
                    break
                tr = getattr(tr, "parent", None)
    return era_to_url


def main():
    parser = argparse.ArgumentParser(description="Process Tracker spreadsheet")
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading the sheet (use existing CSV)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed column statistics",
    )
    args = parser.parse_args()

    data_dir = config.output_path()
    csv_path = os.environ.get("SHEET_CSV_PATH") or os.path.join(data_dir, "sheet.csv")

    if not args.skip_download:
        print("Downloading sheet...")
        csv_path = download_sheet(config.sheet_link(), data_dir)
        print(f"Downloaded sheet to {csv_path}")
    elif not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"CSV not found at {csv_path}. Remove --skip-download to fetch it."
        )

    headers, raw_rows = load_csv_cleaned(csv_path)
    sections, tracks = split_sections(headers, raw_rows)
    summary = analyze_rows(headers, tracks)

    # Section artwork: scrape from HTML and download into data/artwork/
    data_dir = os.path.dirname(csv_path)
    artwork_map: Dict[str, str] = {}
    try:
        artwork_map = scrape_section_artwork_from_html(config.sheet_link(), config.sheet_gid())
        if artwork_map and args.verbose:
            print(f"Scraped {len(artwork_map)} section artwork URL(s) from HTML")
    except Exception as e:
        if args.verbose:
            print(f"HTML scrape for section artwork failed: {e}")
    # Download section artwork into data/artwork/
    if artwork_map:
        for era, url in artwork_map.items():
            if era and url and url.startswith("http"):
                if download_section_artwork(data_dir, era, url) and args.verbose:
                    print(f"  Downloaded section artwork: {era}")

    json_payload = {"sections": sections, "tracks": tracks}
    json_path = save_json(json_payload, data_dir)

    print(f"Loaded sheet from {csv_path}")
    print(f"Columns: {len(summary['headers'])} | Tracks: {len(tracks)} | Sections: {len(sections)}")

    if args.verbose:
        print("\nColumn fill rates and examples:")
        for header in summary["headers"]:
            col = summary["stats"][header]
            non_empty = col["non_empty"]
            numeric_like = col["numeric_like"]
            examples = ", ".join(col["examples"])
            print(
                f"- {header}: {non_empty}/{summary['row_count']} filled; "
                f"{numeric_like} numeric-like; examples: {examples}"
            )

        print("\nSample rows:")
        for row in summary["samples"]:
            print(row)

    print(f"Saved JSON to {json_path}")


if __name__ == "__main__":
    main()

