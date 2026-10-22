// ==UserScript==
// @name         FUT card list refresh
// @namespace    contrapunctus-xii
// @version      1.0
// @description  One click on fut.gg: saves the special-card list pages, looks up holo/untradable info and downloads bundle.json (commit it to the repo; GitHub rebuilds the card list).
// @match        https://www.fut.gg/*
// @grant        GM_xmlhttpRequest
// @connect      r2.fut.gg
// @run-at       document-idle
// ==/UserScript==

(() => {
  "use strict";

  // ---- settings -------------------------------------------------------------
  const LIST_URL = "https://www.fut.gg/api/fut/players/v2/27/?page={p}&quality_id=10&response_format=card-image";
  const DEF_URL = "https://www.fut.gg/api/fut/players/v2/definition-data/?game=27&slugs=";
  const R2 = "https://r2.fut.gg/27/";
  const HOLO_ID_FLOOR = 60_000_000;         // holo and newer special cards have ids in this range
  const MAX_TOTAL = 2000;                   // the special list is ~500; far more = filter ignored
  const PAGE_DELAY_MS = 2000;
  const DEF_DELAY_MS = 1500;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let running = false;

  // ---- tiny UI ----------------------------------------------------------------
  const box = document.createElement("div");
  box.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;font:13px system-ui,sans-serif;" +
    "max-width:360px;color:#eee;";
  const btn = document.createElement("button");
  btn.textContent = "🔄 Refresh cards";
  btn.style.cssText = "display:block;margin-left:auto;padding:9px 14px;border:0;border-radius:8px;" +
    "background:#7c3aed;color:#fff;font-weight:600;cursor:pointer;box-shadow:0 2px 8px #0006;";
  const logEl = document.createElement("div");
  logEl.style.cssText = "display:none;margin-top:8px;padding:10px;border-radius:8px;background:#111d;" +
    "max-height:240px;overflow:auto;white-space:pre-wrap;line-height:1.4;";
  box.append(logEl, btn);
  document.body.appendChild(box);

  function log(msg, replaceLast = false) {
    logEl.style.display = "block";
    const lines = logEl.textContent ? logEl.textContent.split("\n") : [];
    if (replaceLast && lines.length) lines.pop();
    lines.push(msg);
    logEl.textContent = lines.join("\n");
    logEl.scrollTop = logEl.scrollHeight;
    console.log("[card refresh]", msg);
  }

  // ---- helpers ------------------------------------------------------------------
  function gm(method, url, { headers = {}, body } = {}) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method, url, headers, data: body, timeout: 120000,
        onload: (r) => resolve({ status: r.status, text: r.responseText }),
        onerror: () => reject(new Error("network error: " + url)),
        ontimeout: () => reject(new Error("timeout: " + url)),
      });
    });
  }

  async function getJsonFut(url) {                 // same-origin call from the fut.gg page
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 160)}`);
    return r.json();
  }

  async function getJsonR2(url) {
    const r = await gm("GET", url);
    if (r.status !== 200) throw new Error(`${r.status} for ${url}`);
    return JSON.parse(r.text);
  }

  // ---- the three stages ----------------------------------------------------------
  async function fetchListPages() {
    const pages = [];
    let page = 1;
    for (;;) {
      const j = await getJsonFut(LIST_URL.replace("{p}", page));
      if (j.currentPage !== page) throw new Error(`asked for page ${page}, got ${j.currentPage}`);
      if ((j.total || 0) > MAX_TOTAL) throw new Error(`server reports ${j.total} cards: the special-card filter was not applied`);
      pages.push(j);
      log(`list page ${page} (${j.data.length} cards, total ${j.total})`, page > 1);
      if (!j.next || j.next <= page) break;
      page = j.next;
      await sleep(PAGE_DELAY_MS);
    }
    return pages;
  }

  async function priceIndexIds() {
    const manifest = await getJsonR2(R2 + "manifest.json");
    const idx = await getJsonR2(`${R2}player-prices-index.v1.${manifest["player-prices-index"]}.json`);
    const ids = [idx.id0];
    for (const gap of idx.d) ids.push(ids[ids.length - 1] + gap);
    return ids;
  }

  async function lookupDefs(ids) {
    const out = [];
    let size = 25, i = 0;
    while (i < ids.length) {
      const batch = ids.slice(i, i + size);
      const r = await fetch(DEF_URL + batch.map((x) => "27-" + x).join(","), { credentials: "same-origin" });
      if (!r.ok) {
        const t = await r.text();
        if (size > 1 && /many|limit|max/i.test(t)) { size = Math.max(1, Math.floor(size / 2)); continue; }
        throw new Error(`lookup stopped at ${i}/${ids.length}: ${r.status} ${t.slice(0, 120)}`);
      }
      const j = await r.json();
      out.push(...(j.data || []));
      i += batch.length;
      log(`holo/untradable lookup ${i}/${ids.length}`, i > batch.length);
      await sleep(DEF_DELAY_MS);
    }
    return out;
  }

  function download(bundle) {
    const blob = new Blob([JSON.stringify(bundle)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "bundle.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    return blob.size;
  }

  async function refresh() {
    if (running) return;
    running = true; btn.disabled = true; logEl.textContent = "";
    try {
      log("1/4 saving the special-card list pages…");
      const pages = await fetchListPages();
      const known = new Set(pages.flatMap((p) => p.data.map((r) => r.eaId)));
      log(`${known.size} listed cards`);

      log("2/4 reading fut.gg's price index for new ids…");
      const all = await priceIndexIds();
      const candidates = all.filter((i) => i >= HOLO_ID_FLOOR && !known.has(i));
      const ids = [...new Set([...candidates, ...known])].sort((a, b) => a - b);
      log(`${candidates.length} holo candidates + ${known.size} listed cards to look up`);

      log("3/4 looking up holo / untradable info (about a minute)…");
      const defs = await lookupDefs(ids);
      log(`lookup done: ${defs.length} records`);

      log("4/4 saving bundle.json…");
      const size = download({ created: new Date().toISOString(), pages, defs, asked: ids.length });
      log(`✅ bundle.json downloaded (${(size / 1048576).toFixed(1)} MB).\n` +
          "Now upload it to the repo root on github.com (Add file → Upload files → Commit to main). " +
          "GitHub rebuilds the card list and posts the new cards to Discord.");
    } catch (e) {
      log("❌ " + e.message);
    } finally {
      running = false; btn.disabled = false;
    }
  }

  btn.addEventListener("click", refresh);
})();
