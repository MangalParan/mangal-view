# Delta Bot — log.txt Loss Analysis (2026-06-01)

Analysis of the server-side Delta bot session in `log.txt`. Times are UTC.

## Trade-by-trade (LIVE)

| # | Symbol | Strat | Side | Entry | Exit | PnL | Exit reason |
|---|--------|-------|------|-------|------|-----|-------------|
| 1 | HUSD | mfactor | SELL | 0.65412 | 0.65065 | **+1.388** | signal reversal |
| 2 | HUSD | trend | BUY | 0.65065 | 0.66501 | **+5.744** | TP hit |
| 3 | HUSD | sniper | BUY | 0.66501 | 0.68074 | **+6.292** | TP hit |
| 4 | HUSD | sniper | BUY | 0.68074 | 0.69497 | **+5.692** | TP hit |
| 5 | HUSD | sniper | BUY | 0.69497 | 0.67708 | **−7.156** | SL hit |
| 6 | HUSD | trend | BUY | 0.67708 | 0.67016 | **−2.768** | SL hit |
| 7 | HUSD | priceaction | SELL | 0.64919 | 0.65689 | **−3.080** | SL hit → **Max consec losses STOP** |
| 8 | SOLUSD | statarb | BUY | 80.811 | 80.751 | −0.300 | bot stop |
| 9 | PAXGUSD | orderflow | SELL | 4486.6 | 4487.3 | −0.007 | signal reversal |
| 10 | PAXGUSD | statarb | BUY | 4487.3 | 4486.4 | −0.009 | signal reversal → **Max consec losses STOP** |
| 11 | PAXGUSD | statarb | SELL | 4496.0 | 4497.2 | −0.012 | signal reversal |
| 12 | PAXGUSD | orderflow | BUY | 4497.2 | 4498.2 | +0.010 | signal reversal |
| 13 | PAXGUSD | statarb | SELL | 4498.2 | 4494.0 | +0.042 | bot stop |

## What actually happened

On **HUSD** the bot rode a clean uptrend for **four straight wins (+19.12)**, then **gave back −13.00** on the next three trades. Net HUSD ≈ **+6.1**, but it surrendered ~68% of peak profit. Root causes:

1. **Chasing an extended trend (the biggest single loss).** Trade #5 was `sniper` buying at **0.69497 — the local top**, after price had already run +6.8% across three legs (0.665 → 0.681 → 0.695). The momentum had nothing left; it reversed straight into the stop for **−7.156**, the largest loss of the day (bigger than any single win).

2. **No profit lock.** After banking +19.12 there was no mechanism to stop and protect gains. The bot kept opening fresh trades into a topping market.

3. **Counter-trend knife-catching.** Trade #7 (`priceaction` SELL at 0.64919) shorted into a market that was bouncing — stopped out for −3.080. Fading the move with no trend confirmation.

4. **Whipsaw churn on PAXGUSD.** Trades #9–#12 are rapid SELL/BUY/SELL flip-flops, each closing at ~breakeven on `signal reversal`. Individually tiny, but they **count as losses** and tripped the "Max consecutive losses" breaker twice — death by a thousand cuts in a sideways, low-edge market.

5. **Risk asymmetry.** SL ≈ 1% / TP ≈ 2%. One full SL (−7.156) erased more than a full TP win, so a single bad chase undid a good winner.

## Fixes implemented in this change

| Loss cause | Fix |
|------------|-----|
| Chasing extended moves (#1, #5) | **`sniper` removed** from strategy selection entirely; added an **over-extension guard** that blocks entries >1.5× avg-range from the 20-bar mean (no buying tops / selling bottoms). |
| Counter-trend entries (#3, #7) | **Trend-alignment gate** — in a trending regime, no BUY against a downtrend and no SELL against an uptrend. |
| No profit protection (#2) | **Max Daily Profit** — bot banks the open trade and stops once realised+open P/L reaches the target. |
| Whipsaw churn (#4, #9–#12) | **Re-entry cooldown** (default 60s after an exit) + **higher score threshold** (min 4.0, +1.0 in calm markets) so it only takes higher-conviction signals. |

The trend-alignment + over-extension checks live in `_bot_entry_quality()` and are toggled by the **Quality filter** checkbox. Combined with the stricter thresholds, these push toward the **>80% win-rate** goal by trading less often but only on aligned, non-extended setups. Use the **Claude co-pilot chat** in the panel to tune these live.
