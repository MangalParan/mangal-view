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
- **Auto strikes ON** → Claude picks the **CE and PE strikes** near ATM at the nearest expiry from the live option chain (NFO / BFO), then trades each leg with the same per-tick logic, informed by the underlying's trend. See [`_zo_resolve_auto_strikes`](scripts/nifty_chart.py).
- **Auto strikes OFF** → you enter the CE and PE option symbols manually (two legs).
- A CE (call) leg trades with a **rising** underlying; a PE (put) leg trades with a **falling** underlying.

---

## 8. Model & cadence

- The **Model** selector (Haiku / Sonnet / Opus) chooses which Claude model makes the decisions — Haiku is cheapest/fastest, Opus is the most capable.
- **Tick** sets how often the bot checks the market and calls Claude (15s … 10m). Each tick = **one Claude API call per running bot / leg**, so the tick interval drives both responsiveness and API cost.

---

## 9. Honest caveats

- This is a **discretionary, LLM-judgment** strategy, **not** a backtested quant edge. The prompt steers Claude toward selective, structure-based trades to favour win rate, but **no strategy guarantees profit** — markets gap and reverse.
- The **circuit breakers** (max consecutive losses, max daily loss/profit) and **SL on every trade** are what cap the downside.
- Always **paper-trade first** to see how it behaves on your symbols and timeframe before going live.
