#!/usr/bin/env python3
"""
CityRP LimeCasino math toolkit.

Exact RTP / hit-rate / volatility for pCasino machines, Monte Carlo checks,
and item-hunt expected-cost analysis. Reads preset JSON under ../presets/.

See README.md in this folder for the Lua ↔ Python mapping (player-facing).

pCasino payout model (slots):
  - Stake is taken up-front.
  - Cash combo pays: bet * (1 + p)
  - Jackpot combo (j=true) pays the machine jackpot pot (basic) or queues
    the mini-wheel (advanced). Pot resets to startValue after each award
    (house-seeded); RTP includes betAdd + P(award)*startValue/bet.
  - Mini-wheel / mystery-wheel segments are uniform (1/N).
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import statistics
from collections import Counter
from itertools import accumulate
from pathlib import Path
from typing import Any

PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"
TARGET_RTP = 0.95
RTP_TOLERANCE = 0.005  # ±0.5pp


def _chance_probs(chance: dict[str, int]) -> dict[str, float]:
    total = sum(chance.values())
    if total <= 0:
        raise ValueError("chance weights must sum > 0")
    return {k: v / total for k, v in chance.items()}


def _combo_matches(pattern: list[str], a: str, b: str, c: str) -> bool:
    reels = (a, b, c)
    for i, sym in enumerate(pattern):
        if sym == "anything":
            continue
        if reels[i] != sym:
            return False
    return True


def pick_combo(combos: list[dict], a: str, b: str, c: str, jackpot_on: bool) -> dict | None:
    """Mirror pCasino CheckForCombo selection."""
    win: dict | None = None
    for combo in combos:
        if not _combo_matches(combo["c"], a, b, c):
            continue
        if win is not None:
            if jackpot_on and win.get("j") and not combo.get("j"):
                continue
            if jackpot_on and combo.get("j") and not win.get("j"):
                win = combo
                continue
            if float(win.get("p", 0)) > float(combo.get("p", 0)):
                continue
        win = combo
    return win


def exact_basic_slot(cfg: dict) -> dict[str, Any]:
    chance = {k: int(v) for k, v in cfg["chance"].items()}
    combos = cfg["combo"]
    bet = float(cfg["bet"]["default"])
    jackpot_on = bool(cfg["jackpot"]["toggle"])
    bet_add = float(cfg["jackpot"]["betAdd"]) if jackpot_on else 0.0
    symbols = list(chance.keys())
    weights = [chance[s] for s in symbols]
    total_w = sum(weights)

    # Enumerate all weighted outcomes
    cash_return = 0.0  # expected money returned / bet (excludes jackpot pot)
    jackpot_hit_p = 0.0
    hit_p = 0.0
    # Second moment of (return / bet) for cash-only; jackpot modeled separately
    returns: list[tuple[float, float, bool]] = []  # (prob, line_multiplier, is_jackpot)

    for i, s1 in enumerate(symbols):
        for j, s2 in enumerate(symbols):
            for k, s3 in enumerate(symbols):
                p = (weights[i] * weights[j] * weights[k]) / (total_w ** 3)
                win = pick_combo(combos, s1, s2, s3, jackpot_on)
                if not win:
                    returns.append((p, 0.0, False))
                    continue
                hit_p += p
                mult = 1.0 + float(win["p"])
                cash_return += p * mult
                if jackpot_on and win.get("j"):
                    jackpot_hit_p += p
                    # The Lua pays the line AND the pot on a jackpot combo
                    # (pcasino_slot_machine/init.lua:203-217), so the stake comes
                    # back on top of the pot. p is 0 for every jackpot combo in
                    # the presets, making this exactly one stake returned.
                    returns.append((p, mult, True))
                else:
                    returns.append((p, mult, False))

    # Pot resets to startValue after each hit (house-seeded), then grows by bet*betAdd.
    # E[pot at hit] = start + bet*betAdd/P(hit). RTP = betAdd + P(hit)*start/bet.
    start_jp = float(cfg["jackpot"]["startValue"]) if jackpot_on else 0.0
    if jackpot_hit_p > 0:
        avg_jp_mult = (start_jp / bet) + (bet_add / jackpot_hit_p)
        jackpot_rtp = bet_add + jackpot_hit_p * (start_jp / bet)
    else:
        avg_jp_mult = 0.0
        jackpot_rtp = 0.0
    rtp = cash_return + jackpot_rtp

    mean = rtp
    var = 0.0
    for p, mult, is_jackpot in returns:
        x = mult + (avg_jp_mult if is_jackpot else 0.0)
        var += p * (x - mean) ** 2
    stdev = math.sqrt(var)
    cv = stdev / mean if mean > 0 else float("inf")

    paylines = []
    for combo in combos:
        # Exact probability of this combo winning under selection rules
        p_win = 0.0
        for i, s1 in enumerate(symbols):
            for j, s2 in enumerate(symbols):
                for k, s3 in enumerate(symbols):
                    p = (weights[i] * weights[j] * weights[k]) / (total_w ** 3)
                    chosen = pick_combo(combos, s1, s2, s3, jackpot_on)
                    if chosen is combo or (
                        chosen
                        and chosen.get("c") == combo["c"]
                        and chosen.get("p") == combo["p"]
                        and bool(chosen.get("j")) == bool(combo.get("j"))
                    ):
                        # identity via values — use match that selected equal fields
                        if chosen and chosen["c"] == combo["c"] and float(chosen["p"]) == float(combo["p"]) and bool(chosen.get("j")) == bool(combo.get("j")):
                            p_win += p
        label = "-".join(combo["c"])
        if combo.get("j"):
            payout = "jackpot"
            contrib = jackpot_rtp  # attributed at machine level
        else:
            payout = f"{1 + float(combo['p']):.2f}x stake"
            contrib = p_win * (1 + float(combo["p"]))
        paylines.append(
            {
                "pattern": label,
                "p": combo.get("p"),
                "jackpot": bool(combo.get("j")),
                "probability": p_win,
                "payout": payout,
                "rtp_contribution": contrib if not combo.get("j") else None,
            }
        )

    return {
        "type": "basic_slot",
        "bet": bet,
        "rtp": rtp,
        "cash_rtp": cash_return,
        "jackpot_rtp": jackpot_rtp,
        "hit_rate": hit_p,
        "jackpot_hit_rate": jackpot_hit_p,
        "stdev": stdev,
        "volatility_cv": cv,
        "volatility_label": _vol_label(cv, "basic"),
        "paylines": paylines,
        "avg_jackpot_at_hit": bet * avg_jp_mult if jackpot_hit_p else 0.0,
    }


def _vol_label(cv: float, kind: str) -> str:
    # Tuned bands for independent-reel bag slots (higher baseline variance than strip slots).
    if kind == "basic":
        if cv < 3.5:
            return "low"
        if cv < 5.5:
            return "low-medium"
        if cv < 8.0:
            return "medium"
        return "medium-high"
    # advanced
    if cv < 5.0:
        return "medium"
    if cv < 8.0:
        return "medium-high"
    return "high"


def mini_wheel_cash_ev(wheel: list[dict], bet: float, avg_jackpot: float, big_wheel_cash_ev: float) -> dict:
    """Expected cash from one mini-wheel spin (items = $0 cash)."""
    n = len(wheel)
    assert n == 12, "mini-wheel must have 12 segments"
    cash = 0.0
    p_free = 0.0
    p_jp = 0.0
    p_nothing = 0.0
    p_money = 0.0
    p_item = 0.0
    for seg in wheel:
        fn = seg["f"]
        if fn == "money":
            cash += float(seg["i"]) / n
            p_money += 1 / n
        elif fn == "jackpot":
            cash += avg_jackpot / n
            p_jp += 1 / n
        elif fn == "prize_wheel":
            cash += big_wheel_cash_ev / n
            p_free += 1 / n
        elif fn == "nothing":
            p_nothing += 1 / n
        elif fn == "cityrp_giveitem":
            p_item += 1 / n
        else:
            raise ValueError(f"unsupported mini-wheel reward: {fn}")
    return {
        "cash_ev": cash,
        "p_free_spin": p_free,
        "p_jackpot": p_jp,
        "p_nothing": p_nothing,
        "p_money": p_money,
        "p_item": p_item,
        "free_spin_count": round(p_free * n),
    }


def mystery_wheel_cash_ev(wheel: list[dict]) -> dict:
    """Cash EV of one mystery-wheel spin, solving for Spin Again recursion."""
    n = len(wheel)
    assert n == 20, "mystery wheel must have 20 segments"
    money_sum = 0.0
    n_respin = 0
    items: list[str] = []
    breakdown: list[dict] = []
    for seg in wheel:
        fn = seg["f"]
        entry = {"label": seg["n"], "f": fn, "i": seg.get("i"), "p": 1 / n}
        if fn == "money":
            money_sum += float(seg["i"])
            entry["cash"] = float(seg["i"])
        elif fn == "prize_wheel":
            n_respin += 1
            entry["cash"] = "respin"
        elif fn == "nothing":
            entry["cash"] = 0
        elif fn == "cityrp_giveitem":
            items.append(str(seg["i"]))
            entry["cash"] = 0
            entry["item"] = seg["i"]
        else:
            raise ValueError(f"unsupported mystery reward: {fn}")
        breakdown.append(entry)

    # E = (money_sum + n_respin * E) / n  =>  E = money_sum / (n - n_respin)
    denom = n - n_respin
    if denom <= 0:
        raise ValueError("mystery wheel cannot be all respins")
    ev = money_sum / denom
    # Segment probabilities are quoted against `denom`, not n. A Spin Again pays
    # nothing and re-draws, so what a player actually experiences is the chance the
    # activation EVENTUALLY lands a segment -- k/denom. Quoting k/n understates
    # every real rate by the respin share.
    return {
        "cash_ev": ev,
        "raw_money_sum": money_sum,
        "respin_segments": n_respin,
        "absorbing_denominator": denom,
        "item_ids": items,
        "segment_probability": 1 / n,
        "p_any_item": len(items) / denom,
        "p_grand_prize": sum(1 for s in wheel if s["f"] == "cityrp_giveitem" and s["i"] in GRAND_PRIZES) / denom,
        "segments": breakdown,
    }


GRAND_PRIZES = {"vape_dragonsbreath", "bmwm3gtr46", "kawasakininja"}


def exact_wheel_slot(cfg: dict, big_wheel_cash_ev: float) -> dict[str, Any]:
    chance = {k: int(v) for k, v in cfg["chance"].items()}
    combos = cfg["combo"]
    bet = float(cfg["bet"]["default"])
    jackpot_on = bool(cfg["jackpot"]["toggle"])
    bet_add = float(cfg["jackpot"]["betAdd"]) if jackpot_on else 0.0
    symbols = list(chance.keys())
    weights = [chance[s] for s in symbols]
    total_w = sum(weights)

    cash_return = 0.0
    bonus_p = 0.0  # P(mini-wheel)
    hit_p = 0.0
    returns: list[tuple[float, float | None]] = []

    for i, s1 in enumerate(symbols):
        for j, s2 in enumerate(symbols):
            for k, s3 in enumerate(symbols):
                p = (weights[i] * weights[j] * weights[k]) / (total_w ** 3)
                win = pick_combo(combos, s1, s2, s3, jackpot_on)
                if not win:
                    returns.append((p, 0.0))
                    continue
                hit_p += p
                if win.get("j"):
                    bonus_p += p
                    returns.append((p, None))  # bonus — filled after mini EV known
                else:
                    mult = 1.0 + float(win["p"])
                    cash_return += p * mult
                    returns.append((p, mult))

    # Mini-wheel jackpot: pot resets to startValue after each award (house seed),
    # then grows by bet*betAdd per spin. E[pot at hit] = start + bet*betAdd / P(award).
    # RTP from those awards = betAdd + P(award)*start/bet (included via mini cash EV).
    wheel = cfg["wheel"]
    start_jp = float(cfg["jackpot"]["startValue"])
    p_jp_seg = sum(1 for s in wheel if s["f"] == "jackpot") / len(wheel)
    jp_award_rate = bonus_p * p_jp_seg
    if jp_award_rate > 0:
        avg_jp = start_jp + (bet * bet_add / jp_award_rate)
    else:
        avg_jp = start_jp

    mini = mini_wheel_cash_ev(wheel, bet, avg_jp, big_wheel_cash_ev)
    bonus_cash_rtp = (bonus_p * mini["cash_ev"]) / bet if bet else 0.0

    rtp = cash_return + bonus_cash_rtp

    mean = rtp
    bonus_mult = (mini["cash_ev"] / bet) if bet else 0.0
    var = 0.0
    for p, mult in returns:
        x = bonus_mult if mult is None else mult
        var += p * (x - mean) ** 2
    stdev = math.sqrt(var)
    cv = stdev / mean if mean > 0 else float("inf")

    paylines = []
    for combo in combos:
        p_win = 0.0
        for i, s1 in enumerate(symbols):
            for j, s2 in enumerate(symbols):
                for k, s3 in enumerate(symbols):
                    p = (weights[i] * weights[j] * weights[k]) / (total_w ** 3)
                    chosen = pick_combo(combos, s1, s2, s3, jackpot_on)
                    if (
                        chosen
                        and chosen["c"] == combo["c"]
                        and float(chosen["p"]) == float(combo["p"])
                        and bool(chosen.get("j")) == bool(combo.get("j"))
                    ):
                        p_win += p
        paylines.append(
            {
                "pattern": "-".join(combo["c"]),
                "p": combo.get("p"),
                "jackpot": bool(combo.get("j")),
                "probability": p_win,
                "payout": "mini-wheel" if combo.get("j") else f"{1 + float(combo['p']):.2f}x stake",
            }
        )

    return {
        "type": "wheel_slot",
        "bet": bet,
        "rtp": rtp,
        "cash_line_rtp": cash_return,
        "bonus_cash_rtp": bonus_cash_rtp,
        "hit_rate": hit_p,
        "bonus_rate": bonus_p,
        "p_free_spin_per_spin": bonus_p * mini["p_free_spin"],
        "mini_wheel": mini,
        "avg_jackpot": avg_jp,
        "stdev": stdev,
        "volatility_cv": cv,
        "volatility_label": _vol_label(cv, "advanced"),
        "paylines": paylines,
    }


def monte_carlo_basic(cfg: dict, spins: int, seed: int = 1) -> dict:
    rng = random.Random(seed)
    chance = {k: int(v) for k, v in cfg["chance"].items()}
    pool: list[str] = []
    for sym, w in chance.items():
        pool.extend([sym] * w)
    combos = cfg["combo"]
    bet = float(cfg["bet"]["default"])
    jackpot_on = bool(cfg["jackpot"]["toggle"])
    bet_add = float(cfg["jackpot"]["betAdd"]) if jackpot_on else 0.0
    start_jp = float(cfg["jackpot"]["startValue"])
    jp = start_jp
    spent = 0.0
    returned = 0.0
    hits = 0
    jp_hits = 0

    for _ in range(spins):
        spent += bet
        jp += bet * bet_add
        a, b, c = rng.choice(pool), rng.choice(pool), rng.choice(pool)
        win = pick_combo(combos, a, b, c, jackpot_on)
        if not win:
            continue
        hits += 1
        if jackpot_on and win.get("j"):
            returned += jp
            jp = start_jp
            jp_hits += 1
        else:
            returned += bet * (1 + float(win["p"]))

    return {
        "spins": spins,
        "rtp": returned / spent if spent else 0,
        "hit_rate": hits / spins,
        "jackpot_hits": jp_hits,
        "final_jackpot": jp,
        "net_house": spent - returned,
    }


def monte_carlo_wheel(
    cfg: dict,
    mystery_cfg: dict,
    spins: int,
    seed: int = 1,
) -> dict:
    rng = random.Random(seed)
    chance = {k: int(v) for k, v in cfg["chance"].items()}
    pool: list[str] = []
    for sym, w in chance.items():
        pool.extend([sym] * w)
    combos = cfg["combo"]
    bet = float(cfg["bet"]["default"])
    jackpot_on = bool(cfg["jackpot"]["toggle"])
    bet_add = float(cfg["jackpot"]["betAdd"]) if jackpot_on else 0.0
    start_jp = float(cfg["jackpot"]["startValue"])
    jp = start_jp
    wheel = cfg["wheel"]
    mystery = mystery_cfg["wheel"]

    spent = 0.0
    returned = 0.0
    items: dict[str, int] = {}
    free_spins_awarded = 0
    mystery_spins = 0

    def spin_mystery() -> float:
        nonlocal mystery_spins
        cash = 0.0
        # Resolve respins iteratively
        for _ in range(50):
            mystery_spins += 1
            seg = mystery[rng.randrange(len(mystery))]
            fn = seg["f"]
            if fn == "money":
                cash += float(seg["i"])
                return cash
            if fn == "nothing":
                return cash
            if fn == "cityrp_giveitem":
                iid = str(seg["i"])
                items[iid] = items.get(iid, 0) + 1
                return cash
            if fn == "prize_wheel":
                continue  # spin again
            raise ValueError(fn)
        return cash

    for _ in range(spins):
        spent += bet
        jp += bet * bet_add
        a, b, c = rng.choice(pool), rng.choice(pool), rng.choice(pool)
        win = pick_combo(combos, a, b, c, jackpot_on)
        if not win:
            continue
        if win.get("j"):
            seg = wheel[rng.randrange(len(wheel))]
            fn = seg["f"]
            if fn == "money":
                returned += float(seg["i"])
            elif fn == "jackpot":
                returned += jp
                jp = start_jp
            elif fn == "prize_wheel":
                free_spins_awarded += 1
                returned += spin_mystery()
            elif fn == "cityrp_giveitem":
                iid = str(seg["i"])
                items[iid] = items.get(iid, 0) + 1
            elif fn == "nothing":
                pass
            else:
                raise ValueError(fn)
        else:
            returned += bet * (1 + float(win["p"]))

    return {
        "spins": spins,
        "rtp": returned / spent if spent else 0,
        "net_house": spent - returned,
        "free_spins_awarded": free_spins_awarded,
        "mystery_spins_resolved": mystery_spins,
        "items_won": items,
        "final_jackpot": jp,
    }


SESSION_SPINS = 1_000
SESSION_TRIALS = 2_000


def _mystery_terminating_cash(wheel: list[dict]) -> list[float]:
    """
    Cash banked by one Mystery Wheel activation, as a flat distribution.

    A Spin Again segment pays no cash and re-draws, so the cash actually
    awarded is whatever the *terminating* segment pays. Every terminating
    segment is equally likely, which is why the mean here is
    money_sum / (n - respins) -- the same value mystery_wheel_cash_ev solves for.
    """
    terminating = [s for s in wheel if s["f"] != "prize_wheel"]
    if not terminating:
        raise ValueError("mystery wheel cannot be all respins")
    return [float(s["i"]) if s["f"] == "money" else 0.0 for s in terminating]


def spin_pnl_outcomes(cfg: dict, mystery_wheel: list[dict] | None = None) -> dict[str, Any]:
    """
    Exact finite distribution of HOUSE profit for one paid spin.

    House delta = +bet (stake taken) - everything paid back out.

    The jackpot is kept as a separate outcome rather than folded in at its
    average, because the pot depends on how many spins it has been growing.
    Callers resolve it with draw_jackpot_pot().
    """
    chance = {k: int(v) for k, v in cfg["chance"].items()}
    probs = _chance_probs(chance)
    combos = cfg["combo"]
    bet = float(cfg["bet"]["default"])
    jackpot_on = bool(cfg["jackpot"]["toggle"])
    bet_add = float(cfg["jackpot"]["betAdd"]) if jackpot_on else 0.0
    start_jp = float(cfg["jackpot"]["startValue"])

    # Exact probability of each combo (and of no win) over all reel triples.
    by_combo: dict[int, float] = {}
    p_nowin = 0.0
    for a, pa in probs.items():
        for b, pb in probs.items():
            for c, pc in probs.items():
                win = pick_combo(combos, a, b, c, jackpot_on)
                if win is None:
                    p_nowin += pa * pb * pc
                else:
                    by_combo[id(win)] = by_combo.get(id(win), 0.0) + pa * pb * pc

    values: list[float] = [bet]          # no win: house keeps the stake
    weights: list[float] = [p_nowin]
    # Every machine in the presets reaches the pot from more than one combo
    # (three chest patterns on the advanced machines), so this must be a set --
    # tracking a single index silently drops the other awards.
    jackpot_indices: set[int] = set()
    p_jackpot = 0.0

    def add(value: float, weight: float, is_jackpot: bool = False) -> None:
        if weight <= 0:
            return
        if is_jackpot:
            jackpot_indices.add(len(values))
        values.append(value)
        weights.append(weight)

    is_wheel_slot = "wheel" in cfg

    for combo in combos:
        p = by_combo.get(id(combo), 0.0)
        if p <= 0:
            continue
        line = bet - bet * (1 + float(combo.get("p", 0)))   # == -bet * p

        if not (jackpot_on and combo.get("j")):
            add(line, p)
            continue

        if not is_wheel_slot:
            # Basic slot: pays the line AND the pot on a jackpot combo.
            add(line, p, is_jackpot=True)
            p_jackpot += p
            continue

        # Advanced machine: the chest pays no line cash, it queues the mini-wheel.
        wheel = cfg["wheel"]
        seg_p = p / len(wheel)
        for seg in wheel:
            fn = seg["f"]
            if fn == "money":
                add(bet - float(seg["i"]), seg_p)
            elif fn in ("nothing", "cityrp_giveitem"):
                add(bet, seg_p)
            elif fn == "jackpot":
                add(bet, seg_p, is_jackpot=True)
                p_jackpot += seg_p
            elif fn == "prize_wheel":
                if mystery_wheel is None:
                    raise ValueError("mini-wheel awards a Mystery spin; pass mystery_wheel")
                cashes = _mystery_terminating_cash(mystery_wheel)
                for cash in cashes:
                    add(bet - cash, seg_p / len(cashes))
            else:
                raise ValueError(f"unsupported mini-wheel reward: {fn}")

    jackpot_step = bet * bet_add
    avg_pot = start_jp + (jackpot_step / p_jackpot if p_jackpot > 0 else 0.0)
    expected = sum(
        w * ((v - avg_pot) if i in jackpot_indices else v)
        for i, (v, w) in enumerate(zip(values, weights))
    )

    return {
        "values": values,
        "weights": weights,
        "bet": bet,
        "jackpot_indices": jackpot_indices,
        "p_jackpot": p_jackpot,
        "jackpot_start": start_jp,
        "jackpot_step": jackpot_step,
        "avg_jackpot": avg_pot,
        # Analytic mean house profit per spin == (1 - RTP) * bet. Callers assert
        # against the exact RTP so a mis-wired outcome can't pass silently.
        "expected_house_per_spin": expected,
    }


def draw_jackpot_pot(rng: random.Random, start: float, step: float, p_award: float) -> float:
    """
    Sample the pot at award time.

    Each spin adds `step` before the result resolves, and an award resets the
    pot to `start`, so a pot paid after G spins is start + step*G with
    G ~ Geometric(p_award). Mean start + step/p_award -- the preset avg_jackpot.
    """
    if step <= 0 or p_award <= 0:
        return start
    u = 1.0 - rng.random()                       # (0, 1]
    gap = math.ceil(math.log(u) / math.log(1.0 - p_award))
    return start + step * max(1, gap)


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1)))]


def session_pnl(
    cfg: dict,
    mystery_wheel: list[dict] | None,
    spins: int = SESSION_SPINS,
    trials: int = SESSION_TRIALS,
    seed: int = 99,
) -> dict[str, Any]:
    """
    Distribution of house P&L over a session of `spins` paid spins.

    Mean RTP says what happens over millions of spins. This says what a single
    evening can look like -- which is what an RP economy actually experiences.
    """
    dist = spin_pnl_outcomes(cfg, mystery_wheel)
    values, weights = dist["values"], dist["weights"]
    cum = list(accumulate(weights))
    jp_indices = dist["jackpot_indices"]
    start, step, p_jp = dist["jackpot_start"], dist["jackpot_step"], dist["p_jackpot"]
    rng = random.Random(seed)
    indices = range(len(values))

    results: list[float] = []
    for _ in range(trials):
        counts = Counter(rng.choices(indices, cum_weights=cum, k=spins))
        total = 0.0
        for i, n in counts.items():
            total += values[i] * n
            if i in jp_indices:
                total -= sum(draw_jackpot_pot(rng, start, step, p_jp) for _ in range(n))
        results.append(total)

    results.sort()
    turnover = spins * dist["bet"]
    mean = statistics.mean(results)
    return {
        "spins": spins,
        "trials": trials,
        "turnover": turnover,
        "mean": mean,
        "mean_pct_turnover": mean / turnover if turnover else 0.0,
        "expected_mean": dist["expected_house_per_spin"] * spins,
        "worst_1pct": _percentile(results, 0.01),
        "p05": _percentile(results, 0.05),
        "median": _percentile(results, 0.50),
        "p95": _percentile(results, 0.95),
        "worst": results[0],
        "best": results[-1],
        "p_house_down": sum(1 for r in results if r < 0) / len(results),
    }


def economy_session_mix(presets: dict[str, dict], mystery: dict, hours: float = 4.0, seed: int = 42) -> dict:
    """
    Rough floor stress test: mix of players on each machine for `hours`.
    Assumes ~8 spins/minute sustained (optimistic heavy play).
    """
    rng = random.Random(seed)
    spins_per_hour = 8 * 60
    total_spins = int(hours * spins_per_hour)

    # Traffic mix skewed to cheaper machines
    weights = {
        "basic_low": 30,
        "basic_mid": 25,
        "basic_high": 15,
        "advanced_low": 15,
        "advanced_mid": 10,
        "advanced_high": 5,
    }
    names = list(weights.keys())
    w = [weights[n] for n in names]

    house = 0.0
    by_machine: dict[str, float] = {n: 0.0 for n in names}
    items: dict[str, int] = {}

    # Run in batches per machine for speed
    for name in names:
        share = weights[name] / sum(w)
        n = max(1, int(total_spins * share))
        cfg = presets[name]
        if cfg["entity"] == "pcasino_slot_machine":
            r = monte_carlo_basic(cfg["settings"], n, seed=rng.randrange(1 << 30))
        else:
            r = monte_carlo_wheel(cfg["settings"], mystery["settings"], n, seed=rng.randrange(1 << 30))
            for k, v in r.get("items_won", {}).items():
                items[k] = items.get(k, 0) + v
        house += r["net_house"]
        by_machine[name] = r["net_house"]

    return {
        "hours": hours,
        "approx_spins": total_spins,
        "house_profit": house,
        "by_machine": by_machine,
        "items_won": items,
    }


def load_presets() -> tuple[dict[str, dict], dict]:
    presets = {}
    mystery = None
    for path in sorted(PRESETS_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = json.loads(path.read_text())
        if "id" not in data or "entity" not in data:
            continue
        presets[data["id"]] = data
        if data["entity"] == "pcasino_mystery_wheel":
            mystery = data
    if mystery is None:
        raise SystemExit("missing mystery_wheel preset")
    return presets, mystery


def analyze_all(
    mc_spins: int = 200_000,
    session_spins: int = SESSION_SPINS,
    session_trials: int = SESSION_TRIALS,
) -> dict:
    presets, mystery = load_presets()
    bw = mystery_wheel_cash_ev(mystery["settings"]["wheel"])
    report: dict[str, Any] = {"mystery_wheel": bw, "machines": {}, "checks": []}

    for pid, preset in presets.items():
        if preset["entity"] == "pcasino_mystery_wheel":
            continue
        settings = preset["settings"]
        if preset["entity"] == "pcasino_slot_machine":
            exact = exact_basic_slot(settings)
            mc = monte_carlo_basic(settings, mc_spins)
        else:
            exact = exact_wheel_slot(settings, bw["cash_ev"])
            mc = monte_carlo_wheel(settings, mystery["settings"], mc_spins)
        report["machines"][pid] = {"meta": {k: preset[k] for k in ("id", "name", "entity", "tier", "volatility_target", "bet")}, "exact": exact, "monte_carlo": mc}
        if session_trials > 0:
            sess = session_pnl(
                settings,
                mystery["settings"]["wheel"],
                spins=session_spins,
                trials=session_trials,
            )
            # The per-spin P&L distribution must reproduce the exact RTP. If it
            # drifts, the outcome table is mis-wired and every percentile below
            # it is wrong -- fail loudly rather than publish a plausible number.
            want = (1 - exact["rtp"]) * exact["bet"] * session_spins
            if abs(sess["expected_mean"] - want) > max(1.0, abs(want) * 1e-9):
                raise SystemExit(
                    f"{pid}: session P&L model disagrees with exact RTP "
                    f"(${sess['expected_mean']:,.2f} vs ${want:,.2f})"
                )
            report["machines"][pid]["session"] = sess

        # Machines no longer share one RTP target -- advanced machines run
        # leaner than basic ones -- so the gate reads the preset's own figure.
        target = preset.get("target_rtp") or TARGET_RTP
        ok = abs(exact["rtp"] - target) <= RTP_TOLERANCE
        report["checks"].append(
            {
                "id": pid,
                "rtp": exact["rtp"],
                "target": target,
                "ok": ok,
                "volatility": exact["volatility_label"],
                "volatility_target": preset.get("volatility_target"),
                "mc_rtp": mc["rtp"],
            }
        )

    report["economy"] = economy_session_mix(presets, mystery)
    return report


def print_report(report: dict) -> None:
    print("=== Mystery Wheel ===")
    mw = report["mystery_wheel"]
    print(f"  Cash EV / spin: ${mw['cash_ev']:,.2f}")
    print(f"  P(any item | activation): {mw['p_any_item']:.2%}   P(any grand prize): {mw['p_grand_prize']:.2%}"
          f"   (quoted against {mw['absorbing_denominator']} non-respin segments)")
    print(f"  Items: {', '.join(mw['item_ids'])}")
    print()
    print("=== Machines ===")
    for pid, data in report["machines"].items():
        ex = data["exact"]
        mc = data["monte_carlo"]
        bet = ex["bet"]
        print(f"{pid}  bet=${bet:,.0f}  RTP={ex['rtp']*100:.2f}%  MC={mc['rtp']*100:.2f}%  "
              f"hit={ex.get('hit_rate', 0)*100:.1f}%  vol={ex['volatility_label']} (CV={ex['volatility_cv']:.2f})")
        if ex["type"] == "wheel_slot":
            print(f"    bonus={ex['bonus_rate']*100:.2f}%  P(free/spin)={ex['p_free_spin_per_spin']*100:.3f}%  "
                  f"mini free segs={ex['mini_wheel']['free_spin_count']}/12  avgJP=${ex['avg_jackpot']:,.0f}")
        else:
            print(f"    JP hit={ex['jackpot_hit_rate']*100:.4f}%  eq.avgJP@hit=${ex['avg_jackpot_at_hit']:,.0f}")
        sess = data.get("session")
        if sess:
            print(f"    session P&L ({sess['spins']:,} spins = ${sess['turnover']:,.0f} wagered, "
                  f"{sess['trials']:,} sims):")
            print(f"      worst 1% ${sess['worst_1pct']:>+14,.0f}   p05 ${sess['p05']:>+14,.0f}   "
                  f"median ${sess['median']:>+13,.0f}   p95 ${sess['p95']:>+13,.0f}")
            print(f"      mean ${sess['mean']:>+14,.0f} ({sess['mean_pct_turnover']*100:.2f}% of wagered)"
                  f"   P(house down) = {sess['p_house_down']*100:.1f}%")
    print()
    print(f"=== RTP Checks (each machine's own target ±{RTP_TOLERANCE*100:.1f}pp) ===")
    all_ok = True
    for c in report["checks"]:
        mark = "OK" if c["ok"] else "FAIL"
        if not c["ok"]:
            all_ok = False
        print(f"  [{mark}] {c['id']}: {c['rtp']*100:.2f}% vs target {c['target']*100:.0f}%  "
              f"vol={c['volatility']} (target {c['volatility_target']})")
    eco = report["economy"]
    print()
    print(f"=== Economy stress (~{eco['hours']}h heavy floor) ===")
    print(f"  House net: ${eco['house_profit']:,.0f}")
    for k, v in eco["by_machine"].items():
        print(f"    {k}: ${v:,.0f}")
    if eco["items_won"]:
        print(f"  Items awarded: {eco['items_won']}")
    if not all_ok:
        raise SystemExit(1)


def emit_lua(out_path: Path) -> None:
    """Generate sh_presets.lua from JSON (canonical)."""
    presets, _ = load_presets()
    lines = [
        "-- Auto-generated by tools/casino_math.py — do not hand-edit.",
        "-- Canonical source: addons/pcasino/presets/*.json",
        "PerfectCasino.Presets = PerfectCasino.Presets or {}",
        "PerfectCasino.Presets.List = {}",
        "",
    ]
    # Stable order
    order = [
        "basic_low",
        "basic_mid",
        "basic_high",
        "advanced_low",
        "advanced_mid",
        "advanced_high",
        "mystery_wheel",
    ]
    for pid in order:
        preset = presets[pid]
        payload = json.dumps(preset, separators=(",", ":"))
        # Escape for Lua long string — avoid ]==] in JSON (won't appear)
        lines.append(f"PerfectCasino.Presets.List[{json.dumps(pid)}] = util.JSONToTable([==[{payload}]==])")
        lines.append("")
    lines.append("function PerfectCasino.Presets.Get(id)")
    lines.append("\treturn PerfectCasino.Presets.List[id]")
    lines.append("end")
    lines.append("")
    lines.append("function PerfectCasino.Presets.GetAll()")
    lines.append("\treturn PerfectCasino.Presets.List")
    lines.append("end")
    lines.append("")
    out_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {out_path}")


def mystery_item_absorbing_prob(wheel: list[dict], item_id: str) -> dict[str, Any]:
    """
    Probability that one Mystery Wheel activation eventually awards `item_id`,
    accounting for Spin Again (prize_wheel) recursion.

    Each spin is uniform over N segments. Respins repeat. Absorbing outcomes
    are everything except prize_wheel. So:
        P(item) = (count_item / N) / (1 - count_respin / N) = count_item / (N - count_respin)
    """
    n = len(wheel)
    n_item = sum(1 for s in wheel if s["f"] == "cityrp_giveitem" and str(s["i"]) == item_id)
    n_respin = sum(1 for s in wheel if s["f"] == "prize_wheel")
    if n_item == 0:
        raise ValueError(f"item {item_id!r} is not on the mystery wheel")
    denom = n - n_respin
    if denom <= 0:
        raise ValueError("mystery wheel cannot be all respins")
    return {
        "item_id": item_id,
        "segments": n_item,
        "respin_segments": n_respin,
        "wheel_size": n,
        "p_absorbing": n_item / denom,
        "label": next(s["n"] for s in wheel if s["f"] == "cityrp_giveitem" and str(s["i"]) == item_id),
    }


def hunt_exact(machine: dict, mystery: dict, item_id: str) -> dict[str, Any]:
    """Expected spins / wager / net cash until first award of item_id on an advanced machine."""
    if machine["entity"] != "pcasino_wheel_slot_machine":
        raise ValueError("item hunts only apply to wheel slot machines (Big Wheel path)")
    bw = mystery_wheel_cash_ev(mystery["settings"]["wheel"])
    ex = exact_wheel_slot(machine["settings"], bw["cash_ev"])
    abs_info = mystery_item_absorbing_prob(mystery["settings"]["wheel"], item_id)
    p_free = ex["p_free_spin_per_spin"]
    p_item = p_free * abs_info["p_absorbing"]
    bet = ex["bet"]
    e_spins = 1.0 / p_item
    e_wager = e_spins * bet
    e_cash_back = e_wager * ex["rtp"]
    e_net = e_cash_back - e_wager
    free_segs = sum(1 for s in machine["settings"]["wheel"] if s["f"] == "prize_wheel")
    return {
        "machine_id": machine["id"],
        "machine_name": machine.get("name"),
        "item_id": item_id,
        "item_label": abs_info["label"],
        "bet": bet,
        "rtp": ex["rtp"],
        "bonus_rate": ex["bonus_rate"],
        "mini_big_wheel_segments": free_segs,
        "p_free_spin_per_spin": p_free,
        "p_item_given_mystery": abs_info["p_absorbing"],
        "p_item_per_spin": p_item,
        "one_in_spins": e_spins,
        "expected_spins": e_spins,
        "expected_wagered": e_wager,
        "expected_cash_returned": e_cash_back,
        "expected_net_cash": e_net,
        "median_spins": math.log(2) / p_item,  # geometric median
        "median_wagered": (math.log(2) / p_item) * bet,
    }


def hunt_monte_carlo(
    machine: dict,
    mystery: dict,
    item_id: str,
    trials: int = 2000,
    seed: int = 1,
    max_spins: int = 5_000_000,
) -> dict[str, Any]:
    """Simulate many independent 'hunt until item' runs on one advanced machine."""
    rng = random.Random(seed)
    cfg = machine["settings"]
    chance = {k: int(v) for k, v in cfg["chance"].items()}
    pool: list[str] = []
    for sym, w in chance.items():
        pool.extend([sym] * w)
    combos = cfg["combo"]
    bet = float(cfg["bet"]["default"])
    bet_add = float(cfg["jackpot"]["betAdd"])
    start_jp = float(cfg["jackpot"]["startValue"])
    wheel = cfg["wheel"]
    myst = mystery["settings"]["wheel"]

    spins_l: list[int] = []
    wager_l: list[float] = []
    net_l: list[float] = []

    for _ in range(trials):
        jp = start_jp
        spent = 0.0
        returned = 0.0
        got = False
        for spin in range(1, max_spins + 1):
            spent += bet
            jp += bet * bet_add
            a, b, c = rng.choice(pool), rng.choice(pool), rng.choice(pool)
            win = pick_combo(combos, a, b, c, True)
            if not win:
                continue
            if win.get("j"):
                seg = wheel[rng.randrange(len(wheel))]
                fn = seg["f"]
                if fn == "money":
                    returned += float(seg["i"])
                elif fn == "jackpot":
                    returned += jp
                    jp = start_jp
                elif fn == "prize_wheel":
                    for _r in range(50):
                        m = myst[rng.randrange(len(myst))]
                        if m["f"] == "prize_wheel":
                            continue
                        if m["f"] == "money":
                            returned += float(m["i"])
                            break
                        if m["f"] == "nothing":
                            break
                        if m["f"] == "cityrp_giveitem":
                            if str(m["i"]) == item_id:
                                spins_l.append(spin)
                                wager_l.append(spent)
                                net_l.append(returned - spent)
                                got = True
                            break
                        break
                    if got:
                        break
                elif fn == "nothing":
                    pass
                elif fn == "cityrp_giveitem":
                    if str(seg["i"]) == item_id:
                        spins_l.append(spin)
                        wager_l.append(spent)
                        net_l.append(returned - spent)
                        got = True
                        break
            else:
                returned += bet * (1 + float(win["p"]))
        if not got:
            raise RuntimeError(f"hunt exceeded {max_spins} spins without {item_id}")

    def pct(xs: list[float], p: float) -> float:
        s = sorted(xs)
        return s[min(len(s) - 1, int(p * (len(s) - 1)))]

    return {
        "trials": trials,
        "mean_spins": statistics.mean(spins_l),
        "median_spins": statistics.median(spins_l),
        "p25_spins": pct(spins_l, 0.25),
        "p75_spins": pct(spins_l, 0.75),
        "mean_wagered": statistics.mean(wager_l),
        "median_wagered": statistics.median(wager_l),
        "mean_net_cash": statistics.mean(net_l),
        "median_net_cash": statistics.median(net_l),
    }


def print_hunt_report(item_id: str, machines: list[str] | None, trials: int) -> None:
    presets, mystery = load_presets()
    ids = machines or ["advanced_low", "advanced_mid", "advanced_high"]
    print(f"=== Item hunt: {item_id} ===")
    print("Path: reel chest → mini-wheel Big Wheel segment → Mystery Wheel (with Spin Again)")
    print()
    rows = []
    for mid in ids:
        if mid not in presets:
            raise SystemExit(f"unknown machine {mid}")
        exact = hunt_exact(presets[mid], mystery, item_id)
        rows.append(exact)
        print(f"{exact['machine_id']}  ({exact['machine_name']})")
        print(f"  Bet ${exact['bet']:,.0f}  |  Big Wheel mini-segs {exact['mini_big_wheel_segments']}/12  |  "
              f"P(item/spin)={exact['p_item_per_spin']*100:.4f}%  (1 in {exact['one_in_spins']:,.0f})")
        print(f"  Exact E[spins]={exact['expected_spins']:,.1f}  E[wagered]=${exact['expected_wagered']:,.0f}  "
              f"E[cash back]=${exact['expected_cash_returned']:,.0f}  E[net]=${exact['expected_net_cash']:,.0f}")
        print(f"  Exact median spins≈{exact['median_spins']:,.0f}  median wagered≈${exact['median_wagered']:,.0f}")
        if trials > 0:
            mc = hunt_monte_carlo(presets[mid], mystery, item_id, trials=trials)
            print(f"  MC({trials}): mean spins={mc['mean_spins']:,.0f} median={mc['median_spins']:,.0f} "
                  f"(p25={mc['p25_spins']:,.0f} p75={mc['p75_spins']:,.0f})  "
                  f"mean net=${mc['mean_net_cash']:,.0f} median net=${mc['median_net_cash']:,.0f}")
        print()

    if len(rows) >= 2:
        wagers = [r["expected_wagered"] for r in rows]
        nets = [r["expected_net_cash"] for r in rows]
        print("Cross-machine check (single-segment items should be ~equal expected cash cost):")
        print(f"  E[wagered] range ${min(wagers):,.0f} … ${max(wagers):,.0f}  "
              f"(spread {max(wagers)-min(wagers):,.0f})")
        print(f"  E[net cash] range ${min(nets):,.0f} … ${max(nets):,.0f}")
        print("  Per-spin odds still scale with bet — cheaper machines are slower hunts, not cheaper ones.")


def list_mystery_items() -> None:
    _, mystery = load_presets()
    print("Mystery Wheel cityrp_giveitem segments:")
    for seg in mystery["settings"]["wheel"]:
        if seg["f"] == "cityrp_giveitem":
            print(f"  {seg['i']:24}  label={seg['n']!r}")


LINE_CAP = {"basic": 60.0, "advanced": 40.0}
HUNT_REFERENCE_ITEM = "kawasakininja"  # any single-segment Mystery item gives the same wager


def _scale_cash_p(settings: dict, factor: float, cap: float) -> None:
    """Scale every cash combo's p by factor in place, clamped at the tier's line cap.

    Uniform scaling preserves the relative order of p across combos, so which combo
    wins any given reel triple is unchanged -- the per-combo win probabilities stay
    fixed and RTP is exactly affine in factor. That keeps the deliberate payline
    shape (frequent low pays buying RTP back at the bottom) instead of picking
    winners among combos.
    """
    for combo in settings["combo"]:
        if combo.get("j"):
            continue
        combo["p"] = min(cap, round(float(combo["p"]) * factor, 4))


def solve_cash_scale(preset: dict, big_wheel_ev: float, target: float) -> float:
    """Bisect for the cash-p scale factor that lands this machine on target RTP."""
    cap = LINE_CAP["advanced" if preset["tier"] == "advanced" else "basic"]
    lo, hi = 0.1, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        trial = copy.deepcopy(preset["settings"])
        _scale_cash_p(trial, mid, cap)
        rtp = exact_wheel_slot(trial, big_wheel_ev)["rtp"]
        if rtp < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _payline_block(paylines: list[dict]) -> list[dict]:
    return [
        {
            "pattern": pl["pattern"],
            "probability": round(pl["probability"], 8),
            "payout": pl["payout"],
            "jackpot": pl["jackpot"],
            "p": pl["p"],
        }
        for pl in paylines
    ]


def build_math_block(exact: dict) -> dict:
    """Rebuild a preset's published `math` block from its exact analysis."""
    if exact["type"] == "basic_slot":
        return {
            "rtp": round(exact["rtp"], 6),
            "hit_rate": round(exact["hit_rate"], 6),
            "jackpot_hit_rate": round(exact["jackpot_hit_rate"], 8),
            "volatility_label": exact["volatility_label"],
            "volatility_cv": round(exact["volatility_cv"], 3),
            "avg_jackpot_at_hit": round(exact["avg_jackpot_at_hit"], 2),
            "jackpot_rtp": round(exact["jackpot_rtp"], 6),
            "cash_rtp": round(exact["cash_rtp"], 6),
            "paylines": _payline_block(exact["paylines"]),
        }
    mini = exact["mini_wheel"]
    return {
        "rtp": round(exact["rtp"], 6),
        "hit_rate": round(exact["hit_rate"], 6),
        "bonus_rate": round(exact["bonus_rate"], 6),
        "p_free_spin_per_spin": round(exact["p_free_spin_per_spin"], 8),
        "volatility_label": exact["volatility_label"],
        "volatility_cv": round(exact["volatility_cv"], 3),
        "avg_jackpot": round(exact["avg_jackpot"], 2),
        "mini_wheel": {
            "p_free_spin": mini["p_free_spin"],
            "free_spin_count": mini["free_spin_count"],
            "cash_ev": round(mini["cash_ev"], 2),
            "segments": [
                {"n": s["n"], "f": s["f"], "i": s["i"], "p_seg": round(1 / len(exact["segments_src"]), 6)}
                for s in exact["segments_src"]
            ],
        },
        "paylines": _payline_block(exact["paylines"]),
    }


