# TradingView Strategy & Integration

How the AI bots use TradingView — channels, the webhook alert format, **entry/exit
criteria**, symbol matching, SL/TP rules, the full plot map, and ready-to-paste alerts.

Core code lives in [`scripts/nifty_chart.py`](scripts/nifty_chart.py) (functions prefixed `_tv_`).

---

## 1. Two channels
There is **no official API** for a user's private TradingView chart indicators, so the bot uses two realistic equivalents, surfaced per panel by the **📊 TradingView** toggle:

1. **Technical Analysis (auto)** — TradingView's public scanner per symbol+timeframe (`_tv_fetch_ta`): overall **STRONG BUY…STRONG SELL** rating + RSI, MACD histogram, Stochastic, ADX, MA/oscillator ratings. Cached ~20s.
2. **Custom-indicator webhook** — your TradingView **alerts** (e.g. `Mangal_Zp.pine` / `Claude_AI.pine`) POST a JSON signal. This drives **TV-only trading**.

Both merge in `_tv_context()` and show in the panel's TV box / `/api/aibot/tv/peek`.

---

## 2. Webhook endpoint
```
POST  https://mangal-view.onrender.com/api/aibot/tv/webhook?token=mangalview
```
- No login (TradingView can't authenticate) — `?token=` guards it (`TV_WEBHOOK_TOKEN` env, default `mangalview`).
- Parses the body as JSON **regardless of content-type** (TradingView often sends `text/plain`); falls back to plain `SYMBOL SIGNAL` text.
- Stores the latest alert **per symbol**, logs it to `log.txt` as `[TV-ALERT] …`, and mirrors it into any running bot whose symbol matches.

---

## 3. Alert payload — accepted fields

### Fields the bot ACTS on
| Field | Meaning |
|---|---|
| `symbol` (or `ticker`) | **Required.** Root-matched to the bot (exchange prefix, `.P`, `1!`, `…JUNFUT` stripped). |
| `signal` (or `action`) | **Required to trade.** `BUY`/`LONG` → long, `SELL`/`SHORT` → short. |
| `price` | Entry/context price; used to convert absolute `sl`/`tp` → %. |
| `slPct` / `tpPct` | Stop / target **percent** from entry (used directly). |
| `sl` / `tp` | Stop / target **absolute price** → converted to % from `price`. |
| `support`, `resistance`, `psar`, `rangeTop`, `rangeBottom` | Structural levels — bot sets SL = nearest support below price, TP = nearest resistance above (mirrored for SELL). |
| `test` | `true` → **never traded** (the 🧪 Test button) — only shown/logged. |

**SL/TP precedence** (`_tv_apply_sltp`): `slPct`/`tpPct` → else `sl`/`tp` (absolute) → else structural levels → else **panel default %** (from `Default.csv`).
Numeric fields are tolerant of stray wrappers: `"{0.25}"` and `"0.25%"` both parse to `0.25`. `null`/`NaN`/unresolved `{{…}}` are dropped.

### Fields shown / logged / fed to Claude (not traded directly)
| Field | Meaning |
|---|---|
| `indicator` (or `strategy`) | Label in log/display, e.g. `[DIY zp v1]`. |
| `note` (or `comment`, `message`) | Free text. |
| Any other scalar (`tf`, `vwap`, MAs, BB, Ichimoku, `longSignal`, …) | Captured (**max 24**), shown in the box + log, passed to Claude as context. |

### TradingView placeholders (global — these are indicators, not strategies)
`{{ticker}}`, `{{exchange}}`, `{{close}}`, `{{open}}`, `{{high}}`, `{{low}}`, `{{volume}}`, `{{time}}`, `{{timenow}}`, `{{interval}}`, and `{{plot_0}}…{{plot_N}}`.
`{{strategy.order.action}}` does **not** work (indicator, not a `strategy()`).

### JSON rules
- Only `price` may be unquoted (`{{close}}` always resolves to a number). Quote everything else: `"sl":"{{plot_17}}"`.
- Never use `{{plot("Title")}}` — inner quotes break JSON. Use `{{plot_N}}`.

---

## 4. Entry / Exit criteria  ⭐

### TV-only mode (Claude AI unticked, TradingView on)
The bot trades the alert **literally** (`_tv_alert_signal` / `_tv_alert_leg_signal`):

**ENTRY**
- A **real** (not `test`) `BUY`/`SELL` alert whose symbol root-matches the bot → enter that side.
- **Acts once per alert** (`_tv_acted_ts`) — repeated identical alerts don't re-enter.
- Entry SL/TP set from the alert per the precedence above; the fill price (not the alert's `price`) is the reference:
  - Long: `SL = entry × (1 − slPct/100)`, `TP = entry × (1 + tpPct/100)`
  - Short: mirrored (SL above, TP below).

**EXIT** (checked every ~10s, intrabar on the bar's high/low; SL has priority over TP)
1. **SL hit** — price reaches the stop → close (filled at the SL level).
2. **TP hit** — price reaches the target → close.
3. **Signal reversal** — the **opposite** alert arrives → close **and** open the other side (**stop-and-reverse**, see §8).
4. **Daily profit lock** — realised + open P/L ≥ Max daily profit → bank and stop.
5. **Circuit breakers** — Max consecutive losses or Max daily loss → stop the bot.

**Market hours** — the live Zerodha bot places **no orders outside the exchange session** (MCX 09:00–23:30, NSE/BSE/NFO/BFO 09:15–15:30, CDS 09:00–17:00, weekends off). It holds and resumes next session. (Avoids the "could not be converted to AMO" error.)

### Claude AI + TradingView mode
Claude makes the entry/exit decision (trend → S/R → liquidity sweep → volume → R:R, see [claude_strategy.md](claude_strategy.md)); the TV **rating + alert fields** are *context* that lift/lower its conviction. Test alerts are ignored.

---

## 5. Ready-to-paste alerts (Mangal_Zp.pine)

Create **two** alerts — Condition → `DIY Custom Strategy Builder [ZP]` → **Buy Alert** and **Sell Alert** (not "Buy or Sell Alert" — no direction). "Once Per Bar Close", webhook URL above.

### BUY  (fixed 0.4% SL/TP + structural levels as context)
```json
{
  "symbol": "{{ticker}}",
  "exchange": "{{exchange}}",
  "signal": "BUY",
  "price": {{close}},
  "slPct": 0.4,
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
  "ma4": "{{plot_3}}",
  "ma5": "{{plot_4}}",
  "longSignal": "{{plot_21}}",
  "shortSignal": "{{plot_22}}",
  "tf": "{{interval}}",
  "indicator": "DIY zp v1"
}
```
**SELL** = same body with `"signal": "SELL"`.

- With `slPct`/`tpPct` present, **SL/TP = 0.4% fixed**; `support`/`resistance`/etc. are context only.
- To make **structure** drive SL/TP instead, **remove `slPct`/`tpPct`** → SL = nearest support, TP = nearest resistance.
- Keep ≤ **24** non-core fields (the cap).

---

## 6. Mangal_Zp.pine — full plot map
`{{plot_N}}` counts `plot()` calls in declaration order (`plotshape`/`plotcandle` excluded). Each carries a value **only when its Switch Board toggle is ON** — otherwise it sends `na` and the bot drops it.

| Plot | Title | Switch | Use as |
|---|---|---|---|
| `plot_0` | MA 1 | EMA | EMA 5 — fast trend / dynamic S/R |
| `plot_1` | MA 2 | EMA | EMA 13 — fast trend |
| `plot_2` | MA 3 | EMA | EMA 20 (off by default) |
| `plot_3` | MA 4 | EMA | EMA 50 — medium trend |
| `plot_4` | MA 5 | EMA | EMA 200 — major trend / strong S/R |
| `plot_5` | Range Filter | Range Filter | trend filter line |
| `plot_6` | Up Trend | Supertrend | trailing **support** (uptrend) |
| `plot_7` | Down Trend | Supertrend | trailing **resistance** (downtrend) |
| `plot_8` | HalfTrend | Half Trend | trend direction line |
| `plot_9` | Conversion Line | Ichimoku | Tenkan |
| `plot_10` | Base Line | Ichimoku | Kijun |
| `plot_11` | Lagging Span | Ichimoku | Chikou |
| `plot_12` | Leading Span A | Ichimoku | cloud edge (S/R) |
| `plot_13` | Leading Span B | Ichimoku | cloud edge (S/R) |
| `plot_14` | ParabolicSAR | PSAR | trailing stop / dynamic S/R |
| `plot_15` | VWAP | VWAP | volume-weighted mean |
| `plot_16` | Range Top | Range Detector | **resistance** |
| `plot_17` | Range Bottom | Range Detector | **support** |
| `plot_18` | Basis | Bollinger | BB middle (20-SMA) |
| `plot_19` | Upper | Bollinger | BB upper → **resistance** |
| `plot_20` | Lower | Bollinger | BB lower → **support** |
| `plot_21` | long Signal | always | `100` when a BUY fires, else `na` |
| `plot_22` | Short Signal | always | `-100` when a SELL fires, else `na` |

Indices are tied to **this script version** — if you add/remove any `plot()`, they shift; re-check with a probe alert (`"p0":"{{plot_0}}", …`) and read the values in `log.txt`.

The indicator emits 3 `alertcondition`s: **Buy Alert** (`BUY`), **Sell Alert** (`SELL`), **Buy or Sell Alert** (combined — avoid). No exit alerts; exits are the bot's job.

---

## 7. Symbol matching (`_tv_root`)
Alert symbol and bot tradingsymbol are normalised to a **root**:
- strip `EXCHANGE:` → `BITSTAMP:BTCUSD` → `BTCUSD`
- strip `.P` / `.PERP` → `PAXGUSD.P` → `PAXGUSD`
- strip continuous `1!` → `GOLDTEN1!` → `GOLDTEN`
- strip Kite expiry/FUT → `GOLDTEN26JUNFUT` / `GOLDTEN26JULFUT` → `GOLDTEN`

So `GOLDTEN1!` drives a bot on `GOLDTEN26JUNFUT` (any month). `_tv_alert_for()` returns the **newest** matching alert; `skip_test=True` (trading) means a Test alert never blocks or fires a trade.

---

## 8. Stop-and-reverse (directional bots: Zerodha, Delta, MT5)
On a **signal reversal** (opposite alert / Claude flip) while in a position, the bot **closes and immediately opens the opposite side** in the same tick (bypassing cooldown/quality — the flip *is* the signal), using the new alert's SL/TP. Example: long + `SELL` → close long → open short.
- **SL-hit / TP-hit** exits go **flat** (no reverse) — wait for the next alert.
- The Options (two-leg) bot is not included.

---

## 9. Live order placement (Zerodha/Kite)
MCX/Kite reject plain MARKET orders via API ("market protection"), so:
- **Entries & exits** use **LIMIT** with an aggressive spread-crossing band (fills like market, accepted).
- **Broker hard stop** uses **SL (stop-limit)** with a limit only **0.02%** beyond the trigger (≈29 pts on GOLDTEN) so it fills near the trigger; the bot's soft tick-stop backstops a violent skip. Kept in sync with the position SL (`_zd_place_stop_live` / `_zd_modify_stop_live`).
- Hard stops are **DAY** orders → the exchange cancels them at session close; they don't carry overnight.

---

## 10. Panel features
- **↺ Reset** (header) → `POST /api/aibot/<broker>/reset` stops the bot and wipes state (position, trades, log, config, counters); the panel restores defaults.
- **Defaults** (`Default.csv` + HTML): Capital **100000**, Daily loss **2000**, Daily profit **10000**, Model **Sonnet**, Tick **2m**.
- **Zerodha session persistence**: `api_secret` + `access_token` saved to `zerodha_session.json` (gitignored) and reloaded on startup within ~a day, so a restart keeps you logged in (token expires ~6am IST; a dead one is caught by `/verify`).

---

## 11. Test / verify
- **🧪 Test** button: POSTs a sample `test:true` alert → shows as `(TEST — not traded)`, **never trades**. Confirms the webhook + display path.
- **Terminal self-test:**
  ```
  curl -X POST "https://mangal-view.onrender.com/api/aibot/tv/webhook?token=mangalview" \
       -H "Content-Type: application/json" \
       -d '{"symbol":"BTCUSD","signal":"BUY","price":64000,"indicator":"test"}'
  ```
  Expect `{"success":true}`, then the panel TV box shows the alert.

---

## 12. Caveats
- Webhook alerts require a **paid TradingView plan**; use the deployed (Render) URL — TradingView can't reach `localhost`; Render free sleeps, so the first alert after idle may cold-start.
- TV-only mode follows the alert literally — your indicator's quality is the whole edge. Supertrend-type signals whipsaw in chop; the **Max consecutive losses / Max daily loss** breakers are the safety net.
- Open MCX positions are **unprotected overnight** (hard stop is a DAY order; the bot places no orders after close).
- Always paper-trade first.
