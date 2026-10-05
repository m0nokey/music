(() => {
  "use strict";

  const form = document.getElementById("download-form");
  const urlInput = document.getElementById("url");
  const modeInput = document.getElementById("mode");
  const submit = document.getElementById("submit");
  const status = document.getElementById("status");
  const currentTrack = document.getElementById("current-track");
  const metadataAudio = document.getElementById("metadata-audio");
  const skipButton = document.getElementById("skip");
  const trackSearch = document.getElementById("track-search");
  const trackList = document.getElementById("track-list");
  const trackCount = document.getElementById("track-count");

  const modeSelect = document.getElementById("mode-select");
  const modeTrigger = document.getElementById("mode-trigger");
  const modeMenu = document.getElementById("mode-menu");
  const modeValue = document.getElementById("mode-value");
  const modeArrow = modeTrigger.querySelector(".mode-arrow");
  const modeOptions = [...document.querySelectorAll(".mode-option")];

  const config = window.MUSIC_CONFIG;
  const authGate = document.getElementById("auth-gate");
  const authForm = document.getElementById("auth-form");
  const adminPassword = document.getElementById("admin-password");
  const adminApp = document.getElementById("admin-app");
  let apiBase = null;
  let tracks = [];
  let libraryLoading = false;
  let libraryPollTimer = null;
  let currentDisplay = "";
  let actionInProgress = false;
  let metadataArtist = "";
  let metadataTitle = "";
  let metadataHls = null;

  function setStatus(message) {
    status.textContent = message || "";
  }

  async function jsonRequest(url, options = {}) {
    const requestUrl = new URL(url, window.location.origin);

    requestUrl.searchParams.set(
      "_ts",
      Date.now().toString()
    );

    const response = await fetch(
      requestUrl.toString(),
      {
        cache: "no-store",
        ...options,
        headers: {
          "Cache-Control":
            "no-cache, no-store, max-age=0",
          ...(options.headers || {})
        }
      }
    );

    let payload = {};

    try {
      payload = await response.json();
    } catch (_) {
      // HTTP status handled below.
    }

    if (!response.ok) {
      const error = new Error(
        payload.error ||
        `HTTP ${response.status}`
      );
      error.status = response.status;
      error.attemptsRemaining = payload.attempts_remaining;
      error.retryAfter = payload.retry_after;
      if (response.status === 401 && !requestUrl.pathname.endsWith("/auth/login")) {
        showAuth();
      }
      throw error;
    }

    return payload;
  }

  async function bootstrap() {
    if (!config || !config.apiBasePath || !config.hlsBasePath) {
      throw new Error("Radio runtime configuration is missing");
    }
    const payload = await jsonRequest(`${config.apiBasePath}/bootstrap`);
    apiBase = `${config.apiBasePath}${payload.api_base}`;
  }

  async function ensureApi() {
    if (!apiBase) {
      await bootstrap();
    }
  }

  function showAuth() {
    authGate.hidden = false;
    adminApp.hidden = true;
  }

  function showAdmin() {
    authGate.hidden = true;
    adminApp.hidden = false;
    startMetadataReader();
    refreshLibrary().catch((error) => setStatus(`LIBRARY ERROR: ${error.message}`));
  }

  function metadataText(value) {
    if (typeof value === "string") return value.trim();
    if (Array.isArray(value)) return value.map(metadataText).filter(Boolean).join(" / ");
    if (value && typeof value === "object") return metadataText(value.value ?? value.text ?? value.data ?? "");
    return "";
  }

  function applyMetadata(frame) {
    if (!frame || typeof frame !== "object") return;
    const key = String(frame.key ?? frame.id ?? frame.type ?? "").toUpperCase();
    const value = metadataText(frame.data ?? frame.value ?? frame.text ?? "");
    if (!value) return;
    if (key === "TIT2" || key === "TITLE") metadataTitle = value;
    else if (key === "TPE1" || key === "ARTIST") metadataArtist = value;
    else return;
    currentDisplay = metadataArtist && metadataTitle
      ? `${metadataArtist} — ${metadataTitle}`
      : metadataTitle || metadataArtist;
    if (currentTrack.textContent === currentDisplay) return;
    currentTrack.textContent = currentDisplay || "-";
    renderLibrary();
  }

  function decodeId3Text(bytes) {
    if (!bytes.length) return "";
    const encoding = bytes[0];
    let decoder;
    let payload = bytes.subarray(1);
    if (encoding === 1) {
      decoder = new TextDecoder("utf-16");
    } else if (encoding === 2) {
      decoder = new TextDecoder("utf-16be");
    } else if (encoding === 3) {
      decoder = new TextDecoder("utf-8");
    } else {
      decoder = new TextDecoder("windows-1252");
    }
    return decoder.decode(payload).replace(/[\u0000\uFFFD]+$/g, "").trim();
  }

  function parseId3Tag(input) {
    const bytes = input instanceof Uint8Array ? input : new Uint8Array(input || []);
    if (bytes.length < 10 || bytes[0] !== 0x49 || bytes[1] !== 0x44 || bytes[2] !== 0x33) return;
    const version = bytes[3];
    const tagSize = ((bytes[6] & 0x7f) << 21) | ((bytes[7] & 0x7f) << 14) | ((bytes[8] & 0x7f) << 7) | (bytes[9] & 0x7f);
    const end = Math.min(bytes.length, 10 + tagSize);
    let offset = 10;
    while (offset + 10 <= end) {
      const id = String.fromCharCode(...bytes.subarray(offset, offset + 4));
      if (!/^[A-Z0-9]{4}$/.test(id)) break;
      const size = version >= 4
        ? ((bytes[offset + 4] & 0x7f) << 21) | ((bytes[offset + 5] & 0x7f) << 14) | ((bytes[offset + 6] & 0x7f) << 7) | (bytes[offset + 7] & 0x7f)
        : (bytes[offset + 4] * 0x1000000) + (bytes[offset + 5] << 16) + (bytes[offset + 6] << 8) + bytes[offset + 7];
      if (size <= 0 || offset + 10 + size > end) break;
      if (id === "TIT2" || id === "TPE1") {
        applyMetadata({ key: id, data: decodeId3Text(bytes.subarray(offset + 10, offset + 10 + size)) });
      }
      offset += 10 + size;
    }
  }

  function startMetadataReader() {
    if (!window.Hls || !Hls.isSupported()) {
      currentTrack.textContent = "Timed ID3 metadata is not supported in this browser";
      return;
    }
    metadataAudio.muted = true;
    metadataAudio.volume = 0;
    metadataHls = new Hls({ enableID3MetadataCues: false, enableWorker: true, lowLatencyMode: false, liveSyncDuration: 2, liveMaxLatencyDuration: 6, maxBufferLength: 8, maxMaxBufferLength: 16 });
    metadataHls.on(Hls.Events.MEDIA_ATTACHED, () => metadataHls.loadSource(`${config.hlsBasePath}/aac.m3u8`));
    metadataHls.on(Hls.Events.FRAG_PARSING_METADATA, (_event, data) => {
      for (const sample of data.samples || []) parseId3Tag(sample.data);
    });
    metadataHls.on(Hls.Events.MANIFEST_PARSED, () => {
      metadataAudio.play().catch((error) => console.warn("Metadata reader could not start:", error));
    });
    metadataHls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal && data.type === Hls.ErrorTypes.NETWORK_ERROR) metadataHls.startLoad();
      else if (data.fatal && data.type === Hls.ErrorTypes.MEDIA_ERROR) metadataHls.recoverMediaError();
      else if (data.fatal) console.error("HLS metadata reader failed:", data.details);
    });
    metadataHls.attachMedia(metadataAudio);
  }

  function filteredTracks() {
    const query =
      trackSearch.value
        .trim()
        .toLocaleLowerCase();

    if (!query) {
      return tracks;
    }

    return tracks.filter(
      (track) => {
        const haystack =
          `${
            track.display || ""
          } ${
            track.filename || ""
          }`
            .toLocaleLowerCase();

        return haystack.includes(
          query
        );
      }
    );
  }

  function renderLibrary() {
    const visibleTracks =
      filteredTracks();

    trackList.replaceChildren();

    trackCount.textContent =
      `${visibleTracks.length} / ${tracks.length}`;

    if (!visibleTracks.length) {
      const empty =
        document.createElement(
          "div"
        );

      empty.className =
        "empty";

      empty.textContent =
        trackSearch.value
          ? "no matches"
          : libraryLoading
            ? "loading library..."
            : "library is empty";

      trackList.append(
        empty
      );

      return;
    }

    visibleTracks.forEach(
      (track, index) => {
        const row =
          document.createElement(
            "div"
          );

        row.className =
          "track-row";

        const isCurrent =
          currentDisplay &&
          track.display ===
            currentDisplay;

        if (isCurrent) {
          row.classList.add(
            "is-current"
          );
        }

        const number =
          document.createElement(
            "div"
          );

        number.className =
          "track-index";

        number.textContent =
          String(index + 1)
            .padStart(2, "0");

        const main =
          document.createElement(
            "div"
          );

        main.className =
          "track-main";

        const title =
          document.createElement(
            "div"
          );

        title.className =
          "track-title";

        title.textContent =
          track.display ||
          track.filename;

        const file =
          document.createElement(
            "div"
          );

        file.className =
          "track-file";

        file.textContent =
          track.filename;

        main.append(
          title,
          file
        );

        const next =
          document.createElement(
            "button"
          );

        next.type =
          "button";

        next.className =
          "next-button";

        next.textContent =
          "NEXT";

        next.disabled =
          actionInProgress;

        next.addEventListener(
          "click",
          () => {
            playNext(
              track.filename,
              next
            );
          }
        );

        row.append(
          number,
          main,
          next
        );

        trackList.append(
          row
        );
      }
    );
  }

  async function refreshLibrary() {
    clearTimeout(libraryPollTimer);
    libraryPollTimer = null;
    await ensureApi();

    const payload =
      await jsonRequest(
        `${apiBase}/tracks`
      );

    tracks =
      Array.isArray(
        payload.tracks
      )
        ? payload.tracks
        : [];
    libraryLoading = payload.loading === true;

    renderLibrary();

    if (payload.loading) {
      libraryPollTimer = setTimeout(() => {
        refreshLibrary().catch((error) => {
          setStatus(`LIBRARY ERROR: ${error.message}`);
        });
      }, 1000);
    }
  }

  async function playNext(
    filename,
    button
  ) {
    if (
      !filename ||
      actionInProgress
    ) {
      return;
    }

    actionInProgress = true;

    renderLibrary();

    if (button) {
      button.disabled = true;
    }

    try {
      await ensureApi();

      const result =
        await jsonRequest(
          `${apiBase}/play-next`,
          {
            method: "POST",

            headers: {
              "Content-Type":
                "application/json"
            },

            body:
              JSON.stringify({
                filename
              })
          }
        );

      setStatus(
        result.message ||
        "selected track is starting"
      );


    } catch (error) {
      setStatus(
        `ERROR: ${error.message}`
      );

    } finally {
      actionInProgress = false;

      renderLibrary();
    }
  }

  skipButton.addEventListener(
    "click",
    async () => {
      if (actionInProgress) {
        return;
      }

      actionInProgress = true;

      skipButton.disabled =
        true;

      renderLibrary();

      try {
        await ensureApi();

        const result =
          await jsonRequest(
            `${apiBase}/skip`,
            {
              method: "POST"
            }
          );

        setStatus(
          result.message ||
          "current track skipped"
        );


      } catch (error) {
        setStatus(
          `ERROR: ${error.message}`
        );

      } finally {
        actionInProgress =
          false;

        skipButton.disabled =
          false;

        renderLibrary();
      }
    }
  );

  trackSearch.addEventListener(
    "input",
    renderLibrary
  );

  function openModeMenu() {
    modeMenu.hidden = false;

    modeTrigger.setAttribute(
      "aria-expanded",
      "true"
    );

    modeArrow.textContent = "^";
  }

  function closeModeMenu() {
    modeMenu.hidden = true;

    modeTrigger.setAttribute(
      "aria-expanded",
      "false"
    );

    modeArrow.textContent = "v";
  }

  modeTrigger.addEventListener(
    "click",
    () => {
      if (modeMenu.hidden) {
        openModeMenu();
      } else {
        closeModeMenu();
      }
    }
  );

  modeOptions.forEach(
    (option) => {
      option.addEventListener(
        "click",
        () => {
          modeInput.value =
            option.dataset.value;

          modeValue.textContent =
            option.textContent.trim();

          modeOptions.forEach(
            (item) => {
              const selected =
                item === option;

              item.classList.toggle(
                "is-selected",
                selected
              );

              item.setAttribute(
                "aria-selected",
                selected
                  ? "true"
                  : "false"
              );
            }
          );

          closeModeMenu();

          modeTrigger.focus();
        }
      );
    }
  );

  document.addEventListener(
    "click",
    (event) => {
      if (
        !modeSelect.contains(
          event.target
        )
      ) {
        closeModeMenu();
      }
    }
  );

  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Escape") {
        closeModeMenu();
      }
    }
  );

  async function pollJob(
    jobId
  ) {
    for (;;) {
      const job =
        await jsonRequest(
          `${apiBase}/jobs/${
            encodeURIComponent(
              jobId
            )
          }`
        );

      setStatus(
        `JOB ${String(
          job.status || ""
        ).toUpperCase()}: ${
          job.message ||
          "processing"
        }`
      );

      if (
        job.status ===
          "completed" ||
        job.status ===
          "failed"
      ) {
        return job;
      }

      await new Promise(
        (resolve) => {
          setTimeout(
            resolve,
            2000
          );
        }
      );
    }
  }

  form.addEventListener(
    "submit",
    async (event) => {
      event.preventDefault();

      submit.disabled = true;

      setStatus(
        "SUBMITTING..."
      );

      try {
        await ensureApi();

        const job =
          await jsonRequest(
            `${apiBase}/jobs`,
            {
              method: "POST",

              headers: {
                "Content-Type":
                  "application/json"
              },

              body:
                JSON.stringify({
                  url:
                    urlInput.value
                      .trim(),

                  mode:
                    modeInput.value
                })
            }
          );

        urlInput.value =
          "";

        const completed =
          await pollJob(
            job.job_id
          );

        if (
          completed.status ===
          "completed"
        ) {
          await refreshLibrary();
        }

      } catch (error) {
        setStatus(
          `ERROR: ${error.message}`
        );

      } finally {
        submit.disabled =
          false;
      }
    }
  );

  authForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    adminPassword.disabled = true;
    try {
      await ensureApi();
      await jsonRequest(`${config.apiBasePath}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: adminPassword.value })
      });
      adminPassword.value = "";
      showAdmin();
    } catch (error) {
      // Keep the gate visually minimal; the server still enforces lockout.
    } finally {
      adminPassword.disabled = false;
      if (!adminApp.hidden) adminPassword.blur();
      else adminPassword.focus();
    }
  });

  (async () => {
    try {
      await bootstrap();
      await jsonRequest(`${config.apiBasePath}/auth/session`);
      showAdmin();
    } catch (error) {
      showAuth(error.status === 401 ? "password" : error.message);
    }
  })();

})();
