let tracks = [];
let currentTrackId = null;
let currentTrackIndex = -1;
let allTracksFlat = [];
let tracksByEra = {};
let eraOrder = [];
const audioPlayer = document.getElementById("audio-player");
const fixedPlayer = document.getElementById("fixed-player");
const playerTrackName = document.getElementById("player-track-name");
const playerTrackArtists = document.getElementById("player-track-artists");
const playerTrackProducers = document.getElementById("player-track-producers");
const albumArt = document.getElementById("album-art");
const albumArtPlaceholder = document.getElementById("album-art-placeholder");
const btnPlayPause = document.getElementById("btn-play-pause");
const btnPrevious = document.getElementById("btn-previous");
const btnNext = document.getElementById("btn-next");
const progressBar = document.getElementById("progress-bar");
const progressFill = document.getElementById("progress-fill");
const progressCurrent = document.getElementById("progress-current");
const progressTotal = document.getElementById("progress-total");
const volumeSlider = document.getElementById("volume-slider");
const volumeFill = document.getElementById("volume-fill");
const btnVolume = document.getElementById("btn-volume");
const btnShuffle = document.getElementById("btn-shuffle");
const btnRepeat = document.getElementById("btn-repeat");
const tracksContainer = document.getElementById("tracks-container");
const stats = document.getElementById("stats");
const searchInput = document.getElementById("search-input");
const searchClear = document.getElementById("search-clear");
const sectionsListEl = document.getElementById("sections-list");
const sectionsSelectEl = document.getElementById("sections-select");
const playlistTitleEl = document.getElementById("playlist-title");
const playlistSubtitleEl = document.getElementById("playlist-subtitle");
const playlistTracksEl = document.getElementById("playlist-tracks");

// Clear Media Session on load so Web Scrobbler (and OS media controls) don't show stale track
if ('mediaSession' in navigator) {
  navigator.mediaSession.metadata = null;
  navigator.mediaSession.playbackState = 'none';
}

// Load saved volume from localStorage, default to 0.7
let volume = parseFloat(localStorage.getItem('playerVolume')) || 0.7;
audioPlayer.volume = volume;

// Shuffle and repeat state
let shuffleEnabled = localStorage.getItem('playerShuffle') === 'true';
let repeatMode = localStorage.getItem('playerRepeat') || 'off'; // 'off', 'one', 'all'
let shuffledTracks = [];

let sections = [];
let searchQuery = "";
let selectedEra = localStorage.getItem("playerSelectedEra") || null;

