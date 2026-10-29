/* Coin Book: reads data/catalog.json (what slots exist) and
   data/collection.json (which slots are filled), and renders the album.
   Routes live in the URL hash:
     #                         countries
     #us                       denominations of a country
     #us/cent                  series of a denomination
     #us/cent/wheat            the folder board
     #us/cent/wheat/1909-s     the folder board with that coin open in the viewer */
(function () {
  "use strict";

  const SIDE_NAMES = { obverse: "Obverse", reverse: "Reverse", edge: "Edge" };
  const view = document.getElementById("view");
  const crumbs = document.getElementById("crumbs");
  let catalog, collection, osd;
  let filledOnly = false;
  let showVarieties = true;
  const viewerState = { key: null, side: null, order: [] };

  // ---------- data ----------

  function slugify(text) {
    return String(text).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }

  // Same expansion as tools/add_coin.py, so ids always agree.
  function expandSlots(series) {
    const out = [];
    for (const s of series.slots || []) {
      if (typeof s === "string") out.push({ id: slugify(s), label: s, sub: "" });
      else if (s.range) {
        for (let y = s.range[0]; y <= s.range[1]; y++) {
          for (const m of s.mints || [""]) {
            const label = m ? `${y}-${m}` : String(y);
            out.push({ id: slugify(label), label, sub: "" });
          }
        }
      } else out.push({ id: s.id || slugify(s.label), label: s.label, sub: s.sub || "", variety: !!s.variety });
    }
    return out;
  }

  function imageUrl(path) {
    if (!path) return "";
    if (/^(https?:)?\/\//.test(path)) return path;
    return (collection.imageBase || "") + path;
  }

  function coinAt(key) { return collection.coins[key]; }

  function firstSide(coin) {
    for (const s of ["obverse", "reverse", "edge"]) if (coin.sides && coin.sides[s]) return s;
    return Object.keys(coin.sides || {})[0];
  }

  function thumbOf(coin) {
    const side = coin.sides[firstSide(coin)];
    return side ? imageUrl(side.thumb || side.image) : "";
  }

  function seriesStats(country, denom, series) {
    const base = `${country.id}/${denom.id}/${series.id}`;
    const slots = expandSlots(series);
    const filled = slots.filter((s) => coinAt(`${base}/${s.id}`));
    return { total: slots.length, have: filled.length, base, slots, filled };
  }

  function sumStats(list) {
    return list.reduce((a, s) => ({ total: a.total + s.total, have: a.have + s.have }), { total: 0, have: 0 });
  }

  function findPath(parts) {
    const country = catalog.countries.find((c) => c.id === parts[0]);
    const denom = country && parts[1] ? country.denominations.find((d) => d.id === parts[1]) : null;
    const series = denom && parts[2] ? denom.series.find((s) => s.id === parts[2]) : null;
    return { country, denom, series };
  }

  // ---------- small helpers ----------

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(c));
    return el;
  }

  function progress(stats) {
    const pct = stats.total ? Math.round((stats.have / stats.total) * 100) : 0;
    return [
      h("span", { class: "count", text: `${stats.have} / ${stats.total}` }),
      h("span", { class: "bar", role: "img", "aria-label": `${pct}% filled` }, h("i", { style: `width:${pct}%` })),
    ];
  }

  function setCrumbs(items) {
    crumbs.replaceChildren();
    items.forEach((it, i) => {
      if (i) crumbs.append(h("span", { "aria-hidden": "true", text: "/" }));
      crumbs.append(it.href ? h("a", { href: it.href, text: it.text }) : h("span", { "aria-current": "page", text: it.text }));
    });
  }

  function denomName(d) { return d.nickname ? `${d.name} (${d.nickname})` : d.name; }

  // ---------- pages ----------

  function renderHome() {
    document.title = catalog.title || "Coin Book";
    setCrumbs([]);
    const list = h("ul", { class: "index-list" });
    for (const c of catalog.countries) {
      const all = c.denominations.flatMap((d) => d.series.map((s) => seriesStats(c, d, s)));
      const st = sumStats(all);
      list.append(h("li", {}, h("a", { href: `#${c.id}` },
        h("span", { class: "index-name", text: c.name }),
        h("span", { class: "index-meta", text: `${c.denominations.length} denomination${c.denominations.length === 1 ? "" : "s"} · ${all.length} series` }),
        h("span", { class: "index-count" }, progress(st)))));
    }
    view.replaceChildren(
      h("div", { class: "page-head" },
        h("p", { class: "eyebrow", text: "Microscope scans" }),
        h("h1", { text: catalog.title || "Coin Book" }),
        h("p", { text: "Pick a country, then a coin and its series. Every slot in a folder is a date and mint; filled slots open the full stitched scan." })),
      h("section", {}, list),
      renderRecent());
  }

  function renderRecent() {
    const entries = Object.entries(collection.coins)
      .sort((a, b) => String(b[1].updated || b[1].added || "").localeCompare(String(a[1].updated || a[1].added || "")))
      .slice(0, 8);
    const sec = h("section", { class: "recent" }, h("h2", { class: "section-title", text: "Recently added" }));
    if (!entries.length) {
      sec.append(h("p", { class: "empty-note", text: "No coins yet. Scans you export from the scanner show up here." }));
      return sec;
    }
    const row = h("div", { class: "recent-row" });
    for (const [key, coin] of entries) {
      const parts = key.split("/");
      const { country, denom, series } = findPath(parts);
      if (!series) continue;
      const slot = expandSlots(series).find((s) => s.id === parts[3]);
      if (!slot) continue;
      row.append(h("a", { href: `#${key}` },
        h("img", { src: thumbOf(coin), alt: "", loading: "lazy" }),
        h("b", { text: slot.sub ? `${slot.label} ${slot.sub}` : slot.label }),
        h("span", { text: `${series.name} · ${country.name}` })));
    }
    sec.append(row);
    return sec;
  }

  function renderCountry(country) {
    document.title = `${country.name} · ${catalog.title || "Coin Book"}`;
    setCrumbs([{ href: "#", text: "All countries" }, { text: country.name }]);
    const list = h("ul", { class: "index-list" });
    for (const d of country.denominations) {
      const st = sumStats(d.series.map((s) => seriesStats(country, d, s)));
      list.append(h("li", {}, h("a", { href: `#${country.id}/${d.id}` },
        h("span", { class: "index-name", text: denomName(d) }),
        h("span", { class: "index-meta", text: d.series.map((s) => s.name).join(" · ") }),
        h("span", { class: "index-count" }, progress(st)))));
    }
    view.replaceChildren(
      h("div", { class: "page-head" }, h("p", { class: "eyebrow", text: "Country" }), h("h1", { text: country.name })),
      h("section", {}, list));
  }

  function renderDenomination(country, denom) {
    document.title = `${country.name} ${denom.name} · ${catalog.title || "Coin Book"}`;
    setCrumbs([{ href: "#", text: "All countries" }, { href: `#${country.id}`, text: country.name }, { text: denomName(denom) }]);
    const covers = h("div", { class: "covers" });
    for (const s of denom.series) {
      const st = seriesStats(country, denom, s);
      const previews = st.filled.slice(0, 5).map((sl) => h("img", { src: thumbOf(coinAt(`${st.base}/${sl.id}`)), alt: "", loading: "lazy" }));
      covers.append(h("a", { class: "cover", href: `#${st.base}` },
        h("span", { class: "cover-dates", text: s.dates || "" }),
        h("span", { class: "cover-title", text: s.name }),
        s.description ? h("p", { class: "cover-desc", text: s.description }) : null,
        h("span", { class: "cover-foot" },
          h("span", { class: "cover-preview" }, previews),
          h("span", { class: "index-count" }, progress(st)))));
    }
    view.replaceChildren(
      h("div", { class: "page-head" }, h("p", { class: "eyebrow", text: country.name }), h("h1", { text: denomName(denom) })),
      covers);
  }

  function renderSeries(country, denom, series) {
    document.title = `${series.name} ${denom.name} · ${catalog.title || "Coin Book"}`;
    setCrumbs([
      { href: "#", text: "All countries" }, { href: `#${country.id}`, text: country.name },
      { href: `#${country.id}/${denom.id}`, text: denomName(denom) }, { text: series.name },
    ]);
    const st = seriesStats(country, denom, series);
    const grid = h("ol", { class: "slots" + (filledOnly ? " filled-only" : "") + (showVarieties ? "" : " hide-varieties") });
    const varietyCount = st.slots.filter((s) => s.variety).length;
    for (const slot of st.slots) {
      const key = `${st.base}/${slot.id}`;
      const coin = coinAt(key);
      const name = slot.sub ? `${slot.label} ${slot.sub}` : slot.label;
      const label = h("span", { class: "slot-label" }, slot.label, slot.sub ? h("span", { class: "slot-sub", text: slot.sub }) : null);
      if (coin) {
        const flags = h("span", { class: "slot-flags", "aria-hidden": "true" },
          ["obverse", "reverse"].map((s) => h("i", { class: coin.sides[s] ? "" : "off", title: SIDE_NAMES[s] })));
        grid.append(h("li", { class: "slot is-filled" + (slot.variety ? " is-variety" : "") },
          h("button", { class: "hole", type: "button", "aria-label": `Open ${name}`, onclick: () => { location.hash = key; } },
            h("img", { src: thumbOf(coin), alt: "", loading: "lazy" })),
          label, coin.sample ? h("span", { class: "sample-tag", text: "TEST IMAGE" }) : flags));
      } else {
        grid.append(h("li", { class: "slot is-empty" + (slot.variety ? " is-variety" : ""), "aria-label": `${name}${slot.variety ? " (variety)" : ""}, empty` },
          h("span", { class: "hole" }), label));
      }
    }
    const toggle = h("button", { class: "chip", type: "button", "aria-pressed": String(filledOnly) }, "Only filled slots");
    toggle.addEventListener("click", () => {
      filledOnly = !filledOnly;
      toggle.setAttribute("aria-pressed", String(filledOnly));
      grid.classList.toggle("filled-only", filledOnly);
    });
    const varToggle = h("button", { class: "chip", type: "button", "aria-pressed": String(showVarieties) },
      h("span", { class: "variety-key", "aria-hidden": "true" }), `Varieties (${varietyCount})`);
    varToggle.addEventListener("click", () => {
      showVarieties = !showVarieties;
      varToggle.setAttribute("aria-pressed", String(showVarieties));
      grid.classList.toggle("hide-varieties", !showVarieties);
    });
    const pct = st.total ? Math.round((st.have / st.total) * 100) : 0;
    view.replaceChildren(h("section", { class: "board", "aria-label": `${series.name} folder` },
      h("div", { class: "board-head" },
        h("p", { class: "eyebrow", text: `${country.name} · ${denomName(denom)}` }),
        h("h1", { text: series.name }),
        series.description ? h("p", { text: series.description }) : null,
        h("div", { class: "board-stats" },
          h("span", {}, h("b", { text: series.dates || "" })),
          h("span", {}, h("b", { text: `${st.have} of ${st.total}` }), " slots filled"),
          h("span", {}, h("b", { text: `${pct}%` }), " complete"),
          series.diameter_mm ? h("span", {}, h("b", { text: `${series.diameter_mm} mm` }), " diameter") : null)),
      h("div", { class: "board-tools" }, toggle, varietyCount ? varToggle : null),
      grid));
    viewerState.order = st.filled.map((s) => `${st.base}/${s.id}`);
  }

  function renderNotFound(hash) {
    setCrumbs([{ href: "#", text: "All countries" }]);
    view.replaceChildren(h("p", { class: "error", text: `There's no page at "${hash}". It may have been renamed in catalog.json.` }),
      h("p", {}, h("a", { href: "#", text: "Back to all countries" })));
  }

  // ---------- viewer ----------

  const viewerEl = document.getElementById("viewer");
  const sideTabs = document.getElementById("side-tabs");
  let lastFocus = null;

  function ensureOsd() {
    if (osd) return osd;
    if (!window.OpenSeadragon) return null;
    // Zoom tiles are WebP (small, keeps the transparent background); every current browser shows it.
    if (OpenSeadragon.setImageFormatsSupported) OpenSeadragon.setImageFormatsSupported({ webp: true });
    osd =OpenSeadragon({
      element: document.getElementById("osd"),
      showNavigationControl: false,
      showNavigator: false,
      visibilityRatio: 0.6,
      minZoomImageRatio: 0.6,
      maxZoomPixelRatio: 4,
      animationTime: 0.6,
      gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
      crossOriginPolicy: "Anonymous",
    });
    document.getElementById("zoom-in").onclick = () => osd.viewport.zoomBy(1.6);
    document.getElementById("zoom-out").onclick = () => osd.viewport.zoomBy(1 / 1.6);
    document.getElementById("zoom-fit").onclick = () => osd.viewport.goHome();
    document.getElementById("zoom-full").onclick = () => {
      try { osd.setFullScreen(!osd.isFullPage()); } catch (e) { /* full screen not allowed here */ }
    };
    return osd;
  }

  function openCoin(key, series, denom, country, side) {
    const coin = coinAt(key);
    const slot = expandSlots(series).find((s) => s.id === key.split("/")[3]);
    if (!coin || !slot) return closeViewer(true);
    if (viewerEl.hidden) lastFocus = document.activeElement;
    viewerState.key = key;
    viewerState.side = side && coin.sides[side] ? side : firstSide(coin);
    viewerEl.hidden = false;
    document.body.style.overflow = "hidden";

    document.getElementById("viewer-series").textContent = `${country.name} · ${denom.name} · ${series.name}`;
    document.getElementById("viewer-title").textContent = slot.sub ? `${slot.label} · ${slot.sub}` : slot.label;

    sideTabs.replaceChildren();
    for (const s of ["obverse", "reverse", "edge"]) {
      if (s === "edge" && !coin.sides.edge) continue;
      sideTabs.append(h("button", {
        type: "button", role: "tab", "aria-selected": String(s === viewerState.side), disabled: !coin.sides[s],
        title: coin.sides[s] ? "" : "Not scanned yet",
        onclick: () => openCoin(key, series, denom, country, s),
      }, SIDE_NAMES[s]));
    }

    const sideData = coin.sides[viewerState.side];
    const viewer = ensureOsd();
    if (viewer) {
      viewer.open(sideData.tiles ? imageUrl(sideData.tiles) : { type: "image", url: imageUrl(sideData.image) });
    } else {
      document.getElementById("osd").replaceChildren(h("img", { src: imageUrl(sideData.image), alt: "", style: "width:100%;height:100%;object-fit:contain" }));
    }

    const info = document.getElementById("viewer-info");
    const pxPerMm = series.diameter_mm && sideData.width ? Math.round(Math.max(sideData.width, sideData.height) / series.diameter_mm) : null;
    const item = (k, v, cls) => h("div", { class: cls || null }, h("dt", { text: k }), h("dd", {}, v));
    info.replaceChildren(...[
      coin.sample ? item("Note", "Test image from the scanner trials, not this coin. Replace it with a real scan.") : null,
      coin.grade ? item("Grade", coin.grade) : null,
      sideData.width ? item("Scan size", `${sideData.width.toLocaleString()} × ${sideData.height.toLocaleString()} px`) : null,
      pxPerMm ? item("Detail", `≈ ${pxPerMm} px per mm`) : null,
      coin.scan && coin.scan.tiles ? item("Photos stitched", String(coin.scan.tiles)) : null,
      coin.scan && coin.scan.microscope ? item("Microscope", coin.scan.microscope) : null,
      coin.added ? item("Added", coin.added) : null,
      item("Original", h("a", { href: imageUrl(sideData.image), target: "_blank", rel: "noopener", text: "Open full file" })),
      coin.notes ? item("Notes", coin.notes, "notes") : null,
    ].filter(Boolean));

    const i = viewerState.order.indexOf(key);
    document.getElementById("viewer-prev").disabled = i <= 0;
    document.getElementById("viewer-next").disabled = i < 0 || i >= viewerState.order.length - 1;
    document.getElementById("viewer-close").focus();
  }

  function closeViewer(replace) {
    if (viewerEl.hidden) return;
    viewerEl.hidden = true;
    document.body.style.overflow = "";
    if (osd) osd.close();
    viewerState.key = null;
    if (replace) {
      const parts = location.hash.replace(/^#\/?/, "").split("/");
      history.replaceState(null, "", "#" + parts.slice(0, 3).join("/"));
    }
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function step(dir) {
    const i = viewerState.order.indexOf(viewerState.key);
    const next = viewerState.order[i + dir];
    if (next) history.replaceState(null, "", "#" + next), route();
  }

  document.getElementById("viewer-close").onclick = () => {
    const parts = viewerState.key.split("/");
    location.hash = parts.slice(0, 3).join("/");
  };
  document.getElementById("viewer-prev").onclick = () => step(-1);
  document.getElementById("viewer-next").onclick = () => step(1);
  document.addEventListener("keydown", (e) => {
    if (viewerEl.hidden) return;
    if (e.key === "Escape") document.getElementById("viewer-close").click();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
  });

  // ---------- routing ----------

  let lastPage = "";
  function route() {
    const hash = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
    const parts = hash.split("/").filter(Boolean);
    const { country, denom, series } = findPath(parts);
    const page = parts.slice(0, 3).join("/");

    if (!parts.length) { closeViewer(); renderHome(); }
    else if (!country || (parts[1] && !denom) || (parts[2] && !series) || parts.length > 4) { closeViewer(); renderNotFound(hash); }
    else if (!denom) { closeViewer(); renderCountry(country); }
    else if (!series) { closeViewer(); renderDenomination(country, denom); }
    else {
      if (page !== lastPage || !view.querySelector(".board")) renderSeries(country, denom, series);
      if (parts[3]) openCoin(parts.join("/"), series, denom, country);
      else closeViewer();
    }
    if (page !== lastPage) { window.scrollTo(0, 0); if (viewerEl.hidden) view.focus({ preventScroll: true }); }
    lastPage = page;
  }

  async function load() {
    try {
      const bust = `?v=${Date.now()}`; // new scans should show up without a hard refresh
      const [cat, col] = await Promise.all([
        fetch("data/catalog.json" + bust).then((r) => { if (!r.ok) throw new Error("catalog.json: " + r.status); return r.json(); }),
        fetch("data/collection.json" + bust).then((r) => (r.ok ? r.json() : { coins: {} })),
      ]);
      catalog = cat;
      collection = Object.assign({ imageBase: "", coins: {} }, col);
      window.addEventListener("hashchange", route);
      route();
    } catch (err) {
      view.replaceChildren(h("p", { class: "error", text: `Couldn't load the album data (${err.message}). If you opened index.html straight from disk, run a local server instead: python -m http.server` }));
    }
  }

  load();
})();
