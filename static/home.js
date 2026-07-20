const cardTemplate = document.getElementById("card-template");
const skeletonTemplate = document.getElementById("skeleton-card-template");

function stripHtml(html) {
  if (!html) return "";
  const div = document.createElement("div");
  div.innerHTML = html;
  return div.textContent || div.innerText || "";
}

function renderSkeletons(container, count) {
  container.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    frag.appendChild(skeletonTemplate.content.cloneNode(true));
  }
  container.appendChild(frag);
}

function renderCard(media) {
  const node = cardTemplate.content.cloneNode(true);
  const a = node.querySelector(".anime-card");
  const img = node.querySelector(".card-img");
  const title = node.querySelector(".card-title");
  const meta = node.querySelector(".card-meta");
  const scoreBox = node.querySelector(".card-score");
  const scoreVal = scoreBox.querySelector("span");

  const displayTitle = media.title.english || media.title.romaji || media.title.native || "Untitled";

  a.href = `/watch/${media.id}`;
  img.src = media.coverImage?.large || "";
  img.alt = displayTitle;
  title.textContent = displayTitle;

  const metaParts = [];
  if (media.format) metaParts.push(media.format.replace(/_/g, " "));
  if (media.seasonYear) metaParts.push(media.seasonYear);
  meta.textContent = metaParts.join(" · ");

  if (media.averageScore) {
    scoreBox.classList.remove("hidden");
    scoreBox.classList.add("flex");
    scoreVal.textContent = (media.averageScore / 10).toFixed(1);
  }

  return node;
}

function renderGrid(container, items) {
  container.innerHTML = "";
  if (!items || items.length === 0) return false;
  const frag = document.createDocumentFragment();
  items.forEach((m) => frag.appendChild(renderCard(m)));
  container.appendChild(frag);
  if (window.lucide) window.lucide.createIcons();
  return true;
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function renderError(container, message) {
  container.innerHTML = `
    <div class="col-span-full flex flex-col items-center justify-center py-16 text-center text-muted-foreground gap-2">
      <i data-lucide="wifi-off" class="h-6 w-6"></i>
      <p class="text-sm">${message}</p>
    </div>`;
  if (window.lucide) window.lucide.createIcons();
}

async function loadTrending() {
  const grid = document.getElementById("trending-grid");
  renderSkeletons(grid, 12);
  try {
    const items = await fetchJSON("/anilist/trending?per_page=12");
    renderGrid(grid, items);
  } catch (e) {
    renderError(grid, "Couldn't load trending titles right now.");
  }
}

async function loadPopular() {
  const grid = document.getElementById("popular-grid");
  renderSkeletons(grid, 12);
  try {
    const items = await fetchJSON("/anilist/popular?per_page=12");
    renderGrid(grid, items);
  } catch (e) {
    renderError(grid, "Couldn't load popular titles right now.");
  }
}

// --- Search ---

const searchInput = document.getElementById("search-input");
const searchClear = document.getElementById("search-clear");
const searchSection = document.getElementById("search-results-section");
const browseView = document.getElementById("browse-view");
const searchGrid = document.getElementById("search-results-grid");
const searchEmpty = document.getElementById("search-empty");
const searchQueryLabel = document.getElementById("search-query-label");
const backToBrowse = document.getElementById("back-to-browse");

let debounceTimer = null;
let searchToken = 0;

function showBrowse() {
  searchSection.classList.add("hidden");
  browseView.classList.remove("hidden");
  searchInput.value = "";
  searchClear.classList.add("hidden");
}

async function runSearch(query) {
  const token = ++searchToken;
  searchQueryLabel.textContent = query;
  searchSection.classList.remove("hidden");
  browseView.classList.add("hidden");
  searchEmpty.classList.add("hidden");
  renderSkeletons(searchGrid, 12);

  try {
    const items = await fetchJSON(`/anilist/search?q=${encodeURIComponent(query)}&per_page=24`);
    if (token !== searchToken) return; // a newer search superseded this one
    const hasResults = renderGrid(searchGrid, items);
    searchEmpty.classList.toggle("hidden", hasResults);
  } catch (e) {
    if (token !== searchToken) return;
    renderError(searchGrid, "Search failed. Try again in a moment.");
  }
}

searchInput.addEventListener("input", () => {
  const value = searchInput.value.trim();
  searchClear.classList.toggle("hidden", value.length === 0);
  clearTimeout(debounceTimer);

  if (value.length === 0) {
    showBrowse();
    return;
  }

  debounceTimer = setTimeout(() => runSearch(value), 400);
});

searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    clearTimeout(debounceTimer);
    const value = searchInput.value.trim();
    if (value) runSearch(value);
  }
  if (e.key === "Escape") {
    showBrowse();
    searchInput.blur();
  }
});

searchClear.addEventListener("click", () => {
  showBrowse();
  searchInput.focus();
});

backToBrowse.addEventListener("click", showBrowse);

// --- Init ---
document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) window.lucide.createIcons();
  loadTrending();
  loadPopular();
});
