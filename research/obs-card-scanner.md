# OBS Pokémon card scanner: research and recommended build

Researched 2026-10-27. Primary sources only (official docs, source repos, first-party API references, and direct API calls). Each claim cites a source tag like `[S12]`, defined in [Sources](#sources). **UNVERIFIED** marks anything a primary source did not confirm.

---

## TL;DR

**Build an external companion app, not an OBS plugin.** It is a Python service that:

1. pulls frames of the webcam source out of OBS over **obs-websocket v5** (`GetSourceScreenshot`), so the camera never has to be shared between two processes;
2. finds the card in the frame with OpenCV and auto-triggers when the card is held still (a manual hotkey also works);
3. identifies the card through a pluggable `Identifier`. MVP: **Scrydex Vision**, which returns ranked matches with scores. Cheap second opinion and fallback: **Claude Haiku 5.5** (`claude-haiku-5-5`) with structured JSON output;
4. prices it from **Scrydex** (raw NM `market`, per variant). Don't build on pokemontcg.io: it is deprecated and "scheduled to be taken offline on March 1, 2027" `[S31]`;
5. pushes state to a **Browser Source overlay** (a local HTML page the app serves) and to a **Custom Browser Dock** control panel inside OBS;
6. records every scan in **SQLite**, with undo as a soft-delete and a CSV export at the end of the stream.

When confidence is low, the overlay shows "identifying..." and the operator picks from the top 3 candidates with hotkeys (1/2/3), or cycles variants (V).

Why this shape:
- Browser Sources are the OBS-native way to draw web overlays on every desktop platform `[S4]`.
- obs-websocket is built into OBS 28+ `[S1]`.
- A native C/C++ plugin adds a lot of build and ABI work and buys nothing this project needs `[S9][S10]`.

Biggest risks:
- **Data licensing.** Scrydex's ToS say nothing about showing prices on a public stream, and bar "commercially exploit[ing]" the Services without authorization `[S43]`.
- **Variant mis-ID.** Holo vs reverse holo vs 1st Edition changes the price a lot.
- **No published accuracy benchmark** exists for any identification option, so build your own eval set.

Backups to Scrydex, and where commercial apps get their data: see [Alternatives to Scrydex](#alternatives-to-scrydex).

---

## 1. OBS integration options

### 1.1 Comparison

| Option | How it gets camera frames | How it draws the overlay | Dev effort | Cross-platform |
|---|---|---|---|---|
| **Native plugin** (C/C++ libobs, `obs-plugintemplate`) | In-process. `obs_add_raw_video_callback()` "obtain[s] raw video frames without necessarily using an output" `[S7]`. An async video filter gets raw frames via `filter_video` `[S8]`. | libobs graphics (custom source or filter rendering). Text, layout and animation all done by hand. | **High.** CMake, VS 2022 / Xcode 16 / Ubuntu 24.04 toolchains `[S9]`, macOS codesigning and notarization `[S9]`, a rebuild per OBS release. OBS 33.0.0 dropped Qt 6.4–6.9 and deprecated some source APIs `[S10]`. HTTP, JSON and ML in C++ add more. | Yes, but you build and ship per OS `[S9]`. |
| **OBS Python/Lua script** (`obspython`/`obslua`) | No simple frame API is documented for scripts. You would drive libobs graphics through bindings. Only Lua can register sources and main render callbacks `[S6]`. | Lua-only script sources `[S6]`, or by changing text/image sources. | **Medium, fragile.** Python on Windows/macOS needs a separately installed Python that matches OBS's architecture `[S6]`. "Using [`script_tick`] in Python is not recommended due to the global interpreter lock" `[S6]`. Bad scripts can leak memory or crash OBS `[S6]`. Heavy CV/ML inside OBS's process is risky. | Yes, with the Python install caveat `[S6]`. |
| **Browser Source overlay** (local page) | **None by itself.** It is a display surface (CEF). Camera access via `getUserMedia` is not documented `[S4]` (**UNVERIFIED**). | HTML/CSS/JS rendered by CEF. Local file or URL, custom CSS, FPS 1–60 `[S5]`. JS bindings and OBS events, including custom events via the `obs-browser` vendor `emit_event` `[S4]`. | **Low.** Plain web dev. | Yes. Included in official Windows, macOS, Ubuntu PPA and Flatpak builds `[S4]`. Browser Docks work everywhere except Wayland `[S4]`. |
| **External app + obs-websocket v5** | `GetSourceScreenshot` on the webcam input returns a Base64 image, scaled and compressed on request `[S2]`. Alternative: OBS Virtual Camera set to output one **Source** `[S11]`. | Usually a Browser Source fed by the app. obs-websocket can also toggle scene items or set input settings (`SetSceneItemEnabled`, `SetInputSettings`) `[S2]`. | **Low–medium.** Any language. Python and JS client libraries are listed upstream `[S1]`. All CV and ML runs outside OBS, so a crash can't take down the stream. | Yes. obs-websocket ships with OBS 28.0.0+ on port 4455 `[S1]`. |

### 1.2 The "external app + Browser Source + obs-websocket" pattern in detail

- **Connecting.** The client opens a WebSocket (JSON or MsgPack subprotocol), receives `Hello`, sends `Identify` (with auth if enabled), then sends `Request`s and gets `Event`s `[S2]`. OBS 28+ includes the server. The default port is 4455, and auth is configured in OBS's Tools menu `[S1]`. In OBS 33, **Safe Mode no longer starts the WebSocket server** `[S10]`, so the app must handle "OBS up, socket down".
- **Getting frames.** `GetSourceScreenshot` takes `sourceName`/`sourceUuid`, `imageFormat` (check `GetVersion` for supported formats), `imageWidth`/`imageHeight` (8–4096, aspect ratio kept) and `imageCompressionQuality` (-1..100). It returns `imageData` as Base64 and works on inputs as well as scenes `[S2]`. Because you capture the **webcam input**, not the program scene, the overlay never ends up in the image you analyse.

  ```json
  {"op": 6, "d": {"requestType": "GetSourceScreenshot", "requestId": "f1",
    "requestData": {"sourceName": "Card Cam", "imageFormat": "jpg",
                    "imageWidth": 1280, "imageCompressionQuality": 85}}}
  ```
- **How fast is `GetSourceScreenshot`?** No published number exists (**UNVERIFIED**). The source shows each call:
  - enters the graphics context;
  - creates a texrender and stage surface;
  - renders the source;
  - does a synchronous `gs_stagesurface_map` GPU→CPU readback;
  - encodes with Qt `QImage::save` `[S3]`.

  That is a per-call GPU stall plus a JPEG/PNG encode on OBS's side, followed by Base64. Plan for **2–5 polls/sec at ~1280 px wide JPEG**, then measure dropped/lagged frames in OBS's stats while streaming. "Complexity Rating: 4/5" is the protocol's own label `[S2]`.
- **Sharing the camera (alternatives):**
  - **Virtual Camera, "Source" mode.** The virtual camera "always shows one chosen source" `[S11]`, so the app could open "OBS Virtual Camera" with OpenCV and get full-rate frames. Implementations: a macOS CoreMediaIO camera extension, a Windows DirectShow module, and Linux `v4l2loopback` (OBS runs `modprobe v4l2loopback exclusive_caps=1 card_label='OBS Virtual Camera'`) `[S12]`. Downsides: it uses the virtual camera, which the streamer may want for calls. Output resolution and frame rate in Source mode are **UNVERIFIED**.
  - **Both OBS and the app open the physical webcam.** Whether this works depends on the OS and driver (**UNVERIFIED**; no primary source found). Avoid it.
  - **The app owns the camera and feeds OBS.** This means writing your own virtual camera or NDI pipeline, which is the most work.
- **Drawing the overlay.** The app serves `overlay.html` on `http://127.0.0.1:<port>` and pushes state over its own WebSocket/SSE. Browser Source settings are `url` or `is_local_file`/`local_file`, plus `css`, `fps`, `shutdown`, `restart_when_active` and `webpage_control_level` `[S5]`. If you'd rather not run your own push channel, `CallVendorRequest` with `vendorName: "obs-browser"`, `requestType: "emit_event"` broadcasts a JS event to every browser source `[S2][S4]`. The page still needs an HTTP endpoint to recover state after a refresh, so serving the page yourself is simpler.
- **Operator UI.** Load the same app's `/control` page as a Custom Browser Dock, which is supported on all platforms except Wayland `[S4]`.

### 1.3 Recommendation

Use **External app (Python) + Browser Source overlay + obs-websocket `GetSourceScreenshot`**. If screenshot polling turns out to hurt OBS performance, switch to **Virtual Camera in Source mode**.

Reasons:
- The overlay is just web tech.
- All heavy work (OpenCV, network calls, ML) runs outside OBS's process. The scripting docs warn that bindings can crash OBS `[S6]`.
- Nothing needs rebuilding when OBS updates (33.0.0 shipped 2026-10-23 with a large CEF update and API deprecations `[S10]`).
- It is cross-platform.

---

## 2. Card identification

### 2.1 Options

| Approach | Accuracy | Latency | Cost/scan | Variants | Notes |
|---|---|---|---|---|---|
| **OCR of name + collector number** (Tesseract 5.5.3, Apple Vision, PaddleOCR) | No published benchmark (**UNVERIFIED**). Tesseract "works best on images which have a DPI of at least 300", and small regions need a non-default `--psm` `[S21]`. Tiny collector-number text on a hand-held card is the hard part. | Local; fast on CPU (**UNVERIFIED**; measure). | $0 | Can't see foil type. Collector number plus set total narrows the set. | Apple: `VNRecognizeTextRequest` (macOS 10.15+) and the Swift `RecognizeTextRequest` (macOS 15+) `[S18][S19]`. PaddleOCR v3.7.0 `[S22]`. Tesseract 5.5.3 `[S21]`. |
| **Perceptual hash** (imagehash: aHash/pHash/dHash/wHash) | Works for a single set in the reference repo `[S25]`. Expect trouble with reprints that share art across sets, and with glare (**UNVERIFIED**). | Very fast. | $0 | No. Same art = same hash. | `NolanAmblard/Pokemon-Card-Scanner`: OpenCV contour → perspective warp → 4 hash types against a DB, Evolutions set only `[S25]`. imagehash is BSD-2 `[S23]`. |
| **Image embeddings + nearest neighbour** (e.g. DINOv2 + FAISS), optionally fused with number OCR | No accuracy claims published: "No private benchmark outputs or accuracy claims are part of the source release" `[S26]`. | Local, GPU or CPU `[S26]`. | $0 marginal, but you need a reference image set. | Same-art reprints need the collector-number branch `[S26]`. | `remote-event/pokemon-tcg-scanner` (MIT): YOLOv8n crop → DINOv2-S + FAISS top-10 in parallel with RF-DETR + PaddleOCR number reading, then fusion with retries. Its thresholds "are heuristics, not calibrated probabilities" `[S26]`. DINOv2 is Apache-2.0, FAISS is MIT `[S24]`. |
| **Scrydex Vision API** (purpose-built) | "Trained on millions of card images". No percentage given `[S36]`. | "Typically takes between 1-3 seconds" `[S36]`. | 5 credits `[S36][S37]`: ~$0.029 on Starter, ~$0.0099 on Growth (computed from `[S42]`). | Returns `variant` "only ... if we have high confidence" `[S36]`. | `POST https://api.scrydex.com/vision/v1/cards/identify` with `image_url` or a multipart `image`, `games: ["pokemon"]`. Returns `matches[]` sorted by `score` (≈0.7–1.3+) with full Card objects, and detects graded slabs (PSA/BGS/CGC/TAG) `[S36]`. Max 20 MB; ~1500–2500 px long side recommended `[S36]`. |
| **Claude vision** (`claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`) | Not benchmarked for this task (**UNVERIFIED**). Anthropic warns of mistakes on "low-quality, rotated, or very small images under 200 pixels" `[S13]`. | Haiku 5.5 is the lineup's "Fastest", Sonnet 5.5 "Fast", Opus 5.5 "Moderate" `[S14]`. Absolute ms are **UNVERIFIED**. | See 2.2. ≈$0.0002 on Haiku 5.5. | Can be asked for visual cues (1st Edition stamp, foil pattern). Reliability is **UNVERIFIED**. | Images go in as `image` blocks (base64, URL or Files API) `[S13]`. Use `output_config.format` with a JSON schema (`output_format` is deprecated) `[S17]`. Omit `temperature`/`top_p`/`top_k` on Haiku 5.5 `[S16]`. Default effort is `medium`; set `low` for speed `[S14][S16]`. Model IDs: `[S14]`. |
| **Other existing OSS** | `t-sinclair2500/pokemon-scanner` (MIT): OpenCV + Tesseract + pokemontcg.io pricing, macOS only, CSV output `[S27]`. It depends on pokemontcg.io, which goes offline 2027-03-01 `[S31]`. | | | | |

The obvious variant-confuser is a holo, reverse holo, or 1st Edition printing with identical art. Whether it is a holo or reverse holo, or has a 1st Edition stamp, is a **physical** property that embeddings and hashes of the art can't separate. That is domain knowledge, not taken from a primary source. Price variants are keyed separately: pokemontcg.io uses `normal`, `holofoil`, `reverseHolofoil`, `1stEditionHolofoil`, `1stEditionNormal` `[S30]`, and on 2026-10-27 live data also returned an undocumented `unlimitedHolofoil` key `[S33]`. Scrydex uses a `variants[]` array with names like `unlimitedHolofoil` and `firstEditionShadowlessHolofoil` `[S40]`.

### 2.2 Claude cost per scan (computed)

Image cost is `⌈w/28⌉ × ⌈h/28⌉` visual tokens. Claude 4.7+ models use the high-res tier (max 2,576 px long edge, 4,784 tokens) `[S13]`. Prices: Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20 per MTok in/out. Haiku 5.5 prices rise above 100K-token prompts `[S15]`.

| Input | Visual tokens | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 |
|---|---|---|---|---|
| Warped card crop 630×880 + ~400 prompt tokens in, ~150 out | 23×32 = **736** | ≈ **$0.0002** | ≈ $0.004 | ≈ $0.0075 |
| Full 1920×1080 frame | **2,691** `[S13]` | ≈ $0.0004 | ≈ $0.007 | ≈ $0.013 |

Thinking tokens are billed as output; at `low` effort this should stay small (**UNVERIFIED**). **Crop before you send.** That cuts cost and latency, and Anthropic recommends pre-resizing `[S13]`. A 200-card stream costs about $0.04 on Haiku 5.5 versus ~$2–6 on Scrydex Vision.

### 2.3 Recommended identification flow

1. **Detect and warp.** OpenCV finds the largest 4-corner contour and warps it to an upright crop of about 630×880. This is the method in `[S25]`, and `[S26]` uses a 630×880 upright crop.
2. **Primary ID (MVP): Scrydex Vision** on the crop. Treat it as confident if top `score` ≥ threshold and it beats #2 by a margin. **Tune the threshold on your own labelled photos.** Scrydex gives no calibration, and `[S26]` also stresses that scores are heuristics.
3. **Second opinion or fallback: Claude Haiku 5.5.** Run it when Vision is low-confidence or returned no `variant`. It reads `{name, collector_number "NNN/TTT", set_code_or_symbol_text, language, first_edition_stamp: bool, foil: none|holo|reverse|unknown}` as structured JSON `[S17]`, then resolves deterministically against a locally cached Scrydex catalog via `q=name:"..."` plus `printed_number` `[S40]`.
4. **Low-confidence UI.** Show the top 3 candidates (thumbnail, set, number) and let the operator press 1/2/3, V to cycle variant, or Esc to skip. Nothing is added to the total until it is confident or confirmed.
5. **Phase 2 (cost/offline).** Add a local DINOv2+FAISS index plus collector-number OCR (the `[S26]` design) as a free first pass. Call cloud APIs only on disagreement.

### 2.4 Triggering a scan

- **Manual hotkey.** Most reliable for an MVP. Register OBS hotkeys from a tiny OBS Python script with `obs_hotkey_register_frontend(name, description, callback)` `[S6]`. The callback POSTs to the app's `/scan`, `/undo`, `/pick/N` endpoints, so the hotkeys live in OBS's own Settings → Hotkeys. (Whether OBS hotkeys fire while OBS is unfocused depends on OBS settings: **UNVERIFIED**.) The Browser Dock buttons and a Stream Deck calling the same endpoints are equivalent.
- **Auto-detect a card held still.** Every poll:
  - find the card quad;
  - require the corners to move less than ~1–2% of the frame width across N consecutive polls, for about 0.5–1 s;
  - require sharpness above a threshold. Use Cardstream's re-blur score instead of variance-of-Laplacian, and hold the call (send anyway after 3 s) rather than failing it; see [cardstream-review.md](cardstream-review.md#what-to-adopt-in-our-build) #6;
  - then fire once.

  Re-arm only after the card leaves the frame or the warped crop's hash changes. `[S26]` uses the same pattern: retry up to three stable views, a 1.2 s cooldown on unchanged views, and a "presentation gate" to avoid re-scanning the same card. Ship both modes, with auto-scan on a toggle.

---

## 3. Pricing data

| Source | Status (2026-10-27) | Access / cost | Rate limits | Freshness | Per-variant | Licensing notes |
|---|---|---|---|---|---|---|
| **Pokémon TCG API** (pokemontcg.io) | **Deprecated.** "New account registrations are no longer available. Existing API keys will continue to function through March 1, 2027" `[S28]`. The data repo says the API "is scheduled to be taken offline on March 1, 2027" `[S31]`. The homepage says it "is now part of Scrydex" `[S32]`. | Free key (no new signups) `[S28]`. | 20,000/day with key. 1,000/day and 30/min without `[S29]`. | Live check on 2026-10-27: TCGplayer `updatedAt` was **2026/10/15** for all 50 sampled `sv3pt5` cards, and Cardmarket `updatedAt` ranged back to 2026/02/22. Several anonymous calls failed: HTTP 500/502, or a non-JSON body `[S33]`. | TCGplayer prices keyed by variant (USD: low/mid/high/market/directLow). Cardmarket is EUR with reverse-holo fields `[S30]`. | `pokemon-tcg-data` repo has no license file `[S31]`. **Do not build on it.** |
| **Scrydex** (successor) | "The natural evolution of the Pokémon TCG API (previously hosted at pokemontcg.io)" `[S35]`. | Starter $29/mo for 5,000 credits; Growth $99 for 50,000; Pro $399 for 250,000. No free tier listed `[S42]`. Most requests cost 1 credit, price history 3, Vision 5 `[S37][S42]`. Headers `X-Api-Key` + `X-Team-ID` `[S38]`. | 100 req/s on all plans, 429 beyond that. Usage at `/account/v1/usage`, which updates every 20–30 min `[S38]`. | "Cache pricing data for at least 24 hours. This data only changes at most once per day" `[S41]`. Price webhooks exist `[S35]`. | `variants[]` per card, each with its own `prices`; requires `include=prices` `[S40]`. Raw prices by condition NM/LP/MP/HP/DM with `low`, `market` and 1–180-day `trends`. Graded prices too. USD, or JPY for Japanese cards; EUR planned `[S39]`. | ToS §4 bars reselling, redistributing or "commercially exploit[ing] the Services" without written authorization. Nothing on public display `[S43]`. Card images may be used in apps, and consuming them costs no credits `[S41]`. **The marketplace source is not named** `[S39]`. |
| **TCGplayer API** | "We are no longer granting new API access at this time" `[S44]`. The Partner API was deprecated from 2023-08-14 in favour of the Catalog/Pricing APIs `[S45]`. | Existing keys only `[S44]`. | "No limit to the number of requests that can be made with a single key" `[S44]`. | n/a | (SKU-level pricing; details not reviewed because access is closed.) | Existing users "must adhere to the terms of service" `[S44]`. |
| **Cardmarket API** | "Currently, we are not accepting applications for access to the Cardmarket API" `[S46]`. Base URL moved to `apiv2.cardmarket.com`, deadline May 1, 2026 `[S48]`. | Existing credentials only, and they may not be shared with third-party software `[S46]`. | Professionals: 100,000/day, of which 30,000 can be Marketplace requests `[S48]`. | Public catalog and **price-guide export files "updated daily"**. "Anyone can import and incorporate the data into their own applications, no extra permission or access point necessary" `[S47]`. | n/a | Pokémon coverage of the export files is **UNVERIFIED**: the article links Magic paths, and the Pokémon URLs returned HTTP 403 to a scripted fetch `[S47]`. EUR market. |
| **PriceCharting API** | Active `[S49]`. | Paid subscription required. CSV export only on "Legendary" `[S49]`. Price not listed on that page. | 1 call/sec. CSV once per 10 min `[S49]`. | CSV regenerated every 24 h `[S49]`. | Cards: `loose-price` (ungraded) plus graded tiers (`graded-price` = grade 9, `manual-only-price` = PSA 10, etc.). Values in integer pennies `[S49]`. Not per foil variant; each variant is a separate product (**UNVERIFIED**). | "Licensed for internal use only". Showing data to others "requires a commercial license and written permission" `[S49]`. **A public stream overlay likely needs that license.** |

**Recommendation.** Use Scrydex, raw NM `market` USD for the identified variant, cached ≥24 h `[S41]`. Freeze the price at scan time and store `price_as_of`. With a ~1-credit price call plus a 5-credit Vision call, a 200-card stream uses ~1,200 credits. Starter (5,000) covers about 4 such streams a month; Growth covers more than 40 (computed from `[S37][S42]`). **Before going live on a monetized channel, get written confirmation from Scrydex that showing prices on stream is allowed** `[S43]`.

Not reviewed (no first-party docs consulted): eBay sold-listing APIs and community mirrors of TCGplayer data. **UNVERIFIED.**

---

## 4. Session tracking (simple design)

- **Store.** One SQLite file (Python stdlib `sqlite3`, WAL mode). Write each scan the moment it is confirmed, so a crash or restart resumes the same session.
  ```sql
  CREATE TABLE session(id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT, title TEXT);
  CREATE TABLE scan(
    id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES session(id),
    ts TEXT, card_id TEXT, name TEXT, set_name TEXT, number TEXT, variant TEXT,
    condition TEXT DEFAULT 'NM', price_cents INTEGER, currency TEXT,
    price_source TEXT, price_as_of TEXT, id_method TEXT, id_score REAL,
    status TEXT DEFAULT 'active',  -- 'active' | 'voided'
    crop_path TEXT);
  ```
- **Total.** `SUM(price_cents) WHERE session_id=? AND status='active'`. Store integer cents (PriceCharting uses pennies too `[S49]`) and freeze the price at scan time.
- **Undo.** A hotkey sets the latest `active` scan to `voided`, and redo sets it back. Edit means re-pick a candidate or variant for scan N. Voided rows are kept, so the audit trail survives.
- **Overlay state.** On every change, push `{last_card, last_price, variant, total, count, pending_candidates}` to the overlay. On page load, the overlay GETs `/state` to recover after a Browser Source refresh (`restart_when_active` `[S5]`).
- **Export.** `GET /export/<session>.csv` (Python `csv` module) writes all rows, voided rows included with their status, plus a summary line. Save the warped crops to disk for later review.

---

## 5. Recommended architecture and MVP plan

### 5.1 Components and data flow

```
 Webcam ──► OBS "Card Cam" input ──► (program scene)
                     ▲  GetSourceScreenshot (2–5 Hz, 1280px JPEG) via obs-websocket :4455
                     │
   ┌─────────────── pokescan (Python 3.12, asyncio) ────────────────┐
   │ obs client (simpleobsws / obsws-python)                        │
   │ detector: OpenCV quad → warp 630×880 → stillness+sharpness gate│
   │ identifier: ScrydexVision ─┬─(low conf)─► ClaudeHaiku55 + catalog│
   │                            └─► candidates[top3]                │
   │ pricer: Scrydex cards/{id}?include=prices (24h cache)          │
   │ session store: SQLite  ·  CSV export                           │
   │ web: aiohttp/FastAPI → /overlay, /control, /state, WS push     │
   └────────────────────────────────────────────────────────────────┘
        │ WS push                       │ HTTP
        ▼                               ▼
  OBS Browser Source (overlay)    OBS Custom Browser Dock (control) + OBS script hotkeys → POST /scan,/undo,/pick/N
```

- **Language: Python.** It has the obs-websocket clients `[S1]`, OpenCV, imagehash `[S23]`, FAISS/DINOv2 `[S24]`, the Anthropic SDK, and the stdlib `sqlite3`/`csv`. The overlay is plain HTML/CSS/JS with `@types/obs-studio` for the bindings `[S4]`.
- **Claude call (illustrative).** `model="claude-haiku-5-5"`, an image block with the cropped card first and then the text instruction `[S13]`, `output_config={"effort":"low","format":{"type":"json_schema","schema":...}}` `[S17][S16]`, and no sampling params `[S16]`.

### 5.2 MVP milestones

1. **Plumbing.** Connect to obs-websocket, take `GetSourceScreenshot` of the webcam input, and show it in `/control`. Measure screenshot latency and OBS render-lag at 2/5/10 Hz while streaming to a test ingest.
2. **Overlay and session.** Browser Source overlay, SQLite session, manual price entry, undo, CSV export. This is usable even with no ID at all.
3. **Detection.** Card quad, warp and stillness trigger, plus manual hotkey via OBS script `[S6]`.
4. **ID and price.** Scrydex Vision → top-3 → Scrydex price `[S36][S40]`. Confidence threshold and picker UI.
5. **Fallback.** Claude Haiku 5.5 structured read plus catalog resolution for low-confidence cases or a missing `variant`.
6. **Eval.** Build a labelled set of ~100–200 photos from the *actual* stream setup: sleeves, glare, similar-art reprints, holo/reverse/1st Ed. Tune thresholds and record accuracy, latency and cost per path.
7. **Phase 2 (optional).** Local DINOv2+FAISS plus number OCR as a free first pass `[S26]`, to cut cloud spend and keep working offline.

### 5.3 Fallbacks

- **Low confidence.** The overlay shows "Identifying..." with no price. The dock shows the top 3 candidates. 1/2/3 picks, V cycles variant, Esc skips. A timeout keeps the card off the total.
- **No network / API down.** Queue the crop and let the operator enter name and price manually in the dock. Price it later.
- **OBS socket down** (e.g. Safe Mode `[S10]`). Show an error state in the dock and retry with backoff.

### 5.4 Open risks

1. **ToS and licensing for on-stream display.**
   - Scrydex ToS say nothing about public display and bar commercial exploitation without authorization `[S43]`.
   - PriceCharting requires a commercial license to show data to others `[S49]`.
   - Card images and data stay with their owners `[S43]`. Show the live camera crop rather than reference images where you can.
2. **pokemontcg.io shutdown, 2027-03-01** `[S31]`. Its prices already look stale `[S33]`.
3. **Variant and condition errors.** These are the biggest source of wrong prices. Mitigate with operator confirmation and a visible variant label.
4. **Unknown accuracy.** No vendor or OSS project publishes accuracy figures for real-world hand-held cards `[S26][S36]`. You need your own eval.
5. **Effect on OBS performance of `GetSourceScreenshot` polling** (synchronous GPU readback per call `[S3]`). Measure it. The fallback is Virtual Camera in Source mode `[S11]`.
6. **Price provenance and semantics.** Scrydex doesn't name its marketplace source `[S39]`. `market` is an average and updates at most daily `[S41]`.
7. **Vendor cost and lock-in.** Vision costs 5 credits a scan `[S37]`, and Scrydex has no free tier `[S42]`. Keep `Identifier` and `Pricer` behind interfaces.
8. **Non-English cards.** Scrydex prices Japanese cards in JPY `[S39]`. Your OCR models may not read them.
9. **OBS churn.** OBS 33 changed CEF and deprecated some APIs `[S10]`. The external-app design is mostly insulated, but re-test the overlay after each OBS update.

### 5.5 Decisions and changes after review (2026-10-27)

**Decisions:**
- Scrydex is the primary source for both identification and prices. The licensing question is settled on the user's side.
- pokemontcg.io will not be used.
- Cardstream is used for ideas and a few vendored modules, not as a fork or a dependency. Full mapping in [cardstream-review.md](cardstream-review.md#what-to-adopt-in-our-build).

**Changes to the plan above:**
- **Polling (milestone 1).**
  - Poll `GetSourceScreenshot` at a small width (about 640 px). On fire, take one full-resolution screenshot for the crop.
  - Keep one request in flight, never queue, and always analyse the newest frame.
  - Poll at about 1 Hz when the frame is empty or static and about 5 Hz while something moves. Pause polling while a paid call is running.
- **Trigger (milestone 3).** A pure, clock-injected decision core, tested offline, that sees each card once:
  - commit the card's signature when a call *starts*;
  - keep an LRU of recent pHash signatures so A→B→A doesn't pay again;
  - retry an unmatched card after 3–5 s, at most twice, then hand it to Claude or the picker;
  - wait at least 1 s between calls.
- **Overlay correctness.** Hide the price as soon as the card moves or changes, and never let an old card's price stay on screen.
- **Session.** A card already counted this session is not auto-added again. The dock offers "+ add another".
- **Robustness.**
  - Reject boxes smaller than 0.1 of the frame and quads far from card aspect.
  - Use a call guard with a 6–8 s timeout per external call.
  - Save the exact crop bytes before each paid call so failures become eval data.
- **Security.**
  - Bind the server to 127.0.0.1 only.
  - Check `Origin` on WebSocket and state-changing requests.
  - Put a per-run token in the overlay and dock URLs.

---

## Alternatives to Scrydex

Scope: services a new developer can sign up for (or use without signing up) today, 2026-10-27. Excluded because they are ruled out above: pokemontcg.io, the TCGplayer API and the Cardmarket API. A service that does both jobs counts in both categories. "No-key test" is a call I made on 2026-10-27 without credentials.

### A. Pricing data

| Service | Open to new devs? | Price / free tier | Rate limits | Price source and freshness | Per-variant pricing | Public display | No-key test |
|---|---|---|---|---|---|---|---|
| **TCGdex** (open source) | Yes. Free, no key needed `[S53]`. Repo is MIT `[S52]`. | Free. The project asks for sponsorship because "The API is costly to run" `[S50]`. | Not documented `[S50]` (**UNVERIFIED**). | Cardmarket (EUR) and TCGplayer (USD). Docs say "Updated hourly (TCGPlayer) to daily (Cardmarket)" `[S51]`. Server source: Cardmarket comes from Cardmarket's public `price_guide_6.json` file; TCGplayer comes via TCGCSV or the TCGplayer API; reload every 86,400,000 ms (24 h) `[S52]`. **Observed:** Cardmarket `updated` 2026-10-26, but TCGplayer `updated` was **2026-10-14** on every card I sampled `[S53]`. That contradicts the "hourly" claim. | Partly. `variants_detailed[]` lists `normal`/`reverse`/`holo`, with subtype (`unlimited`/`shadowless`) and `stamp: ["1st-edition"]`. TCGplayer prices are keyed `normal`/`reverse-holofoil`/`holofoil`. The Cardmarket block is per product, with reverse holo in its `*-holo` fields. Observed: Bulbasaur's `normal` and `reverse` variants carried the **same** pricing object, and the Base Set 1st Edition Charizard had Cardmarket prices but `tcgplayer: null` `[S53]`. | Not addressed `[S51]` (**UNVERIFIED**). | `GET https://api.tcgdex.net/v2/en/cards/sv03.5-006` returned **HTTP 200 in 0.18 s**, with pricing included `[S53]`. |
| **Cardmarket price-guide files** (public export) | Yes. "Anyone can import and incorporate the data into their own applications, no extra permission or access point necessary" `[S47]`. | Free. | n/a (static files). | Cardmarket marketplace, EUR. The article says "updated daily" `[S47]`. **Confirmed this time:** Pokémon is game 6. `price_guide_6.json` (15.7 MB, `createdAt` 2026-10-27, 80,170 entries) and `products_singles_6.json` (75,066 "Pokémon Single" products) both downloaded fine `[S54]`. I found the URL in TCGdex's source `[S52]`. | Fields `avg`/`low`/`trend`/`avg1`/`avg7`/`avg30`, plus a `*-holo` copy of each (the reverse-holo price on the same product) `[S54]`. 1st Edition and other printings are separate products (inference from the product list, **UNVERIFIED**). You'd need TCGdex `thirdParty.cardmarket` IDs `[S53]` to map products to cards. | Only the access statement above `[S47]`. No license text found (**UNVERIFIED**). | `HTTP 200` with no key `[S54]`. |
| **TCGCSV** (unofficial TCGplayer mirror) | No sign-up. Run by an individual. Describes itself as a "public entrypoint for information from TCGplayer's API". Has a TCGplayer affiliate link but states no affiliation `[S55]`. | Free. Patreon optional `[S55]`. | Not stated `[S55]`. | TCGplayer prices. "All content updates everyday around 20:00:00 UTC" `[S55]`. Observed `last-updated.txt` = 2026-10-26T20:05Z `[S55]`. | Yes, a row per `subTypeName` (e.g. `Normal`, `Reverse Holofoil`, `Holofoil`) with low/mid/high/market/directLow `[S55]`. | No terms or license stated `[S55]`. Whether TCGplayer allows this redistribution is **UNVERIFIED**. | `GET https://tcgcsv.com/tcgplayer/3/23237/prices` (the 151 set) returned **HTTP 200 in 0.04 s**. Charizard ex holofoil market was $8.85, vs $7.83 in TCGdex's stale copy `[S55][S53]`. |
| **JustTCG** | Yes `[S56]`. | Free: 1,000 calls/month, 100/day. Starter $19: 10k/month. Pro $49: 50k. Enterprise $149: 500k `[S56]`. | 10 / 50 / 100 / 500 per minute by plan. 429 with `Retry-After` `[S57]`. | "Real sales data from online marketplaces and from storefronts run by our Partners" (65+ stores). "Updated in real-time" `[S56]`. Exact marketplaces not named. | Yes. "Near Mint through Damaged are priced separately". "Foils, alt arts, promos and regional printings are distinct rows" `[S56]`. Printing examples are `Normal`, `1st Edition`, `Holofoil`, `Foil` `[S58]`. A Pokémon `Reverse Holofoil` value is **UNVERIFIED**. Lookup by `tcgplayerId` or by `set` + `number` `[S58]`. | Free tier is "for personal, non-commercial use". Paid tiers may "Show current prices ... to your users". Attribution optional. No redistributing raw data `[S59]`. | `GET https://api.justtcg.com/v1/cards?tcgplayerId=502558` returned **HTTP 401** `MISSING_API_KEY` `[S58]`. |
| **PokemonPriceTracker** | Yes `[S60]`. | Free: 100 credits/day. API: $9.99/month for 20k/day. Business: $99/month for 200k/day, with Cardmarket EUR and "commercial licensing" `[S60]`. 1 credit per card, +1 for history, +1 for graded `[S61]`. | 60/min (Free, API). 500/min (Business) `[S60]`. | "Updated daily from TCGPlayer and other marketplaces". Graded prices from eBay `[S60]`. | Yes, per printing (e.g. "Normal, Reverse Holofoil, 1st Edition") and per condition (`marketNearMint` ... `marketDamaged`). Note: "marketPrice is not always a Near Mint price" `[S61]`. | Any site or tool with revenue, "including ads" or sponsorship, needs Business. No redistribution on any plan. Attribution optional `[S62]`. | `GET /api/v2/cards?search=charizard` returned **HTTP 401** (needs a Bearer key) `[S61]`. |
| **PriceCharting API** | Yes, with a paid subscription `[S49]`. | **Confirmed this time:** API and CSV come with the **Legendary sub at $49/month**. Collector is $6/month without API `[S63]`. | 1 call/sec `[S49]`. | eBay sold listings plus its own marketplace. Grade is parsed from the listing title `[S64]`. | **Confirmed this time:** variants are separate products, e.g. `Charizard [1st Edition] #4`, `Charizard [Shadowless] #4`, `Bulbasaur [Reverse Holo] #1`, `[Cosmos Holo]`, `[Stamped]` `[S63]`. Ungraded and graded tiers per product `[S49]`. | "Licensed for internal use only". Showing data to others needs a commercial license `[S49]`. | `GET /api/product?q=charizard` returned **HTTP 400** "Must provide an access token" `[S49]`. |
| **Ximilar** (`price_stats` add-on) | Yes. Free plan, "No credit card required" `[S66]`. | See section B. | See section B. | Listing data from eBay, Rakuten and Rakuma, where "Most are without selling price" `[S67]`. Returns min/max/mean stats split into overall, graded and ungraded `[S65]`. | Not documented (**UNVERIFIED**). | Terms don't address display `[S68]`. | See section B. |

### B. Card identification

| Service | Open to new devs? | Price / free tier | Input | Latency | Accuracy claims | Variants | No-key test |
|---|---|---|---|---|---|---|---|
| **Ximilar Collectibles Recognition** (`POST https://api.ximilar.com/collectibles/v2/tcg_id`) | Yes. Free plan has 1,000 credits/month `[S66]`. | "Identify a TCG card": **10 credits**. Pricing: 10. Business is $69/month for 100k credits `[S66]`. My calculation: ≈ **$0.0069 per ID** on Business, and the free plan covers about 100 IDs a month. | JSON `records` (max 10), each a `_url` or `_base64` image. Options include `rotate`, `lang`, `price_stats` and `analyze_all` `[S65]`. | One sample response reports ~1.17 s `[S65]`. The Cardstream README says "roughly half a second" `[S69]`. | None in the docs `[S65]`. | Returns `best_match` plus `alternatives`, and tags such as `Foil/Holo` `[S65]`. Reverse holo and 1st Edition detection are not documented (**UNVERIFIED**). | **HTTP 401**: "Request must contain header authorization" `[S65]`. |
| **Ximilar Cardstream** (open source, Apache-2.0) | Yes (`pip install cardstream`) `[S69]`. | The app is free. You pay only for Ximilar ID calls `[S69]`. | Webcam, RTSP, **OBS Virtual Camera**, or OBS pushing RTMP/SRT to it `[S69]`. | All gating runs locally on CPU (motion gate, RF-DETR detection, pHash/embedding identity gate, sharpness hold). It makes "one identify call per distinct card presentation" `[S69]`. | None. | Same as Ximilar. | n/a (local app). |
| **Claude vision** (already in §2) | Yes. | ≈ $0.0002/scan on Haiku 5.5 (§2.2). | Base64, URL or Files API image `[S13]`. | Haiku 5.5 is "Fastest" in the lineup `[S14]`. | None for this task. | Prompted cues only (§2). | n/a |
| **CardGrader.AI** | Yes. Registering gives 1–3 trial credits `[S70]`. | "Identify" module costs 1 credit. Packs: 25 for $5, 120 for $19, 400 for $49 `[S70]`. My calculation: ≈ $0.12–0.20 per ID. | Card photos sent to `POST /v1/scans`. You poll for the result `[S70]`. | **"Results typically land in 30–120 seconds"** `[S70]`. Too slow for a live overlay. | Identifies "any Pokémon card from a photo" with name/set/number/variant. No percentage `[S70]`. | "Variant" field `[S70]`. | Not tested. |
| **Local OSS** (no account needed) | `remote-event/pokemon-tcg-scanner` (MIT; §2.1 `[S26]`). `pokemon-card-recognizer` (PyPI 0.0.1.3.8.7, 2025-05-02; license metadata contradicts itself, GPL-3.0-or-later vs MIT) `[S71]`. | $0 | Images or video `[S71]`. | GPU "~5x-10x faster" than CPU `[S71]`. | None `[S26][S71]`. | pokemon-card-recognizer needs a pokemontcg.io key to rebuild its references `[S71]`, and that API shuts down 2027-03-01 `[S31]`. | n/a |

### C. Ranking

**Best backup for pricing:**
1. **TCGdex.** It is free, open source and needs no key. It already maps cards to Cardmarket and TCGplayer product IDs, and Cardmarket data is fresh `[S52][S53]`. Its TCGplayer prices were 13 days stale on test day `[S53]`.
2. **JustTCG.** From $19/month it allows commercial display, and it prices by condition and printing `[S56][S59]`.
3. **TCGCSV (USD) or Cardmarket price-guide files (EUR).** Both are free daily bulk files and both were fresh on test day `[S54][S55]`. TCGCSV's legal standing is unclear.

PokemonPriceTracker is cheap, but a monetized stream needs its $99 Business plan `[S62]`. PriceCharting is $49/month and "internal use only" `[S49][S63]`.

**Best backup for identification:**
1. **Ximilar TCG ID.** It has a free tier, costs ≈$0.007 per ID on Business, and returns candidates with alternatives `[S65][S66]`. Its open-source **Cardstream** app is built for exactly this streaming use case `[S69]`.
2. **Claude Haiku 5.5.** Cheapest cloud option, already covered in §2.
3. **Local OSS (remote-event).** Free and works offline `[S26]`. CardGrader.AI is too slow for live use `[S70]`.

### D. How commercial apps get their data

| App | Price source it names | Feed type | How it identifies cards |
|---|---|---|---|
| **Collectr** | "This product uses TCGplayer data but is not endorsed or certified by TCGplayer" (app settings page). "Valued daily from real sales data" (homepage). Live eBay and TCGplayer listings shown side by side `[S72]`. | *Inference:* a licensed TCGplayer API key. TCGplayer's docs say existing API users must follow terms "including ... attributions required" `[S44]`, and Collectr's line reads like that attribution. Which other sales feeds it uses: **not disclosed**. | "Add any card with your camera". Method **not disclosed** `[S72]`. |
| **TCGplayer app** | Its own marketplace. *Inference:* it is TCGplayer's own app. Market Price wording from the app FAQ was not verified (the page returned 403) `[S73]`. | Own marketplace data. | "We built a new scanning engine". Live 2025-08-05, scans every TCG on TCGplayer. Method **not disclosed** `[S73]`. |
| **Dragon Shield Card Manager** | "We receive price information from your selected marketplace daily": Cardmarket (EUR) or TCGplayer, CardKingdom, MTGMintCard (USD) `[S75]`. | Licensed vs scraped: **not disclosed**. | Camera scanner. The FAQ admits it "does struggle a bit more with foil", and says to remove sleeves to cut glare. Method **not disclosed** `[S75]`. |
| **Ludex** | "Many data points from multiple marketplaces" plus "proprietary tech". Shows "every recent eBay sale". "Prices are updated daily" `[S76]`. | Aggregated marketplace sales. Partnerships: **not disclosed**. | "Machine learning technology", with "Parallel Detection" (foil/variation). Pokémon supported `[S76]`. |
| **PriceCharting (app/site)** | "We collect sold listing data from eBay and our own video game Marketplace" `[S64]`. | Own aggregation of eBay sold listings. | UPC barcode scanning, plus a "Photo Appraiser" `[S64]`. Image method **not disclosed**. |
| **CardCastle** (MTG-first) | "Card price data provided by TCGPlayer.com (via MTGJSON)". Pokémon card data and images from pokemontcg.io. Support article: TCGplayer Market Price or Card Kingdom, updated twice a day `[S77]`. | A free aggregator (MTGJSON) and pokemontcg.io. *Inference:* MTGJSON is MTG-only, so its Pokémon prices probably come through pokemontcg.io's embedded TCGplayer data (**UNVERIFIED**). | Phone scanning, "multiple cards in a single scan", and the CardBot sorting machine. Method **not disclosed** `[S77]`. |

What this shows:
- Commercial apps get prices in one of four ways:
  - an older TCGplayer API key (Collectr's attribution line, *inference*);
  - being the marketplace themselves (TCGplayer);
  - aggregating eBay sold listings (Ludex, PriceCharting);
  - free re-publishers (CardCastle via MTGJSON and pokemontcg.io).
- **None of them publish how they identify cards.**

Routes we could actually use:
- **TCGplayer affiliate program** (via Impact). It offers referral links and commission only. There is no data or API access. Contact: affiliates@tcgplayer.com `[S74]`. It could add "buy this card" links to the overlay, but it doesn't solve pricing.
- **Cardmarket public files** (row A above).
- **PriceCharting API**, if you buy a commercial license `[S49][S63]`.
- **Ximilar Cardstream** for identification `[S69]`.

No app offers a partner data feed publicly.

---

## Unverified items (summary)

- `GetSourceScreenshot` latency and throughput, and its effect on streaming performance (only the implementation was inspected `[S3]`).
- Whether OBS and another process can open the same physical webcam at once (OS/driver-dependent).
- Virtual Camera Source-mode output resolution and frame rate. Whether OBS allows more than one virtual camera.
- Browser Source `getUserMedia` camera access.
- Real-world accuracy and latency of every ID option (OCR, hashes, embeddings, Scrydex Vision, Claude) on hand-held cards. Claude's ability to tell foil type apart.
- The marketplace source behind Scrydex prices, and the exact time of day they update. Whether Scrydex allows on-stream display.
- Whether Cardmarket's public export files cover Pokémon (fetch blocked with 403).
- PriceCharting subscription prices, and how it models foil variants.
- Whether OBS frontend hotkeys fire while OBS is unfocused.
- Card-domain facts (1st Edition stamp, reverse-holo pattern, where the collector number sits) come from general knowledge, not a primary source.

---

## Sources

All accessed 2026-10-27.

**OBS**
- `[S1]` obs-websocket README: https://github.com/obsproject/obs-websocket
- `[S2]` obs-websocket 5.x protocol: https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md (GetSourceScreenshot, SetInputSettings, CallVendorRequest, BroadcastCustomEvent, connection steps)
- `[S3]` obs-websocket source, `TakeSourceScreenshot`: https://github.com/obsproject/obs-websocket/blob/master/src/requesthandler/RequestHandler_Sources.cpp
- `[S4]` obs-browser README (JS bindings, events, `obs-browser` vendor `emit_event`, platform support): https://github.com/obsproject/obs-browser/blob/master/README.md
- `[S5]` obs-browser source settings (`is_local_file`, `css`, `fps`, `restart_when_active`): https://github.com/obsproject/obs-browser/blob/master/obs-browser-plugin.cpp
- `[S6]` OBS Python/Lua scripting docs: https://docs.obsproject.com/scripting (source: https://github.com/obsproject/obs-studio/blob/master/docs/sphinx/scripting.rst)
- `[S7]` libobs core reference, `obs_add_raw_video_callback`: https://docs.obsproject.com/reference-core
- `[S8]` libobs source reference, `filter_video`: https://docs.obsproject.com/reference-sources
- `[S9]` obs-plugintemplate README: https://github.com/obsproject/obs-plugintemplate
- `[S10]` OBS Studio 33.0.0 release notes (2026-10-23): https://github.com/obsproject/obs-studio/releases/tag/33.0.0
- `[S11]` OBS Virtual Camera guide: https://obsproject.com/kb/virtual-camera-guide
- `[S12]` OBS virtual camera implementations: https://github.com/obsproject/obs-studio/tree/master/plugins/mac-virtualcam/src/camera-extension, https://github.com/obsproject/obs-studio/tree/master/plugins/win-dshow/virtualcam-module, https://github.com/obsproject/obs-studio/blob/master/plugins/linux-v4l2/v4l2-output.c

**Anthropic**
- `[S13]` Claude Vision docs (limits, `⌈w/28⌉×⌈h/28⌉` token formula, high-res tier, limitations): https://platform.claude.com/docs/en/build-with-claude/vision
- `[S14]` Claude models overview (IDs, comparative latency, default effort): https://platform.claude.com/docs/en/about-claude/models/overview
- `[S15]` Claude pricing: https://platform.claude.com/docs/en/about-claude/pricing
- `[S16]` Claude Haiku 5.5 overview: https://platform.claude.com/docs/en/models/haiku-5-5/overview
- `[S17]` Structured outputs (`output_config.format`, `output_format` deprecated): https://platform.claude.com/docs/en/build-with-claude/structured-outputs

**OCR and vision libraries**
- `[S18]` Apple `VNRecognizeTextRequest`: https://developer.apple.com/documentation/vision/vnrecognizetextrequest
- `[S19]` Apple `RecognizeTextRequest`: https://developer.apple.com/documentation/vision/recognizetextrequest
- `[S20]` Apple `VNGenerateImageFeaturePrintRequest` (on-device image similarity, macOS 10.15+; an option for a macOS-only embedding path): https://developer.apple.com/documentation/vision/vngenerateimagefeatureprintrequest
- `[S21]` Tesseract "Improving the quality of the output": https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html. Latest release 5.5.3 (2026-07-24): https://github.com/tesseract-ocr/tesseract/releases
- `[S22]` PaddleOCR (v3.7.0, 2026-06-11): https://github.com/PaddlePaddle/PaddleOCR
- `[S23]` imagehash (BSD-2-Clause): https://github.com/JohannesBuchner/imagehash
- `[S24]` DINOv2 (Apache-2.0): https://github.com/facebookresearch/dinov2. FAISS (MIT): https://github.com/facebookresearch/faiss

**Existing Pokémon card scanners**
- `[S25]` NolanAmblard/Pokemon-Card-Scanner: https://github.com/NolanAmblard/Pokemon-Card-Scanner
- `[S26]` remote-event/pokemon-tcg-scanner (README and `desktop/HYBRID_SCANNING.md`): https://github.com/remote-event/pokemon-tcg-scanner
- `[S27]` t-sinclair2500/pokemon-scanner: https://github.com/t-sinclair2500/pokemon-scanner

**Pokémon TCG API and Scrydex**
- `[S28]` Pokémon TCG API docs (deprecation notice): https://docs.pokemontcg.io/
- `[S29]` Pokémon TCG API rate limits: https://docs.pokemontcg.io/getting-started/rate-limits
- `[S30]` Pokémon TCG API card object (tcgplayer/cardmarket price fields): https://docs.pokemontcg.io/api-reference/cards/card-object
- `[S31]` PokemonTCG/pokemon-tcg-data README ("scheduled to be taken offline on March 1, 2027"): https://github.com/PokemonTCG/pokemon-tcg-data
- `[S32]` pokemontcg.io homepage banner: https://pokemontcg.io/
- `[S33]` Direct calls on 2026-10-27 to `https://api.pokemontcg.io/v2/cards?q=set.id:sv3pt5&select=id,tcgplayer,cardmarket`, `.../v2/cards?q=name:charizard`, and `.../v2/sets?orderBy=-releaseDate` (newest set: "30th Celebration", 2026/09/16)
- `[S34]` Scrydex homepage: https://scrydex.com/
- `[S35]` Scrydex docs intro: https://scrydex.com/docs
- `[S36]` Scrydex Vision API: https://scrydex.com/docs/vision/overview
- `[S37]` Scrydex API credits: https://scrydex.com/docs/getting-started/api-credits
- `[S38]` Scrydex rate limits: https://scrydex.com/docs/getting-started/rate-limits
- `[S39]` Scrydex pricing data: https://scrydex.com/docs/getting-started/prices
- `[S40]` Scrydex Pokémon cards endpoint and card object: https://scrydex.com/docs/pokemon/cards
- `[S41]` Scrydex best practices (caching, images): https://scrydex.com/docs/getting-started/best-practices
- `[S42]` Scrydex plans: https://scrydex.com/pricing
- `[S43]` Scrydex Terms of Service: https://scrydex.com/terms

**Other price sources**
- `[S44]` TCGplayer API getting started ("no longer granting new API access"): https://docs.tcgplayer.com/docs/getting-started
- `[S45]` TCGplayer Partner API deprecation: https://docs.tcgplayer.com/docs/partner-api-deprecation. Announcements: https://docs.tcgplayer.com/docs/announcements
- `[S46]` Cardmarket API help page: https://help.cardmarket.com/en/cardmarket-api
- `[S47]` Cardmarket "What's New on Cardmarket? (7/2025)" (public catalog and price-guide exports): https://insight.cardmarket.com/en/Insight/Articles/whats-new-on-cardmarket-7-2025
- `[S48]` Cardmarket API 2.0 docs (apiv2 base URL, request limits): https://apiv2.cardmarket.com/ws/documentation
- `[S49]` PriceCharting API documentation: https://www.pricecharting.com/api-documentation

**Alternatives to Scrydex: pricing**
- `[S50]` TCGdex homepage: https://tcgdex.dev/
- `[S51]` TCGdex market prices docs: https://tcgdex.dev/markets-prices
- `[S52]` TCGdex source (MIT): https://github.com/tcgdex/cards-database (`server/src/libs/providers/cardmarket.ts`, `server/src/libs/providers/tcgplayer/tcgcsv.ts`, `server/src/index.ts`)
- `[S53]` Direct no-key calls to TCGdex on 2026-10-27: `https://api.tcgdex.net/v2/en/cards/sv03.5-006`, `.../sv03.5-001`, `.../base1-4`
- `[S54]` Cardmarket Pokémon exports, downloaded 2026-10-27: https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_6.json and https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_6.json
- `[S55]` TCGCSV: https://tcgcsv.com/. Direct calls on 2026-10-27 to `https://tcgcsv.com/last-updated.txt`, `/tcgplayer/3/groups` and `/tcgplayer/3/23237/prices`
- `[S56]` JustTCG homepage and plans: https://justtcg.com/
- `[S57]` JustTCG rate limits: https://justtcg.com/docs/rate-limits
- `[S58]` JustTCG Cards API: https://justtcg.com/docs/api/cards. OpenAPI spec: https://justtcg.com/docs/swagger.json. No-key call to `https://api.justtcg.com/v1/cards?tcgplayerId=502558`
- `[S59]` JustTCG commercial use: https://justtcg.com/docs/commercial-use
- `[S60]` PokemonPriceTracker API plans: https://www.pokemonpricetracker.com/api
- `[S61]` PokemonPriceTracker API docs: https://www.pokemonpricetracker.com/api-docs. No-key call to `https://www.pokemonpricetracker.com/api/v2/cards?search=charizard`
- `[S62]` PokemonPriceTracker licensing: https://www.pokemonpricetracker.com/licensing
- `[S63]` PriceCharting subscriptions: https://www.pricecharting.com/pricecharting-pro. Product search pages: https://www.pricecharting.com/search-products?q=charizard+base+set+4&type=prices and `?q=bulbasaur+pokemon+151+1&type=prices`
- `[S64]` PriceCharting methodology: https://www.pricecharting.com/page/methodology. FAQ: https://www.pricecharting.com/faq

**Alternatives to Scrydex: identification**
- `[S65]` Ximilar Collectibles Recognition docs: https://docs.ximilar.com/collectibles/recognition. No-key call to `https://api.ximilar.com/collectibles/v2/tcg_id`
- `[S66]` Ximilar pricing: https://www.ximilar.com/pricing/
- `[S67]` Ximilar collectibles taxonomy (listing data sources): https://docs.ximilar.com/taxonomy/collectibles
- `[S68]` Ximilar terms: https://www.ximilar.com/terms-of-use-privacy/
- `[S69]` Ximilar Cardstream: https://github.com/Ximilar-com/cardstream. Announcement: https://www.ximilar.com/blog/cardstream-card-scanner-for-live-streams/
- `[S70]` CardGrader.AI API docs: https://cardgrader.ai/api-docs
- `[S71]` pokemon-card-recognizer: https://pypi.org/project/pokemon-card-recognizer/

**Commercial apps**
- `[S72]` Collectr: https://app.getcollectr.com/settings (attribution line), https://getcollectr.com/ and https://getcollectr.com/prices
- `[S73]` TCGplayer mobile app update: https://seller.tcgplayer.com/blog/tcgplayer-mobile-app-update. App FAQ (returned 403 to fetch): https://help.tcgplayer.com/hc/en-us/articles/115009506407-TCGplayer-App-FAQ
- `[S74]` TCGplayer affiliate program: https://docs.tcgplayer.com/docs/tcgplayer-affiliate-program
- `[S75]` Dragon Shield FAQs (Card Manager): https://www.dragonshield.com/pages/faqs
- `[S76]` Ludex FAQ: https://www.ludex.com/faq/. Features: https://www.ludex.com/features/
- `[S77]` CardCastle: https://cardcastle.co/. Price-source support article: https://support.cardcastle.co/en/articles/2031473-where-do-cardcastle-s-card-prices-come-from
