# TradingView Strategy & Integration

How the AI bots trade from TradingView — the **dual SuperTrend + EMA** state machine,
**entry/exit criteria**, TP-continuation, the **win-protect** filters, manual controls,
the webhook alert format, symbol matching, SL/TP rules, and the full plot map.

Core code lives in [`scripts/nifty_chart.py`](scripts/nifty_chart.py) (functions prefixed `_tv_` / `_bot_`).

---

## 1. Two channels
There is **no official API** for a user's private TradingView indicators, so the bot uses two equivalents, toggled per panel by **📊 TradingView**:

1. **Technical Analysis (auto)** — TradingView's public scanner per symbol+timeframe (`_tv_fetch_ta`): overall **STRONG BUY…STRONG SELL** rating + RSI, MACD, Stochastic, ADX, MA ratings. Cached ~20s. Used only as *context* for Claude.
2. **Custom-indicator webhook** — your TradingView **alerts** POST JSON. This drives **TV-only trading** (Claude unticked).

---

## 2. Webhook endpoint
```
POST  https://mangal-view.onrender.com/api/aibot/tv/webhook?token=mangalview
```
- No login (`?token=` guards it — `TV_WEBHOOK_TOKEN` env, default `mangalview`).
- Parses the body as JSON **regardless of content-type** (TradingView often sends `text/plain`); falls back to plain `SYMBOL SIGNAL`.
- Stores the latest alert **per symbol** (`_TV_WEBHOOK`) for display/log, **and** — when `indicator` is `SuperTrend` or `EMA` — the latest of each **per instrument root** (`_TV_IND`) to drive the dual state machine.

---

## 3. The trading model — SuperTrend (primary) + EMA (secondary) ⭐

When Claude is **off** and a `SuperTrend` and/or `EMA` alert exists for the symbol, the bot runs a **dual state machine** (`_tv_dual_signal`):

> **SuperTrend = PRIMARY** → the only thing that **opens** a position.
> **EMA 5/13 = SECONDARY** → **exit only**; it never opens and never reverses.

| SuperTrend | EMA | Action |
|---|---|---|
| BUY/SELL alert, **flat** | — | **OPEN** in the SuperTrend direction |
| same side already held | — | hold (reaffirm) |
| flip while holding (EMA exists) | — | **hold** — exit is EMA's job (faster, less laggy) |
| flip while holding (**no EMA** for symbol) | — | **reverse** (close + open) so there's still a way out |
| — | EMA prints **opposite** to the open side | **CLOSE** (flat) — `EMA exit` |
| — (no SuperTrend yet) | EMA only, flat | **nothing** — entries require SuperTrend |

It is **edge-triggered**: it acts once on the **newest unacted** alert (`_tv_acted_ts`).

**EMA exit is always reachable.** The exit branch runs even when **no SuperTrend alert is stored**, so a **manual / seed / continuation** position still gets the fast EMA exit. (Earlier this was gated behind SuperTrend, which left such positions unprotected — fixed.)

### Why this shape
SuperTrend is a confirmed-but-laggy trend signal — great for *direction*, poor for *exit timing*. EMA 5/13 flips faster, so it cuts losers early instead of waiting for the slow opposite SuperTrend. Entries stay disciplined (SuperTrend only); exits stay fast (EMA).

### EMA-only mode — the "EMA 5/13" checkbox ⭐ (`emaMode`, `_tv_ema_signal`)
A second TV sub-mode (checkbox **default ON**). When on, the **EMA 5/13 alert is BOTH entry and exit** and **SuperTrend + the trend gate are ignored**:

| EMA alert | Action |
|---|---|
| BUY/SELL while **flat** | **OPEN** that side |
| **opposite** to the open side | **REVERSE** (close + open the other side) |
| same side already held | hold (reaffirm) |

- **Exits:** `TP hit` (→ continuation), the **opposite EMA** (reverse), and the **panel SL as a backstop** (capped at `maxSeedSlPct`, default 2% — never the 5% default).
- **Reversals are chop-gated:** a reverse is **skipped while ranging** (`avoidRange`), so EMA 5/13 doesn't whipsaw in chop. Initial entries are chop-gated too.
- **TP-continuation:** re-enter the same side up to **`maxCont`** (set it to **3** for "3 TPs then stop"), then it stays flat and **waits for the next EMA alert**.
- **Seed / Detect** read the **EMA** direction (not SuperTrend) in this mode.
- SL/TP come from the EMA alert's `slPct`/`tpPct` when present, else the panel (SL capped).

