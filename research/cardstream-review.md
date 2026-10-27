# Cardstream source review: what to learn for pokescan

Reviewed 2026-10-27 from source. Repo: https://github.com/Ximilar-com/cardstream (Apache-2.0), cloned at commit `1ba5fc8e3a3da131559dd0ed350f23b70c12cbff` (tag `v0.5.2`, 2026-10-16). Nothing was installed, built or run. No model weights were downloaded and no calls went to Ximilar. Every finding cites `path:line`, relative to the repo root at that commit. Prefix a path with `https://github.com/Ximilar-com/cardstream/blob/1ba5fc8e3a3da131559dd0ed350f23b70c12cbff/` to open it. README statements are treated as claims and checked against the code. **UNVERIFIED** marks anything the code does not settle, such as latency figures, accuracy, and what the Ximilar API returns.

Companion to [obs-card-scanner.md](obs-card-scanner.md). "Our plan" and "§5" refer to that report.

---

## TL;DR

**Verdict: borrow ideas, and copy a few small self-contained modules with attribution. Don't fork Cardstream and don't depend on it.** The project is a polished, well-tested reference for one thing we need: a local state machine that decides when a paid identify call is worth making. It covers very little else of what we're building:
- no obs-websocket, and no overlay that works as an OBS Browser Source;
- no running total, undo, CSV or SQLite;
- no holo / reverse holo / 1st Edition handling;
- no candidate picker;
- identification and session storage are tied to Ximilar.

Checking the README's pipeline claims against the code:
- **Confirmed.** Motion gate → locator → identity gate (pHash or embedding) → sharpness hold → identify on a background thread, all in `src/cardstream/core/engine.py:291-400` and `src/cardstream/client/analyzer.py:257-342`.
  - The default locator is an **RF-DETR Seg Nano instance-segmentation** model (312×312, ONNX Runtime, CPU only). Its mask is reduced to four corners and the crop is deskewed with a perspective warp (`src/cardstream/core/detectors.py:298-356`, `src/cardstream/core/quad.py:57-81,169-197`, `model/README.md:38,140`).
  - Inputs really are webcam / file / RTSP / RTMP / SRT / ws (`src/cardstream/client/sources.py:275-299`).
  - `--price-stats` really is one flag on the same Ximilar call (`src/cardstream/core/identify_options.py:130-139`).
- **Overstated.**
  - "Three tiers" of detection throttling are effectively two by default: idle-with-card and empty are both 0.2 s (`src/cardstream/client/analyzer.py:99-101`).
  - "One call per distinct card" has real exceptions:
    - a card that comes back unmatched is retried every **0.5 s**, which the docs admit is "two calls a second" (`docs/tuning.md:55-59`);
    - a card away from the frame for **≥2 s** is paid for again (`src/cardstream/client/analyzer.py:79`, `src/cardstream/core/engine.py:329-343`);
    - A→B→A pays three times, because the gate remembers only the last card (`src/cardstream/core/engine.py:125,136-137`).
  - The "web UI overlay" is an operator page (header, side panel, Start button) that draws the box on a canvas over the video (`src/cardstream/webui/smart/index.html:24-90`). It is not a transparent stream overlay.
- **OBS input** works only by having OBS **push RTMP/SRT** into Cardstream's ffmpeg listener (`src/cardstream/client/sources.py:204-239`). That ties up OBS's stream output, so it doesn't suit a streamer who is live at the same time. There is no obs-websocket code anywhere: a grep for `obs`, `virtual cam`, `browser source` and `4455` finds only RTMP mentions.

