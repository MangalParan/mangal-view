# TradingView Strategy & Integration

How the AI bots use TradingView — the two data channels, the webhook alert format,
symbol matching, SL/TP rules, trading modes, and the ready-to-paste alert JSON.

The core lives in [`scripts/nifty_chart.py`](scripts/nifty_chart.py) (functions prefixed `_tv_`).

---

## 1. Two channels
There is **no official API** for a user's private TradingView chart indicators, so the bot uses two realistic equivalents, both surfaced per panel by the **📊 TradingView** toggle:

1. **Technical Analysis (auto)** — pulled from TradingView's public scanner per symbol+timeframe (`_tv_fetch_ta`): overall **STRONG BUY…STRONG SELL** rating + RSI, MACD histogram, Stochastic, ADX, and MA/oscillator sub-ratings. Cached ~20s.
2. **Custom-indicator webhook** — your TradingView **alerts** (e.g. from `Mangal_Zp.pine` / `Claude_AI.pine`) POST a JSON signal to the bot. This is what actually drives **TV-only trading**.

Both are merged by `_tv_context()` and shown in the panel's TV display box / `/api/aibot/tv/peek`.

---

## 2. Webhook endpoint
```
POST  https://mangal-view.onrender.com/api/aibot/tv/webhook?token=mangalview
```
- No login (TradingView can't authenticate) — the `?token=` guards it (`TV_WEBHOOK_TOKEN` env, default `mangalview`).
- Parses the alert body as JSON **regardless of content-type** (TradingView often sends `text/plain`); falls back to a plain `SYMBOL SIGNAL` text alert.
- Stores the latest alert **per symbol**; logs it to `log.txt` as `[TV-ALERT] …` and mirrors it into any running bot whose symbol matches.

---

## 3. Alert message payload — accepted fields

### Fields the bot ACTS on
| Field | Meaning |
|---|---|
| `symbol` (or `ticker`) | **Required.** Root-matched to the bot (exchange prefix, `.P`, `1!`, `…JUNFUT` stripped). |
| `signal` (or `action`) | **Required to trade.** `BUY`/`LONG` → long, `SELL`/`SHORT` → short. |
| `price` | Entry/context price; used to convert absolute sl/tp → %. |
| `slPct` / `tpPct` | Stop / target **percent** from entry (used directly). |
| `sl` / `tp` | Stop / target **absolute price** → converted to % from `price`. |
| `psar`, `rangeTop`, `rangeBottom`, `support`, `resistance` | Structural levels — bot picks SL = nearest support, TP = nearest resistance by side. |
| `test` | `true` → **never traded** (the 🧪 Test button) — only shown/logged. |

**SL/TP precedence** (`_tv_apply_sltp`): `slPct/tpPct` → else `sl/tp` → else structural levels → else panel default %.

### Fields shown / logged / fed to Claude (not traded directly)
| Field | Meaning |
|---|---|
| `indicator` (or `strategy`) | Label in log/display, e.g. `[DIY zp v1]`. |
| `note` (or `comment`, `message`) | Free text. |
| Any other scalar (`tf`, `rsi`, `trend`, `vwap`, …) | Captured (max 24), shown in the box + log, passed to Claude as context. Unresolved `{{…}}` and `null`/`NaN` are dropped. |

### TradingView placeholders (global — these are indicators, not strategies)
`{{ticker}}`, `{{exchange}}`, `{{close}}`, `{{open}}`, `{{high}}`, `{{low}}`, `{{volume}}`, `{{time}}`, `{{timenow}}`, `{{interval}}`, and `{{plot_0}}…{{plot_N}}`.
`{{strategy.order.action}}` does **not** work (indicator, not a `strategy()`).

### Rules to keep JSON valid
- Only `price` may be unquoted (`{{close}}` always resolves to a number). Quote everything else: `"sl":"{{plot_17}}"`.
- Never use `{{plot("Title")}}` — the inner quotes break JSON. Use `{{plot_N}}`.

---

## 4. Ready-to-paste alerts (Mangal_Zp.pine)

Create **two** alerts in TradingView — Condition → `DIY Custom Strategy Builder [ZP]` → **Buy Alert** and **Sell Alert** (not "Buy or Sell Alert" — it has no direction). Paste the JSON into each Message box, "Once Per Bar Close", webhook URL above.

### BUY
```json
{
  "symbol": "{{ticker}}",
  "exchange": "{{exchange}}",
  "signal": "BUY",
  "price": {{close}},
  "psar": "{{plot_14}}",
  "rangeTop": "{{plot_16}}",
  "rangeBottom": "{{plot_17}}",
  "rangeFilter": "{{plot_5}}",
  "upTrend": "{{plot_6}}",
  "downTrend": "{{plot_7}}",
  "halfTrend": "{{plot_8}}",
  "vwap": "{{plot_15}}",
  "ma1": "{{plot_0}}",
  "ma2": "{{plot_1}}",
  "ma3": "{{plot_2}}",
  "ma4": "{{plot_3}}",
  "ma5": "{{plot_4}}",
  "conversion": "{{plot_9}}",
  "baseLine": "{{plot_10}}",
  "leadingA": "{{plot_12}}",
  "leadingB": "{{plot_13}}",
  "longSignal": "{{plot_21}}",
  "shortSignal": "{{plot_22}}",
  "tf": "{{interval}}",
  "indicator": "DIY zp v1"
}
```

### SELL
Same body, with `"signal": "SELL"`.

> Want a fixed tight stop instead of the structural levels? Add `"slPct":0.3,"tpPct":0.6` — they take priority over `psar`/`rangeTop`/`rangeBottom`.

---

## 5. Mangal_Zp.pine plot index map
`{{plot_N}}` counts `plot()` calls in declaration order (`plotshape`/`plotcandle` excluded):
```
plot_0–4  : MA 1..MA 5
plot_5    : Range Filter
plot_6    : Up Trend          plot_7  : Down Trend
plot_8    : HalfTrend
plot_9–13 : Ichimoku (Conversion, Base, Lagging, Leading A, Leading B)
plot_14   : ParabolicSAR      ← SL / trailing
plot_15   : VWAP
plot_16   : Range Top         ← resistance (TP long / SL short)
plot_17   : Range Bottom      ← support    (SL long / TP short)
plot_18–20: Bollinger (Basis, Upper, Lower)
plot_21   : long Signal       plot_22 : Short Signal
```
A plot only carries a value when its **Switch Board** toggle is ON (PSAR, Range Detector, etc.); otherwise it sends `na` and the bot drops it. Indices are tied to this script version — re-check with a probe alert if you edit the indicator.

The indicator emits 3 `alertcondition`s: **Buy Alert** (`BUY`), **Sell Alert** (`SELL`), **Buy or Sell Alert** (combined — avoid). No exit alerts; exits are the bot's job.

---

## 6. Symbol matching (`_tv_root`)
The alert symbol and the bot's tradingsymbol rarely match exactly, so both are normalised to a **root**:
- strip `EXCHANGE:` prefix → `BITSTAMP:BTCUSD` → `BTCUSD`
- strip `.P` / `.PERP` → `PAXGUSD.P` → `PAXGUSD`
- strip continuous `1!` → `GOLDTEN1!` → `GOLDTEN`
- strip Kite expiry/FUT → `GOLDTEN26JUNFUT` / `GOLDTEN26JULFUT` → `GOLDTEN`

So a `GOLDTEN1!` alert drives a bot on `GOLDTEN26JUNFUT` (any month), and `PAXGUSD.P` drives a `PAXGUSD` bot. `_tv_alert_for()` returns the **newest** matching alert.

---

## 7. Trading modes
- **Claude AI selected + TradingView on** → the TA rating + your webhook alert (incl. `fields`) are **context**; Claude lifts/lowers conviction but makes the final call. Test alerts (`test:true`) are ignored.
- **Claude AI unticked + TradingView on (TV-only)** → the bot trades the alert **directly**: `BUY`→long, `SELL`→short, acting once per alert. SL/TP from the alert (or panel default). `_tv_alert_for(..., skip_test=True)` ensures **only real alerts trade** — Test/Connect alerts never do.

---

## 8. Stop-and-reverse (directional bots: Zerodha, Delta, MT5)
On a **signal reversal** (opposite alert / Claude flip) while in a position, the bot **closes and immediately opens the opposite side** in the same tick (bypassing cooldown/quality — the flip is the signal), using the new alert's SL/TP. Example: long + `SELL` alert → close long → open short.
- **SL-hit / TP-hit** exits go **flat** (no reverse) — they wait for the next alert.
- The Options (two-leg) bot is not included.

---

## 9. Live order placement (Zerodha/Kite)
MCX/Kite reject plain MARKET orders via API ("market protection"). So the Zerodha bot:
- **Entries & exits** use **LIMIT** with an aggressive spread-crossing band (fills like market, but accepted).
- **Broker hard stop** uses **SL (stop-limit)** with a limit ~0.3% beyond the trigger, kept in sync with the position's SL (`_zd_place_stop_live` / `_zd_modify_stop_live`).

---

## 10. Test / verify
- **🧪 Test** button: POSTs a sample `test:true` alert → shows in the box + log as `(TEST — not traded)`, **never trades**. Use it to confirm the webhook + display path.
- **Self-test from a terminal:**
  ```
  curl -X POST "https://mangal-view.onrender.com/api/aibot/tv/webhook?token=mangalview" \
       -H "Content-Type: application/json" \
       -d '{"symbol":"BTCUSD","signal":"BUY","price":64000,"indicator":"test"}'
  ```
  Expect `{"success":true}`, then the panel TV box shows the alert.

---

## 11. Caveats
- Webhook alerts require a **paid TradingView plan**. The deployed (Render) URL must be used — TradingView can't reach `localhost`; Render's free tier sleeps, so the first alert after idle may cold-start.
- TV-only mode follows the alert literally with the alert's SL/TP — your indicator's quality is the whole edge. Supertrend-type signals whipsaw in chop; the **Max consecutive losses / Max daily loss** breakers are the safety net.
- Always paper-trade first.
