#!/usr/bin/env python3
"""
Music player server for the Tracker sheet data.
Serves a web interface to play tracks from pillows.su, files.yetracker.org, and pixeldrain.com links.
Pillows files live in audio/; yetracker in audio_yetracker/; pixeldrain in audio_pixeldrain/ (separate folders).

Auth: set APP_USERNAME and APP_PASSWORD to require single shared login, or run with --accounts
to use SQLite-backed accounts (invite-key registration).
"""

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional
from flask import Flask, jsonify, render_template, send_file, Response, request, session, redirect, url_for
import waitress

import config
import main as sheet_processor

# Account system (set by main() when --accounts is used)
USE_ACCOUNTS = False
ACCOUNTS_DB_PATH: Optional[Path] = None

try:
    import auth_db
except ImportError:
    auth_db = None

try:
    import lastfm
except ImportError:
    lastfm = None

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
    if USE_ACCOUNTS and ACCOUNTS_DB_PATH and auth_db:
        user_id = session.get("user_id")
        if not user_id:
            return False
        user = auth_db.get_user_by_id(ACCOUNTS_DB_PATH, user_id)
        return user is not None

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
AUDIO_DIR = DATA_DIR / "audio"  # pillows.su files (unchanged)
AUDIO_YETRACKER_DIR = DATA_DIR / "audio_yetracker"  # files.yetracker.org files (separate)
AUDIO_PIXELDRAIN_DIR = DATA_DIR / "audio_pixeldrain"  # pixeldrain.com files (separate)
ARTWORK_DIR = DATA_DIR / "artwork"
JSON_PATH = DATA_DIR / "sheet.json"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_YETRACKER_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_PIXELDRAIN_DIR.mkdir(parents=True, exist_ok=True)
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
        if quality == "Not Available":
            continue

        # Include tracks with pillows.su links (unchanged behavior)
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
        # Include tracks with files.yetracker.org links (separate folder)
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
                    "hash": file_id,  # reuse "hash" key for file id
                    "host": "yetracker",
                    "original_link": link,
                })
        # Include tracks with pixeldrain.com links (separate folder)
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

    file_id = track["hash"]
    host = track.get("host", "pillows")

    if host == "yetracker":
        base_dir = AUDIO_YETRACKER_DIR
    elif host == "pixeldrain":
        base_dir = AUDIO_PIXELDRAIN_DIR
    else:
        base_dir = AUDIO_DIR

    id_dir = base_dir / file_id

    # If id/hash directory exists, look for any audio file in it
    if id_dir.exists() and id_dir.is_dir():
        for file_path in id_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in [".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus"]:
                return file_path

    # Fallback: check old format (flat structure with hash) — pillows only
    if host == "pillows":
        for ext in [".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus"]:
            path = base_dir / f"{file_id}{ext}"
            if path.exists():
                return path

    return None


def download_track(track_id: str) -> Optional[Path]:
    """Download a track from pillows.su, files.yetracker.org, or pixeldrain.com, preserving original filename."""
    track = get_track_index().get(track_id)
    if not track:
        return None

    file_id = track["hash"]
    host = track.get("host", "pillows")

    if host == "yetracker":
        base_dir = AUDIO_YETRACKER_DIR
        download_url = f"https://files.yetracker.org/d/{file_id}"
    elif host == "pixeldrain":
        base_dir = AUDIO_PIXELDRAIN_DIR
        download_url = f"https://pixeldrain.com/api/file/{file_id}"
    else:
        base_dir = AUDIO_DIR
        download_url = f"https://api.pillows.su/api/download/{file_id}"

    id_dir = base_dir / file_id

    # If file already exists, reuse it
    existing_path = get_audio_path(track_id)
    if existing_path and existing_path.exists():
        return existing_path
    
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
            
            # Create id directory to organize files
            id_dir.mkdir(parents=True, exist_ok=True)

            audio_path = id_dir / original_filename
            if audio_path.exists():
                return audio_path

            # Download file
            with open(audio_path, "wb") as f:
                f.write(response.read())

            print(f"Downloaded: {original_filename} ({file_id})")
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