async function loadTracks() {
  try {
    // Load both tracks and sections
    const [tracksRes, jsonRes] = await Promise.all([
      fetch("/api/tracks"),
      fetch("/api/sections")
    ]);
    
    if (!tracksRes.ok) throw new Error(`HTTP ${tracksRes.status}`);
    if (!jsonRes.ok) throw new Error(`HTTP ${jsonRes.status}`);
    
        tracks = await tracksRes.json();
        sections = await jsonRes.json();
        
        processTrackData();
        renderTracks();
        stats.textContent = `${tracks.length} tracks available`;
        
        // Restore shuffle and repeat states
        updateShuffleDisplay();
        updateRepeatDisplay();
        
        // Check for track in URL
        const urlParams = new URLSearchParams(window.location.search);
        const urlTrackId = urlParams.get('track');
        
        if (urlTrackId) {
          // Wait a bit for tracks to be fully rendered
          setTimeout(() => {
            const track = allTracksFlat.find(t => t.id === urlTrackId);
            if (track) {
              // Find the era this track belongs to and select it
              if (track.era) {
                selectedEra = track.era;
                localStorage.setItem("playerSelectedEra", selectedEra);
                renderTracks(); // Re-render to show the correct era
              }
              playTrack(urlTrackId);
              
              // Scroll to the track card
              setTimeout(() => {
                const trackCard = document.querySelector(`.track-card[data-track-id="${urlTrackId}"]`);
                if (trackCard) {
                  trackCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
              }, 200);
            }
          }, 100);
        } else {
          // Try to restore last played track
          const savedTrackId = localStorage.getItem('playerCurrentTrack');
          if (savedTrackId && !currentTrackId) {
            // Wait a bit for tracks to be fully rendered
            setTimeout(() => {
              const track = allTracksFlat.find(t => t.id === savedTrackId);
              if (track) {
                playTrack(savedTrackId);
              }
            }, 100);
          }
        }
        
        // Reset title when tracks load
        if (!currentTrackId) {
          document.title = "Yzyfi";
        }
  } catch (err) {
    tracksContainer.innerHTML = `<div class="error">Failed to load tracks: ${err.message}</div>`;
  }
}

function processTrackData() {
  allTracksFlat = [];
  tracksByEra = {};
  
  tracks.forEach(track => {
    const era = track.era || "Unknown Era";
    if (!tracksByEra[era]) {
      tracksByEra[era] = [];
    }
    
    // Pre-calculate searchable string for better performance
    const artists = extractArtists(track.name);
    track._searchStr = `${track.name} ${era} ${artists} ${track.notes || ""}`.toLowerCase();
    
    tracksByEra[era].push(track);
  });
  // ... rest of the function ...

  // Use section order from JSON, fallback to alphabetical if sections not loaded
  eraOrder = [];
  if (sections.length > 0) {
    eraOrder = sections.map(s => s.era);
    // Add any eras that exist in tracks but not in sections (at the end)
    const sectionEras = new Set(eraOrder);
    const missingEras = Object.keys(tracksByEra).filter(era => !sectionEras.has(era));
    eraOrder = eraOrder.concat(missingEras.sort());
  } else {
    eraOrder = Object.keys(tracksByEra).sort();
  }
  
  // Build flattened track order in era order for prev/next
  eraOrder.forEach(era => {
    const eraTracks = tracksByEra[era] || [];
    eraTracks.forEach(t => allTracksFlat.push(t));
  });
}

/**
 * Display artist for scrobbling/Media Session: "Kanye West (feat. X)".
 * No producer or "with" — those go in a separate producers div.
 */
function extractArtists(name) {
  const featMatch = name.match(/\(feat\.\s+([^)]+)\)/i);
  const featPart = featMatch ? ` (feat. ${featMatch[1]})` : "";
  const firstLine = name.split("\n")[0].trim();
  const leadArtistMatch = firstLine.match(/^([^–—-]+?)\s*[–—-]\s+.+$/);
  if (leadArtistMatch) return leadArtistMatch[1].trim() + featPart;
  return "Kanye West" + featPart;
}

/** Producer credits only — shown in separate div, not used for scrobbling. */
function extractProducers(name) {
  const prodMatch = name.match(/\(prod\.\s+([^)]+)\)/i);
  return prodMatch ? prodMatch[1].trim() : "";
}

/**
 * Artist string for scrobbling/Media Session only — no "(feat. X)" so Last.fm
 * gets a clean artist. The UI still shows extractArtists() (with feat.).
 */
function getScrobbleArtist(name) {
  const firstLine = name.split("\n")[0].trim();
  const leadArtistMatch = firstLine.match(/^([^–—-]+?)\s*[–—-]\s+.+$/);
  if (leadArtistMatch) return leadArtistMatch[1].trim();
  return "Kanye West";
}

/**
 * Title for scrobbling/Media Session only — strips leading emojis and version
 * tag (e.g. [V4]) so Last.fm gets a clean title. The UI still shows cleanTrackTitle().
 */
function getScrobbleTitle(name) {
  let first = (name || "").split("\n")[0].trim();
  first = first.replace(/\s*\[V\d+\]\s*$/i, "").trim();
  first = first.replace(/^[\s\p{So}\p{Sk}]+/u, "").trim();
  return first || (name || "").split("\n")[0].trim();
}

function cleanTrackTitle(name) {
  let first = name.split("\n")[0].trim();
  return first || name.split("\n")[0].trim();
}

