# Quant Strategy

This file documents every "Quant" strategy in the app: the statistical model itself, and the two different ways it's used — as a standalone vote in the non-Claude algo engine, and (new) as a checkbox that automatically drives the Claude AI strategy.

## 1. What the Quant model computes

[`generate_quant_signals`](scripts/nifty_chart.py#L15790) is a **deterministic statistical/mean-reversion model** — no Claude call, pure math on OHLCV. Every bar it computes a composite score from:

| Signal | What it measures | Contribution |
|---|---|---|
| **Z-Score** (20-bar) | How many std-devs price is from its recent mean | \|z\| > 2 → extreme (±2.0); \|z\| > 1 → moderate (±1.0) |
| **Linear-regression deviation** | Distance from the 20-bar regression line, in std-devs | > 1.5σ off → reversion signal (±2.0); on-trend and near the line → small trend-following nudge (±0.5) |
| **Bollinger %B** | Position within the 20-bar Bollinger Bands | < 0.05 → oversold (+1.5); > 0.95 → overbought (−1.5) |
| **Stochastic RSI** | Momentum of RSI itself (14-bar stochastic of RSI) | Cross up from < 20 (+2.0); cross down from > 80 (−2.0) |
| **Keltner Channel** (EMA21 ± 2×ATR14) | Price outside its volatility-adjusted envelope | Below lower (+1.5); above upper (−1.5) |
| **Hurst exponent proxy** (rescaled range, 20-bar returns) | Regime: mean-reverting (H<0.4) vs trending (H>0.6) | Mean-reverting + already oversold/overbought → reversion bet (±1.5); trending → follow the last bar's direction (±1.0) |
| **Variance ratio** (1-bar vs 2-bar return variance) | Mean-reversion vs random-walk/momentum in returns | Ratio < 0.7 + directional z → reversion bet (±1.5) |
| **Price percentile rank** (50-bar) | Where price sits in its recent range | < 10th percentile (+1.0); > 90th (−1.0) |
| **Return skew** (20-bar) | Asymmetry of recent returns | Skew > 0.5 (+1.0); < −0.5 (−1.0) |

Positive score = bullish/oversold-bounce bias, negative = bearish/overbought-fade bias. The composite is turned into a **verdict**: `STRONG BUY` (≥5), `BUY` (≥3.5), `STRONG SELL` (≤−5), `SELL` (≤−3.5), else `NEUTRAL`. The summary returned is `{score, verdict, indicators (top contributors), rsi, macd, vwap}`.

[`_bot_quant_signal(candles)`](scripts/nifty_chart.py#L2104) is the on-demand wrapper: it builds the Bollinger/RSI/MACD/VWAP/EMA9/EMA21/support-resistance inputs `generate_quant_signals` needs straight from raw candles, and returns just the summary (or `None` if there isn't enough history / it errors). It's called fresh each tick, only when Quant is actually enabled — see §3.

## 2. Quant as a standalone vote (non-Claude mode)

`'quant'` has always been one of the [`_BOT_CONFIGURABLE_ALGOS`](scripts/nifty_chart.py#L1909) and is one of the [`_BOT_DEFAULT_ALLOWED`](scripts/nifty_chart.py#L1913) opt-ins. When the **Claude AI** strategy checkbox is *unchecked* on a bot panel, [`_bot_build_algos`](scripts/nifty_chart.py#L1963) includes `generate_quant_signals` in the pool of algos the bot scores and votes across each tick, alongside `mfactor`/`mstreet`/`statarb`/etc. This path is unchanged by this update — it has no UI checkbox of its own (none of the configurable algos besides Claude do); it's simply always in the default voting pool.

## 3. Quant driving the Claude strategy (new)

**When the Claude AI strategy is selected, [`_bot_build_algos`](scripts/nifty_chart.py#L1963) returns an empty pool — Claude trades entirely on its own and the vote above never runs.** That left no way for the Quant model to influence a Claude-driven bot at all. This is the gap point 1 of this spec closes.

### Checkbox

Each of the four legacy AI bot panels (Zerodha AI Bot, Delta AI Bot, MT5 AI Bot, Zerodha Options AI Bot / TradingView bot) now has a **Quant** checkbox next to **Claude AI**, using the existing generic `.strat-chk` / `data-strat="quant"` mechanism — no new plumbing needed, it's collected into `allowedStrategies` exactly like every other strategy checkbox and logged in the `[CONFIG]` line on Start.

### Wiring

Inside [`_claude_trade_signal`](scripts/nifty_chart.py#L2125), when `'quant' in allowedStrategies`:

1. `_bot_quant_signal(candles)` runs fresh for that tick's candles.
2. If it returns a summary, a `QUANT` block is added to the system prompt (mirroring the existing TradingView block) instructing Claude to treat the quant score/verdict as a vote: **raise conviction when it agrees with Claude's own structure read (even on a borderline setup), lower conviction or HOLD when it opposes, treat NEUTRAL as a mild vote for HOLD unless everything else is unanimous.**
3. The summary itself (`score`, `verdict`, `indicators`, `rsi`, `macd`, `vwap`) is attached to the JSON payload as `quant`, so Claude sees the actual numbers, not just the instruction.

Claude still makes the final call and still owns its own SL/TP — Quant is a **vote**, not an override, exactly like the TradingView confirmation block. This keeps the "automatically drives Claude strategy" requirement without letting a single statistical model force trades the price action itself doesn't support.

When the checkbox is off, none of the extra indicator computation runs (`_bot_quant_signal` is never called) — pure-Claude mode stays as cheap as it was before this change.

### Where it applies

All call sites that already share `_claude_trade_signal` — the Zerodha AI Bot, Delta AI Bot, MT5 AI Bot, Zerodha Options AI Bot (per-leg), and the TradingView-alerts bot — get this automatically, since the wiring lives inside `_claude_trade_signal` itself rather than at each call site. The **Strategy Menu** (Iron Condor / Short Strangle / Jade Lizard / EMA Crossover — see `claude_strategy.md` Part 2) is unaffected: it never calls `_claude_trade_signal` and uses Claude only for strike selection, not direction.