**Top 5 ideas to adopt** (details in [What to adopt](#what-to-adopt-in-our-build)):
1. **A pure, clock-injected decision core** (`DecisionCore`), separate from I/O and scheduling. It makes the trigger policy unit-testable without a camera, OBS or a network.
2. **Commit the identity signature when the call fires, not when it returns.** Keep a short "card gone" memory, and allow a bounded retry for unmatched cards. Use a longer retry interval than theirs, because our calls cost 5 credits.
3. **The re-blur sharpness score with a hold, not a drop.** It replaces the variance-of-Laplacian in our §2.4. Start at 0.68 and send anyway after 3 s.
4. **Analyse small, identify big.** Detect on a downscaled frame and cut the paid crop from full resolution. Upscale crops whose long edge is under 500 px, and save the exact bytes sent before the POST.
5. **Cheap shape filters** (min size 0.1 of the frame, aspect ≥ 0.4), plus the robust mask/contour → quad → warp geometry (`order_quad`, an epsilon sweep, and a `minAreaRect` fallback).

**Surprises:**
- The stale-label window. After a fast swap, the previous card's name stays on screen while the new card settles and during the 2 s cooldown. This is deliberate and covered by a test (`tests/core/test_engine.py:588-605`). For a price overlay it means showing a wrong price.
- The camera WebSocket accepts any origin, so any web page could push frames and spend the API key (`src/cardstream/client/web_camera.py:78-80`).
- `SECURITY.md` and `docs/media/demo.gif` are referenced in the README but missing (`README.md:16,212`).
- The repo is about 2 months old: a single author, 29 commits, 1 star, 0 forks.

---

## 1. Pipeline: frame in → result on screen

### 1.1 Two entry points, one analyzer
- **`cardstream-client`** (headless). A synchronous loop: `for frame in source.frames(): analyzer.process(frame)`, with capped exponential reconnect (`src/cardstream/client/stream_client.py:74-94`, `Backoff` at `src/cardstream/client/sources.py:51-69`).
- **`cardstream-web`** (FastAPI + uvicorn, binds `127.0.0.1:8001` by default; `src/cardstream/client/web_client.py:62-145,167-174`). It has two modes, chosen by `--source` (`src/cardstream/client/web_client.py:7-17`):
  - **Camera mode** (default). The *browser* captures the webcam with `getUserMedia` (`src/cardstream/webui/shared/capture.js:23-30`) and pushes JPEGs over `/ws`. Each browser connection gets its own analyzer (`src/cardstream/client/web_camera.py:75-134`).
  - **Stream mode**. The process pulls a `FrameSource` on a worker thread (`asyncio.to_thread`) through one shared analyzer, and relays the frames to viewer pages (`src/cardstream/client/web_stream.py:26-112`).

### 1.2 Camera-mode trace

| Step | Where | Notes |
|---|---|---|
| Capture + encode | `src/cardstream/webui/shared/capture.js:76-111` | Canvas → `toBlob` JPEG at quality 0.8, capped at 1920 px wide (`src/cardstream/webui/smart/app.js:41`). Send rate slider 2–15 fps, default 10 (`src/cardstream/webui/smart/index.html:106-108`). Backpressure: one encode in flight, and skip a frame while `bufferedAmount` exceeds the last frame's size (`capture.js:82-85`). Docs claim encoding costs ~10/18/49 ms at 1280/1920/3840 (`docs/tuning.md:213-215`, **UNVERIFIED**). |
| Receive | `src/cardstream/client/web_camera.py:28-72` | **`LatestFrame`, a one-slot mailbox that drops instead of queueing**. The reader overwrites, and the consumer skips ahead up to 8 buffered frames (`_MAX_SKIP_AHEAD`, `:25`). |
| Decode + analyse | `src/cardstream/client/web_camera.py:118-128` | `decode_jpeg` → `analyzer.process(frame)`, run **synchronously on the asyncio event loop**, so ONNX inference blocks the loop while it runs. |
| Two resolutions | `src/cardstream/client/analyzer.py:260`, `src/cardstream/core/imaging.py:117-195` | `FramePair`: motion, detection, tracking and the gate run on a 960 px-wide copy (`DEFAULT_ANALYSIS_WIDTH`, `analyzer.py:48`). The paid crop is cut or warped from the **full-res** original (`imaging.py:165-195`). |
| Motion gate | `analyzer.py:270`, `src/cardstream/core/motion.py:22-57` | Every frame. See §2. |
| Throttled detect | `src/cardstream/core/engine.py:239-261`, `analyzer.py:288-289,302-342` | Inline and synchronous. Shape filters, then the decision core. |
| Identity gate, then sharpness | `engine.py:372-392`, `analyzer.py:351-372` | The gate runs on the *analysis-space* crop. Sharpness is measured on a full-res cut, and only when a call would otherwise fire. |
| Identify | `analyzer.py:374-398,440-473` | `threading.Thread(daemon=True)` per call → `requests.post` (`src/cardstream/core/ximilar.py:55-82`), 20 s timeout (`src/cardstream/core/identify_client.py:32`). |
| Push to page | `web_camera.py:87-95,128`, `src/cardstream/client/web_common.py:42-68` | A snapshot (`state`, `bbox`, `quad`, `identification`, `identify_calls`, `analysis` size) is sent for **every analysed frame**. A late result is pushed from the identify thread with `run_coroutine_threadsafe`. |

### 1.3 How latency is kept down
- Drop-don't-queue at both ends: the page skips encodes while busy, and the server keeps only the latest frame (`capture.js:82-85`, `web_camera.py:28-72`).
- Detection is throttled (0.1 s while moving, 0.2 s otherwise; `analyzer.py:99-101`). Analysis runs at 960 px, and only the paid crop uses full resolution (`imaging.py:117-135`).
- The network call runs off the frame loop on a thread. A `CallGuard` stops overlapping calls and force-clears a call that hangs past 20 s (`engine.py:47-90`).
- The request skips Ximilar's own detector: a synthetic full-image `_objects` box (`ximilar.py:46-52`) and prefilled category attributes (`identify_options.py:99-128`).
- End-to-end figures are **UNVERIFIED**. The README says the identify call takes "roughly half a second" (`README.md:52`). From the defaults alone, a still card fires after ≥2 below-threshold frames (≈0.2 s at 10 fps), plus at most one 0.2 s detect interval, plus the ~82 ms locator (claimed, `model/README.md:38`). That puts the first result at roughly **0.6–1.0 s** after the card stops moving. This is an estimate, not a measurement.

### 1.4 Weak spots in the pipeline (from reading the code)
- **Stream mode does not drop frames.** `StreamPump._run` analyses every frame the generator yields, in order (`web_stream.py:89-93`). If analysis is slower than the source, nothing skips ahead the way `LatestFrame` does. Whether latency then builds up depends on OpenCV/ffmpeg buffering. That is **UNVERIFIED**, because it would need a run.
- Stream mode re-encodes every analysed frame to JPEG at q70 and pushes it to every connected viewer (`web_stream.py:94-103`). That is per-frame CPU spent purely on preview.
- `DecisionCore` is mutated from both the frame loop and the identify thread with no lock (`engine.py:402-411` vs `:291-400`). It relies on the GIL, which is fine at this scale.

### 1.5 Input from OBS
- **Only by pushing a stream.** `--source rtmp://0.0.0.0:1935/live --listen` starts the system `ffmpeg` as an RTMP/SRT listener and reads MJPEG from its stdout (`src/cardstream/client/sources.py:204-272`, argv at `:224-239`). It needs `ffmpeg` on the PATH, and the hint text is macOS-only (`brew install ffmpeg`, `:242-246`).
  - OBS has one main Stream output, so pointing it at Cardstream means it isn't streaming to Twitch. Sending a second RTMP stream would need a recording output or a plugin (**UNVERIFIED**; not covered by the repo).
  - The path also adds an encode → RTMP → decode round trip.
- **Webcam index** (`--source 0`; `sources.py:88-141`). OBS Virtual Camera would presumably show up as a camera index, but the repo never mentions it (**UNVERIFIED**).
- **No obs-websocket and no `GetSourceScreenshot`.** Our §1.3 choice has no counterpart here, so there is nothing to compare it with.

---

## 2. Gating and triggering (the reusable core)

All policy lives in `DecisionCore` (`src/cardstream/core/engine.py:161-429`). It is pure and synchronous, and it takes `now` as an argument. The driver (`SmartAnalyzer`) owns scheduling and I/O (`engine.py:1-9`). Every default is in one frozen dataclass (`src/cardstream/client/analyzer.py:51-109`), and the CLI reads its defaults from there (`src/cardstream/client/common.py:184-603`).

### 2.1 The stages and their numbers

| Stage | What exactly | Default | Code |
|---|---|---|---|
| **Motion gate** | Whole frame → gray → resize to **160×90** → Gaussian 5×5 → **mean absolute difference** against the previous frame (0–255). "Settled" means the score stays under the threshold for N *consecutive* frames. The first frame returns 255. | threshold **8.0**, N = **2** frames | `src/cardstream/core/motion.py:22-57`; `analyzer.py:97-98` |
| **Detect throttle** | The interval is chosen per frame: tracker locked → 2.0 s; motion ≥ threshold → `moving`; card present → `idle_with_card`; else `empty`. One detection in flight at a time. | 0.1 s / 0.2 s / 0.2 s (tracking 2.0 s) | `engine.py:25-37,239-261`; `analyzer.py:99-104` |
| **Shape filters** | The box must be ≥ fraction × frame in *both* dimensions, and short side / long side ≥ floor (orientation-blind; a card is ~0.71). A rejected box is treated as "no card". | 0.1, 0.4 | `src/cardstream/core/detection_filters.py:35-94`; `analyzer.py:69,73,311-317` |
| **Locator confidence** | Sigmoid score of the best DETR query; below the threshold counts as no card. | **0.6** (raised from 0.35 in 0.5.1) | `src/cardstream/core/detectors.py:112-129,445`; `CHANGELOG.md:29-30` |
| **Settled + cooldown** | An identify is considered only if the motion gate was settled *when the detection was scheduled*, and ≥ cooldown since the last call *started*. | cooldown **2.0 s** | `engine.py:351-354`; `analyzer.py:78` |
| **Identity gate (pHash)** | Grayscale → 32×32 → DCT → top-left 8×8 → threshold against the median (DC term excluded) → 64-bit hash. Hamming distance **> 10** means NEW. | 10 | `motion.py:60-80`; `engine.py:43,115-140` |
| **Identity gate (embedding, default)** | Finetuned MobileNetV2 ONNX (384×384, 9.8 MB, ~5 ms/crop claimed), L2-normalised. Cosine **< 0.85** means NEW. The threshold is "tuned for the finetuned embedder" and won't transfer to another model. | 0.85 | `src/cardstream/client/embedders.py:155-182`; `analyzer.py:57`; `model/README.md:39,169-170` |
| **Sharpness hold** | Re-blur ratio (Crete et al. 2007). Shrink the crop to a 256 px long edge, apply a 9-tap box blur per axis, and compute the share of neighbour-to-neighbour contrast the blur removes. Score = the **minimum over the two axes**, so one-axis motion smear fails. Measured on the full-res crop without expansion, and only when a call is about to fire. | **0.68**; send anyway after **3.0 s** | `src/cardstream/core/sharpness.py:35-66`; `analyzer.py:92,96,351-372`; `engine.py:377-392` |
| **Result threshold** | Drop a match whose best distance is > threshold, and treat it as unmatched. | **0.9** (very loose) | `analyzer.py:59-61,450-458` |
| **Retry unmatched** | After a failed or dropped match, reset the gate after N s so the *same* card gets another call, repeating while it stays unnamed. | **0.5 s** | `engine.py:200-206,356-370`; `analyzer.py:85` |
| **Forget after** | When the card returns after being away ≥ N s, reset the gate and the shown name. | **2.0 s** | `engine.py:195-199,329-343`; `analyzer.py:79` |
| **Watchdog** | Force-clear an in-flight flag after 20 s. The call itself is not cancelled. | 20 s | `engine.py:44,71-75` |

The sharpness docstring claims in-focus crops score 0.75–0.81 "whatever is printed on it" and out-of-focus crops 0.46–0.63, and that variance-of-Laplacian "moves about 2x with the artwork" (`src/cardstream/core/sharpness.py:13-17`). The tests use only *synthetic* cards (`tests/core/test_sharpness.py:1-7,31-41`), and the repo contains no real-crop fixtures. The real-crop numbers are therefore **UNVERIFIED**. So is the anecdote that "four cards cost seven calls … with the hold they cost four" (`docs/tuning.md:86-89`, `CHANGELOG.md:50-54`).

### 2.2 How "new card" is decided, and why they don't pay twice
`DecisionCore.on_detection` (`engine.py:291-400`) checks, in this order:
1. **Card gone** (`det is None`). Stamp `_absent_since`, clear the box and quad, and **keep** the gate signature and the last identification, so a quick return shows instantly at zero cost (`:317-327`).
2. **Card back after a long absence** (≥ `forget_after`). Reset the gate and the name, so the next look counts as NEW (`:329-343`).
3. **Not settled** → no call (`:351-352`). **Cooldown or a call in flight** → no call (`:353-354`).
4. **Retry is due for an unmatched card** → reset the gate (`:356-370`).
5. **Gate says SAME** → no call. "It is already paid for" (`:372-375`).
6. **Gate says NEW, but the crop is blurred** → *hold*. Nothing is committed (no signature, no cooldown), and the next detection asks again. After `send_blurred_after` it is sent anyway (`:377-392`).
7. **Fire.** `identify.begin(now)`, then **`gate.commit(token)` at fire time**, and the shown name is cleared (`:394-400`).

**Commit-on-fire** is the key policy (`engine.py:97-100`). The signature is stored when the call is *launched*, so a card the upstream can't identify is not hammered every frame. It retries only when the card changes, or when `retry_unmatched` expires. `on_identify_done(None)` keeps the committed signature (`:402-411`). The paid-call counter increments on fire, matched or not (`analyzer.py:172-175,444`).

### 2.3 Re-shown and quickly swapped cards (and the gaps)
- **Same card, brief dropout (< 2 s):** free. The name is re-shown from memory (`tests/core/test_engine.py:246`, `:331`).
- **Same card, away ≥ 2 s:** paid again by design (`docs/tuning.md:225-235`, `tests/core/test_engine.py:318`).
- **A → B → A:** the gate holds **one** signature (`engine.py:125,136-137`; `embedders.py:167,178-179`), so the second A is NEW and paid again. There is no recent-cards cache.
- **In-place fast swap.**
  - While the hand moves, `settled` is false and the gate isn't consulted (`engine.py:351`).
  - Once settled, the cooldown (2 s from the *previous call's start*) blocks the call (`:353`; `tests/core/test_engine.py:200-211`).
  - During that whole window `snapshot()` reports `IDENTIFIED` with the **previous card's** identification, because `last_ident` is only cleared when the new call fires (`engine.py:394-400,421-424`).
  - The same holds during a sharpness hold, and that case is deliberate (`tests/core/test_engine.py:588-605`).
  - **For a price overlay, this window shows a wrong price.**
- **The tracker** (optional vitTracker) gives presence only, not identity. An in-place swap keeps its score high, so the tracker never replaces the gate (`README.md:217-218`, `model/README.md:183-184`).
- **The unmatchable-object trap.** At `retry_unmatched = 0.5 s`, a slab back or a hand that keeps being detected costs "two calls a second for as long as it is there" (`docs/tuning.md:57-59`). At Scrydex Vision's 5 credits a call, that would be ~600 credits per minute, 12% of the Starter plan's monthly 5,000 (computed from our report's `[S37][S42]`).

### 2.4 History and session layer on top of the gate
`ShowHistory` (`src/cardstream/core/show_history.py:1-225`) mirrors the page's history list (`src/cardstream/webui/shared/overlay.js:273-377`):
- one row per card, keyed on `full_name|name|set|card_number` (`show_history.py:45-48`);
- a card put back resumes its row and its "on stream" time unless `--split-results` is set;
- a row appears only after the card has totalled **≥ 1.0 s** on stream (`show_history.py:37,215-221`).

A card glimpsed mid-swap is still *paid for*, only hidden (`src/cardstream/client/web_client.py:214-226`).

---

## 3. Detection and cropping

- **Models, all ONNX Runtime with `CPUExecutionProvider` only** (`src/cardstream/core/_onnx.py:20-22`). No CoreML, CUDA or DirectML provider is wired up; only the torch/transformers RT-DETR backend probes CUDA/MPS (`detectors.py:384-403`).
  - **Default: RF-DETR Seg Nano**, 312×312 input, 100 queries, 1 class (`model/README.md:140`). 125 MB `.onnx` per the model README, 108 MB tarball per the README (`model/README.md:38`, `README.md:103`). "~82 ms/frame" on an Apple M-series CPU (`model/README.md:38,50-52`, **UNVERIFIED**).
  - Optional box detectors: RF-DETR or RT-DETRv2, "~80-150 ms/frame", weights not published (`model/README.md:40,90-105`).
  - Optional vitTracker: 698 KB, ~2.7 ms per frame (`model/README.md:41`, `src/cardstream/core/tracking.py:37-65`).
  - No accuracy figures are given, on purpose (`model/README.md:54-58`). The training pipeline lives in a "sibling `cardstream/detector/` repo" (`model/README.md:92-93`); whether that repo is public is **UNVERIFIED**.
- **Licensing.** The code is Apache-2.0 (`LICENSE`, `pyproject.toml:14`). The NOTICE and model README say the architectures (RF-DETR, RT-DETRv2, MobileNetV2, vitTracker) *and* the Ximilar-finetuned weights are Apache-2.0 (`NOTICE:13-42`, `model/README.md:43-48`). The weight licence is asserted in the repo, but I didn't check it against the release assets (**UNVERIFIED**).
- **Decode → quad** (`detectors.py:333-356`, `src/cardstream/core/quad.py`):
  - take the winning query's 78×78 mask logits, upsample bilinearly to the frame and threshold at logit > 0 (`detectors.py:339-348`);
  - take the largest external contour, then sweep `approxPolyDP` epsilon from 1% to 10% of the perimeter in 0.5% steps until it yields exactly 4 vertices, falling back to `minAreaRect` (`quad.py:57-81`);
  - order the corners by **angle around the centroid**, not the usual sum/diff rule (which "degenerates near 45°"), force clockwise, and start at the top-left (`quad.py:35-54`);
  - if no usable quad comes out, fall back to the model's own box (`detectors.py:352-355`).
- **Warp** (`quad.py:169-197`). The output size comes from the quad's own longest edges, so the crop keeps native resolution. It is not a fixed 630×880 like our plan. Corners are *not* clamped, and `warpPerspective` fills what falls outside. Implausible quads (> 4× the frame) are rejected.
  - Orientation is **not normalised**: a landscape or upside-down card stays that way. They instead assert `Rotation: rotation_ok` to Ximilar (`identify_options.py:113-115`).
  - Box locators get a 4% context margin instead (`detectors.py:45-47,151-164`).
- **Crop for the call** (`src/cardstream/core/identify_client.py:74-89`, `src/cardstream/core/imaging.py:18,87-100`): upscale to a ≥ 500 px long edge with cubic interpolation if smaller (the comment says tiny crops caused intermittent HTTP 500s and worse matches), then JPEG q90 → base64.
- **CPU weight compared with plain OpenCV.** By default the detector runs every 0.1 s while moving and every 0.2 s otherwise (`analyzer.py:99-101`). With the claimed ~82 ms per run, that is roughly **80% of a core while the scene moves and ~40% while a card is held**. On top of that come decoding a 1920-px JPEG per frame at 10 fps and ~5 ms of embedding per gate check. These are estimates from claimed figures. A Canny/contour quad on a 640-px frame is typically a few ms (general OpenCV knowledge, **UNVERIFIED** here). Their segmentation's advantage is robustness: sleeves, cluttered playmats, hands over the corners. No benchmark in the repo quantifies it.

---

## 4. Identification and confidence

- **Call.** `POST https://api.ximilar.com/collectibles/v2/tcg_id` with header `Authorization: Token <key>` and `User-Agent: CardStream` (`src/cardstream/core/id_types.py:127-141`, `identify_client.py:37-51`). Body shape (`identify_options.py:99-139`):

  ```
  {"records":[{"_base64":…, "set_code"?:…,
               "_objects":[{"prob":1.0,"name":"Card","bound_box":[0,0,w,h],
                            "Top Category":"Card","Category":"Card/Trading Card Game",
                            "Side":"front","Rotation":"rotation_ok",
                            "Subcategory"?:"Pokemon","Alphabet"?:…}]}],
   "price_stats"?: true}
  ```

  - There is no retry or backoff. On any failure (connection error, non-2xx, non-JSON) the client logs and returns `None` (`ximilar.py:55-82`).
  - A new `requests.post` is made per call because "Ximilar drops idle keep-alive sockets" (`ximilar.py:66-68`).
- **A useful finding about hints.** Prefilling `Subcategory` (the game) switches off Ximilar's alphabet classifier, which then assumes latin, so a Japanese card matched its English print. They therefore require `--alphabet` alongside `--game`, and validate it locally because the API silently accepts unknown values (`id_types.py:28-35,205-220`; `docs/tuning.md:171-180`; `README.md:169-172`). The general lesson for any identifier we use: **a hint can quietly disable a classifier.**
- **`best_match` vs `alternatives`** (`ximilar.py:133-180`):
  - Every field shown comes from `best_match`: name, full_name, set, set_code, card_number, series, year, subcategory and links. The distance is `distances[0]`, defaulting to 1.0.
  - `alternatives` is cut to the **first 4**, keeping only `full_name`, `set` and `links`. The other entries of `distances` are thrown away, so alternatives carry no scores.
  - The UI lists them in a collapsed `<details>` with **no way to pick one** (`overlay.js:256-263`, `index.html:71-74`).
- **Confidence.**
  - Distance tiers: **high ≤ 0.30, medium ≤ 0.40, low > 0.40** (`ximilar.py:24-43`). These moved from 0.18/0.30 one release earlier (`CHANGELOG.md:31-32`), and the repo has no calibration data.
  - The tiers can't be set from the CLI (`common.py:825` doesn't pass `tiers`).
  - Low confidence is only a coloured label (`overlay.js:233-234`). Matches with distance > 0.9 are dropped and treated as unmatched, which triggers the 0.5 s retry (`analyzer.py:450-458`).
  - There is no top-1 vs top-2 margin test, no second opinion, and no confirmation step.
  - Every result carries the **locator confidence** (`object_confidence`) next to the distance, so a bad match can be traced to a bad crop or a bad lookup (`analyzer.py:488-492`, `overlay.js:42-48`).
- **Holo / reverse holo / 1st Edition: not handled anywhere.** A grep for `holo|reverse|foil|1st|edition|variant` over all source, docs and tests finds nothing card-related. `Identification` has no variant field (`src/cardstream/core/models.py:80-99`). Whether Ximilar's response carries variant data is **UNVERIFIED**.

---

## 5. Pricing (`--price-stats`)

- **What it does.** It adds top-level `"price_stats": true` to the *same* identify request. That is one call for ID and price. The flag is sent only for tcg/sport/comics (`identify_options.py:130-139`, `id_types.py:69-73,140`). It is off by default because "the extra data is not documented as free" (`identify_options.py:50-55`; `docs/tuning.md:167-168`). Its credit cost is **UNVERIFIED**.
- **What comes back.** `best_match.price_stats` is a list of entries per `stats_type` (`ungraded`, `graded`, `overall`), each with an `interval` and `min/max/mean/median/q1/q3/latest/oldest` plus `latest_date/oldest_date`. `std`, `range` and `trend` are dropped (`src/cardstream/core/prices.py:1-17,46-77`).
  - Currency is **assumed USD**: "the API names no currency" (`prices.py:7-8,102-104`).
  - Prices are **not per variant or condition**. Only ungraded vs graded aggregates exist.
  - Which marketplace the sales come from is not stated anywhere in the repo (**UNVERIFIED**).
- **How it's shown.** It shows ungraded and graded, with overall only as a fallback (`prices.py:25-28,80-93`). The card panel shows the median, the (min–max) range and the latest sale with its date. A history row reads like `ungraded $24.99 (15–60) · graded $45 (30–80)` (`overlay.js:96-142,316-323`). A line-for-line JS twin of the Python formatter is pinned by tests (`prices.py:12-14`, `tests/webui/price-stats.test.js`).
- **Comparison with our plan.** Our plan wants a per-variant raw NM `market` price, frozen at scan time (§3). Cardstream's figure is a sales-distribution median not tied to a finish, which is the wrong grain for holo vs reverse holo.

---

## 6. Overlay and UI

- **Transport.** One WebSocket `/ws`. JSON text frames carry `{state, bbox, quad, crop_quad, identification, identify_calls, analysis}` on every analysed frame, plus an immediate push when a result lands (`web_common.py:42-68`). In stream mode, binary frames carry JPEG previews (`web_stream.py:94-103`). The client reconnects with backoff from 0.5 s up to 5 s (`src/cardstream/webui/shared/ws.js:4-69`). There is no SSE.
- **Rendering.** Plain ES modules with no build step (`src/cardstream/webui/smart/app.js:16-21`).
  - The box or quad is drawn on a `<canvas>` over `<video>`/`<img>` with `requestAnimationFrame`, mapped through the media's object-fit: contain rectangle (`overlay.js:397-461`).
  - Five state badges: empty / moving / settled / identifying / identified (`overlay.js:194-198`, `models.py:13-20`).
  - DOM is built with `createElement`, so card names aren't injected as HTML. One exception: `innerHTML = ""` is used only to clear (`overlay.js:244,258`).
- **As an OBS Browser Source: not as-is.**
  - The page is a full app: header badges, a 500 px side panel, a debug log, a Start button and a settings dialog (`index.html:24-124`).
  - Camera mode needs a click on Start plus `getUserMedia` (`src/cardstream/webui/shared/session.js:49-64`). Inside OBS that would fight OBS for the webcam (**UNVERIFIED**).
  - Stream mode does auto-start (`app.js:131-137`), but it shows the relayed video and panel, not a transparent layer.
  - The message protocol, however, is a good model for a dedicated overlay page.
- **Session state.**
  - The history list lives in browser memory: at most 50 rows, lost on refresh, no persistence (`overlay.js:162-189,341-343`).
  - A per-analyzer paid-call counter is shown as a badge (`app.js:95-98`).
  - **There is no running price total, no undo, no CSV and no SQLite.** A grep for `total|undo|csv|sqlite` finds nothing relevant.
  - The only persistence is the opt-in `--ximilar-stream`. It uploads history rows, crops and call counts to Ximilar's Cards Cloud (`/cards-cloud/v2`) using a background queue that batches, backs off and re-sends idempotently (`src/cardstream/core/ximilar_session.py:1-33,57,196-235`).
  - It has a platform list that includes whatnot/tiktok/twitch, which suggests live-selling streams are the intended audience (`ximilar_session.py:61`).
- **Security.**
  - The server binds `127.0.0.1` by default (`web_client.py:170-174`), and the README is upfront that the local app is unauthenticated (`README.md:209-212`).
  - The Docker image binds `0.0.0.0` and warns to publish on loopback only (`Dockerfile:3-9`).
  - `/ws` calls `websocket.accept()` with **no Origin check** (`web_camera.py:78-80`, `web_stream.py:115-118`). Browsers don't apply CORS to WebSockets, so any page the operator visits could connect to `ws://127.0.0.1:8001/ws` and push frames that spend the key. That's an inference from the code; I didn't test it.

---

## 7. Code quality and maturity

- **Structure.**
  - Clean split between `core/` (numpy + cv2 + requests, strict mypy) and `client/` (sources, web, CLI) (`pyproject.toml:134-158`).
  - Interfaces everywhere: `CardDetector`, `IdentityGate`, `Embedder`, `FrameSource`, `IdentifyTarget`, `DetectionFilter`.
  - ML imports are lazy, so the base install pulls only numpy, opencv-python-headless and requests (`pyproject.toml:37-41`). Extras add fastapi/uvicorn/websockets/pydantic, onnxruntime, torch/transformers, or LiteRT (`:43-65`).
  - Comments are dense and explain *why*.
  - About 7.1k lines of Python and 2.0k of JS/CSS/HTML under `src/`; about 9.3k lines of tests (counted with `wc`).
- **Tests.**
  - About 527 pytest functions and 45 `node --test` cases (counted with grep).
  - The suite is fully offline: synthetic cards (`tests/_helpers.py`), an injected clock, and a fake DOM for the JS (`tests/webui/_fake-dom.js`).
  - `filterwarnings = ["error"]` (`pyproject.toml:175`), and a docs-contract test keeps the CLI reference in sync with the parser (`tests/test_docs_contract.py`, `.github/workflows/ci.yml:34`).
  - The suite was **not run** for this review.
- **Platforms.**
  - CI runs tests on Ubuntu (Python 3.11–3.13), macOS (3.12) and **Windows (3.12)** (`.github/workflows/ci.yml:44-54`).
  - The one-line installer refuses Windows (`scripts/install.sh:42-46`), and the ffmpeg hints are brew-only (`sources.py:244`).
  - The wheel is pure Python (`scripts/build-release.sh:7-8`). The model weights are a separate download (`README.md:95-114`).
- **Activity.**
  - 29 commits from 2026-08-21 to 2026-10-16, all by one author (Michal Lukac), starting from a squashed "Initial commit" (`git log`).
  - GitHub on 2026-10-27: 1 star, 0 forks, 1 open issue, last push 2026-10-21 (GitHub API).
  - Version 0.5.2, "Development Status :: 4 - Beta" (`pyproject.toml:7,23`).
  - Defaults are still moving: tiers, detector confidence and the sharpness hold all changed in early October (`CHANGELOG.md:20-54`).
- **Loose ends.** `SECURITY.md` and `docs/media/demo.gif` are linked but missing (`README.md:16,212`; `docs/sources.md:47-48`). The model size is quoted inconsistently (108 MB vs 125 MB).
- **Assessment.** High-quality code that is young, single-maintainer and vendor-driven: it exists to sell Ximilar's identify API. It makes **a very good reference** and a **poor base** for us:
  - The `IdentifyTarget` ABC has exactly one implementation, and `Identification` is shaped like Ximilar's response (`src/cardstream/client/identify_target.py:1-37`, `models.py:80-104`).
  - The UI assumes browser-side capture.
  - Everything we'd add (obs-websocket, overlay, dock, SQLite session, totals, undo, CSV, variants, picker, Scrydex, Claude) is outside its scope.

---

## What to adopt in our build

Mapped to the components and milestones in [obs-card-scanner.md §5](obs-card-scanner.md#5-recommended-architecture-and-mvp-plan). "Copy" means vendoring the file with its Apache-2.0 header and a NOTICE entry (Apache-2.0 §4).

| # | Idea (Cardstream source) | Recommendation | Why / how, for pokescan | Our §5 part |
|---|---|---|---|---|
| 1 | **Pure decision core with injected clock** (`engine.py:161-429`). The driver owns I/O (`analyzer.py`). | **Adopt the pattern; rewrite.** | Write `trigger.py` with `on_poll(settled, score, now)`, `on_detection(quad, crop, now, sharp)` and `on_identify_done(result)`, returning actions. Test it like `tests/core/test_engine.py`. Our extra states (`AWAITING_PICK`, `CONFIRMED`, `VOIDED`) slot in cleanly. | detector + identifier glue (5.1); milestone 3 |
| 2 | **Commit-on-fire identity gate** (`engine.py:97-100,394-396`) | **Adopt as-is.** | Store the signature when the Scrydex call *starts*, so an unmatchable card isn't re-sent every poll. | detector (5.1); milestone 3–4 |
| 3 | **Retry unmatched** (0.5 s, `analyzer.py:85`) | **Adapt.** | Their default would burn ~600 credits a minute on an unmatchable object at 5 credits a call. Use a 3–5 s retry, at most 2 attempts per presentation, and after that hand off to Claude Haiku and/or the top-3 picker instead of retrying Vision. | identifier fallback (2.3 step 3, 5.3) |
| 4 | **Forget-after 2 s** (`engine.py:329-343`) | **Adapt.** | Keep a short dropout memory so a card dipping out of frame is free. Replace the single-slot gate with an **LRU of recent signatures → identification** (say the last 20 cards, pHash plus Hamming threshold) so A→B→A doesn't pay again. Whether a re-shown card *adds to the total again* is a session rule (see #12), not a gate rule. | detector (5.1); session (§4) |
| 5 | **pHash gate** (`motion.py:60-80`, threshold 10/64) | **Copy** (`phash`/`hamming`, ~20 lines), as the MVP gate. | Zero dependencies, and it works on our 630×880 warped crop. Retune the threshold on our eval set (milestone 6). The embedding gate (cos 0.85) is tied to their finetuned model. Skip it for now; if we build the phase-2 DINOv2 index, reuse that embedding as the gate. | detector (5.1); milestone 7 |
| 6 | **Re-blur sharpness + hold** (`sharpness.py`; 0.68, send-anyway 3 s; `engine.py:377-392`) | **Copy** `sharpness.py` (~40 lines); **adopt the hold semantics.** | Replaces variance-of-Laplacian in our §2.4. Measure on the warped crop shrunk to 256 px, taking the minimum over the two axes. Hold the call (no commit, no cooldown) instead of failing, and send anyway after 3 s. Check their real-crop ranges (0.75–0.81 / 0.46–0.63, **UNVERIFIED**) against our webcam and sleeves. | detector gate (2.4, 5.1) |
| 7 | **Global motion gate** (160×90 mean abs-diff, threshold 8, 2 frames) | **Adapt; keep our corner-stability trigger as the primary.** | Whole-frame diff counts the streamer's body and background as "motion". Our plan's per-card corner stability (<1–2% of frame width over 0.5–1 s) is the better trigger. Use the cheap global diff only to pick the **poll rate**. | obs client polling (1.3, 5.1); risk 5.4 #5 |
| 8 | **Tiered throttle** (`engine.py:239-261`) applied to *polling* | **Adapt.** | Their tiers throttle detection. For us the expensive step is `GetSourceScreenshot`, a synchronous GPU readback in OBS (§5.4 #5). Poll ~1 Hz when empty/static, ~5 Hz while moving or a card is present, and stop polling while a call is in flight. This directly reduces our biggest OBS-performance risk. | obs client (5.1); milestone 1 |
| 9 | **Analyse small, identify big** (`FramePair`, `imaging.py:117-195`) | **Adapt.** | Poll `GetSourceScreenshot` at a small `imageWidth` (e.g. 640) for detection and stillness. On fire, request **one** full-resolution screenshot, re-find the quad on it, then warp. This costs one extra screenshot per scan and saves most of the per-poll cost. **UNVERIFIED**: the latency of a native-width screenshot (measure it in milestone 1). | obs client + detector (5.1) |
| 10 | **Shape filters** (`detection_filters.py`; size ≥ 0.1, aspect ≥ 0.4) | **Adopt** (trivial). | Run them before any paid call. Because we have corners, add a tighter quad-aspect check around 0.716 ± tolerance, plus convexity. | detector (5.1) |
| 11 | **Quad geometry** (`order_quad` by angle, ε sweep 1–10%, `minAreaRect` fallback, unclamped warp: `quad.py:35-81,169-197`) | **Copy** `quad.py`'s functions (~120 lines). | They work on any contour, including our Canny/threshold contour, not only a segmentation mask. Angle-sorting fixes the 45° failure of the common sum/diff ordering. Keep our fixed 630×880 output and add portrait normalisation, which they lack. | detector (2.3 step 1) |
| 12 | **History row semantics** (one row per presentation, ≥ 1 s on stream, merge re-shows: `show_history.py`) | **Adapt to SQLite.** | Insert a `scan` row only when the result is confident or confirmed **and** the card has been in shot ≥ ~1 s. When the same `card_id` is seen again in the session, **don't auto-add it**: show "already counted, press + to add another" in the dock. That rule is our product decision and needs the streamer's input. | session store (§4, 5.1); milestone 2 |
| 13 | **Stale-label behaviour** (old name kept through moving, cooldown and hold: `engine.py:394-400`, `tests/core/test_engine.py:588-605`) | **Do the opposite.** | Separate "what the overlay shows" from "what's in the total". Hide the price as soon as the quad moves or the gate says different (show "identifying…"). Never let a cooldown keep an old price on screen. | overlay (5.1, 5.3) |
| 14 | **Cooldown 2 s between calls** | **Adapt.** | Keep a small cooldown (~1 s) as a cost safety net. Thanks to #13 it must not cost display correctness. | detector (5.1) |
| 15 | **CallGuard + watchdog** (`-inf` start sentinel, force-clear on hang: `engine.py:47-90`) | **Copy** (~40 lines). | Use one guard each for screenshot, Vision, Claude and price calls. Lower their 20 s timeout to ~6–8 s for a live show, then fall back to the picker. | identifier/pricer (5.1, 5.3) |
| 16 | **Drop-don't-queue mailbox** (`LatestFrame`, `web_camera.py:28-72`) | **Adopt the rule.** | With `GetSourceScreenshot` this means one request in flight, no pipelining, and always analysing the newest frame. With the Virtual Camera fallback (`cv2.VideoCapture`), read on a thread and keep only the latest frame. Cardstream's own stream mode lacks this (`web_stream.py:89-93`). | obs client / Virtual Camera fallback (5.1, 5.3) |
| 17 | **Upscale small crops ≥ 500 px, JPEG q90, save the exact bytes before the POST** (`imaging.py:87-100`; `identify_client.py:107-110`) | **Adopt.** | Saving first means failed or unmatched calls are still on disk for review and become eval data (milestone 6). It fills our `crop_path` column. Check the size against each vendor's guidance: Claude pre-resize `[S13]`, Scrydex (**UNVERIFIED**). | identifier (5.1); §4 `crop_path`; milestone 6 |
| 18 | **Show the locator score next to the match score; count paid calls** (`analyzer.py:444,488-492`; `app.js:95-98`) | **Adopt.** | Store `det_score`, `sharpness`, `id_score` and `id_margin` per scan in SQLite. Show "N paid calls / ≈ credits" in the dock. | session store; dock (5.1) |
| 19 | **Tier thresholds on distance** (0.30 / 0.40) and loose drop (0.9) | **Skip the numbers; keep the idea.** | Ximilar-specific, and they changed one release earlier. We use the Scrydex score plus a top-1/top-2 margin, tuned on our eval set (§2.3 step 2), with a real **top-3 picker**, which Cardstream lacks (`overlay.js:256-263`). | identifier (2.3 step 4) |
| 20 | **"A hint can disable a classifier"** (game prefill → latin assumed: `docs/tuning.md:171-180`) | **Adopt the lesson.** | If Scrydex Vision or our Claude prompt takes hints (game, language, set), test hinted vs unhinted on Japanese cards in the eval before shipping a default (**UNVERIFIED** for Scrydex). | identifier (2.3); risk 5.4 #8 |
| 21 | **WebSocket protocol** (`state` + `bbox/quad` + `identification` per frame; immediate push on result: `web_common.py:42-68`) | **Adapt.** | Our overlay needs far less traffic. Push only on state change: `{state, last_card, price, variant, total, count, pending_candidates}`, as in our §4. Add a `quad` channel to the dock only if we want a live outline. | web / overlay / dock (5.1) |
| 22 | **Unauthenticated `/ws` with no Origin check** (`web_camera.py:78-80`) | **Do better.** | Bind 127.0.0.1, check `Origin` on every WebSocket and state-changing POST (allow only our own origin or none). Consider a random per-run token in the overlay and dock URLs. | web (5.1) |
| 23 | **RF-DETR Seg Nano locator** (125 MB, ~82 ms/frame CPU claimed, CPU-only ORT) | **Skip for MVP; possible phase 2.** | Plain OpenCV is enough for a controlled stream setup. If sleeves, glare or playmats break contour detection in the eval, their Apache-2.0 weights (**UNVERIFIED** licence on the asset) could be loaded with onnxruntime directly, without depending on cardstream. | detector; milestone 6–7 |
| 24 | **vitTracker** | **Skip.** | It adds a model for overlay smoothness we don't need. Presence comes from detection at our poll rate. | — |
| 25 | **RTMP `--listen` input** | **Skip.** | It ties up OBS's stream output and adds an encode/decode hop. `GetSourceScreenshot` and the Virtual Camera fallback stay. | obs client (1.3) |
| 26 | **Ximilar `tcg_id` + `price_stats`** as an `Identifier` | **Optional: evaluate only.** | One call returns ID and a price distribution, which is attractive. But the price isn't per variant or condition, the currency is assumed, the credit cost is unknown, and display/ToS terms aren't covered in the repo (all **UNVERIFIED**). It could join Scrydex Vision and Claude in the eval set behind our `Identifier` interface. | identifier (5.1); "Alternatives to Scrydex" |
| 27 | **Testing style** (offline, synthetic cards, injected clock, `node --test` for page logic, docs-contract test) | **Adopt.** | Cheap to copy and keeps the trigger policy honest as thresholds move. | all; milestone 3 onward |
| 28 | **Live-tunable settings with an explicit `LIVE_FIELDS` allowlist** (`analyzer.py:112-114,243-255`) | **Adopt** (small). | Make sharpness and confidence thresholds tunable from the dock during a show. Anything baked in at start-up should say so instead of silently ignoring a change. | dock (5.1) |

### Verdict

**Borrow ideas, and vendor four small modules** (`phash`/`hamming`, `sharpness.py`, the `quad.py` geometry and `CallGuard`), about 220 lines in total, with Apache-2.0 attribution. Re-implement the `DecisionCore` pattern ourselves with our extra states.

- **Not a fork.** The parts we'd keep are a few hundred lines. The parts we'd have to replace or add (OBS input, overlay, dock, SQLite session, totals, undo, CSV, variants, picker, Scrydex and Claude) are most of our product. Its web layer and identifier are built around browser capture and Ximilar.
- **Not a library dependency.**
  - It is 0.x Beta, with one maintainer and defaults that changed in the last release.
  - It was not designed as a public API; only a comment mentions "an embedder of this package" (`analyzer.py:105-107`).
  - It would pin our OpenCV wheel to `opencv-python-headless` (`pyproject.toml:39`). Mixing that with `opencv-python` in one environment is a known packaging conflict (**UNVERIFIED** here).

### Items not confirmed from code (UNVERIFIED)
- All latency and size figures: the "~0.5 s" identify call, ~82 ms per segmentation frame, ~5 ms embedding, ~2.7 ms tracker, browser encode times, and the 108 vs 125 MB weight size.
- The real-crop sharpness ranges and the "seven calls → four" anecdote.
- The licence attached to the published weight files, and whether the training repo is public.
- What Ximilar returns beyond the parsed fields (variant/foil data, alternative distances), the marketplace and currency behind `price_stats`, and its credit cost.
- Whether OBS Virtual Camera works as a `--source` index, and how OBS could push RTMP to Cardstream while also streaming.
- Whether stream mode builds up latency without frame dropping, and whether the open `/ws` is exploitable in practice.
- Every number quoted for our own build (credits per minute, the polling plan) is computed from our report's sources, not measured.

---

## Sources

All accessed 2026-10-27.

- `[C1]` Cardstream repository, commit `1ba5fc8e3a3da131559dd0ed350f23b70c12cbff` (tag v0.5.2): https://github.com/Ximilar-com/cardstream/tree/1ba5fc8e3a3da131559dd0ed350f23b70c12cbff. Every `path:line` above refers to this tree.
- `[C2]` Cardstream README (claims checked): https://github.com/Ximilar-com/cardstream/blob/1ba5fc8e3a3da131559dd0ed350f23b70c12cbff/README.md
- `[C3]` Model zoo and export contracts: https://github.com/Ximilar-com/cardstream/blob/1ba5fc8e3a3da131559dd0ed350f23b70c12cbff/model/README.md
- `[C4]` Tuning doc (call economics, sharpness, price stats, alphabet): https://github.com/Ximilar-com/cardstream/blob/1ba5fc8e3a3da131559dd0ed350f23b70c12cbff/docs/tuning.md
- `[C5]` Changelog: https://github.com/Ximilar-com/cardstream/blob/1ba5fc8e3a3da131559dd0ed350f23b70c12cbff/CHANGELOG.md
- `[C6]` GitHub API repo metadata (stars, forks, issues, last push): https://api.github.com/repos/Ximilar-com/cardstream
- `[C7]` Model weights release (not downloaded): https://github.com/Ximilar-com/cardstream/releases/tag/v2.0.0
- `[C8]` RF-DETR (architecture, per NOTICE): https://github.com/roboflow/rf-detr
- Our plan and its sources `[S1]`–`[S49]`: [obs-card-scanner.md](obs-card-scanner.md#sources)