**Highest-churn mode** — it trades every 5/13 cross. The **chop filter is what keeps it profitable**; don't disable `avoidRange` or drop `minER` below ~0.28, or the EMA whipsaw losses return. Uncheck the box to return to the SuperTrend-primary dual model above.

---

## 4. Entry / Exit criteria

### ENTRY (open a position)
| Source | Opens when | Direction | SL / TP | Skips cooldown+quality? |
|---|---|---|---|---|
| **SuperTrend** (primary) | SuperTrend alert while **flat**, **not ranging** (chop filter), **and trend-aligned** (EMA50/200 gate) | alert BUY/SELL | from the SuperTrend alert (`slPct`/`tpPct` → `sl`/`tp` → structure → panel default) | No |
| **EMA** | never | — | — | — |
| **TP-continuation** | a `TP hit`, SuperTrend still backs the side, **not ranging**, depth < cap | same side | same TP%, **tighter SL** (= TP%, 1:1) | Yes |
| **Manual seed** (Start bias) | you set Long/Short before Start | chosen | configured SL/TP (reuses last SuperTrend's if present) | Yes |
| **Manual Open** (button) | you click Open while running | selected radio | configured SL/TP | Yes |

### EXIT (close a position) — checked every tick, intrabar on the bar's high/low; **SL has priority over TP**
| Trigger | Reason label | Then |
|---|---|---|
| **EMA opposite cross** (any position, even with no SuperTrend) | `EMA exit` | flat — **no reverse** |
| **TP hit** | `TP hit` | → TP-continuation (if eligible) |
| **SL hit** | `SL hit` | flat |
| **SuperTrend flip, no EMA** (ST-only symbol) | `signal reversal` | close + reverse |
| **SuperTrend flip, EMA exists** | *(none)* | hold — EMA exits |
| **Manual Close** button | `manual close` | flat |
| **Manual Open opposite** | `manual reverse` | close + open chosen side |
| **Max daily profit** | `max daily profit` | flat (profit-lock) |
| **Max consec losses / daily loss** | — | flat **and bot stops** |
| **Broker hard stop** (Zerodha live) | `SL hit (broker)` | flat |

**Entry-bar guard:** on the bar a position opens, SL/TP use **close only** (not the bar's high/low, which may predate the fill), so a stale wick can't instantly false-trigger an exit or a re-entry loop. Intrabar high/low applies from the next bar.

**SL/TP from the fill** (not the alert price): Long `SL = entry×(1−slPct/100)`, `TP = entry×(1+tpPct/100)`; Short mirrored.

**Market hours** — the live Zerodha bot places **no orders outside the session** (MCX 09:00–23:30, NSE/BSE/NFO/BFO 09:15–15:30, CDS 09:00–17:00, weekends off).

---

## 5. TP-continuation (trend scalping)
When a position closes on **TP** and SuperTrend still backs the side, the bot **re-enters the same side** at the current price to keep riding the trend. Win-protected:

- **Tighter SL on continuation legs** = `contSlPct` (default **0 → use TP%**, i.e. **1:1**). After the first banked TP the chain can't be given back ("breakeven after first TP").
- **Depth cap** = `maxCont` (default **4**) — after that it stays flat and waits for a fresh SuperTrend (avoids chasing exhaustion). Tracked on `position['contCount']`.
- **Chop guard** — continuation is **skipped if the market is ranging** (see §6).
- Re-entry log: `TP re-entry (continuation #N)`.

---

## 6. Win-protect: the chop filter & how `minER` works ⭐

Every loss cluster in the live logs happened in **ranging / choppy** markets. The bot **skips TV entries (and continuation) in chop** using the **Kaufman Efficiency Ratio (ER)** — `_bot_is_ranging()`.

### How ER is calculated
Over the last **N** candles (`rangeLook`, default **20**), on closes:

```
       | close[last] − close[first] |          (net directional move)
ER  =  ─────────────────────────────────
        Σ | close[i] − close[i−1] |            (total path travelled)
```

- **Numerator** = how far price moved **net** (start → end).
- **Denominator** = the **whole zig-zag path** (sum of every bar-to-bar move).
- **ER ∈ [0, 1]**: **1.0** = a perfectly straight trend (net = path); **→0** = lots of back-and-forth that goes nowhere (chop).

### What `minER` is
`minER` is **not calculated** — it's the **threshold you set**. The market is treated as **ranging when `ER < minER`** (default **0.28**):

| ER vs minER | Meaning | Bot |
|---|---|---|
| `ER ≥ minER` | clean enough trend | **allows** entries / continuation |
| `ER < minER` | choppy, going nowhere | **skips** entries (`skipped — ranging`) |

It's **instrument-agnostic** (a ratio, not a %), so it works for crypto % swings *and* small GOLDTEN ticks — unlike a fixed-% trend test. Lower `minER` to allow choppier entries; raise it to demand cleaner trends. Turn the whole filter off with **avoidRange = false**.

### EMA 50 / 200 trend gate ⭐ (`_tv_trend_gate_ok`)
A direction filter on top of the chop filter: **only enter WITH the higher trend**, so the bot won't short a market grinding up (the SOL −5.36) or buy one grinding down. It reads **`ma4` (EMA 50)** and **`ma5` (EMA 200)** from the **SuperTrend alert**:

```
BUY  allowed only if  price > EMA200 (ma5)  AND  EMA50 (ma4) > EMA200
SELL allowed only if  price < EMA200         AND  EMA50 < EMA200
else → skip the entry, logged "skip <side> — counter-trend (…EMA200)"
```

- Applied at the **SuperTrend flat-open** only (seed / manual / continuation / EMA-exit are untouched).
- **Fails open:** if `ma4`/`ma5` aren't in the alert (Switch Board toggles off / not sent), it **can't judge → passes** — nothing breaks if you omit them.
- On the live SOL data it blocks every counter-trend SuperTrend SELL (price was above a rising EMA200) — only longs into the up-drift would have fired.

### Win-protect config knobs (panel "🛡 Win-protect" row, all directional bots)
| Knob | Default | Meaning |
|---|---|---|
| `avoidRange` | **on** | enable the chop filter |
| `minER` | **0.28** | ranging when ER < this |
| `rangeLook` | 20 | ER lookback (candles) |
| `maxCont` | **4** | TP-continuation chain-depth cap |
| `contSlPct` | **0** | continuation SL% (0 = use TP%, 1:1) |
| `trendGate` | **on** | EMA50/200 trend gate (needs `ma4`/`ma5` in the SuperTrend alert) |
| `emaMode` | **on** | EMA-only mode — EMA alert is entry+exit, SuperTrend ignored (§3) |
| `maxSeedSlPct` | **2.0** | cap on the SL% a seed / manual Open / EMA-mode entry can use (blocks the 5% panel default) |

**Stacked entry filters (max win):** trend gate (direction) → chop filter (only in a trend, not chop) → EMA 5/13 (fast exit) → 1:1 continuation (protect banked profit).

---

## 7. Manual controls (all directional bots: Delta, Zerodha, MT5)

### Start bias — Option D (`🧭 Start bias` row)
Open the **first** entry at Start instead of waiting for a SuperTrend flip. Buttons **▲ Long / ▼ Short / 🔍 Detect current / ✖ None**; Detect reads the live SuperTrend (`GET /api/aibot/tv/bias`). The chosen bias rides the start payload (`seedBias`) and is consumed **once** on the first flat tick (`_tv_alert_signal`), reusing the last SuperTrend's SL/TP if present (else panel defaults). Bypasses cooldown/quality.

### Open / Close override (`✋ Manual` row)
Hand-trade while the bot runs: **Long/Short radios + Open + Close**. `POST /api/aibot/<broker>/manual {action, side}` queues a command the tick executes (before the signal logic):
- **Open** → enter at market in the selected direction with the configured SL/TP; if holding the opposite side it **reverses**; same side is a no-op.
- **Close** → flatten at market (`manual close`).

Manual and seed positions are still **EMA-exited** and obey SL/TP — the §3 fix makes that work even with no SuperTrend.

---

## 8. Ready-to-paste alerts

Send **both** indicators to the same webhook (same `{{ticker}}`; matched by root). `indicator` **must** be exactly `SuperTrend` or `EMA` (case-insensitive) for the dual machine to engage.

### SuperTrend (primary — opens, carries SL/TP + context)
```json
{
  "symbol": "{{ticker}}",
  "exchange": "{{exchange}}",
  "signal": "BUY",
  "price": {{close}},
  "slPct": 1.0,
  "tpPct": 0.4,
  "support": "{{plot_17}}",
  "resistance": "{{plot_16}}",
  "psar": "{{plot_14}}",
  "rangeFilter": "{{plot_5}}",
  "upTrend": "{{plot_6}}",
  "downTrend": "{{plot_7}}",
  "halfTrend": "{{plot_8}}",
  "vwap": "{{plot_15}}",
  "bbBasis": "{{plot_18}}",
  "bbUpper": "{{plot_19}}",
  "bbLower": "{{plot_20}}",
  "ma1": "{{plot_0}}",
  "ma2": "{{plot_1}}",
  "ma3": "{{plot_2}}",
  "ma4": "{{plot_3}}",
  "ma5": "{{plot_4}}",
  "tf": "{{interval}}",
  "indicator": "SuperTrend"
}
```
**SELL** = same with `"signal": "SELL"`. Create **two** alerts (Buy / Sell), "Once Per Bar Close".

- **`ma4` = EMA 50 (`plot_3`)** and **`ma5` = EMA 200 (`plot_4`)** drive the **trend gate** (§6) — turn their Switch Board toggles **ON** or they send `na` and the gate can't judge (passes through).
- `slPct`/`tpPct` shown are the SOL recommendation (`1.0` / `0.4`). Rule of thumb: **TP ≥ 3× round-trip fee** and **SL ≈ 2–2.5× TP**; e.g. GOLDTEN `slPct 0.6 / tpPct 0.25`. Continuation legs ignore this SL and use `contSlPct` (0 → TP%, 1:1).

### EMA 5/13 (exit in dual mode; **entry+exit in EMA-only mode** — add `slPct`/`tpPct` for EMA mode)
```json
{
  "symbol": "{{ticker}}",
  "exchange": "{{exchange}}",
  "signal": "BUY",
  "price": {{close}},
  "slPct": 1.0,
  "tpPct": 0.4,
  "tf": "{{interval}}",
  "indicator": "EMA"
}
```
**SELL** = same with `"signal": "SELL"`. Fire on the EMA 5/13 cross (Buy = 5 crosses above 13, Sell = below).
- In the **dual** model this alert only **exits** (SL/TP optional). In **EMA-only mode** (§3, the "EMA 5/13" checkbox) it also **opens** — so include `slPct`/`tpPct` (else the panel default applies, SL capped at `maxSeedSlPct`).

---

## 9. Alert payload — accepted fields

### Acted on
| Field | Meaning |
|---|---|
| `symbol`/`ticker` | **Required.** Root-matched (exchange prefix, `.P`, `1!`, `…JUNFUT` stripped). |
| `signal`/`action` | **Required.** `BUY`/`LONG` → long, `SELL`/`SHORT` → short. |
| `indicator` | `SuperTrend` / `EMA` engage the dual machine; anything else = a legacy single-alert symbol. |
| `price` | Fill/context price; converts absolute `sl`/`tp` → %. |
| `slPct`/`tpPct` | Stop/target **percent** (used directly). |
| `sl`/`tp` | Stop/target **absolute** → converted to % from `price`. |
| `support`,`resistance`,`psar`,`rangeTop`,`rangeBottom` | Structure — SL = nearest support below, TP = nearest resistance above (mirrored for SELL). |
| `ma4` (EMA 50), `ma5` (EMA 200) | **Trend gate** (§6) — entry allowed only with the EMA50/200 trend. Omit them and the gate passes through. |
| `test` | `true` → **never traded** (🧪 Test). |

**SL/TP precedence** (`_tv_apply_sltp`): `slPct`/`tpPct` → `sl`/`tp` → structure → **panel default**. Numeric fields tolerate `"{0.25}"` / `"0.25%"` → `0.25`; `null`/`NaN`/`{{…}}` dropped.

### Context only (shown/logged/fed to Claude)
`note`/`comment`/`message`, and any other scalar (`tf`, `vwap`, MAs, BB, Ichimoku, `longSignal`…), **max 24** fields.

### Placeholders
`{{ticker}}`, `{{exchange}}`, `{{close}}`, `{{open}}`, `{{high}}`, `{{low}}`, `{{volume}}`, `{{interval}}`, `{{plot_0}}…{{plot_N}}`. Only `price` may be unquoted; quote everything else. Never `{{plot("Title")}}` (inner quotes break JSON) — use `{{plot_N}}`.

---

## 10. Mangal_Zp.pine — full plot map
`{{plot_N}}` counts `plot()` calls in declaration order. Each carries a value **only when its Switch Board toggle is ON** (else `na`, dropped).

| Plot | Title | Switch | Use as |
|---|---|---|---|
| `plot_0` | MA 1 | EMA | EMA 5 — fast trend |
| `plot_1` | MA 2 | EMA | EMA 13 — fast trend |
| `plot_2` | MA 3 | EMA | EMA 20 (off by default) |
| `plot_3` | MA 4 | EMA | EMA 50 — medium trend |
| `plot_4` | MA 5 | EMA | EMA 200 — major trend |
| `plot_5` | Range Filter | Range Filter | trend filter line |
| `plot_6` | Up Trend | Supertrend | trailing **support** (uptrend) |
| `plot_7` | Down Trend | Supertrend | trailing **resistance** (downtrend) |
| `plot_8` | HalfTrend | Half Trend | trend direction line |
| `plot_9`–`plot_13` | Ichimoku | Ichimoku | Tenkan / Kijun / Chikou / cloud A / cloud B |
| `plot_14` | ParabolicSAR | PSAR | trailing stop / dynamic S/R |
| `plot_15` | VWAP | VWAP | volume-weighted mean |
| `plot_16` | Range Top | Range Detector | **resistance** |
| `plot_17` | Range Bottom | Range Detector | **support** |
| `plot_18` | Basis | Bollinger | BB middle (20-SMA) |
| `plot_19` | Upper | Bollinger | BB upper → **resistance** |
| `plot_20` | Lower | Bollinger | BB lower → **support** |
| `plot_21` | long Signal | always | `100` on a BUY, else `na` |
| `plot_22` | Short Signal | always | `-100` on a SELL, else `na` |

Indices are tied to **this script version** — if you add/remove any `plot()` they shift; re-check with a probe alert (`"p0":"{{plot_0}}", …`) and read `log.txt`.

---

## 11. Symbol matching (`_tv_root`)
Alert symbol and bot tradingsymbol normalise to a **root**: strip `EXCHANGE:`, `.P`/`.PERP`, continuous `1!`, Kite `…26JUNFUT`. So `GOLDTEN1!` drives a bot on `GOLDTEN26JUNFUT` (any month). `_tv_alert_for()` returns the **newest** match; trading uses `skip_test=True` so a Test alert never fires a trade.

---

## 12. Live order placement (Zerodha/Kite)
MCX/Kite reject plain MARKET orders ("market protection"), so:
- **Entries & exits** use **LIMIT** with a spread-crossing band (fills like market).
- **Broker hard stop** = **SL (stop-limit)**, limit only **0.02%** beyond the trigger; the bot's soft tick-stop backstops a violent skip (`_zd_place_stop_live` / `_zd_modify_stop_live`).
- Hard stops are **DAY** orders → cancelled at session close (no overnight carry).

---

## 13. Panel features
- **🧭 Start bias** (§7), **✋ Manual** open/close (§7), **🛡 Win-protect** knobs (§6) — on Delta, Zerodha, MT5.
- **💰 Leverage & Qty** (Delta) — capital × leverage → quantity.
- **↺ Reset** → `POST /api/aibot/<broker>/reset` stops the bot and wipes state.
- **Defaults** (`Default.csv`): Capital 100000, Daily loss 2000, Daily profit 10000, Model Sonnet.
- **Zerodha session persistence**: `zerodha_session.json` (gitignored) keeps you logged in across restarts (token expires ~6am IST).

---

## 14. Test / verify
- **🧪 Test** button → sample `test:true` alert → `(TEST — not traded)`.
- **Terminal:**
  ```
  curl -X POST "https://mangal-view.onrender.com/api/aibot/tv/webhook?token=mangalview" \
       -H "Content-Type: application/json" \
       -d '{"symbol":"BTCUSD","signal":"BUY","price":64000,"indicator":"SuperTrend","slPct":2,"tpPct":0.3}'
  ```
  Expect `{"success":true}`.
- **Bias readout:** `GET /api/aibot/tv/bias?symbol=SOLUSD` → current SuperTrend/EMA direction + age.

---

## 15. Caveats
- Webhook alerts need a **paid TradingView plan**; use the Render URL (TradingView can't reach `localhost`; Render free sleeps → first alert may cold-start).
- The dual machine only engages when `indicator` is exactly `SuperTrend`/`EMA`. An **EMA-only** symbol (no SuperTrend yet) **never opens** — by design.
- The chop filter (`avoidRange`) trades **fewer but cleaner** entries; if a bot looks idle the log shows `skipped — ranging (ER<…)` — lower `minER` or untick Avoid chop to loosen.
- Open MCX positions are **unprotected overnight** (hard stop is a DAY order).
- Always paper-trade first.
