# LimeCasino math toolkit (player-facing)

This folder is meant to be shared with the community so anyone can **independently validate** the published LimeCasino odds.

Any changes we make to casinos in-game will be reflected in this repository so you can continue to validate.

| File | Role |
| --- | --- |
| [`tools/casino_math.py`](tools/casino_math.py) | Exact RTP + Monte Carlo + **item hunt** calculator |
| [`presets/*.json`](presets/) | Canonical machine configs (what staff load in the toolgun) |
| This README | How the Python mirrors the live Lua, and how to run checks |

You do **not** need Garry’s Mod installed. You need **Python 3.10+** (stdlib only).

```bash
cd addons/pcasino/tools
python3 casino_math.py --spins 200000
python3 casino_math.py --list-items
python3 casino_math.py --hunt bmwm3gtr46 --skip-rtp
python3 casino_math.py --hunt kawasakininja --machine advanced_low --machine advanced_mid --machine advanced_high --skip-rtp
```

---

## Design goals (what “fair” means here)

1. **Basic slots target ~95% cash RTP; advanced machines target ~92%.** Each preset carries its own `target_rtp` and the validator checks against that, not one global number.
2. **Mystery Wheel items are not cash.** Cars / Dragon’s Breath / Rolex / etc. are prestige. Bound prizes use hidden `tradable=false` item meta so they cannot be given, dropped, or inventory-sold (Trabbi is the exception — still tradable as the joke prize).
3. **Line payouts are capped at 40× stake on advanced machines**, with frequent low-multiplier gold/emerald pair pays buying the RTP back at the bottom. This is a deliberate trade: a lower headline top prize in exchange for a far smaller tail. The uncapped tables could pay $3.03M from a single spin.
4. **Cheaper machines are slower**, not cheaper. Chest weights are tuned per machine (7 / 14 / 34 on Low / Mid / High) so that expected cash wagered per single-segment Mystery item is equal on all three — see below.

**Hunt costs are equalised across the advanced machines.** Expected cash wagered for a specific single-segment Mystery item is ≈ **$59.0M / $57.5M / $58.7M** on Low / Mid / High (a 1.03× spread), for an expected net cash cost of ≈ **$4.7M** per item on any of them.

All three run **1/12** Big Wheel mini-segments, so equalisation comes entirely from the chest weight: per-spin bonus rate is held proportional to the stake (1.93% / 3.97% / 9.71% against bets of $5k / $10k / $25k). The residual spread is integer granularity — one unit of chest moves Adv Mid by about 7%, so 1.03× is the closest reachable without inflating every weight on the reel.

One interaction worth knowing: **the bonus rate also sets the jackpot pot.** The pot grows by `bet * betAdd` every spin and only resets when the mini-wheel lands its jackpot segment, so a rarer bonus means a longer accumulation. At these weights the pot averages **$98k / $101k / $118k** — about 20× / 10× / 5× the stake. Push the bonus rate much lower and the pot balloons past the 40× line cap and becomes the dominant tail again; at a 0.28% bonus on Adv Low it reached $649k, or 130× stake.

---

## How live Lua works (and how Python mirrors it)

pCasino does **not** use real reel strips or a hidden RTP field. Odds are whatever weights/combos/wheel segments are saved on the entity. Our presets encode those settings in JSON; the simulator implements the **same rules** as the Lua.

### 1) Taking the bet

**Lua** (`pcasino_slot_machine` / `pcasino_wheel_slot_machine` `StartRound`):

- `CanAfford` → `AddMoney(-bet)`
- If jackpots enabled: `jackpot += bet * betAdd`

**Python:** every simulated spin spends `bet` and grows the pot the same way.

### 2) Picking three reel symbols

**Lua** (`GenerateResult`):

```lua
-- chance[symbol] is an integer weight
for symbol, weight in pairs(self.data.chance) do
    for i = 1, weight do table.insert(pool, symbol) end
end
return table.Random(pool)  -- independent draw per reel
```

**Python:** build the same multiset pool; `random.choice` three times.  
Per-reel probability of symbol `s` is `weight[s] / sum(weights)`.

### 3) Choosing the winning combo

**Lua** (`CheckForCombo`): walk the combo list; keep the best match. Patterns may use `"anything"`. If jackpots are on, a jackpot/`j=true` combo beats a cash combo when both match (chest / dollar lines).

