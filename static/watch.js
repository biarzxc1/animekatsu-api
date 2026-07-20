const ANILIST_ID = document.body.dataset.anilistId;

const el = {
  animeInfo: document.getElementById("anime-info"),
  providerPicker: document.getElementById("provider-picker"),
  providerStatus: document.getElementById("provider-status"),
  matchSection: document.getElementById("match-picker-section"),
  matchGrid: document.getElementById("match-picker-grid"),
  playerSection: document.getElementById("player-section"),
  video: document.getElementById("video-player"),
  playerOverlay: document.getElementById("player-overlay"),
  nowPlayingTitle: document.getElementById("now-playing-title"),
  nowPlayingSub: document.getElementById("now-playing-sub"),
  serverList: document.getElementById("server-list"),
  episodeList: document.getElementById("episode-list"),
  episodeCount: document.getElementById("episode-count"),
  episodeFilter: document.getElementById("episode-filter"),
  dubSubToggle: document.getElementById("dub-sub-toggle"),
  synopsisText: document.getElementById("synopsis-text"),
  fatalError: document.getElementById("fatal-error"),
  fatalErrorText: document.getElementById("fatal-error-text"),
};

const templates = {
  matchCard: document.getElementById("match-card-template"),
  episodeRow: document.getElementById("episode-row-template"),
  serverBtn: document.getElementById("server-btn-template"),
  providerBtn: document.getElementById("provider-btn-template"),
};

const state = {
  anime: null,
  providers: [],
  activeProvider: null,
  activeAlias: null,
  episodes: [],
  activeEpisode: null,
  dub: false,
  streams: [],
  activeStream: null,
  hls: null,
};

