#!/usr/bin/env python3
"""
Music player server for the Tracker sheet data.
Serves a web interface to play tracks from pillows.su links.
"""

import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional
from flask import Flask, jsonify, render_template, send_file, Response, request, session, redirect, url_for
from werkzeug.serving import WSGIRequestHandler

import config
import main as sheet_processor

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3NoHeaderError
    from PIL import Image
    HAS_ARTWORK_SUPPORT = True
except ImportError:
    HAS_ARTWORK_SUPPORT = False

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "super-secret-ye-key"


def is_authenticated():
    app_username = os.environ.get("APP_USERNAME")
    app_password = os.environ.get("APP_PASSWORD")

    # If no credentials are set, allow all
    if not app_username or not app_password:
        return True

    return session.get("authenticated") is True


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function


# Configuration
DATA_DIR = Path(config.output_path())
AUDIO_DIR = DATA_DIR / "audio"
ARTWORK_DIR = DATA_DIR / "artwork"
JSON_PATH = DATA_DIR / "sheet.json"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
ARTWORK_DIR.mkdir(parents=True, exist_ok=True)

# In-memory track cache
_tracks_cache: Optional[List[Dict]] = None
_track_index: Optional[Dict[str, Dict]] = None
_cache_lock = threading.Lock()
_last_refresh: Optional[datetime] = None


def extract_pillows_hash(link: str) -> Optional[str]:
    """Extract hash from pillows.su link."""
    if not link:
        return None
    match = re.search(r"pillows\.su/f/([a-f0-9]+)", link, re.IGNORECASE)
    return match.group(1) if match else None


def generate_track_id(track: Dict) -> str:
    """Generate a unique ID for a track based on its content."""
    # Use row number + era + name hash for uniqueness
    row = track.get("_row", 0)
    era = track.get("Era", "")
    name = track.get("Name", "")
    key = f"{row}:{era}:{name}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def refresh_sheet() -> bool:
    """Download and process the sheet, then clear the cache."""
    global _tracks_cache, _track_index, _last_refresh
    
    try:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Refreshing sheet data...")
        
        # Download and process the sheet
        data_dir = config.output_path()
        csv_path = sheet_processor.download_sheet(config.sheet_link(), data_dir)
        
        headers, raw_rows = sheet_processor.load_csv_cleaned(csv_path)
        sections, tracks = sheet_processor.split_sections(headers, raw_rows)
        
        json_payload = {"sections": sections, "tracks": tracks}
        sheet_processor.save_json(json_payload, data_dir)
        
        # Clear caches so they reload on next access
        with _cache_lock:
            _tracks_cache = None
            _track_index = None
            _last_refresh = datetime.now()
        
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sheet refreshed: {len(tracks)} tracks")
        return True
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error refreshing sheet: {e}")
        return False


def _background_refresh():
    """Background thread that refreshes the sheet periodically."""
    while True:
        time.sleep(config.refresh_interval())
        refresh_sheet()


def load_tracks() -> List[Dict]:
    """Load and process tracks from JSON."""
    global _tracks_cache
    
    with _cache_lock:
        if _tracks_cache is not None:
            return _tracks_cache

    if not JSON_PATH.exists():
        raise FileNotFoundError(f"JSON file not found: {JSON_PATH}. Run `python main.py` first.")

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    tracks = data.get("tracks", [])
    processed = []

    for track in tracks:
        link = track.get("Link", "").strip()
        quality = track.get("Quality", "").strip()
        
        # Only include tracks with pillows.su links and quality != "Not Available"
        if "pillows.su" in link.lower() and quality != "Not Available":
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
                    "original_link": link,
                })

    with _cache_lock:
        _tracks_cache = processed
    return processed


def get_track_index() -> Dict[str, Dict]:
    """Get index of tracks by ID."""
    global _track_index
    
    with _cache_lock:
        if _track_index is not None:
            return _track_index

    tracks = load_tracks()
    
    with _cache_lock:
        _track_index = {t["id"]: t for t in tracks}
    return _track_index


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