function formatTime(seconds) {
  if (isNaN(seconds)) return "0:00";
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

function updateProgress() {
  const current = audioPlayer.currentTime || 0;
  const duration = audioPlayer.duration || 0;
  const percent = duration > 0 ? (current / duration) * 100 : 0;
  progressFill.style.width = percent + "%";
  progressCurrent.textContent = formatTime(current);
  progressTotal.textContent = formatTime(duration);
}

function updateVolumeDisplay() {
  volumeFill.style.width = (volume * 100) + "%";
  const volumeHigh = btnVolume.querySelector(".volume-high");
  const volumeMedium = btnVolume.querySelector(".volume-medium");
  const volumeMuted = btnVolume.querySelector(".volume-muted");
  
  if (volume === 0) {
    if (volumeHigh) volumeHigh.style.display = "none";
    if (volumeMedium) volumeMedium.style.display = "none";
    if (volumeMuted) volumeMuted.style.display = "block";
  } else if (volume < 0.5) {
    if (volumeHigh) volumeHigh.style.display = "none";
    if (volumeMedium) volumeMedium.style.display = "block";
    if (volumeMuted) volumeMuted.style.display = "none";
  } else {
    if (volumeHigh) volumeHigh.style.display = "block";
    if (volumeMedium) volumeMedium.style.display = "none";
    if (volumeMuted) volumeMuted.style.display = "none";
  }
  
  // Save volume to localStorage
  localStorage.setItem('playerVolume', volume.toString());
}

function updateShuffleDisplay() {
  if (shuffleEnabled) {
    btnShuffle.classList.add('active');
    btnShuffle.style.color = '#1db954';
  } else {
    btnShuffle.classList.remove('active');
    btnShuffle.style.color = '';
  }
  localStorage.setItem('playerShuffle', shuffleEnabled.toString());
}

function updateRepeatDisplay() {
  const repeatIcon = btnRepeat.querySelector('.ti');
  btnRepeat.classList.remove('repeat-off', 'repeat-one', 'repeat-all');
  
  if (repeatMode === 'off') {
    btnRepeat.classList.add('repeat-off');
    btnRepeat.style.color = '';
    if (repeatIcon) repeatIcon.className = 'ti ti-repeat';
    btnRepeat.title = 'Repeat';
  } else if (repeatMode === 'one') {
    btnRepeat.classList.add('repeat-one');
    btnRepeat.style.color = '#1db954';
    if (repeatIcon) repeatIcon.className = 'ti ti-repeat';
    btnRepeat.title = 'Repeat One';
  } else if (repeatMode === 'all') {
    btnRepeat.classList.add('repeat-all');
    btnRepeat.style.color = '#1db954';
    if (repeatIcon) repeatIcon.className = 'ti ti-repeat';
    btnRepeat.title = 'Repeat All';
  }
  localStorage.setItem('playerRepeat', repeatMode);
}

async function playTrack(trackId) {
  const track = allTracksFlat.find(t => t.id === trackId);
  if (!track) return;

  // Check if this is the same track (for position restoration) BEFORE updating
  const savedTrackId = localStorage.getItem('playerCurrentTrack');
  const isSameTrack = savedTrackId === trackId;
  
  // If it's a different track, clear the saved position
  if (!isSameTrack) {
    localStorage.removeItem('playerPosition');
  }

  currentTrackId = trackId;
  // Use shuffled tracks if shuffle is enabled, otherwise use normal order
  const trackList = shuffleEnabled && shuffledTracks.length > 0 ? shuffledTracks : allTracksFlat;
  currentTrackIndex = trackList.findIndex(t => t.id === trackId);
  
  // Save current track to localStorage
  localStorage.setItem('playerCurrentTrack', trackId);
  
  // Update UI
  document.querySelectorAll(".track-card").forEach(card => {
    card.classList.toggle("playing", card.dataset.trackId === trackId);
  });

  const artists = extractArtists(track.name);
  const cleanName = cleanTrackTitle(track.name);
  const producers = extractProducers(track.name);

  playerTrackName.textContent = cleanName;
  playerTrackArtists.textContent = artists || "";
  if (playerTrackProducers) {
    playerTrackProducers.textContent = producers ? `prod. ${producers}` : "";
    playerTrackProducers.style.display = producers ? "" : "none";
  }

  // Update document title to show current track
  const titleSuffix = artists ? `${cleanName} - ${artists}` : cleanName;
  document.title = `${titleSuffix} | Yzyfi`;

  // Update Media Session metadata (Web Scrobbler / Last.fm get clean title + artist)
  if ('mediaSession' in navigator) {
    const artworkUrl = `/api/artwork/${trackId}`;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: getScrobbleTitle(track.name),
      artist: getScrobbleArtist(track.name) || track.era || "Unknown Artist",
      album: track.era || "Yzyfi",
      artwork: [
        { src: artworkUrl, sizes: '300x300', type: 'image/jpeg' },
        { src: artworkUrl, sizes: '512x512', type: 'image/jpeg' }
      ]
    });
  }
  
  // Reset artwork display
  albumArt.style.display = "none";
  albumArtPlaceholder.style.display = "flex";
  
  // Function to load artwork (called after audio is loaded)
  function loadArtwork() {
    albumArt.src = `/api/artwork/${trackId}`;
    albumArt.onload = () => {
      albumArt.style.display = "block";
      albumArtPlaceholder.style.display = "none";
      // Update media session artwork if it loaded successfully
      if ('mediaSession' in navigator && navigator.mediaSession.metadata) {
        const artworkUrl = `/api/artwork/${trackId}`;
        navigator.mediaSession.metadata.artwork = [
          { src: artworkUrl, sizes: '300x300', type: 'image/jpeg' },
          { src: artworkUrl, sizes: '512x512', type: 'image/jpeg' }
        ];
      }
    };
    albumArt.onerror = () => {
      albumArt.style.display = "none";
      albumArtPlaceholder.style.display = "flex";
    };
  }
  
  // Show loading state on player controls only
  btnPlayPause.classList.add("loading");
  btnPlayPause.disabled = true;
  
  // Load and play audio
  audioPlayer.src = `/api/play/${trackId}`;
  
  // Wait for audio metadata to load before requesting artwork
  const loadArtworkOnce = () => {
    loadArtwork();
    audioPlayer.removeEventListener("loadedmetadata", loadArtworkOnce);
  };
  audioPlayer.addEventListener("loadedmetadata", loadArtworkOnce);
  
  audioPlayer.load();
  
  try {
    await audioPlayer.play();
    // Restore playback position from localStorage only if this is the same track
    if (isSameTrack) {
      const savedPosition = localStorage.getItem('playerPosition');
      if (savedPosition) {
        const position = parseFloat(savedPosition);
        if (position > 0 && position < audioPlayer.duration) {
          audioPlayer.currentTime = position;
        }
      }
    } else {
      // New track - start from beginning
      audioPlayer.currentTime = 0;
    }
  } catch (err) {
    console.error("Playback error:", err);
  } finally {
    btnPlayPause.classList.remove("loading");
    btnPlayPause.disabled = false;
    // Update the play/pause icon based on current state
    updatePlayPauseIcon();
  }
}