def build_hunt_block(preset: dict, mystery: dict) -> dict:
    h = hunt_exact(preset, mystery, HUNT_REFERENCE_ITEM)
    return {
        "target_expected_wager_per_single_segment_item": preset["hunt"][
            "target_expected_wager_per_single_segment_item"
        ],
        "expected_wager": round(h["expected_wagered"], 2),
        "expected_net_cash": round(h["expected_net_cash"], 2),
        "p_item_per_spin": h["p_item_per_spin"],
        "one_in_spins": round(h["one_in_spins"], 2),
    }


def refresh_presets(retune: bool = False) -> None:
    """Recompute every preset's derived `math` (and `hunt`) block from its settings.

    The published blocks are what /odds renders in-game, so they go stale the moment
    payout selection or any setting changes -- exactly what happened when the
    jackpot-vs-cash tie-break was fixed. Run this after any settings change.
    """
    presets, mystery = load_presets()
    bw = mystery_wheel_cash_ev(mystery["settings"]["wheel"])["cash_ev"]

    if retune:
        for pid, preset in presets.items():
            if preset["entity"] != "pcasino_wheel_slot_machine":
                continue
            target = float(preset["target_rtp"])
            factor = solve_cash_scale(preset, bw, target)
            _scale_cash_p(preset["settings"], factor, LINE_CAP["advanced"])
            print(f"  {pid}: cash p x{factor:.4f} -> target {target*100:.2f}%")

    for pid, preset in presets.items():
        path = PRESETS_DIR / f"{pid}.json"
        if preset["entity"] == "pcasino_mystery_wheel":
            continue
        if preset["entity"] == "pcasino_slot_machine":
            exact = exact_basic_slot(preset["settings"])
        else:
            exact = exact_wheel_slot(preset["settings"], bw)
            exact["segments_src"] = preset["settings"]["wheel"]
            preset["hunt"] = build_hunt_block(preset, mystery)
        preset["math"] = build_math_block(exact)
        path.write_text(json.dumps(preset, indent=2) + "\n")
        print(f"  wrote {path.name}: RTP {preset['math']['rtp']*100:.3f}%")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python3 casino_math.py
  python3 casino_math.py --spins 500000
  python3 casino_math.py --hunt bmwm3gtr46
  python3 casino_math.py --hunt bmwm3gtr46 --machine advanced_high --hunt-trials 3000
  python3 casino_math.py --list-items
  python3 casino_math.py --emit-lua ../lua/perfectcasino/config/sh_presets.lua
