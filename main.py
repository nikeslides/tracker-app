import argparse
import csv
import json
import os
import re
import ssl
import urllib.request
from collections import Counter, defaultdict
from typing import Dict, List

import config

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

    json_payload = {"sections": sections, "tracks": tracks}
    json_path = save_json(json_payload, os.path.dirname(csv_path))

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

