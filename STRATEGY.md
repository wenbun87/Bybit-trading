# Momentum Bot Strategy Document

## Overview

An automated Bybit USDT perpetual futures bot that finds **quiet, low-turnover coins showing early accumulation** (insider positioning, volume waking up, OI building) and enters LONG **before the pump**. Designed specifically for "scam coins" / cabal-controlled tokens where a small group accumulates then orchestrates a squeeze.

The bot runs on a 5-minute scan cycle, checking all Bybit linear perpetual pairs.

---

## Universe & Coin Selection

### Four-Pool Classification

Every scan cycle, all Bybit USDT perps are sorted into four pools:

| Pool | Criteria | Purpose |
|------|----------|---------|
| **A** | Top 50 by 24h turnover | Established, liquid coins (BTC, ETH, SOL, etc.) |
| **B** | Top 30 by 24h % change (>$1M turnover, >1% move, not in A) | Active movers / breakouts |
| **C** | Top 30 flat-to-down with decent turnover ($2M+, -10% to +5% change, not in A/B) | MYX-style trap setups |
| **D** | Top 50 quiet coins ($100K-$5M turnover, not in A/B/C), sorted by absolute % change | **Accumulation sweet spot** |

**The bot ONLY enters Pool D coins.** These are the quiet, under-the-radar tokens where insiders accumulate before the pump hits the mainstream.

### Pre-filters

- Must have >$100K 24h turnover (not completely dead)
- Must have >1% 24h price change (skip pure noise)
- Must be a USDT perpetual pair

---

## Entry Signals — Momentum Score

Each Pool D coin is scored 0-100 using a weighted composite of 7 signals, minus a distribution penalty. Entry requires **score >= 40**.

### Signal Weights

| Signal | Weight | What It Detects |
|--------|--------|-----------------|
| **Accumulation** | 30% | The core signal — quiet buildup before a pump |
| **Pre-Squeeze Setup** | 20% | MYX-style trap: sustained negative funding + consolidation + rising OI |
| **Volume Anomaly** | 10% | Recent volume spike vs 24h baseline |
| **Price Acceleration** | 10% | Short-term rate of change accelerating vs longer-term |
| **OI Surge** | 10% | Open interest building (new money entering) |
| **Squeeze Setup** | 10% | Negative funding + rising OI + rising price (active setup) |
| **Streak** | 10% | Consecutive green candles with rising volume |

### Accumulation Signal (30% weight, scored 0-100)

The most important signal. Detects the quiet buildup before a pump:

1. **Volume Ramp** (up to +30 pts)
   - Compares recent 3-day average turnover vs older baseline, AND today vs prior 7-day average
   - Uses whichever is stronger
   - 5x+ = +30 pts, 3x = +25, 2x = +15, 1.5x = +8

2. **OI Building from Low Base** (up to +25 pts)
   - Rising OI over 4 hours on a quiet coin = new positions being opened
   - +30% OI = +25 pts, +15% = +18, +5% = +10

3. **Price Coiling** (up to +20 pts, or -15 penalty)
   - 5-day high-low range — tighter = better (accumulation happens during boring action)
   - <10% range = +20 pts, <20% = +12, <30% = +5
   - >80% range = -15 pts (already pumped, not accumulation)

4. **Negative Funding on Quiet Coin** (up to +15 pts)
   - Shorts arriving early = future squeeze fuel
   - 4+ negative cycles out of 6 = +15 pts, 2+ = +8
   - Only counts if price range is <30% (not during active pump)

5. **Low Absolute Turnover** (up to +10 pts)
   - <$2M = +10 pts (under the radar), <$5M = +5, >$50M = -10

6. **Low Circulating Supply** (up to +20 pts)
   - CoinGecko data: if team/insiders hold 80%+ of supply, float is tiny
   - <=10% circulating = +20 pts, <=20% = +15, <=35% = +8
   - FDV >5x market cap adds +5 pts (unlock risk = manipulation signal)

### Pre-Squeeze Setup (20% weight, scored 0-100)

Detects the MYX-style trap — the consolidation after an initial bait pump, right before the explosive squeeze:

1. **Sustained Negative Funding** (up to +35 pts) — 6+ negative cycles out of 8 = +35
2. **Price Consolidation** (up to +25 pts) — 24h range <8% = +25 (tight range)
3. **OI Rising During Consolidation** (up to +25 pts) — Shorts piling in while price holds
4. **Recent Bait Pump** (up to +15 pts) — Had a 40%+ pump and now sitting near the range top

### Squeeze Setup (10% weight, scored 0-100)

The active squeeze pattern — all four conditions aligning:

1. **Negative Funding** — Shorts paying longs (crowd is short)
2. **OI Rising** — New short positions stacking
3. **Price Stable-to-Rising** — Shorts underwater = fuel (-2% to +15% sweet spot)
4. **Long/Short Ratio Dropping** — Retail shorting into strength (bonus)

### Distribution Risk (Penalty, capped at -30 pts)

Prevents entering late/distribution setups:

- Already up 50%+ from 12h low = -15 pts
- Within 2% of 48h high while up 30%+ = -10 pts
- OI dropping >3% while price elevated = -15 pts

### Crime Pump Risk (Hard Block)

