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
  const API_BOOTSTRAP_URL = `${window.MUSIC_CONFIG.apiBasePath}/bootstrap`;
  const DEFAULT_TITLE = "♩♫♪♬";
  const NO_TRACK_TEXT = "-";
  const VOLUME = 0.1;

  const STALL_TIMEOUT = 12000;
  const STALL_PROGRESS_SECONDS = 0.5;
  const COMMAND_POLL_INTERVAL = 750;

  const DEBUG =
    new URLSearchParams(window.location.search).get("debug") === "1";

  let isUserPlaying = false;
  let hls = null;
  let generation = 0;
  let stallTimer = null;
  let rebuildTimer = null;

  let timedTitle = "";
  let timedArtist = "";
  let sourceTitle = "";
  let sourceArtist = "";
  let sourceDisplay = "";
  let apiBase = null;
  let serverClockOffsetMs = null;
  let lastCommandRevision = null;
  let commandPollTimer = null;
  let commandPollRunning = false;
  let pendingPlaybackCommand = null;
  const sourceMetadataSamples = [];

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

  function id3Display(artist, title) {
    return artist && title ? `${artist} — ${title}` : title || artist;
  }

  function decodeId3Text(bytes) {
    if (!bytes.length) return "";

    const encoding = bytes[0];
    const payload = bytes.subarray(1);
    let decoder;

    if (encoding === 1) decoder = new TextDecoder("utf-16");
    else if (encoding === 2) decoder = new TextDecoder("utf-16be");
    else if (encoding === 3) decoder = new TextDecoder("utf-8");
    else decoder = new TextDecoder("windows-1252");

    return decoder.decode(payload).replace(/[\u0000\uFFFD]+$/g, "").trim();
  }

  function sameText(left, right) {
    return String(left || "").trim().toLocaleLowerCase() ===
      String(right || "").trim().toLocaleLowerCase();
  }

  function matchesTarget(sample, target) {
    if (!target || (!target.artist && !target.title)) return false;
    return (!target.artist || sameText(sample.artist, target.artist)) &&
      (!target.title || sameText(sample.title, target.title));
  }

  function dropOldBufferAndJump(sample) {
    if (!isUserPlaying || !hls || !Number.isFinite(sample.pts)) return;

    const targetPosition = Math.max(0, sample.pts + 0.02);
    try {
      audioPlayer.currentTime = targetPosition;
    } catch (error) {
      debug("Could not seek to selected track metadata:", error);
    }

    const flushEvent = window.Hls.Events.BUFFER_FLUSHING;
    if (flushEvent) {
      try {
        hls.trigger(flushEvent, {
          startOffset: 0,
          endOffset: Math.max(0, sample.pts - 0.05),
          type: "audio"
        });
      } catch (error) {
        debug("Could not flush old HLS buffer:", error);
      }
    }

    debug("Dropped old HLS buffer; jumping to track:", sample.display, sample.pts);
    pendingPlaybackCommand = null;
  }

  function considerPlaybackHandoff(sample) {
    const command = pendingPlaybackCommand;
    if (!command || !sample.display || !Number.isFinite(sample.pts)) return;
    if (sample.pts <= audioPlayer.currentTime + 0.05) return;
    if (
      sample.serverTimeMs === null ||
      sample.serverTimeMs < command.issuedAtMs - 250
    ) return;

    const matched = command.target
      ? matchesTarget(sample, command.target)
      : sample.display !== command.baselineDisplay;

    if (matched) dropOldBufferAndJump(sample);
  }

  function parseSourceId3(input) {
    const bytes = input instanceof Uint8Array ? input : new Uint8Array(input || []);
    if (bytes.length < 10 || bytes[0] !== 0x49 || bytes[1] !== 0x44 || bytes[2] !== 0x33) return;

    const version = bytes[3];
    const tagSize =
      ((bytes[6] & 0x7f) << 21) |
      ((bytes[7] & 0x7f) << 14) |
      ((bytes[8] & 0x7f) << 7) |
      (bytes[9] & 0x7f);
    const end = Math.min(bytes.length, 10 + tagSize);
    let offset = 10;
    let changed = false;

    while (offset + 10 <= end) {
      const id = String.fromCharCode(...bytes.subarray(offset, offset + 4));
      if (!/^[A-Z0-9]{4}$/.test(id)) break;

      const size = version >= 4
        ? ((bytes[offset + 4] & 0x7f) << 21) |
          ((bytes[offset + 5] & 0x7f) << 14) |
          ((bytes[offset + 6] & 0x7f) << 7) |
          (bytes[offset + 7] & 0x7f)
        : (bytes[offset + 4] * 0x1000000) +
          (bytes[offset + 5] << 16) +
          (bytes[offset + 6] << 8) + bytes[offset + 7];

      if (size <= 0 || offset + 10 + size > end) break;
      if (id === "TIT2") {
        sourceTitle = decodeId3Text(bytes.subarray(offset + 10, offset + 10 + size));
        changed = true;
      } else if (id === "TPE1") {
        sourceArtist = decodeId3Text(bytes.subarray(offset + 10, offset + 10 + size));
        changed = true;
      }
      offset += 10 + size;
    }

    if (!changed) return;

    sourceDisplay = id3Display(sourceArtist, sourceTitle);
    if (!sourceDisplay) return;

    const sample = {
      artist: sourceArtist,
      title: sourceTitle,
      display: sourceDisplay,
      pts: Number.NaN,
      serverTimeMs: serverClockOffsetMs === null
        ? null
        : Date.now() + serverClockOffsetMs
    };
    return sample;
  }

  async function fetchPlaybackCommand() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2500);

    try {
      if (!apiBase) {
        const bootstrap = await fetch(API_BOOTSTRAP_URL, {
          cache: "no-store",
          signal: controller.signal
        });
        if (!bootstrap.ok) throw new Error(`Bootstrap HTTP ${bootstrap.status}`);
        const payload = await bootstrap.json();
        apiBase = `${window.MUSIC_CONFIG.apiBasePath}${payload.api_base}`;
      }

      const url = new URL(`${apiBase}/playback-command`, window.location.origin);
      url.searchParams.set("_ts", Date.now().toString());
      const requestStartedAt = Date.now();
      const response = await fetch(url, {
        cache: "no-store",
        signal: controller.signal,
        headers: { "Cache-Control": "no-cache, no-store, max-age=0" }
      });
      if (!response.ok) throw new Error(`Playback command HTTP ${response.status}`);
      const payload = await response.json();
      const requestFinishedAt = Date.now();
      if (Number.isFinite(Number(payload.server_time_ms))) {
        serverClockOffsetMs = Number(payload.server_time_ms) -
          ((requestStartedAt + requestFinishedAt) / 2);
      }
      return payload;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function pollPlaybackCommand(prime = false) {
    if (commandPollRunning || !isUserPlaying) return false;
    commandPollRunning = true;

    try {
      const command = await fetchPlaybackCommand();
      const revision = Number(command.revision) || 0;

      if (lastCommandRevision === null || prime) {
        lastCommandRevision = revision;
        return true;
      }
      if (revision <= lastCommandRevision) return true;

      lastCommandRevision = revision;
      const issuedAtMs = Number(command.issued_at_ms) || Number(command.server_time_ms) || Date.now();
      let baselineDisplay = sourceDisplay;
      for (let index = sourceMetadataSamples.length - 1; index >= 0; index -= 1) {
        const sample = sourceMetadataSamples[index];
        if (sample.serverTimeMs !== null && sample.serverTimeMs <= issuedAtMs) {
          baselineDisplay = sample.display;
          break;
        }
      }
      pendingPlaybackCommand = {
        action: command.action,
        target: command.target,
        issuedAtMs,
        baselineDisplay: baselineDisplay || id3Display(timedArtist, timedTitle)
      };
      debug("Playback handoff requested:", pendingPlaybackCommand);

      // The selected fragment can arrive between the command and this poll.
      for (const sample of sourceMetadataSamples) {
        considerPlaybackHandoff(sample);
        if (!pendingPlaybackCommand) break;
      }
      return true;
    } catch (error) {
      debug("Playback command poll failed:", error);
      return false;
    } finally {
      commandPollRunning = false;
    }
  }

  function schedulePlaybackCommandPoll(delay = COMMAND_POLL_INTERVAL) {
    clearTimeout(commandPollTimer);
    if (!isUserPlaying) return;

    commandPollTimer = setTimeout(async () => {
      const succeeded = await pollPlaybackCommand();
      schedulePlaybackCommandPoll(succeeded ? COMMAND_POLL_INTERVAL : 5000);
    }, delay);
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

    instance.on(window.Hls.Events.FRAG_PARSING_METADATA, (_event, data) => {
      if (hls !== instance || generation !== instanceGeneration || !isUserPlaying) {
        return;
      }

      for (const id3Sample of data.samples || []) {
        const sample = parseSourceId3(id3Sample.data);
        if (!sample) continue;

        sample.pts = Number(id3Sample.pts);
        if (Number.isFinite(sample.pts)) {
          sample.serverTimeMs = serverClockOffsetMs === null
            ? null
            : Date.now() + serverClockOffsetMs;
          sourceMetadataSamples.push(sample);
          if (sourceMetadataSamples.length > 80) sourceMetadataSamples.shift();
          considerPlaybackHandoff(sample);
        }
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
      sourceTitle = "";
      sourceArtist = "";
      sourceDisplay = "";
      sourceMetadataSamples.length = 0;
      pendingPlaybackCommand = null;
      // Snapshot the current revision before this fresh player session begins.
      void pollPlaybackCommand(true);
      await startPlayback();
      schedulePlaybackCommandPoll();
    } else {
      clearTimeout(commandPollTimer);
      commandPollTimer = null;
      pendingPlaybackCommand = null;
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
    if (document.visibilityState !== "visible") {
      clearTimeout(commandPollTimer);
      commandPollTimer = null;
      return;
    }

    if (isUserPlaying) {
      void pollPlaybackCommand();
      schedulePlaybackCommandPoll();
    }

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
