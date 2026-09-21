# Claude AI Trading Strategy

There are now **two distinct places** Claude makes trading decisions in this app, with very different scopes:

1. **The six legacy AI bots** (Zerodha, Delta, MT5, Zerodha Options, Delta Options, TradingView bot) — Claude is given full autonomy: it decides entries, exits, direction, and its own stop-loss/target every tick. This is sections 1–12 below.
2. **The Strategy Menu** (Iron Condor / Short Strangle / Jade Lizard / EMA 5/13 Crossover, on NSE index options via Zerodha or BTC/ETH/XAUT options via Delta Exchange) — Claude's role is much narrower: it only ever picks **which strike** to sell, inside a target-delta window you set. It never sets its own SL/TP, never decides direction, and its output is validated/snapped to real strikes with a deterministic fallback if it fails. See section 13.

## Part 1 — the six legacy AI bots

The core lives in [`_claude_trade_signal`](scripts/nifty_chart.py#L2167) and runs **once per tick** for each bot / option leg. When the Claude AI strategy is selected (the default on four bots; the only mode on Delta Options and the TradingView bot), Claude decides entries, exits, stop-loss and target on its own — none of the legacy algo strategies are used.

---

## 1. What it looks at (data gathered each tick)

Every tick the bot fetches recent candles and builds a market-context bundle that is sent to Claude:

| Field | Meaning |
|-------|---------|
| `regime` | `uptrend` / `downtrend` / `range/choppy` (from SMA stack + medium trend) |
| `sma10`, `sma20`, `sma50` | Moving averages for trend structure |
| `trendShortPct`, `trendMediumPct` | % change over short / medium windows |
| `rangePct20`, `recentHigh20`, `recentLow20` | 20-bar range and extremes |
| `supports`, `resistances` | Swing-pivot S/R levels (5-bar fractals) |
| `nearestSupport`, `nearestResistance` | Closest level below / above price |
| `distToSupportPct`, `distToResistancePct` | % distance to those levels |
| `volAvg20`, `volRecent3`, `volRatio` | Volume / participation (whipsaw filter) |
| `position` | Current open trade: side, entry, unrealized % |
| `recentTrades` | Last ~8 closed trades (P/L + exit reason) so it learns |
| `ohlcv` | Raw last 36 candles, so Claude reads the actual price action |
| `underlying*` (options only) | Underlying spot + trend so a CE/PE leg trades with direction |

Support/Resistance is computed from swing-pivot fractals — see [code](scripts/nifty_chart.py#L1875).

---

## 2. Decision logic (how Claude is instructed to trade)

The system prompt frames Claude as an **elite intraday trader** optimizing for **maximum profit with a high win rate**, trading like a market maker reading order flow — not a retail breakout-chaser. In priority order:

1. **Market direction** — trade WITH the higher-timeframe trend (regime / SMAs / trend %). In `range/choppy` regimes, default to **HOLD** unless price is reacting at a clean level.
2. **Structure (S/R)** — BUY near **support** in uptrends, SELL near **resistance** in downtrends. **Never** buy straight into resistance or sell straight into support. Best entries are *at* structure, not mid-range.
3. **Market-maker / smart money** — anticipate liquidity. Market makers run price into obvious stop clusters just beyond recent swing highs/lows (**liquidity grabs / stop hunts**), then reverse. A sweep that **immediately reclaims** is a high-probability reversal entry in the reclaim direction. Do **not** be the liquidity: avoid chasing a breakout that just swept a level on a long wick — wait for the reclaim / retest. Favour entries where the stops sit *behind* the invalidation.
4. **Confirmation** — require participation: rising volume (`volRatio > 1.1`) on the move being traded. Weak / declining volume = whipsaw → HOLD.
5. **Anti-whipsaw** — if already in a position, DEFAULT to staying (HOLD to keep, or same-direction). Only flip on a genuine structural reversal (level break + reclaim against the position). Don't flip on noise — costs add up.
6. **Learn** — review `recentTrades`. If recent trades lost in conditions like now (especially `signal reversal` exits), be more selective and HOLD more.

**What it deliberately avoids (by design).** It does **not** chase vertical breakouts / runaway moves. Buying a fast, extended rally means buying *into* resistance with a wide stop — exactly the "be the liquidity" trap. In fast, wide-bar, range-expanding conditions it **HOLDs** and waits for the **pullback / retest** of the breakout level (or a clean support bounce) to enter with a tight stop and ≥ 1.5 R:R. So sitting out a strong one-way move is the strategy working as intended, not a fault — the edge is selective structure entries with small losses, not catching every move. (It also only re-decides every `tickSec`, so a fast move can complete between decisions; by the time it looks, price is extended → HOLD.)

---

## 3. Self-scoring (Claude's own conviction)

There is **no fixed Min score gate** anymore. Claude rates each setup on its **own 0–10 conviction scale** and only returns BUY/SELL when conviction is genuinely high (**≥ 7**); otherwise HOLD. A high win rate comes from passing on mediocre setups — when in doubt, HOLD. See the [gate logic](scripts/nifty_chart.py#L1884).

---

## 4. Risk — Claude owns SL / TP

On **every** BUY/SELL, Claude must return:

- **`slPct`** — stop-loss %, **derived from a specific price level**, never a round guess: pick the real invalidation level (from `nearestSupport`/`nearestResistance`/`supports`/`resistances`, or a price/zone it names in `reason`) and compute `slPct = abs(lastPrice - level) / lastPrice * 100`, with a small buffer beyond it so ordinary noise doesn't clip it.
- **`tpPct`** — target %, computed the same way against the **next opposing S/R level**, with **reward : risk ≥ 1.5**.
- **Consistency check (mandatory)** — if `reason` names a price or zone, `slPct`/`tpPct` must correspond to that same price. Claude must not cite one level in the reasoning and then quietly set a tighter number just to make the trade look better on paper.
- **In high volatility, it must not tighten the stop below the real level to compensate** — a stop tighter than the actual invalidation just gets clipped by noise. If the genuine invalidation sits far away, that means the setup risks too much right now: lower conviction / HOLD, not a faked tighter stop. *(Added after a live loss where the stated reasoning named an 80,800 demand zone ~0.5–0.7% away but the actual `slPct` set was only ~0.22% — an artificially tight, structurally-inconsistent stop that, combined with 25x leverage, turned ordinary noise into a ~5.5%-of-capital loss.)*

These **override** the panel's SL/TP. The bot converts them into exact SL/TP prices on the position — see [`_delta_bot_open`](scripts/nifty_chart.py#L2089), [`_zd_bot_open`](scripts/nifty_chart.py#L2747), [`_mt_bot_open`](scripts/nifty_chart.py#L3736), [`_zo_open_leg`](scripts/nifty_chart.py#L3217).

In the UI, the manual SL %, Target %, Min score, Score buffer, Cooldown and Quality filter controls are hidden — Claude manages all of them. Only **Max consec losses** and **Movable TP/SL** remain user-adjustable.

---

## 5. Response format

Claude replies with strict JSON only:

```json
{ "signal": "BUY" | "SELL" | "HOLD", "score": -10..10, "reason": "<=160 chars", "slPct": <num>, "tpPct": <num> }
```

`score` sign matches the signal; `reason` is logged to the activity log.

---

## 6. How the bot acts on the signal

Each tick ([`_delta_bot_tick`](scripts/nifty_chart.py#L2197) and the Zerodha / MT5 / Options equivalents):

- **Flat** → if signal is BUY/SELL, open at market with Claude's SL/TP. Quantity is the manual Qty field, or **capital-sized** (qty + leverage chosen by Claude) when **Auto symbol** is on for Zerodha / Delta / MT5. Options quantity is always manual.
- **In a position** → exit on:
  - **SL hit** (price crosses the stop),
  - **TP hit** (price reaches the target),
  - **Signal reversal** (Claude flips to the opposite side).
- **Profit lock** → if realised + open P/L reaches the **Daily profit** target, bank the trade and stop.
- **Circuit breakers** (auto-stop the bot): **Max consecutive losses**, **Max daily loss**, **Max daily profit**.

---

## 7. Options specifics (Zerodha Options bot)

- **Underlying base** (NIFTY / BANKNIFTY / FINNIFTY / SENSEX / any stock) feeds Claude. No chart is drawn for the base — it is only input to the strategy.
- **Auto strikes ON** → Claude picks the **CE and PE strikes** near ATM at the nearest expiry from the live option chain (NFO / BFO), then trades each leg with the same per-tick logic. See [`_zo_resolve_auto_strikes`](scripts/nifty_chart.py).
- **Auto strikes OFF** → you enter the CE and PE option symbols manually (two legs).

### Option Buyer vs Option Seller (premium decay / IV crush aware)
The price Claude analyses for an option leg is the **premium**, not the index. The bot maps the signal to an action: **BUY → go long the option** (Option Buyer), **SELL → go short / sell the option** (Option Seller). Each leg is fed extra context — `optionType` (CE/PE), `strike`, `moneyness`, `underlyingVsStrikePct`, `dte`, the underlying trend, and which modes (`buyerEnabled`/`sellerEnabled`) are on — via [`_zo_option_meta`](scripts/nifty_chart.py).

- **Buyer (BUY / long premium)** — only when the premium will likely **expand**: a fresh directional thrust in the option's favour (CE = underlying breaking up with momentum + volume; PE = breaking down) and IV not collapsing. It will **not** buy a bleeding/mid-range premium into a flat or opposing underlying — that's the classic morning trap where theta + IV crush melt a long.
- **Seller (SELL / short premium)** — harvests **decay**: it shorts a rich premium that is likely to fall — choppy/range-bound or opposing underlying (both CE and PE bleed when the index goes nowhere), **post-open IV crush**, or OTM strikes the underlying won't reach. Prefers OTM, lower-DTE, elevated premium. These theta trades can score ≥7 even in a flat/choppy regime, so the bot stops *holding through* an obvious decay.
- **Short-option risk is large/undefined**, so a SELL always carries a **tight premium-% stop** (`slPct` = the premium rise that stops you out, e.g. 25–40%) with `tpPct` = the decay target; it exits immediately if the underlying starts trending in the option's favour.
- With **both modes on**, the two legs can be shorted together (a theta/short-strangle posture) to profit from a range-bound index, each with its own stop.

---

## 8. TradingView confirmation (optional)

Each panel has a **📊 TradingView** toggle. There's no official API for a user's private chart indicators, so it provides the realistic equivalents, fed to Claude as **context only** (Claude still decides):

- **Technical Analysis (auto)** — pulled from TradingView's public scanner for the symbol + timeframe: overall **STRONG BUY → STRONG SELL** rating plus RSI, MACD histogram, Stochastic, ADX, and the moving-average / oscillator sub-ratings. Shown in the small panel display and refreshed ~20s. See [`_tv_fetch_ta`](scripts/nifty_chart.py).
- **Custom indicators (webhook)** — create a TradingView **Alert** on your own Pine indicator with a Webhook URL `<origin>/api/aibot/tv/webhook?token=<token>` and a JSON body like `{"symbol":"NIFTY","signal":"BUY","indicator":"MyPine"}`. The latest signal per symbol is stored and passed to Claude with its age. See [`aibot_tv_webhook`](scripts/nifty_chart.py).
- For the **Options** bot, TradingView is read on the **underlying** (e.g. NSE:NIFTY), since option contracts aren't on TradingView TA.
- Influence is **context only**: Claude lifts conviction when TV aligns with its own read and raises its bar when TV opposes — it never trades against its own structure read just because TV disagrees. A stale webhook (large `ageSec`) is weighted weakly.

### Quant confirmation (optional)

Each panel also has a **📊 Quant** checkbox next to Claude AI (`data-strat="quant"`, collected into `allowedStrategies` the same way as every other strategy checkbox). When ticked, [`_bot_quant_signal`](scripts/nifty_chart.py) runs a deterministic statistical/mean-reversion model (Z-score, regression deviation, Bollinger %B, StochRSI, Keltner Channel, Hurst exponent, variance ratio, percentile rank, return skew — see `generate_quant_signals`) fresh each tick and adds a `quant` block to the prompt: Claude raises conviction when the quant score/verdict agrees with its own read and lowers it when it opposes, same "confirmation, not override" relationship as TradingView. Off by default; see `Quant_strategy.md` for the full model.

## 9. Chart image input (vision)

Each bot chat has a **📎 attach** button. Upload a TradingView chart screenshot (SuperTrend / EMA / PSAR / support-resistance, any or multiple timeframes) and Claude uses it as **vision input** (base64 image blocks on the Anthropic Messages API):

- **In the chat** — it reads the chart to answer questions about the current setup.
- **In the live trade loop** — the latest uploaded chart is fed to Claude on **every decision** while the bot runs, and the prompt tells it to treat the chart as the **PRIMARY structure read**: trend, key S/R and indicator alignment come from what it *sees*, and it aligns the decision with the chart. If the picture contradicts the numeric fields, it trusts the chart's structure.
- The chart stays "current" for **~3 hours**, then expires so a stale picture can't keep driving trades. **Re-upload anytime** during a trade to re-steer the strategy around the newest chart.
- Available on all six bots (Delta / Zerodha / MT5 / Zerodha Options / Delta Options / TradingView). See [`_call_claude`](scripts/nifty_chart.py) (the `images` param) and the per-bot chart store (`_chart_store` / `_chart_images_for`).

### Multi-timeframe chart upload + Analyse — every AI bot

Every AI bot panel (Delta / Zerodha / MT5 / Zerodha Options / Delta Options / TradingView bot) has a **Charts bar** with five dedicated upload buttons — **1D, 1H, 30m, 15m, 5m** — instead of relying only on the single 📎 chat slot above. On the four bots with a Claude AI checkbox, the bar only shows once that checkbox is ticked; the Delta Options and TradingView bots are Claude-only, so their bar is always visible.

- Each button uploads into its own **named timeframe slot** (`_chart_store_slot`), so all 5 can be held at once rather than one overwriting the last (`_chart_images_for_multi`, falling back to the single 📎 slot if none of the 5 are used).
- **Analyse** (`/api/aibot/<bot>/analyse`, one shared handler — `_bot_analyse_route`) sends every uploaded slot to Claude in **one call**, with a dedicated pre-flight prompt: per-chart notes (trend/structure/S-R/pattern), then **one combined multi-timeframe verdict** ending in an explicit `READY FOR TRADING` or `NOT READY` line. This is purely advisory — it does not touch the running bot or place an order.
- Once **Start** is clicked, the live decision loop keeps resending all uploaded charts to Claude as vision context on **every** decision tick (same ~3h freshness window as the single-slot version) — so Analyse is a pre-flight check, and Start is what actually trades off the same charts continuously.
- Each image gets a `Chart: <label>` text block immediately before it (a small, backward-compatible extension to `_attach_images`) so Claude can tell the timeframes apart, and the system prompt explicitly instructs it to use the **higher timeframes for bias, lower timeframes for entry timing** — a lower-timeframe wiggle should never override a clear higher-timeframe trend.

### Automatic multi-timeframe analysis (no upload required)

Chart images are now optional. Every Claude-driven bot **auto-fetches** 1D/1H/30m/15m/5m candles each decision tick via [`_bot_multi_tf_snapshot`](scripts/nifty_chart.py) — lastPrice, sma20, changePct, trend, recentHigh/recentLow per timeframe (cached ~90s so a fast tick cadence doesn't refetch 5 timeframes every call) — and adds it to the prompt as a `multiTF` block with the **same higher-timeframe-for-bias, lower-timeframe-for-entry instruction** as the image case. If a timeframe also has an uploaded image, the image is treated as the richer, primary read for that timeframe; `multiTF` fills in the rest.

In addition, the first time a bot is flat and about to consider an entry (and at most once an hour after — `_ANALYSIS_MAX_AGE`), it runs one **pre-flight analysis** call — [`_bot_run_preflight_analyse`](scripts/nifty_chart.py) — using uploaded images if present, else the auto-fetched `multiTF` numbers, producing the same kind of per-timeframe notes + `READY FOR TRADING` / `NOT READY` verdict as the manual Analyse button. The result is stored on `lastAnalysis` and returned in `/status`; each panel's status poll shows it in the chat window automatically (`_surfaceAnalysis` client-side), so you see Claude's reasoning **before** it places its first trade — with or without ever uploading a chart.

---

## 10. Data source & fallback

Candles come from the bot's broker feed — Delta, Kite (Zerodha), or MT5. For the **MT5 bot in paper mode with the TradingView toggle on**, if the MT5 (MCP/EA) feed is unavailable the bot **falls back to TradingView candles** (`cfg.tvSymbol`, e.g. `OANDA:XAUUSD`) so paper trading keeps running; MT5 stays primary and it switches back when the feed recovers. Live mode has no such fallback (you can't place live MT5 orders without the bridge).

---

## 11. Model & cadence

- The **Model** selector (Haiku / Sonnet / Opus) chooses which Claude model makes the decisions — Haiku is cheapest/fastest, Opus is the most capable.
- **Tick** sets how often the bot checks the market and calls Claude. Each tick = **one Claude API call per running bot / leg**, so the tick interval drives both responsiveness and API cost. A slower tick (e.g. 180s) = fewer, more-considered trades and lower cost.
  - All six bots share the same range — **15s … 1d** (15s/30s/1m/2m/3m/5m/10m/15m/30m/1h/1d) — so cadence can match a higher-timeframe, multi-chart read (see §9).

---

## 12. Honest caveats (legacy bots)

- This is a **discretionary, LLM-judgment** strategy, **not** a backtested quant edge. The prompt steers Claude toward selective, structure-based trades to favour win rate, but **no strategy guarantees profit** — markets gap and reverse.
- The **circuit breakers** (max consecutive losses, max daily loss/profit) and **SL on every trade** are what cap the downside.
- Always **paper-trade first** to see how it behaves on your symbols and timeframe before going live.

---

## Part 2 — the Strategy Menu (multi-leg options strategies)

Opened from **Automation → Strategy Menu**: a completely different engine from Part 1, built for **defined multi-leg options strategies** rather than free-form directional trading. Two independent sides — **Zerodha Options Strategy** (NIFTY / BANKNIFTY / FINNIFTY / SENSEX, via Kite) and **Delta Options Strategy** (BTC / ETH / XAUT options, via Delta Exchange — unrelated to the Part-1 Delta AI Bot above, a naming coincidence) — sharing the same strategy engine (`_strat_*` for Zerodha, `_dstrat_*` for Delta).

## 13. Strategy types

| Type | Legs | Notes |
|---|---|---|
| **Iron Condor** | sellCE, sellPE, hedgeCE, hedgePE | Both sides sold + hedged, always. |
| **Short Strangle** | sellCE, sellPE | No hedges — **undefined risk by design**, flagged in the UI. |
| **Jade Lizard** | sellCE, sellPE, hedgeCE | Call side hedged, put side naked (classic Jade Lizard construction — no upside risk if credit > call-spread width). |
| **EMA 5/13 Crossover** | sellCE, sellPE, hedgeCE, hedgePE | **Not Claude-driven at all** — see §13.3. All 4 legs are resolved/shown, but only the side matching the current EMA regime is ever actually open. |

## 13.1 Claude's role: strike selection only

The **"Claude strategy" button** (`/claude_pick`, or automatically on Start if never clicked) is the *only* place Claude is consulted, and its job is narrow: **pick which strike to sell**, nothing else.

- **Prompt** (`_strat_resolve_strikes` / `_dstrat_resolve_strikes`): "Choose `ceSellStrike` from `ceCandidates` and `peSellStrike` from `peCandidates` ONLY — each must have `|delta|` at or below `targetDelta`, preferring the value closest to `targetDelta` without exceeding it." Candidates are pre-filtered to a plausible delta window (≤ 1.5× target) so Claude can't pick something wildly off-target, and its answer is **snapped to the nearest real candidate strike** — it can never invent a strike or symbol.
- **Delta comes from real greeks, not a guess**: Zerodha side computes Black-Scholes delta from the NSE/Kite chain's IV (`_bs_delta`); Delta Exchange side uses the **exchange's own native `greeks.delta`** from its ticker snapshot (more accurate than Black-Scholes for crypto vol surfaces), falling back to Black-Scholes only if a symbol's native greeks are missing.
- **Any Claude failure — bad JSON, timeout, unparseable strike — silently falls back to the deterministic delta-walk** (pick the strike with the largest `|delta|` still ≤ `targetDelta`). The user is never left with zero legs.
- **Hedge strikes are NEVER Claude's call.** They're always deterministic: sold strike ± `hedgeDistancePoints` (default **500** for NIFTY/BTC, **50** for XAUT/ETH), snapped to the nearest strike actually listed in the chain. This is a hardcoded design decision, not a judgment call the prompt leaves open.
- Claude is **not** asked about SL/TP, direction, or timing here — those are plain configured percentages (see §13.2), applied identically regardless of how the strike was chosen.

## 13.2 Risk, sizing, and exits

- **SL % / TP %** (default **50% / 50%**, user-configurable) apply to **sold legs only** — computed the same simple way as the legacy bots' manual mode: `sl = price × (1 ± slPct/100)`, `tp = price × (1 ∓ tpPct/100)`. Hedge (BUY) legs never carry their own SL/TP.
- **Hedge-linked exit**: when a sold leg's SL or TP fires, its paired hedge leg is closed **in the same step** (`_strat_close_leg` / `_dstrat_close_leg`, `cascade=True`) — never left open alone.
- **Position size** = `qty` (lots for Zerodha, raw contracts for Delta) — a plain manual number, not capital/leverage-sized like the legacy bots.
- **Circuit breakers**: Max consecutive losses, Max daily loss — same concept as Part 1, checked once per tick (`_strat_check_breakers`).

## 13.3 EMA 5/13 Crossover — a pure technical strategy, no LLM signal

This strategy type computes `compute_ema(closes, 5)` vs `compute_ema(closes, 13)` on the underlying (configurable EMA periods and timeframe, default 15m) **every tick** (`_strat_ema_signal` / `_strat_ema_tick_logic`):

- **Bullish** regime (fast EMA > slow EMA) → sell PE (+ its hedge). **Bearish** → sell CE (+ its hedge). The active side switches automatically as the regime flips, closing the wrong side (hedge cascades with it) and freshly re-resolving the correct one.
- **Claude is only invoked, if at all, to pick the strike** for whichever side the EMA crossover says should be active — the *decision* to be bullish or bearish is a deterministic formula, not an LLM judgment call. This is the opposite of Part 1, where Claude reads structure and decides direction itself.

## 13.4 Non Stop — continuous re-entry

A **Non Stop** checkbox (both sides) makes the strategy trade continuously instead of stopping after one round-trip:

- When a sold leg closes on TP or SL, it **immediately re-resolves that same role** at the **same target delta** (a fresh strike — same or different depending on where the market is now) and **same SL%/TP%**, and re-opens it (`_strat_nonstop_reenter` / `_dstrat_nonstop_reenter`). This is deterministic strike resolution, not another Claude call.
- Stops itself once **Max consec losses** trips — the existing breaker already halts the bot at that point.
- **EMA Crossover strategies are inherently continuous** regardless of the checkbox — the regime-following logic in §13.3 already reopens the active side whenever it's flat.
- **Zerodha only**: Non Stop (and EMA Crossover) is additionally time-boxed to a configurable market-hours window, default **09:25–15:00 IST** (`_strat_nonstop_session_gate`) — squares off everything once at/after the close time and withholds new re-entries outside the window; existing SL/TP risk management on already-open legs is never gated by this. **Delta side has no such window** — crypto trades 24/7.

## 13.5 What's logged

`log_options.txt` (separate from the legacy bots' shared `log.txt`) gets a full `[CONFIG]` line on every Start (`_strat_cfg_summary`) — strategy type, underlying, expiry, target delta, hedge distance, position size, SL%/TP%, buyer/seller mode, non-stop + its window, EMA periods — everything needed to reproduce or audit a run, auto-picked up by `log_to_journal.py`'s existing Config sheet.

## 13.6 Honest caveats (Strategy Menu)

- Claude's **only** discretion here is which strike to sell within a delta band you set — it is not deciding whether to trade, which direction, or how much risk to take. Get the direction/timing wrong (manually, or via EMA Crossover) and the strike selection won't save you.
- **Short Strangle has no hedge — undefined risk.** Iron Condor / Jade Lizard cap risk via the hedge leg(s), but only as far as `hedgeDistancePoints` actually reaches — a large enough gap can still blow through it.
- **Non Stop compounds losing streaks fast** in a choppy market: every SL hit immediately re-enters, so a string of stop-outs (common when SL is much tighter than TP — see Part 1 §4's incident) adds up quickly. The Max consec losses breaker is the only backstop.
- No cross-restart persistence: trades/P&L live in memory only, same as the legacy bots — a server restart loses history (the log file is the durable record).
- Always **paper-trade first**.
