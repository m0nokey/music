(() => {
  "use strict";

  const audioPlayer = document.getElementById("audioPlayer");
  const toggleBtn = document.getElementById("toggleBtn");
  const trackName = document.getElementById("trackName");

  if (!audioPlayer || !toggleBtn || !trackName) {
    console.error("Radio player elements are missing.");
    return;
  }

  audioPlayer.disableRemotePlayback = true;
  audioPlayer.preload = "none";

  const HLS_URL = `${window.MUSIC_CONFIG.hlsBasePath}/index.m3u8`;
  const DEFAULT_TITLE = "♩♫♪♬";
  const NO_TRACK_TEXT = "-";
  const VOLUME = 0.1;

  const STALL_TIMEOUT = 12000;
  const STALL_PROGRESS_SECONDS = 0.5;

  const DEBUG =
    new URLSearchParams(window.location.search).get("debug") === "1";

  let isUserPlaying = false;
  let hls = null;
  let generation = 0;
  let stallTimer = null;
  let rebuildTimer = null;

  let timedTitle = "";
  let timedArtist = "";

  function debug(...args) {
    if (!DEBUG) return;

    const time = new Date().toISOString().slice(11, 23);
    console.log(`[radio ${time}]`, ...args);
  }

  function setTrack(track) {
    const value =
      typeof track === "string" && track.trim() !== "—"
        ? track.trim()
        : "";

    trackName.textContent = value || NO_TRACK_TEXT;
    document.title = value || DEFAULT_TITLE;
  }

  function updateToggleButton() {
    toggleBtn.setAttribute("aria-label", isUserPlaying ? "Pause" : "Play");
    toggleBtn.setAttribute("aria-pressed", String(isUserPlaying));
    toggleBtn.innerHTML = isUserPlaying
      ? '<span class="pause-symbol">&#x23F8;&#xFE0E;</span>'
      : '<span class="play-symbol">►</span>';
  }

  function metadataValue(value) {
    if (typeof value === "string") return value.trim();

    if (Array.isArray(value)) {
      return value.map(metadataValue).filter(Boolean).join(" / ");
    }

    if (value && typeof value === "object") {
      return metadataValue(value.value ?? value.text ?? value.data ?? "");
    }

    return "";
  }

  function applyId3Frame(frame) {
    if (!frame || typeof frame !== "object") return;

    const key = String(frame.key ?? frame.id ?? frame.type ?? "")
      .toUpperCase();
    const value = metadataValue(
      frame.data ?? frame.value ?? frame.text ?? ""
    );

    if (!value) return;

    if (key === "TIT2" || key === "TITLE") {
      timedTitle = value;
    } else if (key === "TPE1" || key === "ARTIST") {
      timedArtist = value;
    } else {
      return;
    }

    const display =
      timedArtist && timedTitle
        ? `${timedArtist} — ${timedTitle}`
        : timedTitle || timedArtist;

    setTrack(display);
    debug("Timed ID3:", key, value);
  }

  function cueFrame(cue) {
    if (cue?.value) return cue.value;

    if (typeof cue?.text === "string" && cue.text) {
      try {
        return JSON.parse(cue.text);
      } catch (_) {
        return null;
      }
    }

    return null;
  }

  function processMetadataTrack(track) {
    if (!track || (track.kind !== "metadata" && track.label !== "id3")) {
      return;
    }

    track.mode = "hidden";

    track.addEventListener("cuechange", () => {
      const cues = track.activeCues;
      if (!cues) return;

      for (let i = 0; i < cues.length; i += 1) {
        applyId3Frame(cueFrame(cues[i]));
      }
    });
  }

  function bindMetadataTracks() {
    for (let i = 0; i < audioPlayer.textTracks.length; i += 1) {
      processMetadataTrack(audioPlayer.textTracks[i]);
    }
  }

  audioPlayer.textTracks.addEventListener("addtrack", (event) => {
    processMetadataTrack(event.track);
  });

  function clearRecoveryTimers() {
    clearTimeout(stallTimer);
    clearTimeout(rebuildTimer);
    stallTimer = null;
    rebuildTimer = null;
  }

  function destroyHls() {
    generation += 1;

    if (hls) {
      try {
        hls.destroy();
      } catch (_) {
        // Ignore teardown errors.
      }
      hls = null;
    }

    audioPlayer.pause();
    audioPlayer.removeAttribute("src");
    audioPlayer.load();
  }

  function stopPlayback() {
    clearRecoveryTimers();
    destroyHls();
  }

  function scheduleStallRecovery(reason) {
    if (!isUserPlaying || stallTimer !== null) return;

    const initialTime = audioPlayer.currentTime;

    stallTimer = setTimeout(() => {
      stallTimer = null;

      if (!isUserPlaying || !hls) return;

      const progressed =
        Number.isFinite(initialTime) &&
        Number.isFinite(audioPlayer.currentTime) &&
        audioPlayer.currentTime > initialTime + STALL_PROGRESS_SECONDS;

      if (progressed) {
        debug("Playback recovered without rebuild:", reason);
        return;
      }

      console.warn("HLS playback stalled; rebuilding:", reason);
      scheduleRebuild(reason, 0);
    }, STALL_TIMEOUT);
  }

  function scheduleRebuild(reason, delay = 1000) {
    if (!isUserPlaying || rebuildTimer !== null) return;

    rebuildTimer = setTimeout(() => {
      rebuildTimer = null;
      if (isUserPlaying) startPlayback(reason);
    }, delay);
  }

  function createHlsConfig() {
    return {
      debug: false,
      enableWorker: true,
      enableID3MetadataCues: true,
      preferManagedMediaSource: true,
      lowLatencyMode: false,
      liveSyncDuration: 10,
      liveMaxLatencyDuration: 20,
      maxLiveSyncPlaybackRate: 1,
      initialLiveManifestSize: 6,
      maxBufferLength: 30,
      maxMaxBufferLength: 60,
      backBufferLength: 15,
      maxBufferHole: 0.5,

      fragLoadPolicy: {
        default: {
          maxTimeToFirstByteMs: 5000,
          maxLoadTimeMs: 15000,
          timeoutRetry: {
            maxNumRetry: 8,
            retryDelayMs: 250,
            maxRetryDelayMs: 2000,
            backoff: "linear"
          },
          errorRetry: {
            maxNumRetry: 10,
            retryDelayMs: 250,
            maxRetryDelayMs: 2000,
            backoff: "linear"
          }
        }
      },

      playlistLoadPolicy: {
        default: {
          maxTimeToFirstByteMs: 5000,
          maxLoadTimeMs: 10000,
          timeoutRetry: {
            maxNumRetry: 6,
            retryDelayMs: 250,
            maxRetryDelayMs: 2000,
            backoff: "linear"
          },
          errorRetry: {
            maxNumRetry: 8,
            retryDelayMs: 250,
            maxRetryDelayMs: 2000,
            backoff: "linear"
          }
        }
      },

      manifestLoadPolicy: {
        default: {
          maxTimeToFirstByteMs: 5000,
          maxLoadTimeMs: 10000,
          timeoutRetry: {
            maxNumRetry: 6,
            retryDelayMs: 250,
            maxRetryDelayMs: 2000,
            backoff: "linear"
          },
          errorRetry: {
            maxNumRetry: 8,
            retryDelayMs: 250,
            maxRetryDelayMs: 2000,
            backoff: "linear"
          }
        }
      }
    };
  }

  async function startPlayback(reason = "user") {
    clearRecoveryTimers();
    if (!isUserPlaying) return;

    if (!window.Hls || !window.Hls.isSupported()) {
      console.error("This browser does not support hls.js playback.");
      isUserPlaying = false;
      updateToggleButton();
      return;
    }

    destroyHls();
    if (!isUserPlaying) return;

    audioPlayer.volume = VOLUME;

    const instanceGeneration = generation;
    const instance = new window.Hls(createHlsConfig());
    hls = instance;

    debug("Starting hls.js:", reason);

    instance.on(window.Hls.Events.MEDIA_ATTACHED, () => {
      if (hls !== instance || generation !== instanceGeneration || !isUserPlaying) {
        return;
      }

      instance.loadSource(HLS_URL);
    });

    instance.on(window.Hls.Events.MANIFEST_PARSED, async () => {
      if (hls !== instance || generation !== instanceGeneration || !isUserPlaying) {
        return;
      }

      try {
        await audioPlayer.play();
      } catch (error) {
        console.warn("Audio start failed:", error);
        scheduleStallRecovery("play-failed");
      }
    });

    instance.on(window.Hls.Events.ERROR, (_event, data) => {
      if (hls !== instance || generation !== instanceGeneration || !isUserPlaying) {
        return;
      }

      debug("hls.js error:", data.type, data.details, "fatal:", data.fatal);
      if (!data.fatal) return;

      if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
        try {
          instance.startLoad();
        } catch (error) {
          debug("startLoad failed:", error);
        }
        scheduleStallRecovery("fatal-network-error");
        return;
      }

      if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
        try {
          instance.recoverMediaError();
        } catch (error) {
          debug("recoverMediaError failed:", error);
        }
        scheduleStallRecovery("fatal-media-error");
        return;
      }

      scheduleRebuild("fatal-hls-error");
    });

    instance.attachMedia(audioPlayer);
  }

  toggleBtn.addEventListener("click", async () => {
    isUserPlaying = !isUserPlaying;
    updateToggleButton();

    if (isUserPlaying) {
      await startPlayback();
    } else {
      stopPlayback();
    }
  });

  audioPlayer.addEventListener("playing", () => {
    debug("Playing");
    clearTimeout(stallTimer);
    stallTimer = null;
  });

  audioPlayer.addEventListener("waiting", () => {
    debug("Waiting for HLS data");
    scheduleStallRecovery("waiting");
  });

  audioPlayer.addEventListener("stalled", () => {
    debug("HLS media stalled");
    scheduleStallRecovery("stalled");
  });

  audioPlayer.addEventListener("error", () => {
    if (!isUserPlaying) return;

    console.warn("Audio element error:", audioPlayer.error);
    scheduleStallRecovery("media-error");
  });

  audioPlayer.addEventListener("ended", () => {
    if (isUserPlaying) scheduleRebuild("unexpected-ended");
  });

  window.addEventListener("online", () => {
    if (!isUserPlaying || !hls) return;

    debug("Network online; resuming HLS loading");

    try {
      hls.startLoad();
    } catch (error) {
      debug("startLoad after reconnect failed:", error);
    }

    scheduleStallRecovery("network-online");
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;

    if (isUserPlaying && hls) {
      try {
        hls.startLoad();
      } catch (error) {
        debug("startLoad after page visible failed:", error);
      }
    }
  });

  bindMetadataTracks();
  setTrack("");
  updateToggleButton();
})();