def _era_slug(era: str) -> str:
    """Slugify era name for cache filenames."""
    s = re.sub(r"[^\w\s-]", "", era).strip().lower()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:80] or "unknown"


# Flask routes
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if USE_ACCOUNTS and ACCOUNTS_DB_PATH and auth_db:
            user = auth_db.get_user_by_username(ACCOUNTS_DB_PATH, username or "")
            if user and auth_db.verify_password(user, password or ""):
                session["user_id"] = user["id"]
                session["authenticated"] = True
                return redirect(request.args.get("next") or url_for("index"))
            return render_template("login.html", error="Invalid credentials", use_accounts=True)

        app_username = os.environ.get("APP_USERNAME")
        app_password = os.environ.get("APP_PASSWORD")
        if username == app_username and password == app_password:
            session["authenticated"] = True
            return redirect(request.args.get("next") or url_for("index"))
        return render_template("login.html", error="Invalid credentials")

    return render_template("login.html", use_accounts=USE_ACCOUNTS)


@app.route("/register", methods=["GET", "POST"])
def register():
    if not USE_ACCOUNTS or not ACCOUNTS_DB_PATH or not auth_db:
        return redirect(url_for("login"))

    if request.method == "POST":
        invite_key = request.form.get("invite_key", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not invite_key:
            return render_template("register.html", error="Invite key is required", invite_key=invite_key, username=username)
        if not username:
            return render_template("register.html", error="Username is required", invite_key=invite_key, username=username)
        if not password:
            return render_template("register.html", error="Password is required", invite_key=invite_key, username=username)
        if not auth_db.validate_invite_key(ACCOUNTS_DB_PATH, invite_key):
            return render_template("register.html", error="Invalid or already used invite key", invite_key=invite_key, username=username)

        user = auth_db.create_user(ACCOUNTS_DB_PATH, username, password)
        if not user:
            return render_template("register.html", error="Username already taken", invite_key=invite_key, username=username)

        if not auth_db.use_invite_key(ACCOUNTS_DB_PATH, invite_key, user["id"]):
            return render_template("register.html", error="Invite key was invalid or already used", invite_key=invite_key, username=username)

        session["user_id"] = user["id"]
        session["authenticated"] = True
        return redirect(request.args.get("next") or url_for("index"))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    session.pop("user_id", None)
    return redirect(url_for("login"))


def _index_context():
    """Context passed to index and settings when using accounts."""
    if USE_ACCOUNTS and ACCOUNTS_DB_PATH and auth_db:
        user_id = session.get("user_id")
        user = auth_db.get_user_by_id(ACCOUNTS_DB_PATH, user_id) if user_id else None
        lastfm_connected = False
        lastfm_username = None
        if user_id and hasattr(auth_db, "get_lastfm_session"):
            lfm = auth_db.get_lastfm_session(ACCOUNTS_DB_PATH, user_id)
            if lfm:
                lastfm_connected = True
                lastfm_username = lfm["lastfm_username"] or None
        return {
            "use_accounts": True,
            "current_username": user["username"] if user else None,
            "lastfm_connected": lastfm_connected,
            "lastfm_username": lastfm_username,
            "lastfm_available": lastfm.is_configured() if lastfm else False,
        }
    return {"use_accounts": False, "current_username": None, "lastfm_connected": False, "lastfm_username": None, "lastfm_available": False}


@app.route("/")
@login_required
def index():
    """Serve the player interface."""
    return render_template("index.html", **_index_context())


@app.route("/settings")
@login_required
def settings():
    """Account settings (when using --accounts)."""
    ctx = _index_context()
    if not ctx["use_accounts"]:
        return redirect(url_for("index"))
    return render_template("settings.html", **ctx)


@app.route("/settings/lastfm/connect")
@login_required
def lastfm_connect():
    """Start Last.fm connect flow: redirect to Last.fm to authorize; they redirect back to cb with ?token=."""
    if not USE_ACCOUNTS or not ACCOUNTS_DB_PATH or not auth_db or not lastfm or not lastfm.is_configured():
        return redirect(url_for("settings"))
    callback_url = url_for("lastfm_callback", _external=True)
    url = f"{lastfm.AUTH_URL}?api_key={urllib.parse.quote(lastfm.API_KEY)}&cb={urllib.parse.quote(callback_url)}"
    return redirect(url)


@app.route("/settings/lastfm/callback")
@login_required
def lastfm_callback():
    """Last.fm callback: exchange token for session, save to user, redirect to settings."""
    if not USE_ACCOUNTS or not ACCOUNTS_DB_PATH or not auth_db:
        return redirect(url_for("settings"))
    token = request.args.get("token")
    if not token:
        return redirect(url_for("settings"))
    result = lastfm.get_session(token) if lastfm else None
    if not result:
        return redirect(url_for("settings"))
    session_key, lastfm_username = result
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("settings"))
    auth_db.set_lastfm_session(ACCOUNTS_DB_PATH, user_id, session_key, lastfm_username)
    return redirect(url_for("settings"))