**Python:** `pick_combo()` copies that preference order (`j` preferred, else higher `p`).

### 4) Cash line payout

**Lua:**

```lua
baseWinnings = bet + bet * tonumber(win.p)   -- i.e. bet * (1 + p)
AddMoney(baseWinnings)
```

**Python:** same multiplier.  
`p = 0.5` ⇒ get **1.5× stake** back (net +0.5 bet if you ignore the stake already taken).

### 5) Jackpots and the `startValue` footgun

**Lua:**

- Each spin contributes `bet * betAdd` into the pot.
- On a jackpot award, pay `GetCurrentJackpot()`, then **reset pot to `startValue`**.

That reset **creates** `startValue` dollars every cycle (not player money). Exact RTP must include:

```text
jackpot RTP = betAdd + P(jackpot award) * startValue / bet
```

**Python** uses that formula (basic slots) and the equivalent pot average for mini-wheel jackpot segments (advanced).

On a **basic** slot the jackpot combo pays the line *and* the pot — `AddMoney(bet + bet*p)` runs before `AddMoney(jackpot)` — so the stake comes back on top of the pot. Every jackpot combo in these presets has `p = 0`, making that exactly one stake. Advanced machines differ: the chest pays no line cash at all, it only queues the mini-wheel.

### 6) Advanced mini-wheel (12 segments, uniform)

**Lua** (`pcasino_wheel_slot_machine` `StartSpin`):

```lua
local result = math.random(12)
local winData = self.data.wheel[result]
RewardsFunctions[winData.f](ply, ent, winData.i)
```

Chest combos (`j=true`) do **not** pay line cash; they queue this wheel.

| `f` | Meaning in our presets |
| --- | --- |
| `money` | Pay `i` dollars |
| `jackpot` | Pay machine pot, reset to `startValue` |
| `nothing` | No cash |
| `prize_wheel` | Grant one Mystery / Big Wheel free spin |

**Python:** `math.random(12)` → `rng.randrange(12)`; same reward table.

### 7) Mystery / Big Wheel (20 segments, uniform, free-spin only)

**Lua:** `math.random(20)` over `data.wheel`. Our preset sets `buySpin.buy = false`.

**Spin Again** is `f = "prize_wheel"` again (recursive free spin).

**Python** cash EV of one activation (items = $0):

```text
E_cash = (sum of money segment amounts) / (20 - number_of_spin_again_segments)
```

Probability a free spin **eventually** awards a specific item with `k` matching segments and `r` Spin Again segments:

```text
P(item | free spin) = k / (20 - r)
```

(With one M3 segment and one Spin Again: `1/19`.)

### 8) End-to-end item hunt probability

On an advanced machine:

```text
P(item on one paid spin)
  = P(mini-wheel)                 -- chest appears (bonus_rate)
  * (BigWheelSegments / 12)       -- mini lands prize_wheel
  * P(item | Mystery activation)  -- absorbing prob above
```

Then:

```text
E[spins until first item] = 1 / P
E[money wagered]          = E[spins] * bet
E[cash returned]          = E[wagered] * RTP
E[net cash]               = E[cash returned] - E[wagered]   # ≈ -8% of wagered (advanced)
```

Geometric distribution ⇒ **median** spins ≈ `ln(2) / P` (often much lower than the mean).

---

## Commands

### Full RTP validation

```bash
python3 casino_math.py --spins 500000
```

Prints exact RTP, hit rate, volatility label, Monte Carlo check, a **session P&L spread**, and a rough floor stress mix. Exit code `1` if any machine is outside **its own `target_rtp` ± 0.5pp** (95% for basic slots, 92% for advanced).

### Session P&L spread (how bad can one evening get?)

RTP is a long-run average. It says nothing about what a single session looks like, and an RP economy experiences sessions, not limits. Every machine now also reports the distribution of **house** profit over a session:

```text
advanced_high  bet=$25,000  RTP=92.00%  hit=30.4%  vol=medium (CV=3.74)
    session P&L (1,000 spins = $25,000,000 wagered, 2,000 sims):
      worst 1% $    -4,901,370   p05 $    -2,569,321   median $   +2,192,985   p95 $   +6,440,115
      mean $    +2,018,340 (8.07% of wagered)   P(house down) = 24.1%
```