def get_audio_path(track_id: str) -> Path:
    """Get local path for a track's audio file."""
    track = get_track_index().get(track_id)
    if not track:
        return None
    
    hash_val = track["hash"]
    hash_dir = AUDIO_DIR / hash_val
    
    # If hash directory exists, look for any audio file in it
    if hash_dir.exists() and hash_dir.is_dir():
        for file_path in hash_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in [".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus"]:
                return file_path
    
    # Fallback: check old format (flat structure with hash)
    for ext in [".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus"]:
        path = AUDIO_DIR / f"{hash_val}{ext}"
        if path.exists():
            return path
    
    return None


def download_track(track_id: str) -> Optional[Path]:
    """Download a track from pillows.su API, preserving original filename."""
    track = get_track_index().get(track_id)
    if not track:
        return None

    hash_val = track["hash"]
    hash_dir = AUDIO_DIR / hash_val
    
    # If file already exists for this hash, reuse it
    existing_path = get_audio_path(track_id)
    if existing_path and existing_path.exists():
        return existing_path
    
    download_url = f"https://api.pillows.su/api/download/{hash_val}"
    
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
            
            # Create hash directory to organize files
            hash_dir.mkdir(parents=True, exist_ok=True)
            
            # If file with same name exists, add track_id to make it unique
            audio_path = hash_dir / original_filename
            if audio_path.exists():
                # Same hash, same filename - it's the same file, reuse it
                return audio_path
            
            # Download file
            with open(audio_path, "wb") as f:
                f.write(response.read())
            
            print(f"Downloaded: {original_filename} (hash: {hash_val})")
            return audio_path
    except Exception as e:
        print(f"Error downloading track {track_id}: {e}")
        return None


def extract_artists(name: str) -> str:
    """Extract artist/feature info from track name."""
    # Look for patterns like "(with X)", "(feat. Y)", "(prod. Z)"
    parts = []
    
    # Extract "with" collaborators
    with_match = re.search(r"\(with\s+([^)]+)\)", name, re.IGNORECASE)
    if with_match:
        parts.append(f"with {with_match.group(1)}")
    
    # Extract features
    feat_match = re.search(r"\(feat\.\s+([^)]+)\)", name, re.IGNORECASE)
    if feat_match:
        parts.append(f"feat. {feat_match.group(1)}")
    
    # Extract producers
    prod_match = re.search(r"\(prod\.\s+([^)]+)\)", name, re.IGNORECASE)
    if prod_match:
        parts.append(f"prod. {prod_match.group(1)}")
    
    return " • ".join(parts) if parts else ""


def extract_album_art(audio_path: Path) -> Optional[Path]:
    """Extract album art from audio file and save it."""
    if not HAS_ARTWORK_SUPPORT or not audio_path or not audio_path.exists():
        return None
    
    try:
        audio_file = MutagenFile(str(audio_path))
        if not audio_file:
            return None
        
        # Try to get artwork
        artwork_data = None
        
        # For MP3 files
        if audio_path.suffix.lower() == ".mp3":
            try:
                if hasattr(audio_file, 'tags') and audio_file.tags:
                    # Look for APIC (album art) frames
                    for key in audio_file.tags.keys():
                        if key.startswith('APIC') or key == 'APIC:':
                            artwork_data = audio_file.tags[key].data
                            break
            except (ID3NoHeaderError, AttributeError, KeyError):
                pass
        
        # For MP4/M4A files
        elif audio_path.suffix.lower() in [".m4a", ".mp4"]:
            if hasattr(audio_file, 'tags') and 'covr' in audio_file.tags:
                artwork_data = audio_file.tags['covr'][0]
        
        # For FLAC files
        elif audio_path.suffix.lower() == ".flac":
            if hasattr(audio_file, 'pictures') and audio_file.pictures:
                artwork_data = audio_file.pictures[0].data
        
        if artwork_data:
            # Save artwork
            artwork_path = ARTWORK_DIR / f"{audio_path.stem}.jpg"
            
            # Process image with PIL to ensure it's a valid image
            try:
                img = Image.open(BytesIO(artwork_data))
                # Convert to RGB if necessary (handles RGBA, P, etc.)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                # Resize to reasonable size (300x300 max)
                img.thumbnail((300, 300), Image.Resampling.LANCZOS)
                img.save(artwork_path, 'JPEG', quality=85)
                return artwork_path
            except Exception as e:
                print(f"Error processing artwork for {audio_path}: {e}")
                return None
        
    except Exception as e:
        print(f"Error extracting artwork from {audio_path}: {e}")
    
    return None


