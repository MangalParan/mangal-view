# Claude AI Trading Strategy

How the **Claude AI** strategy works in the AI bots (Zerodha, Delta, MT5, Zerodha Options).

The core lives in [`_claude_trade_signal`](scripts/nifty_chart.py#L1845) and runs **once per tick** for each bot / option leg. When the Claude AI strategy is selected (the default), Claude decides entries, exits, stop-loss and target on its own — none of the legacy algo strategies are used.

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

---

## 3. Self-scoring (Claude's own conviction)

There is **no fixed Min score gate** anymore. Claude rates each setup on its **own 0–10 conviction scale** and only returns BUY/SELL when conviction is genuinely high (**≥ 7**); otherwise HOLD. A high win rate comes from passing on mediocre setups — when in doubt, HOLD. See the [gate logic](scripts/nifty_chart.py#L1884).

---

## 4. Risk — Claude owns SL / TP

On **every** BUY/SELL, Claude must return:

- **`slPct`** — stop-loss %, placed just **beyond the invalidation level** (the swing / structure that proves the trade wrong).
- **`tpPct`** — target %, placed at the **next opposing S/R level**, with **reward : risk ≥ 1.5**.

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

## 9. Model & cadence

- The **Model** selector (Haiku / Sonnet / Opus) chooses which Claude model makes the decisions — Haiku is cheapest/fastest, Opus is the most capable.
- **Tick** sets how often the bot checks the market and calls Claude (15s … 10m). Each tick = **one Claude API call per running bot / leg**, so the tick interval drives both responsiveness and API cost.

---

## 10. Honest caveats

- This is a **discretionary, LLM-judgment** strategy, **not** a backtested quant edge. The prompt steers Claude toward selective, structure-based trades to favour win rate, but **no strategy guarantees profit** — markets gap and reverse.
- The **circuit breakers** (max consecutive losses, max daily loss/profit) and **SL on every trade** are what cap the downside.
- Always **paper-trade first** to see how it behaves on your symbols and timeframe before going live.