@app.route("/settings/lastfm/disconnect", methods=["POST"])
@login_required
def lastfm_disconnect():
    """Remove Last.fm link for current user."""
    if not USE_ACCOUNTS or not ACCOUNTS_DB_PATH or not auth_db:
        return redirect(url_for("settings"))
    user_id = session.get("user_id")
    if user_id:
        auth_db.clear_lastfm_session(ACCOUNTS_DB_PATH, user_id)
    return redirect(url_for("settings"))


@app.route("/api/lastfm/now-playing", methods=["POST"])
@login_required
def api_lastfm_now_playing():
    """Set Last.fm now playing for current user. Body: JSON { "track_id": "..." }."""
    if not USE_ACCOUNTS or not ACCOUNTS_DB_PATH or not auth_db:
        return jsonify({"error": "Accounts not enabled"}), 400
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    lfm = auth_db.get_lastfm_session(ACCOUNTS_DB_PATH, user_id) if hasattr(auth_db, "get_lastfm_session") else None
    if not lfm:
        return jsonify({"error": "Last.fm not connected"}), 400
    data = request.get_json(silent=True) or {}
    track_id = (data.get("track_id") or "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
    track = get_track_index().get(track_id)
    if not track:
        return jsonify({"error": "Track not found"}), 404
    if not lastfm:
        return jsonify({"ok": False})
    artist, title, album, duration_sec = lastfm.track_to_scrobble_meta(track)
    ok = lastfm.update_now_playing(lfm["session_key"], artist, title, album, duration_sec)
    return jsonify({"ok": ok})


@app.route("/api/lastfm/scrobble", methods=["POST"])
@login_required
def api_lastfm_scrobble():
    """Scrobble one play to Last.fm. Body: JSON { "track_id": "...", "timestamp": <unix_utc> }."""
    if not USE_ACCOUNTS or not ACCOUNTS_DB_PATH or not auth_db:
        return jsonify({"error": "Accounts not enabled"}), 400
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    lfm = auth_db.get_lastfm_session(ACCOUNTS_DB_PATH, user_id) if hasattr(auth_db, "get_lastfm_session") else None
    if not lfm:
        return jsonify({"error": "Last.fm not connected"}), 400
    data = request.get_json(silent=True) or {}
    track_id = (data.get("track_id") or "").strip()
    timestamp = data.get("timestamp")
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
    if timestamp is None:
        return jsonify({"error": "timestamp required"}), 400
    try:
        timestamp = int(timestamp)
    except (TypeError, ValueError):
        return jsonify({"error": "timestamp must be integer"}), 400
    track = get_track_index().get(track_id)
    if not track:
        return jsonify({"error": "Track not found"}), 404
    if not lastfm:
        return jsonify({"ok": False})
    artist, title, album, duration_sec = lastfm.track_to_scrobble_meta(track)
    ok = lastfm.scrobble(lfm["session_key"], artist, title, timestamp, album, duration_sec)
    return jsonify({"ok": ok})


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


@app.route("/api/favorites", methods=["GET", "POST"])
@login_required
def api_favorites():
    """GET: list favorite track IDs. POST: { track_id, favorite } to toggle (accounts only)."""
    if not USE_ACCOUNTS or not ACCOUNTS_DB_PATH or not auth_db:
        if request.method == "GET":
            return jsonify([])
        return jsonify({"ok": False}), 404
    user_id = session.get("user_id")
    if not user_id:
        if request.method == "GET":
            return jsonify([])
        return jsonify({"ok": False}), 401
    if request.method == "GET":
        ids = auth_db.get_favorites(ACCOUNTS_DB_PATH, user_id)
        return jsonify(ids)
    data = request.get_json(silent=True) or {}
    track_id = (data.get("track_id") or "").strip()
    favorite = data.get("favorite", True)
    if not track_id:
        return jsonify({"ok": False}), 400
    if favorite:
        auth_db.add_favorite(ACCOUNTS_DB_PATH, user_id, track_id)
    else:
        auth_db.remove_favorite(ACCOUNTS_DB_PATH, user_id, track_id)
    return jsonify({"ok": True})


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
    """Serve album artwork for a track (embedded first, then section default from sheet)."""
    audio_path = get_audio_path(track_id)
    if not audio_path or not audio_path.exists():
        return jsonify({"error": "Track not found"}), 404

    # 1. Existing extracted artwork
    artwork_path = ARTWORK_DIR / f"{audio_path.stem}.jpg"
    if artwork_path.exists():
        return send_file(
            str(artwork_path),
            mimetype="image/jpeg",
            as_attachment=False,
        )

    # 2. Extract from audio file
    if HAS_ARTWORK_SUPPORT:
        artwork_path = extract_album_art(audio_path)
    if artwork_path and artwork_path.exists():
        return send_file(
            str(artwork_path),
            mimetype="image/jpeg",
            as_attachment=False,
        )

    # 3. Fallback: section default (data/artwork/_era_{slug}.jpg, downloaded by main.py)
    track = get_track_index().get(track_id)
    if track:
        era = (track.get("era") or "").strip()
        if era:
            slug = _era_slug(era)
            section_path = ARTWORK_DIR / f"_era_{slug}.jpg"
            if section_path.exists():
                return send_file(
                    str(section_path),
                    mimetype="image/jpeg",
                    as_attachment=False,
                )

    return Response(status=204)


def main():
    """Run the server or generate an invite key."""
    global USE_ACCOUNTS, ACCOUNTS_DB_PATH

    parser = argparse.ArgumentParser(description="Yzyfi player server")
    parser.add_argument(
        "--accounts",
        action="store_true",
        help="Use SQLite-backed accounts (invite-key registration) instead of APP_USERNAME/APP_PASSWORD",
    )
    parser.add_argument(
        "--gen-invite",
        action="store_true",
        help="Generate a new invite key (requires --accounts). Print key and exit.",
    )
    args = parser.parse_args()

    if args.gen_invite:
        if not args.accounts:
            print("--gen-invite requires --accounts", file=sys.stderr)
            sys.exit(1)
        if not auth_db:
            print("auth_db module not available", file=sys.stderr)
            sys.exit(1)
        db_path = Path(config.output_path()) / "auth.db"
        auth_db.init_db(db_path)
        key = auth_db.create_invite_key(db_path)
        print(f"Invite key (use once for registration): {key}")
        sys.exit(0)

    if args.accounts:
        if not auth_db:
            print("Account system requested but auth_db module not available.", file=sys.stderr)
            sys.exit(1)
        USE_ACCOUNTS = True
        ACCOUNTS_DB_PATH = Path(config.output_path()) / "auth.db"
        auth_db.init_db(ACCOUNTS_DB_PATH)
        print(f"Account system enabled (SQLite: {ACCOUNTS_DB_PATH})")

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
    
    # Production WSGI server (no dev-server warning)
    waitress.serve(app, host="0.0.0.0", port=5000, threads=6)


if __name__ == "__main__":
    main()