function updatePlayPauseIcon() {
  const playIcon = btnPlayPause.querySelector(".play-icon");
  const pauseIcon = btnPlayPause.querySelector(".pause-icon");
  if (audioPlayer.paused) {
    if (playIcon) playIcon.style.display = "block";
    if (pauseIcon) pauseIcon.style.display = "none";
    btnPlayPause.title = "Play";
  } else {
    if (playIcon) playIcon.style.display = "none";
    if (pauseIcon) pauseIcon.style.display = "block";
    btnPlayPause.title = "Pause";
  }
}

function playPause() {
  if (audioPlayer.paused) {
    audioPlayer.play();
  } else {
    audioPlayer.pause();
  }
  updatePlayPauseIcon();
}

function playNext() {
  const trackList = shuffleEnabled && shuffledTracks.length > 0 ? shuffledTracks : allTracksFlat;
  
  if (repeatMode === 'one') {
    // Repeat current track
    audioPlayer.currentTime = 0;
    audioPlayer.play();
    return;
  }
  
  if (currentTrackIndex >= 0 && currentTrackIndex < trackList.length - 1) {
    playTrack(trackList[currentTrackIndex + 1].id);
  } else if (currentTrackIndex === trackList.length - 1) {
    // End of playlist
    if (repeatMode === 'all') {
      // Start from beginning
      playTrack(trackList[0].id);
    } else {
      // Stop playback
      audioPlayer.pause();
      audioPlayer.currentTime = 0;
      updatePlayPauseIcon();
    }
  }
}

function playPrevious() {
  const trackList = shuffleEnabled && shuffledTracks.length > 0 ? shuffledTracks : allTracksFlat;
  
  if (audioPlayer.currentTime > 3) {
    // If more than 3 seconds in, restart current track
    audioPlayer.currentTime = 0;
  } else if (currentTrackIndex > 0) {
    playTrack(trackList[currentTrackIndex - 1].id);
  } else if (currentTrackIndex === 0) {
    if (repeatMode === 'all') {
      // Go to last track
      playTrack(trackList[trackList.length - 1].id);
    } else {
      audioPlayer.currentTime = 0;
    }
  }
}

// Event listeners
btnPlayPause.addEventListener("click", playPause);
btnNext.addEventListener("click", playNext);
btnPrevious.addEventListener("click", playPrevious);

