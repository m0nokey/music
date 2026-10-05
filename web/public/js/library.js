(() => {
  "use strict";

  const config = window.MUSIC_CONFIG;
  const grid = document.getElementById("playlist-grid");
  const homeView = document.getElementById("home-view");
  const playlistView = document.getElementById("playlist-view");
  const liveView = document.getElementById("live-view");
  const audio = document.getElementById("playlist-audio");
  const rows = document.getElementById("playlist-tracks");
  const title = document.getElementById("playlist-title");
  const art = document.getElementById("playlist-art");
  const summary = document.getElementById("playlist-summary");
  const error = document.getElementById("playlist-error");
  const toggle = document.getElementById("playlist-toggle");
  const repeat = document.getElementById("repeat-mode");
  const shuffle = document.getElementById("shuffle-mode");
  const total = document.getElementById("playlist-total");
  const seek = document.getElementById("seek-track");
  const elapsed = document.getElementById("elapsed-time");
  const remaining = document.getElementById("remaining-time");

  let apiBase;
  let activePlaylist = null;
  let playlistTracks = [];
  let currentIndex = -1;
  let repeatMode = "off";
  let isShuffle = false;
  let generation = 0;
  let seeking = false;
  let shuffleBag = [];

  const formatTime = (seconds) => {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const rest = value % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
      : `${minutes}:${String(rest).padStart(2, "0")}`;
  };

  async function request(path) {
    const response = await fetch(`${path}${path.includes("?") ? "&" : "?"}_ts=${Date.now()}`, {
      cache: "no-store",
      headers: { "Cache-Control": "no-cache" }
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  async function getApiBase() {
    if (apiBase) return apiBase;
    const bootstrap = await request(`${config.apiBasePath}/bootstrap`);
    apiBase = `${config.apiBasePath}${bootstrap.api_base}`;
    return apiBase;
  }

  function show(view) {
    homeView.hidden = view !== homeView;
    playlistView.hidden = view !== playlistView;
    liveView.hidden = view !== liveView;
  }

  function renderGrid(playlists) {
    grid.replaceChildren();
    for (const playlist of playlists) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "playlist-card";
      card.setAttribute("aria-label", `Open ${playlist.name}`);

      const cover = document.createElement("pre");
      cover.className = "playlist-cover";
      cover.textContent = playlist.ascii_art || `[ ${String(playlist.slot).padStart(2, "0")} ]`;
      const name = document.createElement("span");
      name.className = "playlist-card-name";
      name.textContent = playlist.name;
      const details = document.createElement("span");
      details.className = "playlist-card-details";
      details.textContent = `${playlist.track_count} tracks · ${formatTime(playlist.duration)}`;
      card.append(cover, name, details);
      card.addEventListener("click", () => openPlaylist(playlist.slot));
      grid.append(card);
    }

    const live = document.createElement("button");
    live.type = "button";
    live.className = "playlist-card live-card";
    const cover = document.createElement("pre");
    cover.className = "playlist-cover";
    cover.textContent = "((●))\n LIVE";
    const name = document.createElement("span");
    name.className = "playlist-card-name";
    name.textContent = "LIVE RADIO";
    const details = document.createElement("span");
    details.className = "playlist-card-details";
    details.textContent = "Live stream · community queue";
    live.append(cover, name, details);
    live.addEventListener("click", () => show(liveView));
    grid.append(live);
  }

  function renderTracks() {
    rows.replaceChildren();
    playlistTracks.forEach((track, index) => {
      const row = document.createElement("li");
      row.className = "playlist-track";
      if (index === currentIndex) row.classList.add("is-playing");
      const number = document.createElement("span");
      number.className = "playlist-track-number";
      number.textContent = index === currentIndex && !audio.paused ? "♪" : String(index + 1);
      const info = document.createElement("span");
      info.className = "playlist-track-info";
      const trackTitle = document.createElement("span");
      trackTitle.className = "playlist-track-title";
      trackTitle.textContent = track.title || track.display || track.filename;
      const artist = document.createElement("span");
      artist.className = "playlist-track-artist";
      artist.textContent = track.artist || "Unknown artist";
      info.append(trackTitle, artist);
      const duration = document.createElement("span");
      duration.className = "playlist-track-duration";
      duration.textContent = formatTime(track.duration);
      row.append(number, info, duration);
      row.addEventListener("click", () => playIndex(index));
      rows.append(row);
    });
  }

  async function openPlaylist(slot) {
    const requestGeneration = ++generation;
    error.textContent = "";
    show(playlistView);
    title.textContent = "Loading...";
    rows.replaceChildren();
    try {
      const base = await getApiBase();
      const payload = await request(`${base}/playlists/${slot}`);
      if (requestGeneration !== generation) return;
      activePlaylist = payload.playlist;
      playlistTracks = activePlaylist.tracks || [];
      currentIndex = -1;
      shuffleBag = [];
      title.textContent = activePlaylist.name;
      art.textContent = activePlaylist.ascii_art || `[ ${String(slot).padStart(2, "0")} ]`;
      summary.textContent = `${activePlaylist.track_count} tracks`;
      total.textContent = `TOTAL ${formatTime(activePlaylist.duration)}`;
      renderTracks();
      if (!playlistTracks.length) error.textContent = "This playlist is empty. Add tracks in the control panel.";
    } catch (cause) {
      if (requestGeneration === generation) error.textContent = `Could not load playlist: ${cause.message}`;
    }
  }

  async function playIndex(index) {
    if (!playlistTracks.length || index < 0 || index >= playlistTracks.length) return;
    const track = playlistTracks[index];
    currentIndex = index;
    const base = await getApiBase();
    audio.src = `${base}/media/${encodeURIComponent(track.filename)}`;
    renderTracks();
    toggle.textContent = "Ⅱ";
    toggle.setAttribute("aria-label", "Pause playlist");
    try {
      await audio.play();
      renderTracks();
    } catch (cause) {
      error.textContent = `Playback could not start: ${cause.message}`;
      toggle.textContent = "▶";
    }
  }

  function nextIndex() {
    if (!playlistTracks.length || currentIndex < 0) return -1;
    if (isShuffle) {
      if (!shuffleBag.length && repeatMode === "all") {
        shuffleBag = playlistTracks.map((_track, index) => index).filter((index) => index !== currentIndex);
        for (let index = shuffleBag.length - 1; index > 0; index -= 1) {
          const other = Math.floor(Math.random() * (index + 1));
          [shuffleBag[index], shuffleBag[other]] = [shuffleBag[other], shuffleBag[index]];
        }
      }
      return shuffleBag.shift() ?? -1;
    }
    if (currentIndex + 1 < playlistTracks.length) return currentIndex + 1;
    return repeatMode === "all" ? 0 : -1;
  }

  toggle.addEventListener("click", async () => {
    if (!playlistTracks.length) return;
    if (!audio.paused) {
      audio.pause();
      toggle.textContent = "▶";
      toggle.setAttribute("aria-label", "Play playlist");
      renderTracks();
    } else if (currentIndex < 0) {
      await playIndex(0);
    } else {
      await audio.play();
      toggle.textContent = "Ⅱ";
      toggle.setAttribute("aria-label", "Pause playlist");
      renderTracks();
    }
  });

  audio.addEventListener("ended", () => {
    const next = nextIndex();
    if (next >= 0) playIndex(next);
    else {
      toggle.textContent = "▶";
      toggle.setAttribute("aria-label", "Play playlist");
      renderTracks();
    }
  });
  audio.addEventListener("pause", renderTracks);
  audio.addEventListener("play", renderTracks);
  audio.addEventListener("timeupdate", () => {
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    if (!seeking) seek.value = duration ? String(Math.round(audio.currentTime / duration * 1000)) : "0";
    elapsed.textContent = formatTime(audio.currentTime);
    remaining.textContent = duration ? `-${formatTime(duration - audio.currentTime)}` : "0:00";
  });
  seek.addEventListener("pointerdown", () => { seeking = true; });
  seek.addEventListener("pointerup", () => { seeking = false; });
  seek.addEventListener("input", () => {
    if (Number.isFinite(audio.duration) && audio.duration > 0) {
      audio.currentTime = Number(seek.value) / 1000 * audio.duration;
    }
  });

  repeat.addEventListener("click", () => {
    repeatMode = repeatMode === "off" ? "all" : repeatMode === "all" ? "one" : "off";
    audio.loop = repeatMode === "one";
    repeat.textContent = `REPEAT: ${repeatMode.toUpperCase()}`;
    repeat.setAttribute("aria-pressed", String(repeatMode !== "off"));
  });

  shuffle.addEventListener("click", () => {
    isShuffle = !isShuffle;
    shuffleBag = isShuffle
      ? playlistTracks.map((_track, index) => index).filter((index) => index !== currentIndex)
      : [];
    for (let index = shuffleBag.length - 1; index > 0; index -= 1) {
      const other = Math.floor(Math.random() * (index + 1));
      [shuffleBag[index], shuffleBag[other]] = [shuffleBag[other], shuffleBag[index]];
    }
    shuffle.textContent = `SHUFFLE: ${isShuffle ? "ON" : "OFF"}`;
    shuffle.setAttribute("aria-pressed", String(isShuffle));
  });

  document.getElementById("playlist-back").addEventListener("click", () => show(homeView));
  document.getElementById("live-back").addEventListener("click", () => show(homeView));

  getApiBase()
    .then((base) => request(`${base}/playlists`))
    .then((payload) => renderGrid(payload.playlists || []))
    .catch((cause) => {
      grid.replaceChildren();
      const message = document.createElement("div");
      message.className = "grid-loading";
      message.textContent = `Could not load playlists: ${cause.message}`;
      grid.append(message);
    });
})();