Read that as: the house still finishes down on **24.1%** of thousand-spin sessions, with a 1-in-100 session costing $4.9M. That is *after* capping lines at 40×; the uncapped 95%-RTP table was down on 38.6% of sessions with a 1-in-100 session over **$10M**, because a 121.2× top line on a $25,000 stake pays $3.03M from one spin.

```bash
python3 casino_math.py --session-spins 2000 --session-trials 5000   # deeper sample
python3 casino_math.py --session-trials 0                           # skip it (faster)
```

Defaults are 1,000 spins × 2,000 simulated sessions per machine, which is roughly one dedicated player for an hour.

How it works: the tool builds the **exact** finite distribution of house profit for a single spin — every reel combo, every mini-wheel segment, and the Mystery Wheel's absorbing cash distribution — then samples sessions from it. Jackpots are drawn from their real steady-state distribution (`start + step × G`, `G ~ Geometric(p_award)`) rather than pinned at the average, so pot-driven tails show up honestly. The distribution's analytic mean is asserted against the exact RTP on every run; a mismatch fails the tool rather than printing a plausible-looking percentile.

### Item hunt

```bash
python3 casino_math.py --list-items
python3 casino_math.py --hunt bmwm3gtr46 --skip-rtp
python3 casino_math.py --hunt vape_dragonsbreath --skip-rtp --hunt-trials 3000
python3 casino_math.py --hunt bmwm3gtr46 --machine advanced_high --skip-rtp
```

Shows, per advanced machine:

- chance per spin / “1 in N”
- exact expected spins, wagered, cash back, **net cash**
- exact median estimate
- optional Monte Carlo distribution (mean / median / p25 / p75)

### Regenerate Lua (staff only)

```bash
python3 casino_math.py --emit-lua ../lua/perfectcasino/config/sh_presets.lua --skip-rtp
```

JSON is canonical; `sh_presets.lua` is a generated mirror for the game.

---

## Reading the JSON presets

Each `presets/<id>.json` has:

- `settings` — exact pCasino toolgun payload (`bet`, `chance`, `combo`, `jackpot`, `wheel`, …)
- `math` — published RTP / payline probabilities (what `/odds` shows in-game)
- `hunt` (advanced) — equalised expected wager for a single-segment item (`target_expected_wager_per_single_segment_item` is the design target the chest weights are solved against).

Machine ids:

`basic_low`, `basic_mid`, `basic_high`, `advanced_low`, `advanced_mid`, `advanced_high`, `mystery_wheel`

---

## FAQ

**Why isn’t Adv Low the same chance as Adv High?**  
In expected dollars it is. All three run **1/12** Big Wheel mini-segments, and the chest weight is scaled with the stake (7 / 14 / 34 against $5k / $10k / $25k) so the bonus rate stays proportional to what you are betting. Per-spin odds are therefore *worse* on the cheap machines, which is what keeps expected dollars-per-item aligned.

**Was it always equalized?**  
Mostly. An earlier draft made Adv Low the *most expensive* hunt; later ones equalised all three, first at ≈ $7.5M wagered per single-segment item and now at ≈ **$58M** after the Big Wheel dropped to 1/12 and the chest weights were scaled down. The mechanism changed along the way — it used to be a 3/2/1 Big Wheel split paired with chest weights, and is now chest weight alone.

**Does winning the M3 count as cash RTP?**  
No. You still “pay” via the ~5% house edge on all the cash you cycled to get there.

**What about the $500k Mystery cash prize?**  
It’s real money and is included in Mystery cash EV (~$49k per spin including respins). Advanced machines are tuned so overall cash RTP lands on ~92%.

**Casino-bound items**  
i8, Gold Rolex, Stolen Police Uniform, and Magical Cake are granted with hidden metadata `tradable=false`. Give / drop / inventory-sell are blocked. Trabbi is not bound. M3 / Ninja / Dragon’s Breath / Golden Vape are already locked in their item definitions.

**Can I change the JSON and re-check?**  
Yes. Edit a preset, re-run `casino_math.py`. If you only care about hunts: `--hunt … --skip-rtp`.

**In-game `/odds`**  
Shows the same published `math` block players get from these files — so the sheet and the game stay in sync when staff place machines from these presets.