audioPlayer.addEventListener("timeupdate", () => {
  updateProgress();
  // Save current playback position to localStorage
  if (currentTrackId && !audioPlayer.paused) {
    localStorage.setItem('playerPosition', audioPlayer.currentTime.toString());
  }
});
audioPlayer.addEventListener("loadedmetadata", () => {
  updateProgress();
});
audioPlayer.addEventListener("ended", () => {
  playNext();
});
audioPlayer.addEventListener("play", () => {
  updatePlayPauseIcon();
  // Update Media Session playback state
  if ('mediaSession' in navigator) {
    navigator.mediaSession.playbackState = 'playing';
  }
});
audioPlayer.addEventListener("pause", () => {
  updatePlayPauseIcon();
  // Update Media Session playback state
  if ('mediaSession' in navigator) {
    navigator.mediaSession.playbackState = 'paused';
  }
});

// Set up Media Session action handlers for iOS Control Center, etc.
if ('mediaSession' in navigator) {
  navigator.mediaSession.setActionHandler('play', () => {
    audioPlayer.play();
  });
  
  navigator.mediaSession.setActionHandler('pause', () => {
    audioPlayer.pause();
  });
  
  navigator.mediaSession.setActionHandler('previoustrack', () => {
    playPrevious();
  });
  
  navigator.mediaSession.setActionHandler('nexttrack', () => {
    playNext();
  });
  
  // Set initial playback state
  navigator.mediaSession.playbackState = 'none';
}

// Progress bar interaction
function handleProgressInteraction(e) {
  const rect = progressBar.getBoundingClientRect();
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  const percent = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  audioPlayer.currentTime = percent * audioPlayer.duration;
}

progressBar.addEventListener("mousedown", (e) => {
  handleProgressInteraction(e);
  
  const onMouseMove = (moveEvent) => {
    handleProgressInteraction(moveEvent);
  };
  
  const onMouseUp = () => {
    document.removeEventListener("mousemove", onMouseMove);
    document.removeEventListener("mouseup", onMouseUp);
  };
  
  document.addEventListener("mousemove", onMouseMove);
  document.addEventListener("mouseup", onMouseUp);
});

progressBar.addEventListener("touchstart", (e) => {
  handleProgressInteraction(e);
  
  const onTouchMove = (moveEvent) => {
    moveEvent.preventDefault(); // Prevent scrolling while seeking
    handleProgressInteraction(moveEvent);
  };
  
  const onTouchEnd = () => {
    document.removeEventListener("touchmove", onTouchMove);
    document.removeEventListener("touchend", onTouchEnd);
  };
  
  document.addEventListener("touchmove", onTouchMove, { passive: false });
  document.addEventListener("touchend", onTouchEnd);
}, { passive: true });

progressBar.addEventListener("click", handleProgressInteraction);

// Volume control interaction
function handleVolumeInteraction(e) {
  const rect = volumeSlider.getBoundingClientRect();
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  volume = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  audioPlayer.volume = volume;
  updateVolumeDisplay();
}

volumeSlider.addEventListener("mousedown", (e) => {
  handleVolumeInteraction(e);
  
  const onMouseMove = (moveEvent) => {
    handleVolumeInteraction(moveEvent);
  };
  
  const onMouseUp = () => {
    document.removeEventListener("mousemove", onMouseMove);
    document.removeEventListener("mouseup", onMouseUp);
  };
  
  document.addEventListener("mousemove", onMouseMove);
  document.addEventListener("mouseup", onMouseUp);
});

volumeSlider.addEventListener("touchstart", (e) => {
  handleVolumeInteraction(e);
  
  const onTouchMove = (moveEvent) => {
    moveEvent.preventDefault(); // Prevent scrolling while adjusting volume
    handleVolumeInteraction(moveEvent);
  };
  
  const onTouchEnd = () => {
    document.removeEventListener("touchmove", onTouchMove);
    document.removeEventListener("touchend", onTouchEnd);
  };
  
  document.addEventListener("touchmove", onTouchMove, { passive: false });
  document.addEventListener("touchend", onTouchEnd);
}, { passive: true });

volumeSlider.addEventListener("click", handleVolumeInteraction);

btnVolume.addEventListener("click", () => {
  if (volume > 0) {
    volume = 0;
    audioPlayer.volume = 0;
  } else {
    // Restore to saved volume or default to 0.7
    volume = parseFloat(localStorage.getItem('playerVolume')) || 0.7;
    audioPlayer.volume = volume;
  }
  updateVolumeDisplay();
});