""",
    )
    parser.add_argument("--spins", type=int, default=200_000, help="Monte Carlo spins per machine (RTP check)")
    parser.add_argument(
        "--session-spins",
        type=int,
        default=SESSION_SPINS,
        help=f"Spins per simulated session for the P&L spread (default {SESSION_SPINS:,})",
    )
    parser.add_argument(
        "--session-trials",
        type=int,
        default=SESSION_TRIALS,
        help=f"Sessions to simulate per machine (default {SESSION_TRIALS:,}; 0 disables)",
    )
    parser.add_argument("--json-out", type=Path, help="Write full RTP analysis JSON")
    parser.add_argument("--emit-lua", type=Path, help="Generate sh_presets.lua from JSON")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--hunt", metavar="ITEM_ID", help="Expected cost to hunt a Mystery Wheel item")
    parser.add_argument(
        "--machine",
        action="append",
        dest="machines",
        help="Machine id for --hunt (repeatable). Default: all three advanced machines",
    )
    parser.add_argument("--hunt-trials", type=int, default=1500, help="MC hunt trials per machine (0=exact only)")
    parser.add_argument("--list-items", action="store_true", help="List Mystery Wheel item ids")
    parser.add_argument("--skip-rtp", action="store_true", help="Skip full RTP report (useful with --hunt)")
    parser.add_argument(
        "--refresh-presets",
        action="store_true",
        help="Recompute every preset's published math/hunt block from its settings",
    )
    parser.add_argument(
        "--retune-advanced",
        action="store_true",
        help="Solve advanced-machine cash p for target_rtp, then refresh presets",
    )
    args = parser.parse_args()

    if args.list_items:
        list_mystery_items()
        return

    if args.refresh_presets or args.retune_advanced:
        refresh_presets(retune=args.retune_advanced)

    if args.emit_lua:
        emit_lua(args.emit_lua)

    if args.hunt:
        print_hunt_report(args.hunt, args.machines, args.hunt_trials)
        if args.skip_rtp and not args.json_out:
            return

    if args.skip_rtp and not args.json_out and not args.hunt:
        return

    if not args.skip_rtp or args.json_out:
        report = analyze_all(
            mc_spins=args.spins,
            session_spins=args.session_spins,
            session_trials=args.session_trials,
        )
        if args.json_out:
            args.json_out.write_text(json.dumps(report, indent=2))
            print(f"Wrote {args.json_out}")
        if not args.quiet and not args.skip_rtp:
            print_report(report)


if __name__ == "__main__":
    main()