function icons() {
  if (window.lucide) window.lucide.createIcons();
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function stripHtml(html) {
  if (!html) return "";
  const div = document.createElement("div");
  div.innerHTML = html;
  return div.textContent || div.innerText || "";
}

function showFatalError(message) {
  el.fatalErrorText.textContent = message;
  el.fatalError.classList.remove("hidden");
  el.animeInfo.classList.add("hidden");
  document.getElementById("provider-picker-section").classList.add("hidden");
}

// ---------------------------------------------------------------------
// Anime info header
// ---------------------------------------------------------------------

async function loadAnimeInfo() {
  try {
    const media = await fetchJSON(`/anilist/anime/${ANILIST_ID}`);
    state.anime = media;
    renderAnimeInfo(media);
  } catch (e) {
    showFatalError("Couldn't load this title from AniList. It may not exist, or AniList may be temporarily unreachable.");
  }
}

function renderAnimeInfo(media) {
  const title = media.title.english || media.title.romaji || media.title.native || "Untitled";
  const desc = stripHtml(media.description).slice(0, 400);
  const meta = [media.format?.replace(/_/g, " "), media.seasonYear, media.status?.replace(/_/g, " "), media.episodes ? `${media.episodes} episodes` : null]
    .filter(Boolean)
    .join(" · ");

  el.animeInfo.innerHTML = `
    <div class="flex gap-4 sm:gap-5">
      <img src="${media.coverImage?.large || ""}" alt="${title}" class="h-40 sm:h-48 aspect-[2/3] object-cover rounded-lg border border-border shrink-0" />
      <div class="min-w-0 flex flex-col">
        <h1 class="text-xl sm:text-2xl font-bold tracking-tight leading-tight">${title}</h1>
        <p class="text-sm text-muted-foreground mt-1">${meta}</p>
        <div class="flex flex-wrap gap-1.5 mt-2.5">
          ${(media.genres || []).slice(0, 4).map((g) => `<span class="text-xs px-2 py-0.5 rounded-md border border-border text-muted-foreground">${g}</span>`).join("")}
        </div>
        <p class="text-sm text-muted-foreground mt-3 line-clamp-3 hidden sm:block">${desc}</p>
      </div>
    </div>`;

  document.title = `${title} — Anime Stream`;
  el.synopsisText.textContent = stripHtml(media.description) || "No synopsis available.";
}

// ---------------------------------------------------------------------
// Provider picker
// ---------------------------------------------------------------------

async function loadProviders() {
  try {
    const data = await fetchJSON("/providers");
    state.providers = data.providers || [];
    renderProviderPicker();
  } catch (e) {
    el.providerStatus.textContent = "Couldn't load the list of sources.";
  }
}

function renderProviderPicker() {
  el.providerPicker.innerHTML = "";
  state.providers.forEach((name) => {
    const node = templates.providerBtn.content.cloneNode(true);
    const btn = node.querySelector(".provider-btn");
    btn.querySelector(".provider-name").textContent = name;
    btn.dataset.provider = name;
    btn.addEventListener("click", () => selectProvider(name));
    el.providerPicker.appendChild(node);
  });
  icons();
}

function setActiveProviderButton(name) {
  el.providerPicker.querySelectorAll(".provider-btn").forEach((btn) => {
    const active = btn.dataset.provider === name;
    btn.classList.toggle("bg-foreground", active);
    btn.classList.toggle("text-background", active);
    btn.classList.toggle("border-foreground", active);
    const dot = btn.querySelector(".provider-dot");
    dot.classList.toggle("bg-background", active);
    dot.classList.toggle("bg-muted-foreground", !active);
  });
}

async function selectProvider(name) {
  state.activeProvider = name;
  state.activeAlias = null;
  state.episodes = [];
  setActiveProviderButton(name);
  el.matchSection.classList.add("hidden");
  el.playerSection.classList.add("hidden");
  el.providerStatus.innerHTML = `<span class="inline-flex items-center gap-1.5"><i data-lucide="loader-circle" class="h-3.5 w-3.5 animate-spin"></i> Searching ${name}…</span>`;
  icons();

  const title = state.anime?.title?.english || state.anime?.title?.romaji || state.anime?.title?.native;
  if (!title) {
    el.providerStatus.textContent = "No title available to search with.";
    return;
  }

  try {
    const results = await fetchJSON(`/${name}/search?q=${encodeURIComponent(title)}`);
    if (!results || results.length === 0) {
      el.providerStatus.textContent = `No matches found on ${name} for "${title}".`;
      return;
    }
    if (results.length === 1) {
      el.providerStatus.textContent = "";
      pickMatch(results[0]);
      return;
    }
    el.providerStatus.textContent = `Found ${results.length} possible matches — pick the right one:`;
    renderMatchPicker(results);
  } catch (e) {
    el.providerStatus.innerHTML = `<span class="text-destructive">${e.message}</span>`;
  }
}

function renderMatchPicker(results) {
  el.matchGrid.innerHTML = "";
  results.slice(0, 12).forEach((r) => {
    const node = templates.matchCard.content.cloneNode(true);
    const img = node.querySelector(".match-img");
    const title = node.querySelector(".match-title");
    img.src = r.imageUrl || "";
    img.alt = r.name || "";
    title.textContent = r.name || "Untitled";
    node.querySelector(".match-card").addEventListener("click", () => pickMatch(r));
    el.matchGrid.appendChild(node);
  });
  el.matchSection.classList.remove("hidden");
  icons();
}

async function pickMatch(result) {
  state.activeAlias = result.alias;
  el.matchSection.classList.add("hidden");
  await loadEpisodes();
}

// ---------------------------------------------------------------------
// Episodes
// ---------------------------------------------------------------------

async function loadEpisodes() {
  if (!state.activeProvider || !state.activeAlias) return;

  el.episodeList.innerHTML = `<div class="p-4 text-sm text-muted-foreground flex items-center gap-2"><i data-lucide="loader-circle" class="h-3.5 w-3.5 animate-spin"></i> Loading episodes…</div>`;
  icons();

  try {
    const url = `/${state.activeProvider}/episodes?alias=${encodeURIComponent(state.activeAlias)}&dub=${state.dub}`;
    const episodes = await fetchJSON(url);
    state.episodes = episodes || [];
    el.playerSection.classList.remove("hidden");
    renderEpisodeList(state.episodes);
    if (state.episodes.length > 0) {
      playEpisode(state.episodes[0]);
    } else {
      el.episodeList.innerHTML = `<div class="p-4 text-sm text-muted-foreground">No episodes found.</div>`;
    }
  } catch (e) {
    el.episodeList.innerHTML = `<div class="p-4 text-sm text-destructive">${e.message}</div>`;
  }
}

function renderEpisodeList(episodes) {
  el.episodeList.innerHTML = "";
  el.episodeCount.textContent = `${episodes.length} ep`;
  const frag = document.createDocumentFragment();

  episodes.forEach((ep) => {
    const node = templates.episodeRow.content.cloneNode(true);
    const btn = node.querySelector(".episode-row");
    btn.querySelector(".episode-number").textContent = ep.episodeNumber;
    btn.querySelector(".episode-title").textContent = ep.episodeTitle || `Episode ${ep.episodeNumber}`;
    if (ep.isFiller) btn.querySelector(".episode-filler").classList.remove("hidden");
    btn.dataset.episodeLink = ep.episodeLink;
    btn.addEventListener("click", () => playEpisode(ep));
    frag.appendChild(node);
  });

  el.episodeList.appendChild(frag);
  icons();
  highlightActiveEpisode();
}

function highlightActiveEpisode() {
  el.episodeList.querySelectorAll(".episode-row").forEach((btn) => {
    const active = state.activeEpisode && btn.dataset.episodeLink === state.activeEpisode.episodeLink;
    btn.classList.toggle("bg-accent", !!active);
  });
}

el.episodeFilter.addEventListener("input", () => {
  const q = el.episodeFilter.value.trim().toLowerCase();
  el.episodeList.querySelectorAll(".episode-row").forEach((btn) => {
    const text = btn.textContent.toLowerCase();
    btn.classList.toggle("hidden", q.length > 0 && !text.includes(q));
  });
});

// ---------------------------------------------------------------------
// Streams + player
// ---------------------------------------------------------------------

async function playEpisode(ep) {
  state.activeEpisode = ep;
  highlightActiveEpisode();

  const title = state.anime?.title?.english || state.anime?.title?.romaji || "";
  el.nowPlayingTitle.textContent = `Episode ${ep.episodeNumber}${ep.episodeTitle ? ` — ${ep.episodeTitle}` : ""}`;
  el.nowPlayingSub.textContent = title;

  el.serverList.innerHTML = "";
  setPlayerLoading(true, "Fetching stream links…");

  try {
    const params = new URLSearchParams({
      episode: ep.episodeLink,
      dub: String(state.dub),
    });
    if (ep.metadata) params.set("metadata", ep.metadata);

    const streams = await fetchJSON(`/${state.activeProvider}/streams?${params.toString()}`);
    state.streams = streams || [];

    if (state.streams.length === 0) {
      setPlayerLoading(false);
      el.serverList.innerHTML = `<p class="text-sm text-muted-foreground">No playable servers found for this episode.</p>`;
      return;
    }

    renderServerList(state.streams);
    // loadStream handles its own playback errors internally (via the
    // overlay) rather than throwing, so a player quirk never wipes out
    // the server list we just rendered above.
    loadStream(state.streams[0]);
  } catch (e) {
    setPlayerLoading(false);
    el.serverList.innerHTML = `<p class="text-sm text-destructive">${e.message}</p>`;
  }
}

function renderServerList(streams) {
  el.serverList.innerHTML = "";
  streams.forEach((stream, idx) => {
    const node = templates.serverBtn.content.cloneNode(true);
    const btn = node.querySelector(".server-btn");
    btn.querySelector(".server-name").textContent = `${stream.server} · ${stream.quality}`;
    btn.dataset.index = String(idx);
    btn.addEventListener("click", () => {
      loadStream(stream);
      setActiveServerButton(idx);
    });
    el.serverList.appendChild(node);
  });
  setActiveServerButton(0);
  icons();
}

function setActiveServerButton(activeIdx) {
  el.serverList.querySelectorAll(".server-btn").forEach((btn) => {
    const active = Number(btn.dataset.index) === activeIdx;
    btn.classList.toggle("bg-foreground", active);
    btn.classList.toggle("text-background", active);
    btn.classList.toggle("border-foreground", active);
  });
}

function setPlayerLoading(isLoading, text) {
  if (isLoading) {
    el.playerOverlay.innerHTML = `
      <i data-lucide="loader-circle" class="h-4 w-4 animate-spin"></i>
      <span>${text || "Loading stream…"}</span>`;
    el.playerOverlay.classList.remove("hidden");
    icons();
  } else {
    el.playerOverlay.classList.add("hidden");
  }
}

function destroyHls() {
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
}

function safePlay() {
  // Some environments/older WebKit builds don't return a Promise from
  // play() — guard against that instead of assuming .catch() exists.
  try {
    const result = el.video.play();
    if (result && typeof result.catch === "function") {
      result.catch(() => {});
    }
  } catch (_e) {
    // Autoplay can be blocked by the browser; that's fine, the user can
    // hit play manually. Nothing to surface as an error here.
  }
}

function loadStream(stream) {
  state.activeStream = stream;
  setPlayerLoading(true, "Loading stream…");

  const onFail = () => {
    setPlayerLoading(false);
    const overlay = el.playerOverlay;
    overlay.classList.remove("hidden");
    overlay.innerHTML = `
      <div class="flex flex-col items-center gap-2 px-4 text-center">
        <i data-lucide="circle-alert" class="h-5 w-5"></i>
        <span class="text-sm">This server failed to load. Try another server below.</span>
      </div>`;
    icons();
  };

  try {
    destroyHls();
    el.video.removeAttribute("src");
    el.video.load();

    const isM3u8 = stream.url.includes(".m3u8");
    const onReady = () => setPlayerLoading(false);

    if (isM3u8 && window.Hls && window.Hls.isSupported()) {
      // Note: some providers attach customHeaders (Referer/Origin) to stream
      // URLs that browsers won't let JS set on cross-origin video/XHR requests.
      // Streams that need those headers may fail here even though the URL is
      // valid — that's a browser CORS limitation, not a bug in this fetch.
      const hls = new window.Hls();
      state.hls = hls;
      hls.loadSource(stream.url);
      hls.attachMedia(el.video);
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        onReady();
        safePlay();
      });
      hls.on(window.Hls.Events.ERROR, (_evt, data) => {
        if (data.fatal) {
          onFail();
        }
      });
    } else {
      // native HLS (Safari) or a direct mp4/dash-ish URL
      el.video.src = stream.url;
      el.video.addEventListener("loadedmetadata", onReady, { once: true });
      el.video.addEventListener("error", onFail, { once: true });
      safePlay();
    }

    if (stream.subtitle) {
      const existingTrack = el.video.querySelector("track");
      if (existingTrack) existingTrack.remove();
      const track = document.createElement("track");
      track.kind = "subtitles";
      track.label = "English";
      track.srclang = "en";
      track.src = stream.subtitle;
      track.default = true;
      el.video.appendChild(track);
    }
  } catch (_e) {
    // Any unexpected playback-setup error surfaces via the overlay rather
    // than bubbling up into playEpisode's catch block, which would
    // otherwise wipe out the already-rendered server list.
    onFail();
  }
}

// ---------------------------------------------------------------------
// Dub / Sub toggle
// ---------------------------------------------------------------------

function setDubSubUI() {
  el.dubSubToggle.querySelectorAll(".dub-sub-btn").forEach((btn) => {
    const active = (btn.dataset.mode === "dub") === state.dub;
    btn.classList.toggle("bg-background", active);
    btn.classList.toggle("shadow-sm", active);
    btn.classList.toggle("text-foreground", active);
    btn.classList.toggle("text-muted-foreground", !active);
  });
}

el.dubSubToggle.querySelectorAll(".dub-sub-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const wantsDub = btn.dataset.mode === "dub";
    if (wantsDub === state.dub) return;
    state.dub = wantsDub;
    setDubSubUI();
    if (state.activeProvider && state.activeAlias) {
      await loadEpisodes();
    }
  });
});

// ---------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
  icons();
  setDubSubUI();
  await loadAnimeInfo();
  await loadProviders();
});