// Shuffle functionality
function shuffleArray(array) {
  const shuffled = [...array];
  for (let i = shuffled.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
  }
  return shuffled;
}

btnShuffle.addEventListener("click", () => {
  shuffleEnabled = !shuffleEnabled;
  
  if (shuffleEnabled) {
    // Create shuffled copy of tracks
    shuffledTracks = shuffleArray(allTracksFlat);
    // If a track is currently playing, move it to the front of shuffled list
    if (currentTrackIndex >= 0 && currentTrackId) {
      const currentTrack = shuffledTracks.find(t => t.id === currentTrackId);
      if (currentTrack) {
        const currentIdx = shuffledTracks.indexOf(currentTrack);
        shuffledTracks.splice(currentIdx, 1);
        shuffledTracks.unshift(currentTrack);
        currentTrackIndex = 0;
      }
    }
  }
  
  updateShuffleDisplay();
});

// Repeat functionality
btnRepeat.addEventListener("click", () => {
  if (repeatMode === 'off') {
    repeatMode = 'all';
  } else if (repeatMode === 'all') {
    repeatMode = 'one';
  } else {
    repeatMode = 'off';
  }
  updateRepeatDisplay();
});

function matchesSearch(track, query) {
  if (!query) return true;
  const lowerQuery = query.toLowerCase();
  return track._searchStr.includes(lowerQuery);
}

function renderTracks() {
  if (tracks.length === 0) {
    if (playlistTracksEl) {
      playlistTracksEl.innerHTML = "<div class='error'>No tracks available</div>";
    } else {
      tracksContainer.innerHTML = "<div class='error'>No tracks available</div>";
    }
    return;
  }

  // If searching, show all matches across all eras
  if (searchQuery) {
    const visible = allTracksFlat.filter(t => matchesSearch(t, searchQuery));
    
    renderSidebar(eraOrder, tracksByEra);
    
    if (playlistTitleEl) playlistTitleEl.textContent = `Search results for "${searchQuery}"`;
    if (playlistSubtitleEl) {
      playlistSubtitleEl.textContent = `${visible.length} track${visible.length === 1 ? '' : 's'} found`;
    }
    
    const target = playlistTracksEl || tracksContainer;
    if (!target) return;
    
    if (visible.length === 0) {
      target.innerHTML = "<div class='error'>No tracks found matching your search.</div>";
    } else {
      const fragment = document.createDocumentFragment();
      visible.forEach((track, idx) => {
        const card = createTrackCard(track, idx);
        fragment.appendChild(card);
      });
      target.innerHTML = "";
      target.appendChild(fragment);
    }
  } else {
    // Normal era-based rendering
    // Ensure we have a valid selected era
    if (!selectedEra || !tracksByEra[selectedEra]) {
      selectedEra = eraOrder[0] || "Unknown Era";
    }
    
    renderSidebar(eraOrder, tracksByEra);
    renderPlaylist(tracksByEra);
  }
  
  updateVolumeDisplay();
  
  // Recreate shuffled list if shuffle is enabled
  if (shuffleEnabled) {
    shuffledTracks = shuffleArray(allTracksFlat);
    // Keep current track at front if playing
    if (currentTrackId) {
      const currentTrack = shuffledTracks.find(t => t.id === currentTrackId);
      if (currentTrack) {
        const currentIdx = shuffledTracks.indexOf(currentTrack);
        shuffledTracks.splice(currentIdx, 1);
        shuffledTracks.unshift(currentTrack);
        currentTrackIndex = 0;
      }
    }
  }
}

