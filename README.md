# Contrapunctus XII · FUT Trader

An automated **EA FC 27 Ultimate Team PC-market scanner**. Every 15 minutes it reads fut.gg's public
price files, keeps a history in SQLite, infers real sales from floor-price moves, and posts dip,
buy-signal and sell alerts to a Discord webhook.

- Data source: **fut.gg only** (static price files on `r2.fut.gg`). No EA login, no real sales log.
- Platform: PC (`FC_PLATFORM=pc`; `ps` is supported in code).
- Timezone shown in notices and reports: Austin (`America/Chicago`, `FC_TIMEZONE`).
- This README describes `main`. Branch-only extras are in [Branch `buy-windows`](#branch-buy-windows).

> **Keep this file in sync.** Whenever an alert rule, threshold, table or retention period changes,
> update the matching section here in the same commit and bump the "Last updated" line at the bottom.

---

## 1. How it runs

| Piece | What it does |
|---|---|
| `.github/workflows/scan.yml` | Launched every 15 min (a backup `cron` plus an outside timer via `workflow_dispatch`). One launch = **13 scans, 60 s apart** (`FC_RUNS`, `FC_RUN_GAP`). A scan is skipped when fut.gg has not published new prices. Never two at once (`concurrency: fc-scan`). |
| `fc_market_scanner.py` | The scanner: fetch → judge → post → save. |
| `sales_model.py` | Infers completed sales from floor moves (section 3). |
| `data` branch | Holds the SQLite database as one **gzip-compressed** file `market_pc.db.gz`, force-pushed as a single commit after every launch so the repo never grows. Non-`main` branches use `data-<branch>`. The workflow restores it at the start and saves it at the end (even if the scan failed). |
| `refresh-cards.yml` | When you commit `bundle.json`, rebuilds `players.json` / `holo_ids.json`, commits them, deletes the bundle and posts the new cards to Discord. |
| `userscript/fut_card_refresh.user.js` | Tampermonkey helper on fut.gg (see section 7). |

Order inside one scan (`run_cycle`): fetch prices → register new cards → **judge against history**
(`scan`) → post embeds → **then** save the snapshot → update the market index / reports → feed the sales
model → resolve old dip outcomes. Judging first means every alert compares the new price with
history that does not yet contain it.

Which cards are tracked: every special card in `players.json` priced ≥ `MIN_TRACK_PRICE` (5k), plus any
watched or purchased card. Alerts only consider cards ≥ `FC_MIN_PRICE` (15k) and ≤ `FC_MAX_PRICE` (2M).

## 2. Repository files

| File | Purpose |
|---|---|
| `players.json` | All special cards (id, name, rating, rarity, URL, image). Built by `setup.py`/`build_players.py` from saved fut.gg list pages (`pages/`). |
| `holo_ids.json` | Ids of holographic cards. fut.gg's data has no holo flag, so holos are labelled from here (`🃏 Holo`). |
| `watchlist.json` | Manual watchlist. **Empty on purpose**: the whole watchlist is dynamic. |
| `watch_seed.json` | 13 cards that seed the auto-watchlist once (with a 3-day grace period). |
| `purchased.json` | Cards you hold: `{"<card id or "name promo">": {"price": 399000, "target": 445000, "name": "…"}}`. `target` is optional. Re-read every scan. You commit it yourself. |
| `bundle.json` | Transient: produced by the userscript, consumed by `refresh-cards.yml`. |
| `setup.py`, `build_players.py`, `apply_bundle.py`, `report_refresh.py`, `holo_lookup_console.js` | Card-list refresh tooling. |
| `test_scan.py` | Quick manual test runner (`--demo` previews alert formatting with fake data). |

CLI: `python fc_market_scanner.py --once --runs 13 --run-gap 60` (Actions), `--dry-run` (print, don't post),
`--demo --dry-run` (fake data).

---

## 3. Data stored (the SQLite database)

Everything lives in `market_pc.db` (gzip-compressed on the `data` branch). One retention length
governs every long-term table: **8 weeks** (`HISTORY_DAYS = 56`). The size is a few tens of MB
uncompressed (≈ one third of that compressed); GitHub's 100 MB file limit is not a concern.

### 3.1 Price history, in three tiers

| Table | Grain | Kept | Notes |
|---|---|---|---|
| `prices` | every scan (≈ each minute fut.gg updates), every tracked card | **26 h** (`RAW_HOURS`) | The raw floor ("cheapest BIN") snapshots. Source of the 24 h stats, the 5-minute reference, the card behaviour type and thin-card filter. |
| `prices_hourly` | median floor per card per hour | **28 days** (Active & Mixed cards) · **current day only** for Quiet cards | Written when raw rows age out. |
| `prices_daily` | per card per UTC day: open, high, low, median, close, hours present, hour-to-hour changes | **56 days** | Built when hourly rows age out (Quiet cards roll up almost immediately). |

Quiet cards barely move, so they do not need hourly history; the type rarely changes.

### 3.2 Inferred sales (see "Sales inference" below)

| Table | Grain | Kept |
|---|---|---|
| `floor_state` | one row per card: the small stack of listings currently being tracked | live state |
| `sales_inferred` | one row per inferred sale/ambiguous disappearance: time, price, group, price band, kind, listing-age window | **4 days** (`SALES_RAW_DAYS`). Active non-premium cards keep them only ~26 h (their floor path already tells the story); Icons/Heroes, Mixed and Quiet cards keep the full 4 days. |
| `sales_hourly` | per card per hour: sales count, ambiguous count, median, lowest sale | **42 days**, all cards |
| `sales_daily` | per card per day: counts, median, low, high | **56 days** |

### 3.3 Market-wide and research tables

| Table | What | Kept |
|---|---|---|
| `index_ret` | hourly return of the equal-weighted price basket for each segment (All specials, Icons, Heroes, TOTW, Other promos, Holos) **and** each behaviour type (Active/Mixed/Quiet) | 56 days |
| `dip_events` | the **dip ledger** (includes `d6`, the % vs the 6 h-ago reference, and the Austin weekday/hour for buy-window research): every card ≥ 8% under its 24 h median, once per 6 h episode, with its situation (behaviour type, depth, market move, persisted?, fair value, nearby sale, weekday/hour) and later outcomes (net result at +15 min, +1 h, +3 h, +6 h, +12 h, and best exit within 3 h; `999` = unresolvable) | 56 days |
| `outcomes` | dips that were actually **alerted**, with the floor 1/3/6 h later (net of tax + haircut). Kept for review of alerted and 6 h dips (no longer shown in alerts) | 30 days |

### 3.4 State tables

| Table | What |
|---|---|
| `alerts` | what was posted, per card, kind, time and price (drives cooldowns / repeat filters) |
| `seen` | when each card id first appeared in `players.json` (new-card mute) |
| `watch_auto` | the auto-watchlist and when each card was last confirmed |
| `watch_positions` | open watchlist buys: card, buy price, target, buy time, estimated hours, "reassess" warning sent? |
| `meta` | key/value: feed hash, last post times, last roll-up, etc. |

### 3.5 Sales inference (how "sales" are obtained)

fut.gg publishes only each card's **floor** (cheapest listing). EA listings last a fixed 1, 3, 6 or 12
hours. `sales_model.py` keeps a stack of known listings per card; when the floor listing disappears:

- it vanished at an age that matches **no** listing duration → **`sale`** (sold at exactly its listed price);
- its age matches a duration within ±90 s → **`ambiguous`** (probably expired; never counted as a sale);
- listings that were already there when first seen have no known age and are not classified.

Limits to keep in mind: roughly **40 % recall** (it misses same-price stack sales), a gap of > 20 min between
scans resets a card's state, and the figures are *inferred*, not an official sales log.

`sale_stats` turns the last 4 days of `sale` rows into **fair value** (recency-weighted median, 24 h
half-life), **σ%** (robust spread), **n / n24**, the median of the last 3 sales, and `p3`, the chance of a
sale within 3 h (`1 − e^(−3·rate)` from the 24 h sale rate).

### 3.6 Card behaviour types

From the last 26 h of raw floor snapshots (≥ 40 snapshots, median ≥ 10k), the share of updates where the floor did not change:

| Type | Floor unchanged in | Meaning |
|---|---|---|
| **Active** | ≤ 30 % of updates | busy, thin pile; the floor moves with real trading |
| **Quiet** | ≥ 60 % | deep pile or little trading; floor barely moves |
| **Mixed** | in between | |

Used for: dip cooldowns, retention (Quiet), the dip ledger, buy-window rhythm, sales pruning. Cached hourly.

---

## 4. Which cards get which rule

1. **Muted**: cards new to `players.json` stay silent for **4 days** (`FC_NEW_CARD_DAYS`); a bulk add of > 120 ids at once is treated as a list update, not a promo drop.
2. **Thin-card filter** (applies to everything, including the watchlist): no alerts for a card that has
   **< 15 inferred sales in 4 days**, or whose 24 h floor p10–p90 spread is **> 60 %** of its median, or whose
   floor is **> 2×** its 4-day sold median (the cheap listings are gone; what is left is not the market).
3. **TOTW fodder**: TOTW cards under 15k are skipped.
4. **Watched or held** cards skip the price-range / new-card filters and the normal lists; they use the
   watchlist rules (section 5.4–5.5).
5. **Premium list**: Icons/Heroes (and their holos) worth ≥ **100k**, any other special (promo, TOTW … and holos) worth ≥ **300k**,
   or any card ≥ 100k with ≥ 20 inferred sales in 4 days. Value is judged on the higher of the current price and
   its 24 h median, so a card that dips stays on the list. Premium-list cards get **only** the premium detector
   (they are left out of the 6 h and 5-minute alerts).
6. Everything else: **standard** rules (6 h dip, 5-minute drop).

**Market adjustment:** dips are measured *beyond the card's peer group* (Icon, Hero, TOTW, other promo, holo …). The group's
median move is subtracted, so a market-wide slide does not look like 200 separate dips.

## 5. Alerts and their logic

`EA_TAX = 5 %`. "Net" always means profit after the 5 % tax. Alert order in Discord: dips, fast drops, premium
dips, watchlist buy, watchlist sell, owned-card alerts, then the red premium alert **last** (so it sits at the bottom, where you look first).

### 5.1 📉 Dip Alert · vs 6 h ago (regular cards) — **OFF by default** (`FC_DIP6H_ALERTS=1` turns it back on)
Turned off because 6 h dips did not pay after tax in practice, **but the rule still runs silently**: every 6 h dip is scored and logged to `outcomes` (floor 1/3/6 h later), and each dip-ledger event stores its `d6` (% vs the 6 h-ago reference) so longer-hold / buy-window research keeps its data. When on:
- Reference = median of the hour before "6 h ago" (needs ≥ 12 snapshots in 24 h).
- Fires when the price is **≥ 8 % below the reference after the market adjustment**, **and** below its 24 h median ("usual") **and** at or under its recent-sales average.
- Grouped by card version; a tier emoji shows depth (🔴 15 %+ · 🟠 12–15 % · 🟢 8–12 %).
- Each line can carry: **% chance to sell within 3 h**. (The old "similar dips x/y profitable" score line and the seasonal-drift estimate were removed: not useful.)
- **Cooldown by behaviour type**: Active 5 min · Mixed 10 min · Quiet 15 min (`FC_DIP_COOLDOWN_*`); inside the cooldown it only re-posts if the price fell another 3 %.

### 5.2 ⚡ Fast Drop · vs 5 min ago
- Reference = median of the last 5 minutes of scans (`FC_FLASH_WINDOW_MIN`). Cards ≥ 10k, not on the premium list.
- Fires at **≥ 10 %** below it after market adjustment (`FC_FLASH_DIP_PCT`; was 8 %, but 8–10 % drops returned ≈ +1 % net on cards under 100k). There is no separate crash tier. Same per-type cooldowns as dips (Active 5 / Mixed 10 / Quiet 15 min).
- **Sold-median filter:** the alert is posted **only if the new floor is ≥ 5 % under the 4-day sold median** (recency-weighted, needs ≥ 8 sales; `FC_FAST_MIN_UNDER_SOLD`). In the 26 h sample, drops landing ≥ 8 % under sold returned about +27 % net at 3 h (14 of 14 won), while drops within 5 % of it (or above) returned ≈ −1.5 % to −3.5 % — about a third of all fast drops. Cards without enough sales data are not alerted (the thin-card filter already requires ≥ 15).
- **Hint line:** 🟢 8 %+ under sold · 🟡 5–8 % under, with the sold price, size (<40k / 40–100k / 100k+), behaviour type and "holo".
- Reading it: the profit comes from being first. When the cheap listing is still there on the next scan the 3 h result was negative (price reset lower); when it was gone (sniped) the floor bounced back.

### 5.3 💎 Premium Dip and 🚨 Premium below 4-day sold
Premium-list cards are judged on real (inferred) sales:
- **Enough sales** (≥ 4 in 4 days): fair value = recency-weighted median sold price. It fires only if **all** hold:
  the price is **≥ 10 % below fair value and ≥ 2 sale-spreads deep**; recent sales have not already fallen
  (last-3 median not ≥ half the dip below fair, when ≥ 6 sales); **net ≥ 5 %** if resold at fair value; and
  **P(sale within 3 h) ≥ 25 %**. Ranked by `net × p3`.
  These go to the **red 🚨💎 "PREMIUM BELOW 4-DAY SOLD"** embed, posted last (🔴 net 20 %+ · 🟠 10–20 % · 🟢 5–10 %).
- **Thin sales** (< 4): fallback on the floor: needs ≥ 8 snapshots and ≥ 4 price changes in 24 h (liquidity), and
  the price **≥ 8 % below the 24 h median of sales points** after market adjustment. Posted in the purple
  "💎 Premium Dip · vs 24 h median" embed.
- **No cooldown**: a premium card re-posts when its price moved ≥ 1 % since the last post, or after 30 min.
- A watched premium card that passes the 4-day-sold rule is also routed to the red alert.

### 5.4 🎯 Watchlist Buy Signal
Applies to the **dynamic watchlist** (section 6) only. It has its **own** rule and does not reuse the 6 h /
premium / flash rules. It fires when **both** hold:
1. the price is **≥ 6 % below its 24 h median**; and
2. selling at **fair value** (4-day sold median, else the 24 h median) would net **≥ 4 %** after tax.

Each alert shows: the dip vs the 24 h median, the **target** (fair value, net, and break-even price), and an
**estimated sell time** (how long this card's own dips took to recover over the last 7 days, else from its sale
rate, else 6 h; clamped 1–24 h), shown as a Discord timestamp with a countdown.
No cooldown: it re-posts when the price moves ≥ 1 % since the last post or after 30 min. A buy alert opens a
**position** in `watch_positions` (buy price, target, time).

### 5.5 💰 Watchlist Sell (closes the cycle)
For each open position, every scan:
- **Target reached** (price ≥ target): "🎯 Watchlist Sell" embed with net after tax, buy price, hold time vs the estimate. Position closed.
- **Not reached after 36 h** (`FC_WATCH_REASSESS_HOURS`): one **⏰ "target not reached, reassess"** note with the net if sold now. The position stays open.
- Positions expire after **72 h** (`FC_WATCH_EXPIRE_HOURS`).

### 5.6 💰 Your Card · Sell Now / 🚀 Your Card Spiked (purchased list)
For cards in `purchased.json` with a buy price:
- **Sell Now**: floor ≥ your `target`; if you gave none, the target is fair value (4-day sold, else 24 h median) but never below **+3 % net** over your buy price. Re-posts on a ≥ 1 % move.
- **Spiked**: floor ≥ **15 %** above your buy price (`FC_OWNED_SPIKE_PCT`).
- Entries that cannot be resolved to a single card are reported once in a "⚠️ purchased.json: fix these entries" embed.

### 5.7 📊 Market Index · 24 h trend (every 3 h)
Equal-weighted, chain-linked price basket (cards ≥ 10k, ≥ 5 cards per step) per segment, shown over 1 h / 6 h / 12 h / 24 h with a sparkline and the mood of the last 6 h.

### 5.8 Off or limited
- 🚀 Rise Alert and ⚡ Quick Spike exist but are **off** (`FC_SPIKE_ALERTS=1` enables them; regular-card spike alerts are off by design).
- Buy alerts (fast drop, premium, watchlist buy) show a one-line `[open]` link to the fut.gg card and the player name in a code block below the card: Discord puts a copy button on it (press and hold on mobile) so the name can be pasted into the Web App Transfer Market search, which has no search link.
- 🕒 Buy Windows report: code is on `main` but **never posts there** (`FC_WINDOWS_POST_HOURS=0`); the data behind it (`index_ret`) is always collected.
- Per-scan "🔄 scan ran" status line in Discord (`FC_NOTIFY_EACH_RUN`).

---

## 6. The dynamic watchlist

The manual `watchlist.json` is empty; the list is built automatically.
- **Seed**: 13 hand-screened cards (`watch_seed.json`) enter once with a 3-day grace period.
- **Auto-pick** (every 6 h): for each liquid card (≥ 30k, ≥ 30 inferred sales in 4 days, not new/muted) replay the last 7 days of
  hourly prices: *buy whenever the hourly price is ≥ 6 % under its trailing 24 h median (at most one buy per card per 6 h) and sell 3, 6, 12 or 24 h later*, net of tax and a 1 % exit haircut.
  A card qualifies when at **least 2 of the 4 holds** have ≥ 3 events, an average net **> 2 %** and a win rate **≥ 55 %**.
  Up to 20 best cards (ranked by mean × win rate × number of paying holds) are watched; a card stays watched for 3 days after it last qualified.
- Marks: ⭐ on your list / seed, 🔁 auto-picked.

## 7. Userscript (`userscript/fut_card_refresh.user.js`, v1.2)
- **Refresh cards**: reads fut.gg's price index and downloads `bundle.json` (new cards + holo lookup). Commit it and `refresh-cards.yml` does the rest.
- **➕ Bought** (on a card page): asks for your buy price and an optional sell target, stores them in Tampermonkey. **📋 Purchased** shows the list, exports `purchased.json` (copy/download), imports an existing file. It never holds a GitHub token; you commit the file yourself.

## 8. Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | where alerts go (repo secret) |
| `FC_PLATFORM` | `pc` | `pc` or `ps` |
| `FC_RUNS` / `FC_RUN_GAP` | 13 / 60 s | scans per launch / spacing |
| `FC_MIN_PRICE` / `FC_MAX_PRICE` | 15k / 2M (workflow) | alert price range |
| `FC_MAX_LINES` | 16 | cards per section |
| `FC_NEW_CARD_DAYS` | 4 | new-card mute |
| `FC_DIP_COOLDOWN_ACTIVE/MIXED/QUIET` | 5 / 10 / 15 min | dip cooldown by type |
| `FC_FLASH_WINDOW_MIN` | 5 | reference window of the fast drop |
| `FC_FLASH_DIP_PCT` | 10 | fast-drop minimum % below the 5-minute reference |
| `FC_FAST_MIN_UNDER_SOLD` | 5 | a fast drop must land this % under the 4-day sold median or it is not posted |
| `FC_DIP6H_ALERTS` | 0 | turn the 6 h dip alert back on (1) |
| `FC_PREMIUM_ICON_HERO_MIN` / `FC_PREMIUM_PROMO_MIN` | 100k / 300k | premium list thresholds |
| `FC_PREMIUM_LIQUID_MIN_SALES` | 20 | sales in 4 days to count a 100k+ card as premium |
| `FC_THIN_MIN_SALES` / `FC_THIN_MAX_SPREAD` | 15 / 60 | thin-card filter |
| `FC_AUTO_MIN_EVENTS` / `FC_AUTO_MAX` | 3 / 20 | auto-watch qualification / size |
| `FC_WATCH_REASSESS_HOURS` / `FC_WATCH_EXPIRE_HOURS` | 36 / 72 | watch position timing |
| `FC_OWNED_SPIKE_PCT` | 15 | owned-card spike threshold |
| `FC_INDEX_POST_HOURS` | 3 | market index cadence (0 = off) |
| `FC_WINDOWS_POST_HOURS` | 0 | buy-window report cadence (0 = off) |
| `FC_SPIKE_ALERTS` | 0 | enable spike alerts |
| `FC_TIMEZONE` | `America/Chicago` | displayed time zone |
| `FC_SALES_RAW_DAYS`, `FC_LISTING_HOURS`, `FC_SALE_TOL_SECONDS` | 4, `1,3,6,12`, 90 | sales inference |

## 9. Findings that shaped the design (for context)
- **Fast drops (26 h, liquid non-thin cards 15k–250k):** 10 %+ drops on cards under 100k returned about +13 % net at 3 h (79 % won, n = 28); 8–10 % only ≈ +1 %. The deciding factor was the gap to the 4-day sold median (see 5.2). Small sample, re-check as data grows.
- **Sustained dips do not pay:** when the cheap price is still the floor an hour later (406 hourly dips ≥ 6 % under the prior 24 h median, 680 hourly floors under the sold median) results were −2 % to −6 % net — the market had repriced (often new promo cards). The edge exists only in the first minutes of a sudden drop.
- Blind dip-buying is **net-negative after tax**; the market median is roughly flat over a week. Dips are only worth taking on liquid cards whose own history says they recover, which is why the watchlist and the premium rules are selective.
- Sales surges are only weakly predictive. Holo Future Stars / DfG cards in the 100–300k range showed a steady rise through a week in one sample, which a dip rule alone does not capture (planned: rebound / weekly-cycle detector, needs ~3 weeks of hourly index data).
- Cards that almost never sell at their listed floor (e.g. a 93k floor with real sales under 20k) are misleading; the thin-card filter removes them.

---

## Branch `buy-windows` (experimental, not on `main`)
Merges `main` regularly and adds, for research:
- **Buy-window report** posted daily (`FC_WINDOWS_POST_HOURS=24`): for each segment and behaviour type, the cheapest hour and best entry hour in the next 24 h (Austin time), from the hour-of-day pattern (weekly pattern after 3 weeks of data), with "(weak)" when within the noise.
- **Shadow dip scorer** (`judge_dip`, `scorer_report`, daily scoreboard via `FC_SCORER_POST_HOURS`): every ledger dip gets a verdict from *similar past events of the same behaviour type* (depth band, persistence, market direction):
  **BUY** when the lower confidence bound (z = 2.5, winsorized mean) at some horizon (15 min / 1 h / 3 h / 6 h / 12 h) is above zero net with a win rate ≥ 55 % over ≥ 30 similar events; **WATCH** when profitable on average but not reliably; **SKIP** when it lost; **LEARNING** when too few events. Nothing is posted per dip: it only logs verdicts and, once a day, the scoreboard comparing each verdict group's real results against all dips. (The old "x/y profitable @3h" line was removed from `main`; the scorer is now research only.)
- Its own database branch (`data-buy-windows`).

---
*Last updated: 2026-10-26 — removed the 'similar dips' score line and seasonal drift, fast drop minimum 10 % with tier hint, 6 h dip alert off by default; covers watchlist buy/sell with estimated sell time and 36 h reassess note, purchased-list sell alerts with optional targets, userscript v1.2, 8-week storage tiers, thin-card filter, dynamic watchlist.*