# Flask routes
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        app_username = os.environ.get("APP_USERNAME")
        app_password = os.environ.get("APP_PASSWORD")
        
        if username == app_username and password == app_password:
            session["authenticated"] = True
            return redirect(request.args.get("next") or url_for("index"))
        
        return render_template("login.html", error="Invalid credentials")
    
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    """Serve the player interface."""
    return render_template("index.html")


@app.route("/api/tracks")
@login_required
def api_tracks():
    """Get list of available tracks."""
    tracks = load_tracks()
    
    # Return minimal info (no links)
    return jsonify([
        {
            "id": t["id"],
            "name": t["name"],
            "era": t["era"],
            "notes": t["notes"],
            "quality": t["quality"],
            "available_length": t["available_length"],
            "track_length": t["track_length"],
        }
        for t in tracks
    ])


@app.route("/api/sections")
@login_required
def api_sections():
    """Get list of sections in order."""
    if not JSON_PATH.exists():
        return jsonify([])
    
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    sections = data.get("sections", [])
    # Return just the era names in order
    return jsonify([{"era": s.get("era", "")} for s in sections])


@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    """Manually trigger a sheet refresh."""
    success = refresh_sheet()
    if success:
        return jsonify({"status": "ok", "message": "Sheet refreshed successfully"})
    return jsonify({"status": "error", "message": "Failed to refresh sheet"}), 500


@app.route("/api/status")
@login_required
def api_status():
    """Get server status including last refresh time."""
    tracks = load_tracks()
    return jsonify({
        "track_count": len(tracks),
        "last_refresh": _last_refresh.isoformat() if _last_refresh else None,
        "refresh_interval_minutes": config.refresh_interval() // 60,
    })


@app.route("/api/play/<track_id>")
@login_required
def api_play(track_id: str):
    """Serve audio file for a track, downloading if necessary."""
    track = get_track_index().get(track_id)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    audio_path = get_audio_path(track_id)
    
    # Download if not exists
    if not audio_path or not audio_path.exists():
        print(f"Downloading track {track_id}...")
        audio_path = download_track(track_id)
        if not audio_path or not audio_path.exists():
            return jsonify({"error": "Failed to download track"}), 500
    
    # Extract artwork if available
    if HAS_ARTWORK_SUPPORT:
        try:
            extract_album_art(audio_path)
        except Exception as e:
            print(f"Error extracting artwork: {e}")

    # Determine MIME type from extension
    ext = audio_path.suffix.lower()
    mime_types = {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
    }
    mimetype = mime_types.get(ext, "audio/mpeg")

    return send_file(
        str(audio_path),
        mimetype=mimetype,
        as_attachment=False,
    )


@app.route("/api/artwork/<track_id>")
@login_required
def api_artwork(track_id: str):
    """Serve album artwork for a track."""
    audio_path = get_audio_path(track_id)
    if not audio_path or not audio_path.exists():
        return jsonify({"error": "Track not found"}), 404
    
    # Try to find existing artwork
    artwork_path = ARTWORK_DIR / f"{audio_path.stem}.jpg"
    
    # If artwork doesn't exist, try to extract it
    if not artwork_path.exists() and HAS_ARTWORK_SUPPORT:
        artwork_path = extract_album_art(audio_path)
    
    if artwork_path and artwork_path.exists():
        return send_file(
            str(artwork_path),
            mimetype="image/jpeg",
            as_attachment=False,
        )
    
    # Return 204 No Content if no artwork
    return Response(status=204)


def main():
    """Run the server."""
    print("Loading tracks...")
    try:
        tracks = load_tracks()
        print(f"Loaded {len(tracks)} tracks")
    except Exception as e:
        print(f"Error loading tracks: {e}")
        return

    print(f"Audio directory: {AUDIO_DIR}")
    
    # Start background refresh thread
    refresh_thread = threading.Thread(target=_background_refresh, daemon=True)
    refresh_thread.start()
    print(f"Auto-refresh enabled: every {config.refresh_interval() // 60} minutes")
    
    print(f"Starting server on http://localhost:5000")
    print("Press Ctrl+C to stop")
    
    # Use threaded server for better performance
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)


if __name__ == "__main__":
    main()