function createTrackCard(track, idx) {
  const card = document.createElement("div");
  card.className = "track-card" + (track.id === currentTrackId ? " playing" : "");
  card.dataset.trackId = track.id;
  
  const artists = extractArtists(track.name);
  const cleanName = cleanTrackTitle(track.name);
  const producers = extractProducers(track.name);

  card.innerHTML = `
    <div class="track-index">${idx + 1}</div>
    <div class="track-info">
      <div class="track-name">${escapeHtml(cleanName)}</div>
      <div class="track-meta-line">
        ${artists ? `<span class="track-artists">${escapeHtml(artists)}</span>` : ""}
        ${producers ? `<span class="track-producers">prod. ${escapeHtml(producers)}</span>` : ""}
      </div>
      ${searchQuery ? `<div class="track-era-label">${escapeHtml(track.era || "Unknown Era")}</div>` : ""}
    </div>
    <div class="track-meta">
      ${track.quality ? `<span>${escapeHtml(track.quality)}</span>` : ""}
      ${track.track_length ? `<span>${escapeHtml(track.track_length)}</span>` : ""}
    </div>
    <div class="share-btn" title="Copy link to track">
      <i class="ti ti-share"></i>
    </div>
    <div class="info-icon" title="Notes">i</div>
    <div class="info-tooltip">${escapeAttr(track.notes || "")}</div>
  `;
  
  // Share button logic
  const shareBtn = card.querySelector(".share-btn");
  if (shareBtn) {
    shareBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const url = new URL(window.location.href);
      url.searchParams.set("track", track.id);
      
      navigator.clipboard.writeText(url.toString()).then(() => {
        const originalTitle = shareBtn.title;
        shareBtn.title = "Link copied!";
        shareBtn.innerHTML = '<i class="ti ti-check"></i>';
        shareBtn.classList.add("copied");
        
        setTimeout(() => {
          shareBtn.title = originalTitle;
          shareBtn.innerHTML = '<i class="ti ti-share"></i>';
          shareBtn.classList.remove("copied");
        }, 2000);
      }).catch(err => {
        console.error("Failed to copy: ", err);
      });
    });
  }

  // Prevent clicking the info icon from starting playback
  const infoIcon = card.querySelector(".info-icon");
  const infoTooltip = card.querySelector(".info-tooltip");
  if (infoIcon && infoTooltip) {
    infoIcon.addEventListener("click", (e) => {
      e.stopPropagation();
      // Toggle visibility for mobile
      const isVisible = infoTooltip.style.display === "block";
      
      // Close all other tooltips first
      document.querySelectorAll(".info-tooltip").forEach(t => t.style.display = "none");
      
      if (!isVisible) {
        infoTooltip.style.display = "block";
      } else {
        infoTooltip.style.display = "none";
      }
    });
  }
  
  card.addEventListener("click", () => playTrack(track.id));
  return card;
}

function clearSearch() {
  if (searchQuery) {
    searchInput.value = "";
    searchQuery = "";
    searchClear.style.display = "none";
  }
}

function renderSidebar(eraOrder, tracksByEra) {
  // Mobile dropdown
  if (sectionsSelectEl) {
    if (!sectionsSelectEl.dataset.bound) {
      sectionsSelectEl.addEventListener("change", (e) => {
        selectedEra = e.target.value;
        localStorage.setItem("playerSelectedEra", selectedEra);
        clearSearch();
        renderTracks();
      });
      sectionsSelectEl.dataset.bound = "true";
    }
    
    const selectFragment = document.createDocumentFragment();
    eraOrder.forEach(era => {
      const eraTracks = tracksByEra[era] || [];
      const total = eraTracks.length;
      const matches = searchQuery ? eraTracks.filter(t => matchesSearch(t, searchQuery)).length : total;
      const countText = searchQuery ? `${matches}/${total}` : `${total}`;
      
      const opt = document.createElement("option");
      opt.value = era;
      opt.textContent = `${era} (${countText})`;
      selectFragment.appendChild(opt);
    });
    
    sectionsSelectEl.innerHTML = "";
    sectionsSelectEl.appendChild(selectFragment);
    
    if (selectedEra) {
      sectionsSelectEl.value = selectedEra;
    }
  }
  
  // Desktop list
  if (!sectionsListEl) return;
  
  const listFragment = document.createDocumentFragment();
  eraOrder.forEach(era => {
    const eraTracks = tracksByEra[era] || [];
    const total = eraTracks.length;
    const matches = searchQuery ? eraTracks.filter(t => matchesSearch(t, searchQuery)).length : total;
    const countText = searchQuery ? `${matches}/${total}` : `${total}`;
    
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "section-item" + (era === selectedEra ? " active" : "");
    btn.dataset.era = era;
    btn.innerHTML = `
      <span class="section-name">${escapeHtml(era)}</span>
      <span class="section-count">${escapeHtml(countText)}</span>
    `;
    
    btn.addEventListener("click", () => {
      selectedEra = era;
      localStorage.setItem("playerSelectedEra", selectedEra);
      clearSearch();
      renderTracks();
      // Bring the playlist back to top when switching sections
      if (playlistTracksEl) playlistTracksEl.scrollTop = 0;
    });
    
    listFragment.appendChild(btn);
  });
  
  sectionsListEl.innerHTML = "";
  sectionsListEl.appendChild(listFragment);
}

