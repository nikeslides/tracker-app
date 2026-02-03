#!/usr/bin/env python3
"""
Download all tracks that can be played in the player.
Uses the same filtering and download logic as the player.
Beware this script will take a lot of time to run.
"""

import hashlib
import json
import os
import re
import ssl
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

import config

# Configuration - uses shared config
DATA_DIR = Path(config.output_path())
AUDIO_DIR = DATA_DIR / "audio"  # pillows.su files
AUDIO_YETRACKER_DIR = DATA_DIR / "audio_yetracker"  # files.yetracker.org files (separate)
AUDIO_PIXELDRAIN_DIR = DATA_DIR / "audio_pixeldrain"  # pixeldrain.com files (separate)
JSON_PATH = DATA_DIR / "sheet.json"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_YETRACKER_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_PIXELDRAIN_DIR.mkdir(parents=True, exist_ok=True)


def extract_pillows_hash(link: str) -> Optional[str]:
    """Extract hash from pillows.su link."""
    if not link:
        return None
    match = re.search(r"pillows\.su/f/([a-f0-9]+)", link, re.IGNORECASE)
    return match.group(1) if match else None


def extract_yetracker_id(link: str) -> Optional[str]:
    """Extract file ID from files.yetracker.org link (e.g. f/5YWrQGTB)."""
    if not link:
        return None
    match = re.search(r"files\.yetracker\.org/f/([a-zA-Z0-9]+)", link, re.IGNORECASE)
    return match.group(1) if match else None


def extract_pixeldrain_id(link: str) -> Optional[str]:
    """Extract file ID from pixeldrain.com link (e.g. u/4Hjx3akP)."""
    if not link:
        return None
    match = re.search(r"pixeldrain\.com/u/([a-zA-Z0-9]+)", link, re.IGNORECASE)
    return match.group(1) if match else None


def generate_track_id(track: Dict) -> str:
    """Generate a unique ID for a track based on its content."""
    # Use row number + era + name hash for uniqueness
    row = track.get("_row", 0)
    era = track.get("Era", "")
    name = track.get("Name", "")
    key = f"{row}:{era}:{name}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def load_tracks() -> List[Dict]:
    """Load and process tracks from JSON, using the same filtering as the player."""
    if not JSON_PATH.exists():
        raise FileNotFoundError(f"JSON file not found: {JSON_PATH}. Run `python main.py` first.")

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    tracks = data.get("tracks", [])
    processed = []

    for track in tracks:
        link = track.get("Link", "").strip()
        quality = track.get("Quality", "").strip()
        if quality == "Not Available":
            continue

        # Pillows.su links (unchanged — matches player logic)
        if "pillows.su" in link.lower():
            hash_val = extract_pillows_hash(link)
            if hash_val:
                track_id = generate_track_id(track)
                processed.append({
                    "id": track_id,
                    "name": track.get("Name", "").strip(),
                    "era": track.get("Era", "").strip(),
                    "notes": track.get("Notes", "").strip(),
                    "quality": quality,
                    "available_length": track.get("Available Length", "").strip(),
                    "track_length": track.get("Track Length", "").strip(),
                    "hash": hash_val,
                    "host": "pillows",
                    "original_link": link,
                })
        # files.yetracker.org links (separate folder)
        elif "files.yetracker.org" in link.lower():
            file_id = extract_yetracker_id(link)
            if file_id:
                track_id = generate_track_id(track)
                processed.append({
                    "id": track_id,
                    "name": track.get("Name", "").strip(),
                    "era": track.get("Era", "").strip(),
                    "notes": track.get("Notes", "").strip(),
                    "quality": quality,
                    "available_length": track.get("Available Length", "").strip(),
                    "track_length": track.get("Track Length", "").strip(),
                    "hash": file_id,
                    "host": "yetracker",
                    "original_link": link,
                })
        # pixeldrain.com links (separate folder)
        elif "pixeldrain.com" in link.lower():
            file_id = extract_pixeldrain_id(link)
            if file_id:
                track_id = generate_track_id(track)
                processed.append({
                    "id": track_id,
                    "name": track.get("Name", "").strip(),
                    "era": track.get("Era", "").strip(),
                    "notes": track.get("Notes", "").strip(),
                    "quality": quality,
                    "available_length": track.get("Available Length", "").strip(),
                    "track_length": track.get("Track Length", "").strip(),
                    "hash": file_id,
                    "host": "pixeldrain",
                    "original_link": link,
                })

    return processed