Score 0-100. **>= 60 = coin is blocked entirely** (you'd be the exit liquidity).

Checks: fake volume (Vol/OI >20x), parabolic runs (+100-200% in 24h during distribution), outsized OI vs turnover, derivatives frenzy (positive funding + extreme volume).

Crime score at entry also modifies position size:
- Crime score < 30 → full size
- Crime score 30-60 → **half size** (pump already starting, entering late)
- Crime score >= 60 → **blocked**

---

## Position Sizing

### Score-Based Sizing

Base margin = account balance / 5. Multiplied by score tier and leverage:

| Score | Multiplier | Example ($500 balance, 5x leverage) |
|-------|------------|--------------------------------------|
| 90+ | 3.0x | $1,500 notional |
| 80+ | 2.5x | $1,250 |
| 70+ | 2.0x | $1,000 |
| 60+ | 1.5x | $750 |
| 50+ | 1.0x | $500 |
| 40+ | 0.75x | $375 |

### Safety Limits

- **Max total exposure** = account balance x 10 (with leverage)
- **Max trades per cycle** = 2
- **Max trades per day** = 6
- **Re-entry cooldown** = 6 hours per symbol
- **Default leverage** = 5x

---

## Exit Strategy

### Layer 1: Hard Stops (Always Active)

| Exit | Condition | Purpose |
|------|-----------|---------|
| **Hard Stop Loss** | P&L <= -8% | Limit damage at 5x leverage. Fires immediately, no minimum hold. |
| **Stale Position** | Held >72h AND P&L between -10% and +5% | Cut dead money — coin isn't moving. |

### Layer 2: Scale-Out (First Target)

| Exit | Condition | Action |
|------|-----------|--------|
| **Scale-Out** | P&L >= +25% | **Sell 50% of position.** Lock in profit on half, let the other half ride as a "runner." |

### Layer 3: Runner Exits (After Scale-Out Only)

Once 50% has been sold at +25%, the remaining runner position uses these exits to try to catch a 5-20x move:

| Exit | Condition | Logic |
|------|-----------|-------|
| **OI Divergence** | OI dropped 20%+ from peak while price still within 70% of peak P&L | Smart money leaving — the move is over. OI collapsing while price holds = distribution. |
| **Funding Decay** | Peak negative funding was beyond -0.3%/cycle AND current funding has decayed to <30% of peak magnitude | Squeeze fuel exhausted. E.g., peak was -2.5%/cycle → exits when funding rises above -0.75%. No more short liquidation cascades. |
| **Structure Break** | 1h candle close below the most recent higher low (L2/R2 swing low detection) | Pump staircase broken. Finds swing lows on 1h chart, identifies the most recent one that was higher than its predecessor (confirming uptrend), then exits if price closes below it. |
| **Ratchet Floors** | Safety net — locks profit based on peak P&L | Peak +200% → floor at +80%. Peak +100% → floor at +30%. Peak +50% → floor at breakeven. |

### Layer 4: Signal-Based Exits (Pre-Scale-Out, After 30min Hold)

For positions that haven't yet hit +25%, these softer exits run after a 30-minute minimum hold:

| Exit | Condition |
|------|-----------|
| **Graduation Stale** | Coin moved to Pool A/B (mainstream) for 24h+ AND profitable |
| **Graduation + Weakness** | In Pool A/B + funding positive OR OI dropping + profitable |
| **Funding Flipped Positive** | 3/4 recent cycles positive OR average > +0.03%, while P&L > +30% |
| **OI Dropping** | OI down >10% over 4h while profitable |
| **Extreme Extension** | P&L >= +200% from entry |
| **Crime Pump Detected** | Crime pump score hit 60+ post-entry |

Signal-based exits have a **profit lock range**: exits are blocked when P&L is between +10% and +30% (let winners run past the noise zone).

---

## Position Tracking

The bot continuously tracks per position:
- **Peak P&L %** — for ratchet floor calculations
- **Peak OI** — for OI divergence detection (OI drop from peak)
- **Peak Negative Funding** — for funding decay detection (funding recovery from extreme)

These are updated every check cycle (every ~1 minute between scan cycles, every ~5 minutes during main scan).

---

## Data Sources

All data from Bybit v5 public API:
- `/v5/market/tickers` — 24h turnover, price change, current price
- `/v5/market/kline` — 1h candles (48 bars), daily candles (14 bars)
- `/v5/market/open-interest` — 5min OI data (48 points = 4 hours)
- `/v5/market/funding/history` — Last 10 funding rate cycles
- CoinGecko API — Circulating supply ratio (cached)

---

## Summary: The Edge

The bot is designed to exploit one specific inefficiency: **cabal-controlled small-cap coins go through a predictable lifecycle**.

1. **Accumulation** (quiet, low volume, insiders positioning) ← **BOT ENTERS HERE**
2. **Initial pump** (volume spikes, OI builds, shorts arrive)
3. **Consolidation** (negative funding, tight range, shorts stack = squeeze fuel)
4. **Explosive squeeze** (short liquidation cascade, 5-20x moves)
5. **Distribution** (funding flips positive, OI drops, insiders dump) ← **BOT EXITS HERE**

The bot catches phase 1-2 through the accumulation and pre-squeeze signals, rides through phase 3-4 with the runner strategy, and exits at phase 5 through OI divergence, funding decay, and structure break detection.

**What it does NOT catch:** news-driven pumps (no insider positioning visible beforehand), large-cap breakouts (filtered out by Pool D), or coins that pump and dump within a single candle (too fast for 5-min scan cycle).