function renderPlaylist(tracksByEra) {
  const era = selectedEra || "Unknown Era";
  const allInEra = tracksByEra[era] || [];
  const visible = allInEra; // renderPlaylist is only called when not searching
  
  if (playlistTitleEl) playlistTitleEl.textContent = era;
  if (playlistSubtitleEl) {
    playlistSubtitleEl.textContent = `${allInEra.length} tracks`;
  }
  
  const target = playlistTracksEl || tracksContainer;
  if (!target) return;
  
  if (!visible.length) {
    target.innerHTML = "<div class='error'>No tracks available in this section.</div>";
    return;
  }
  
  const fragment = document.createDocumentFragment();
  visible.forEach((track, idx) => {
    const card = createTrackCard(track, idx);
    fragment.appendChild(card);
  });
  
  target.innerHTML = "";
  target.appendChild(fragment);
}

// Close tooltips when clicking anywhere else
document.addEventListener("click", (e) => {
  if (!e.target.closest(".info-icon")) {
    document.querySelectorAll(".info-tooltip").forEach(t => t.style.display = "none");
  }
});

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeAttr(str) {
  // Tooltip content is displayed as HTML; we escape to avoid breaking markup.
  // Preserve newlines via CSS (white-space: pre-wrap).
  return escapeHtml(str);
}

// Debounce function to limit how often a function can run
function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

// Search functionality with debounce for performance
const debouncedRenderTracks = debounce(() => {
  renderTracks();
}, 250);

searchInput.addEventListener("input", (e) => {
  searchQuery = e.target.value.trim();
  searchClear.style.display = searchQuery ? "flex" : "none";
  debouncedRenderTracks();
});

searchClear.addEventListener("click", () => {
  searchInput.value = "";
  searchQuery = "";
  searchClear.style.display = "none";
  renderTracks();
  searchInput.focus();
});

// Allow Escape key to clear search
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    searchInput.value = "";
    searchQuery = "";
    searchClear.style.display = "none";
    renderTracks();
  }
});

// Keyboard shortcuts (Spotify-like)
document.addEventListener("keydown", (e) => {
  // Don't trigger shortcuts when user is typing in input fields
  const activeElement = document.activeElement;
  const isInputFocused = activeElement && (
    activeElement.tagName === "INPUT" ||
    activeElement.tagName === "TEXTAREA" ||
    activeElement.isContentEditable
  );
  
  // Allow Escape to work in search input
  if (isInputFocused && e.key !== "Escape") {
    return;
  }
  
  // Prevent default for space to avoid page scroll
  if (e.key === " " && !isInputFocused) {
    e.preventDefault();
    playPause();
    return;
  }
  
  // Arrow keys for navigation and volume
  if (e.key === "ArrowLeft") {
    e.preventDefault();
    playPrevious();
    return;
  }
  
  if (e.key === "ArrowRight") {
    e.preventDefault();
    playNext();
    return;
  }
  
  if (e.key === "ArrowUp") {
    e.preventDefault();
    // Increase volume by 5%
    volume = Math.min(1, volume + 0.05);
    audioPlayer.volume = volume;
    updateVolumeDisplay();
    return;
  }
  
  if (e.key === "ArrowDown") {
    e.preventDefault();
    // Decrease volume by 5%
    volume = Math.max(0, volume - 0.05);
    audioPlayer.volume = volume;
    updateVolumeDisplay();
    return;
  }
  
  // M key for mute/unmute
  if (e.key === "m" || e.key === "M") {
    e.preventDefault();
    if (volume > 0) {
      volume = 0;
      audioPlayer.volume = 0;
    } else {
      // Restore to saved volume or default to 0.7
      volume = parseFloat(localStorage.getItem('playerVolume')) || 0.7;
      audioPlayer.volume = volume;
    }
    updateVolumeDisplay();
    return;
  }
  
  // S key for shuffle toggle
  if (e.key === "s" || e.key === "S") {
    e.preventDefault();
    btnShuffle.click();
    return;
  }
  
  // R key for repeat toggle
  if (e.key === "r" || e.key === "R") {
    e.preventDefault();
    btnRepeat.click();
    return;
  }
});

// Initialize button states on page load
updateShuffleDisplay();
updateRepeatDisplay();
updateVolumeDisplay();

loadTracks();

