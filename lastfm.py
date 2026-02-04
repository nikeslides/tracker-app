"""
Last.fm API client for per-account scrobbling.
Used when the player runs with --accounts and LASTFM_API_KEY / LASTFM_API_SECRET are set.
"""

import hashlib
import json
import os
import re
import unicodedata
import urllib.request
import urllib.parse
from typing import Dict, Optional, Tuple

API_KEY = os.environ.get("LASTFM_API_KEY", "").strip()
API_SECRET = os.environ.get("LASTFM_API_SECRET", "").strip()
BASE_URL = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL = "https://www.last.fm/api/auth"


def is_configured() -> bool:
    """Return True if API key and secret are set."""
    return bool(API_KEY and API_SECRET)


def _sig(params: Dict[str, str], secret: str) -> str:
    """Build api_sig: sort params by key, concat name+value (exclude format/callback), append secret, md5."""
    exclude = {"format", "callback"}
    s = "".join(k + (params[k] or "") for k in sorted(params.keys()) if k not in exclude)
    return hashlib.md5((s + secret).encode("utf-8")).hexdigest()


def _request(params: Dict[str, str], use_secret: bool = True) -> Optional[Dict]:
    """POST to Last.fm API; params must include 'method'. Returns JSON dict or None on failure."""
    if not API_KEY or not API_SECRET:
        return None
    params = dict(params)
    params.setdefault("api_key", API_KEY)
    params.setdefault("format", "json")
    if use_secret:
        params["api_sig"] = _sig(params, API_SECRET)
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(BASE_URL, data=data, method="POST", headers={"User-Agent": "Yzyfi/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except Exception as e:
        print(f"Last.fm request error: {e}")
        return None


def get_token() -> Optional[str]:
    """Get unauthorized request token for web auth."""
    r = _request({"method": "auth.getToken"}, use_secret=True)
    if not r or r.get("error"):
        return None
    return (r.get("token") or "").strip()


def get_session(token: str) -> Optional[Tuple[str, str]]:
    """Exchange token for session key and username. Returns (session_key, username) or None."""
    r = _request({"method": "auth.getSession", "token": token}, use_secret=True)
    if not r or r.get("error"):
        return None
    sess = r.get("session") or {}
    sk = (sess.get("key") or "").strip()
    name = (sess.get("name") or "").strip()
    return (sk, name) if sk and name else None


def update_now_playing(
    session_key: str,
    artist: str,
    track: str,
    album: str = "",
    duration_sec: Optional[int] = None,
) -> bool:
    """Set now playing on Last.fm. Returns True on success."""
    params = {"method": "track.updateNowPlaying", "sk": session_key, "artist": artist, "track": track}
    if album:
        params["album"] = album
    if duration_sec is not None:
        params["duration"] = str(int(duration_sec))
    r = _request(params, use_secret=True)
    if not r:
        return False
    return "error" not in r


def scrobble(
    session_key: str,
    artist: str,
    track: str,
    timestamp_utc: int,
    album: str = "",
    duration_sec: Optional[int] = None,
) -> bool:
    """Scrobble one track. timestamp_utc = when the track started (Unix UTC). Returns True on success."""
    params = {
        "method": "track.scrobble",
        "sk": session_key,
        "artist[0]": artist,
        "track[0]": track,
        "timestamp[0]": str(timestamp_utc),
    }
    if album:
        params["album[0]"] = album
    if duration_sec is not None:
        params["duration[0]"] = str(int(duration_sec))
    r = _request(params, use_secret=True)
    if not r:
        return False
    if r.get("error"):
        return False
    scrobbles = r.get("scrobbles") or {}
    accepted = scrobbles.get("accepted")
    if accepted is None:
        accepted = (scrobbles.get("@attr") or {}).get("accepted")
    try:
        return int(accepted or 0) >= 1
    except (TypeError, ValueError):
        return False


# --- Track name → scrobble metadata (mirrors JS getScrobbleArtist / getScrobbleTitle) ---


def get_scrobble_artist(name: str) -> str:
    """Artist for Last.fm — lead artist only, no feat. Mirrors JS getScrobbleArtist."""
    if not name:
        return "Unknown Artist"
    first_line = name.split("\n")[0].strip()
    lead_match = re.match(r"^([^–—-]+?)\s*[–—-]\s+.+$", first_line)
    if lead_match:
        return lead_match.group(1).strip()
    return "Kanye West"


def get_scrobble_title(name: str) -> str:
    """Title for Last.fm — strips leading emojis and [V4] etc. Mirrors JS getScrobbleTitle."""
    if not name:
        return "Unknown"
    first = name.split("\n")[0].strip()
    first = re.sub(r"\s*\[V\d+\]\s*$", "", first, flags=re.IGNORECASE)
    i = 0
    while i < len(first):
        c = first[i]
        if c.isspace() or unicodedata.category(c) in ("So", "Sk"):
            i += 1
        else:
            break
    first = first[i:].strip()
    return first or "Unknown"


def track_to_scrobble_meta(track: Dict) -> Tuple[str, str, str, Optional[int]]:
    """From a track dict (name, era, track_length), return (artist, title, album, duration_sec)."""
    name = (track.get("name") or "").strip()
    artist = get_scrobble_artist(name)
    title = get_scrobble_title(name)
    album = (track.get("era") or "").strip()
    duration_str = (track.get("track_length") or "").strip()
    duration_sec = None
    if duration_str:
        parts = duration_str.split(":")
        if len(parts) == 2:
            try:
                duration_sec = int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                pass
        elif len(parts) == 1:
            try:
                duration_sec = int(parts[0])
            except ValueError:
                pass
    return (artist, title, album, duration_sec)
