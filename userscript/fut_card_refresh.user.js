// ==UserScript==
// @name         FUT card list refresh
// @namespace    contrapunctus-xii
// @version      1.3
// @description  fut.gg helper: (1) Refresh cards: saves the card list + holo lookup into bundle.json; (2) Bought: collect cards you bought (price + optional sell target) and export purchased.json; (3) live bid badge on player pages (max bid for +3/+5/+8% net, from the scanner's sold prices).
// @match        https://www.fut.gg/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_setClipboard
// @connect      r2.fut.gg
// @connect      raw.githubusercontent.com
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
  const row = document.createElement("div");
  row.style.cssText = "display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;";
  const mk = (text, bg) => {
    const b = document.createElement("button");
    b.textContent = text;
    b.style.cssText = `padding:9px 14px;border:0;border-radius:8px;background:${bg};color:#fff;` +
      "font-weight:600;cursor:pointer;box-shadow:0 2px 8px #0006;";
    return b;
  };
  const addBtn = mk("➕ Bought", "#059669");
  const listBtn = mk("📋 Purchased (0)", "#2563eb");
  btn.style.cssText = btn.style.cssText.replace("display:block;margin-left:auto;", "");
  row.append(listBtn, addBtn, btn);
  box.append(logEl, row);
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

  // ---- purchased list ---------------------------------------------------------------
  // Stored in Tampermonkey: { "<card id or name text>": { price: number, target?: number, name: string } }
  const load = () => { try { return JSON.parse(GM_getValue("purchased", "{}")) || {}; } catch (_) { return {}; } };
  const save = (o) => { GM_setValue("purchased", JSON.stringify(o)); listBtn.textContent = `📋 Purchased (${Object.keys(o).length})`; };
  const fmt = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2).replace(/\.?0+$/, "") + "M" : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k" : String(n));

  function parsePrice(t) {
    const m = String(t || "").trim().toLowerCase().replace(/[, ]/g, "").match(/^(\d+(?:\.\d+)?)([km]?)$/);
    if (!m) return null;
    return Math.round(parseFloat(m[1]) * (m[2] === "m" ? 1e6 : m[2] === "k" ? 1e3 : 1));
  }

  function purchasedJson() {                          // the format the scanner reads
    const out = {};
    for (const [k, v] of Object.entries(load())) out[k] = v.target ? { price: v.price, target: v.target, name: v.name } : { price: v.price, name: v.name };
    return JSON.stringify(out, null, 2) + "\n";
  }

  function addCard() {
    const m = location.pathname.match(/\/27-(\d+)\/?/);
    if (!m) { log("Open a card's own page first (address like …/players/…/27-12345/)."); return; }
    const id = m[1];
    const name = (document.querySelector("h1")?.textContent || document.title.split(" - ")[0] || id).trim().replace(/\s+/g, " ");
    const cur = load();
    const old = cur[id] ? ` (currently ${fmt(cur[id].price)})` : "";
    const price = parsePrice(prompt(`Price you bought "${name}" for${old}?\n(e.g. 150000, 150k, 1.2m)`));
    if (!price) { log("Cancelled (no valid price)."); return; }
    const oldT = cur[id]?.target ? ` (currently ${fmt(cur[id].target)})` : "";
    const tIn = prompt(`Sell target for "${name}"?${oldT}\n(e.g. 445k — leave empty to let the scanner use fair value)`);
    const target = parsePrice(tIn);
    cur[id] = target ? { price, target, name } : { price, name };
    save(cur);
    log(`✅ ${name} @ ${fmt(price)}${target ? " → target " + fmt(target) : ""} added (${Object.keys(cur).length} in the list).`);
  }

  function showList() {
    const cur = load(), keys = Object.keys(cur);
    logEl.style.display = "block";
    logEl.textContent = "";
    const head = document.createElement("div");
    head.textContent = keys.length ? "Purchased list (✖ = sold / remove):" : "Purchased list is empty. Open a card page and click ➕ Bought.";
    logEl.append(head);
    for (const k of keys) {
      const d = document.createElement("div");
      d.textContent = `${cur[k].name} · ${fmt(cur[k].price)}${cur[k].target ? " → " + fmt(cur[k].target) : ""}  `;
      const x = document.createElement("a");
      x.textContent = "✖"; x.style.cssText = "cursor:pointer;color:#f87171;";
      x.onclick = () => { const o = load(); delete o[k]; save(o); showList(); };
      d.append(x);
      logEl.append(d);
    }
    const bar = document.createElement("div");
    bar.style.cssText = "margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;";
    const b = (t, f) => { const e = document.createElement("button"); e.textContent = t; e.onclick = f;
      e.style.cssText = "padding:4px 8px;border:0;border-radius:6px;background:#374151;color:#fff;cursor:pointer;"; return e; };
    bar.append(
      b("Copy purchased.json", () => { GM_setClipboard(purchasedJson()); log("Copied. Paste it into purchased.json on GitHub and commit."); }),
      b("Download", () => { const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([purchasedJson()], { type: "application/json" }));
        a.download = "purchased.json"; document.body.appendChild(a); a.click(); a.remove(); }),
      b("Import existing", () => {
        const t = prompt("Paste your current purchased.json (names + prices or ids):");
        if (!t) return;
        try {
          const raw = JSON.parse(t), o = load();
          for (const [k, v] of Object.entries(raw)) {
            const price = parsePrice(typeof v === "object" && v ? v.price : v);
            const target = parsePrice(typeof v === "object" && v ? v.target : null);
            if (price) o[k] = target ? { price, target, name: (v && v.name) || k } : { price, name: (v && v.name) || k };
          }
          save(o); showList();
        } catch (e) { alert("Not valid JSON: " + e.message); }
      }),
      b("Clear all", () => { if (confirm("Remove every card from the list?")) { save({}); showList(); } }),
    );
    logEl.append(bar);
  }

  addBtn.addEventListener("click", addCard);
  listBtn.addEventListener("click", showList);
  save(load());                                      // refreshes the counter on the button

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

  // ---- live bid badge ------------------------------------------------------------
  // Reads bid_sheet.json (published by the scanner on the data branch) every time a player page
  // opens, so the numbers are as fresh as the last scan. Nothing to update by hand.
  const BID_URL = "https://raw.githubusercontent.com/Art-of-FUgue/Contrapunctus_XII_FUT_Trader/data/bid_sheet.json";
  let sheet = null, sheetAt = 0, badgeEl = null, lastPath = "";

  async function loadSheet() {
    if (sheet && Date.now() - sheetAt < 60_000) return sheet;
    try {
      const r = await gm("GET", BID_URL + "?t=" + Math.floor(Date.now() / 60_000));
      if (r.status === 200) { sheet = JSON.parse(r.text); sheetAt = Date.now(); }
    } catch (e) { console.log("[bid badge]", e.message); }
    return sheet;
  }

  const fmtK = (n) => (n >= 1000 ? (n / 1000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "") + "K" : String(n));

  async function updateBadge() {
    const m = location.pathname.match(/\/players\/(\d+)/);
    if (!m) { if (badgeEl) badgeEl.style.display = "none"; return; }
    if (!badgeEl) {
      badgeEl = document.createElement("div");
      badgeEl.style.cssText = "position:fixed;left:12px;bottom:12px;z-index:99999;background:#10151c;color:#e8eef5;" +
        "border:1px solid #3b82f6;border-radius:8px;padding:8px 10px;font:12px/1.4 system-ui,sans-serif;max-width:260px;" +
        "box-shadow:0 2px 10px rgba(0,0,0,.5)";
      document.body.append(badgeEl);
    }
    badgeEl.style.display = "block";
    badgeEl.textContent = "bid sheet…";
    const d = await loadSheet();
    if (!d) { badgeEl.textContent = "bid sheet: unreachable"; return; }
    const c = d.cards[m[1]];
    const age = Math.round((Date.now() / 1000 - d.ts) / 60);
    if (!c) { badgeEl.textContent = `No reliable sold price yet (needs 4+ sales in 4 days) · sheet ${age} min old`; return; }
    const under = ((c.floor / c.fair - 1) * 100).toFixed(1);
    const bids = d.nets.map((n, i) => `+${n}% ≤ <b>${fmtK(c.bid[i])}</b>`).join("<br>");
    badgeEl.innerHTML = `<b>Sold median ${fmtK(c.fair)}</b> (${c.n} sales, ${c.n24} in 24h, ±${c.sig}%)<br>` +
      `Floor ${fmtK(c.floor)} (${under > 0 ? "+" : ""}${under}% vs sold)<br>Max bid for net profit:<br>${bids}` +
      `<br><span style="opacity:.6">sheet ${age} min old${c.n24 < 4 ? " · thin, be careful" : ""}</span>`;
  }

  setInterval(() => { if (location.pathname !== lastPath) { lastPath = location.pathname; updateBadge(); } }, 800);
  setInterval(() => { if (badgeEl && badgeEl.style.display !== "none") updateBadge(); }, 120_000);
})();