def get_audio_path(track: Dict) -> Optional[Path]:
    """Get local path for a track's audio file."""
    file_id = track["hash"]
    host = track.get("host", "pillows")
    base_dir = AUDIO_YETRACKER_DIR if host == "yetracker" else (AUDIO_PIXELDRAIN_DIR if host == "pixeldrain" else AUDIO_DIR)
    id_dir = base_dir / file_id

    # Supported audio/video file extensions
    audio_extensions = [".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aif", ".aiff", ".mp4", ".mov", ".webm", ".mkv"]

    if id_dir.exists() and id_dir.is_dir():
        for file_path in id_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in audio_extensions:
                return file_path

    # Fallback: flat structure (pillows only)
    if host == "pillows":
        for ext in audio_extensions:
            path = base_dir / f"{file_id}{ext}"
            if path.exists():
                return path

    return None


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for filesystem safety."""
    # Remove or replace invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Remove leading/trailing dots and spaces
    filename = filename.strip('. ')
    # Limit length
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:200-len(ext)] + ext
    return filename or "audio_file"


def download_track(track: Dict) -> Optional[Path]:
    """Download a track from pillows.su, files.yetracker.org, or pixeldrain.com, preserving original filename."""
    file_id = track["hash"]
    host = track.get("host", "pillows")
    base_dir = AUDIO_YETRACKER_DIR if host == "yetracker" else (AUDIO_PIXELDRAIN_DIR if host == "pixeldrain" else AUDIO_DIR)
    id_dir = base_dir / file_id

    existing_path = get_audio_path(track)
    if existing_path and existing_path.exists():
        return existing_path

    if host == "yetracker":
        download_url = f"https://files.yetracker.org/d/{file_id}"
    elif host == "pixeldrain":
        download_url = f"https://pixeldrain.com/api/file/{file_id}"
    else:
        download_url = f"https://api.pillows.su/api/download/{file_id}"
    
    # Create SSL context
    context = ssl._create_unverified_context()
    
    try:
        req = urllib.request.Request(download_url)
        req.add_header("User-Agent", "Mozilla/5.0")
        
        with urllib.request.urlopen(req, context=context, timeout=30) as response:
            # Get original filename from Content-Disposition header
            original_filename = None
            content_disp = response.headers.get("Content-Disposition", "")
            
            if content_disp:
                # Try RFC 5987 format first: filename*=UTF-8''encoded-name
                filename_star_match = re.search(r"filename\*=UTF-8''([^;]+)", content_disp, re.IGNORECASE)
                if filename_star_match:
                    original_filename = urllib.parse.unquote(filename_star_match.group(1).strip())
                else:
                    # Try standard format: filename="name" (quoted)
                    filename_match = re.search(r'filename\s*=\s*"([^"]+)"', content_disp, re.IGNORECASE)
                    if filename_match:
                        original_filename = filename_match.group(1)
                    else:
                        # Try single-quoted: filename='name'
                        filename_match = re.search(r"filename\s*=\s*'([^']+)'", content_disp, re.IGNORECASE)
                        if filename_match:
                            original_filename = filename_match.group(1)
                        else:
                            # Try unquoted filename (everything after = until semicolon or end)
                            filename_match = re.search(r'filename\s*=\s*([^;]+)', content_disp, re.IGNORECASE)
                            if filename_match:
                                original_filename = filename_match.group(1).strip()
                    
                    if original_filename:
                        # Handle URL encoding
                        try:
                            original_filename = urllib.parse.unquote(original_filename)
                        except:
                            pass
            
            # If no filename in header, try Content-Type to determine extension
            if not original_filename:
                content_type = response.headers.get("Content-Type", "")
                ext = ".mp3"  # default
                if "audio/mpeg" in content_type or "audio/mp3" in content_type:
                    ext = ".mp3"
                elif "audio/mp4" in content_type or "audio/m4a" in content_type:
                    ext = ".m4a"
                elif "audio/flac" in content_type:
                    ext = ".flac"
                elif "audio/wav" in content_type:
                    ext = ".wav"
                elif "audio/ogg" in content_type:
                    ext = ".ogg"
                original_filename = f"audio{ext}"
            
            # Sanitize filename
            original_filename = sanitize_filename(original_filename)
            
            # Debug output
            if not original_filename or len(original_filename) < 3:
                print(f"Warning: Extracted filename seems incorrect: '{original_filename}'")
                print(f"Content-Disposition header: {content_disp}")
            
            id_dir.mkdir(parents=True, exist_ok=True)
            audio_path = id_dir / original_filename
            if audio_path.exists():
                return audio_path

            with open(audio_path, "wb") as f:
                f.write(response.read())

            return audio_path
    except Exception as e:
        raise Exception(f"Error downloading track: {e}")


def process_track(track: Dict, idx: int, total: int) -> Tuple[str, str, Optional[Path]]:
    """Process a single track: check if exists, download if needed.
    
    Returns: (status, message, audio_path)
    Status can be: 'skipped', 'downloaded', 'failed'
    """
    track_name = track["name"]
    hash_val = track["hash"]
    
    # Check if already exists
    existing_path = get_audio_path(track)
    if existing_path and existing_path.exists():
        return ('skipped', f"[{idx}/{total}] ✓ Already exists: {track_name} (hash: {hash_val[:8]}...)", existing_path)
    
    # Download
    try:
        audio_path = download_track(track)
        if audio_path and audio_path.exists():
            return ('downloaded', f"[{idx}/{total}] ✓ Downloaded: {track_name} (hash: {hash_val[:8]}...)", audio_path)
        else:
            return ('failed', f"[{idx}/{total}] ✗ Download failed: file not found after download - {track_name}", None)
    except Exception as e:
        return ('failed', f"[{idx}/{total}] ✗ Error: {e} - {track_name}", None)


def main():
    """Download all playable tracks with concurrent downloads."""
    print("Loading tracks from sheet.json...")
    try:
        tracks = load_tracks()
        print(f"Found {len(tracks)} playable tracks")
    except Exception as e:
        print(f"Error loading tracks: {e}")
        return
    
    if len(tracks) == 0:
        print("No tracks to download.")
        return
    
    print(f"Audio directory: {AUDIO_DIR}")
    print(f"Downloading up to 4 files concurrently...")
    print()
    
    # Track statistics (thread-safe with lock)
    stats_lock = Lock()
    downloaded = 0
    skipped = 0
    failed = 0
    
    # Filter tracks that need downloading (check existence first)
    tracks_to_download = []
    for idx, track in enumerate(tracks, 1):
        existing_path = get_audio_path(track)
        if existing_path and existing_path.exists():
            # Print skipped tracks immediately (not in thread pool)
            track_name = track["name"]
            hash_val = track["hash"]
            print(f"[{idx}/{len(tracks)}] ✓ Already exists: {track_name} (hash: {hash_val[:8]}...)")
            skipped += 1
        else:
            tracks_to_download.append((idx, track))
    
    if not tracks_to_download:
        print("\nAll tracks already downloaded!")
    else:
        # Download remaining tracks concurrently
        with ThreadPoolExecutor(max_workers=4) as executor:
            # Submit all download tasks
            future_to_track = {
                executor.submit(process_track, track, idx, len(tracks)): (idx, track)
                for idx, track in tracks_to_download
            }
            
            # Process completed downloads as they finish
            for future in as_completed(future_to_track):
                idx, track = future_to_track[future]
                try:
                    status, message, audio_path = future.result()
                    print(message)
                    if audio_path:
                        print(f"  → Saved to: {audio_path}")
                    
                    # Update statistics (thread-safe)
                    with stats_lock:
                        if status == 'downloaded':
                            downloaded += 1
                        elif status == 'skipped':
                            skipped += 1
                        elif status == 'failed':
                            failed += 1
                except Exception as e:
                    print(f"[{idx}/{len(tracks)}] ✗ Unexpected error: {e} - {track['name']}")
                    with stats_lock:
                        failed += 1
    
    # Summary
    print()
    print("=" * 60)
    print("Download Summary:")
    print(f"  Total tracks: {len(tracks)}")
    print(f"  Downloaded: {downloaded}")
    print(f"  Already existed: {skipped}")
    print(f"  Failed: {failed}")
    print("=" * 60)


if __name__ == "__main__":
    main()

