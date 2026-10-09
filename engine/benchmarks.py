"""Static coaching data: rank benchmarks, mechanics advice, drills, agent classes.

Everything here is plain data (no I/O, no Discord) so you can tune it without
touching the analysis logic.

IMPORTANT: the numeric targets are *climbing targets* - rough community-style
averages nudged slightly upward - not official Riot figures. Treat them as a
starting point and adjust them once you know your own baseline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------------- #
# Thresholds used by the analyzer
# --------------------------------------------------------------------------- #
GREAT_RATIO = 1.10  # value / target >= this  -> "great"
LOW_RATIO = 0.85    # value / target <  this  -> "low" (flagged as a problem)

MIN_HITS_FOR_HS = 40       # below this many hits, HS% is a small-sample guess
MIN_FIRST_DUELS = 4        # opening duels needed before judging win rate
MIN_TEAM_FK_ROUNDS = 5     # rounds where your team got first kill, for conversion

# --------------------------------------------------------------------------- #
# Rank benchmarks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RankBenchmark:
    """Target stats for a rank, plus which coaching group it belongs to."""

    name: str
    group: str                         # key into COACHING_GROUPS
    hs_pct: float                      # headshot % of all hits
    adr: float                         # average damage per round
    kd: float                          # kills / deaths
    first_duel_win_pct: float = 50.0   # FB / (FB + FD)
    conversion_pct: float = 70.0       # round win % when your team gets first kill


RANK_BENCHMARKS: dict[str, RankBenchmark] = {
    "Iron":      RankBenchmark("Iron",      "iron_bronze",        hs_pct=15, adr=125, kd=0.95, conversion_pct=66),
    "Bronze":    RankBenchmark("Bronze",    "iron_bronze",        hs_pct=17, adr=130, kd=1.00, conversion_pct=67),
    "Silver":    RankBenchmark("Silver",    "silver_gold",        hs_pct=19, adr=135, kd=1.00, conversion_pct=68),
    "Gold":      RankBenchmark("Gold",      "silver_gold",        hs_pct=20, adr=140, kd=1.00, conversion_pct=69),
    "Platinum":  RankBenchmark("Platinum",  "platinum_diamond",   hs_pct=22, adr=145, kd=1.00, conversion_pct=70),
    "Diamond":   RankBenchmark("Diamond",   "platinum_diamond",   hs_pct=24, adr=150, kd=1.05, conversion_pct=72),
    "Ascendant": RankBenchmark("Ascendant", "ascendant_immortal", hs_pct=25, adr=155, kd=1.05, conversion_pct=73),
    "Immortal":  RankBenchmark("Immortal",  "ascendant_immortal", hs_pct=27, adr=160, kd=1.10, conversion_pct=75),
}

DEFAULT_RANK = "Gold"  # used when the API reports no usable rank (e.g. unrated)

# currenttier IDs from the API: 0 = Unrated, 3-5 Iron 1-3, 6-8 Bronze ... 27 = Radiant.
_TIER_RANGES: tuple[tuple[int, int, str], ...] = (
    (3, 5, "Iron"),
    (6, 8, "Bronze"),
    (9, 11, "Silver"),
    (12, 14, "Gold"),
    (15, 17, "Platinum"),
    (18, 20, "Diamond"),
    (21, 23, "Ascendant"),
    (24, 27, "Immortal"),  # Radiant (27) is benchmarked as Immortal
)


def tier_to_rank_name(tier_id: Optional[int]) -> Optional[str]:
    """Map an API ``currenttier`` to a key of RANK_BENCHMARKS (None if unrated/unknown)."""
    if tier_id is None:
        return None
    for low, high, name in _TIER_RANGES:
        if low <= tier_id <= high:
            return name
    return None


# --------------------------------------------------------------------------- #
# Coaching groups: mechanical focus + drills, scaled by rank
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Drill:
    name: str
    how: str
    fixes: tuple[str, ...]  # metric keys this drill helps: hs_pct, adr, kd, first_duel, conversion


@dataclass(frozen=True)
class CoachingGroup:
    title: str
    focus: tuple[str, ...]
    drills: tuple[Drill, ...]


COACHING_GROUPS: dict[str, CoachingGroup] = {
    "iron_bronze": CoachingGroup(
        title="Iron / Bronze - Fundamentals",
        focus=(
            "**Crosshair placement:** keep it at head height and pre-aim where an enemy will appear, not at the floor or wall.",
            "**Clear corners one angle at a time** (slice the pie) instead of wide-swinging into several angles at once.",
            "**Unbind crouch** (or move it off your aim hand): stand still, then shoot.",
        ),
        drills=(
            Drill(
                "Head-height corridor walk",
                "In the Range or a Custom game, walk each corridor with your crosshair parked at head height and pre-aim the next corner before you reach it. Fire only when a head is on the crosshair. 5-10 min per session.",
                ("hs_pct", "kd"),
            ),
            Drill(
                "Slice-the-pie corner clears",
                "Pick a corner and expose one angle at a time with small strafe-outs, stopping on each new angle. 10 reps per corner, then repeat on a different map spot. Never wide-swing a corner you haven't cleared.",
                ("first_duel", "kd"),
            ),
            Drill(
                "Crouch-unbind week",
                "Unbind crouch for a week. In Deathmatch, make every first bullet a stand-still shot. Track your HS% afterwards.",
                ("hs_pct",),
            ),
            Drill(
                "Buddy-swing damage goal",
                "Each round, call one swing with a teammate and play for damage, not just kills. Goal: deal damage in every round you survive the first contact.",
                ("adr", "conversion"),
            ),
        ),
    ),
    "silver_gold": CoachingGroup(
        title="Silver / Gold - Movement & Discipline",
        focus=(
            "**Counter-strafing:** tap the opposite movement key so you are fully stopped when the first bullet leaves.",
            "**Dead-zoning:** never fire while still moving; your first shot is the one that decides duels.",
            "**Burst firing:** 1 bullet up close-to-mid range, 2-3 bullet bursts at range; don't hold the trigger.",
            "**Trade-fragging:** stay one second behind a teammate so every death is traded.",
        ),
        drills=(
            Drill(
                "Counter-strafe ladder",
                "Range, Vandal and Phantom: strafe, counter-tap, fire a single bullet at a bot's head. 50 reps per weapon, aiming for 40%+ first-bullet headshots.",
                ("hs_pct", "kd"),
            ),
            Drill(
                "Burst distance ladder",
                "Bots at close, mid and long range: tap at close, 2-bullet bursts at mid, 3-bullet bursts at long. Reset when you catch yourself spraying.",
                ("hs_pct", "adr"),
            ),
            Drill(
                "Trade-timing review",
                "Pick three rounds from your last match. For each death, mark whether a teammate could have swung within one second. Next match, be that teammate.",
                ("first_duel", "adr", "conversion"),
            ),
            Drill(
                "Stop-and-shoot Deathmatch",
                "In Deathmatch, only fire when your model is stationary. Ignore the score; the goal is a clean first shot every duel.",
                ("hs_pct", "kd"),
            ),
        ),
    ),
    "platinum_diamond": CoachingGroup(
        title="Platinum / Diamond - Information & Spacing",
        focus=(
            "**Peeker's advantage:** the peeker sees the holder slightly first. Use wide peeks with a plan, and jiggle or shoulder-peek to bait when holding.",
            "**Spacing:** stay close enough to trade, but never stacked on the same angle.",
            "**Utility timing:** flash, recon or smoke *before* contact; no dry-peeking.",
        ),
        drills=(
            Drill(
                "Jiggle and shoulder-peek reps",
                "With a friend holding an angle in a Custom game, practise jiggle-peek baits and wide peeks, 10 minutes each side. Log how often you win the first shot.",
                ("first_duel", "kd"),
            ),
            Drill(
                "No-dry-peek rule",
                "For a full session, never take an entry fight without a flash, recon, smoke or teammate swing first. Review your deaths and aim for zero dry-peek deaths.",
                ("first_duel", "kd", "conversion"),
            ),
            Drill(
                "Spacing audit",
                "In VOD review, pause at each death and check teammate positions. Was anyone within a one-second trade? Note the pattern.",
                ("adr", "kd", "conversion"),
            ),
            Drill(
                "First-bullet range test",
                "Hard bots at set distances: log first-bullet headshot rate at each. Don't leave until you beat last session's best.",
                ("hs_pct",),
            ),
        ),
    ),
    "ascendant_immortal": CoachingGroup(
        title="Ascendant / Immortal - Isolation & Conversion",
        focus=(
            "**Isolate 1v1 duels:** use utility and teammates so only one enemy can see you.",
            "**Late-round trade setups:** staggered crossfires and fast trades in 3v3/2v2.",
            "**Conversion:** turn first bloods into round wins by trading fast and avoiding over-extension.",
        ),
        drills=(
            Drill(
                "Isolation VOD tagging",
                "Review every death and tag it isolated (one enemy had line of sight) or unisolated (two or more did). Aim to cut unisolated deaths each week.",
                ("kd", "first_duel"),
            ),
            Drill(
                "Late-round crossfire scrim",
                "In Custom 3v3 and 2v2 post-plant and retake setups, rehearse staggered crossfires and trade timing until the swing timing is automatic.",
                ("conversion", "adr"),
            ),
            Drill(
                "First-blood conversion review",
                "For each round where your team got first blood but lost, write the cause: over-extend, no trade, or utility misplay. Fix the most common cause first.",
                ("conversion", "first_duel"),
            ),
            Drill(
                "Aim maintenance block",
                "15 minutes before queueing: 5 min Range tracking, 5 min flick and first-bullet headshots, 5 min Deathmatch. Consistency matters more than volume.",
                ("hs_pct",),
            ),
        ),
    ),
}

# --------------------------------------------------------------------------- #
# Diagnostic copy (why is this metric low?)
# --------------------------------------------------------------------------- #
METRIC_LABELS: dict[str, str] = {
    "hs_pct": "Headshot %",
    "adr": "ADR",
    "kd": "K/D",
    "first_duel": "Opening duel win %",
    "conversion": "First-kill conversion",
}

HS_DIAGNOSIS: dict[str, str] = {
    "iron_bronze": "Usually crouch-spraying and a crosshair resting at chest or floor level around corners. Keep it at head height, pre-aim the spot an enemy will appear, and stand still for the first shot.",
    "silver_gold": "Likely shooting while still moving (no counter-strafe) and holding the trigger past the accurate window. Stop fully before firing and burst at range.",
    "platinum_diamond": "Often caused by dry-peeking or wide-swinging into unchecked angles. Pre-aim the angle, time utility first, and let enemies come to your crosshair when holding.",
    "ascendant_immortal": "Usually first-bullet inconsistency in isolated duels or late re-clears of off-angles. Audit your pre-aims on common off-angles and prefer one-tap first bullets over spray transfers.",
}

FIRST_DUEL_DIAGNOSIS: dict[str, str] = {
    "iron_bronze": "Opening duels are going against you. Clear one angle at a time instead of wide-swinging, and don't shoot while crouched or moving.",
    "silver_gold": "Opening duels are going against you. Only take them when a teammate can trade, and counter-strafe before firing.",
    "platinum_diamond": "Opening duels are going against you. Check for dry-peeks: lead with a flash, recon or smoke, and use jiggle or shoulder peeks to bait when holding.",
    "ascendant_immortal": "Opening duels are going against you. Make sure only one enemy can see you before you commit, and have a teammate staggered for the trade.",
}

ADR_PASSIVE = "Low damage and low kills together point to passive play: you're not creating contact. Join the first swing with a teammate and play for damage early in the round."
ADR_EXIT_FRAGS = "Your kills are fine but your damage is low, which suggests exit frags or finishing kills others set up. Look for earlier damage and trade proactively instead of arriving late."
KD_EARLY_DEATHS = "A large share of your deaths are first deaths. Stop taking opening fights alone: wait for utility or a teammate to swing with you."
KD_LATE_DEATHS = "Most deaths happen mid or late round, which suggests isolated fights or no trade. Check your positioning and stay within trade distance."
CONVERSION_LOW = "Your team is losing too many rounds after getting first blood. Trade quickly, take space together after the opener, avoid over-peeking, and rehearse post-plant and late-round setups."

# --------------------------------------------------------------------------- #
# Playstyle archetypes
# --------------------------------------------------------------------------- #
ARCHETYPES: dict[str, str] = {
    "Entry Fragger": "You seek and win first contact. High-tempo and aim-driven.",
    "Over-extending Entry": "You take lots of first contacts but lose too many of them. Great initiative, rough execution.",
    "Precision Anchor": "You play fewer duels and win them with accurate shots. Patient and survival-focused.",
    "Utility Support": "You contribute through assists and team plays rather than raw frags.",
    "Damage Dealer": "You deal lots of damage but convert fewer shots into headshots. Volume over precision.",
    "Passive Survivor": "You avoid contact and deal little damage. Safe, but it costs your team numbers.",
    "Balanced Slayer": "Well-rounded fragger: solid K/D without a single standout trait.",
    "Developing Player": "No clear identity yet. Focus on fundamentals before specialising.",
}

# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


def normalize_agent_name(name: str) -> str:
    """'KAY/O' -> 'kayo', 'Gekko' -> 'gekko'."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Add new agents here as Riot releases them; unknown agents are handled gracefully.
AGENT_CLASSES: dict[str, str] = {
    **{n: "Duelist" for n in ("jett", "phoenix", "raze", "reyna", "yoru", "neon", "iso", "waylay")},
    **{n: "Controller" for n in ("brimstone", "omen", "viper", "astra", "harbor", "clove")},
    **{n: "Initiator" for n in ("sova", "breach", "skye", "kayo", "fade", "gekko", "tejo")},
    **{n: "Sentinel" for n in ("sage", "cypher", "killjoy", "chamber", "deadlock", "vyse")},
}

CLASS_BLURBS: dict[str, str] = {
    "Duelist": "creating space and winning opening duels",
    "Controller": "controlling sight lines with smokes and map control",
    "Initiator": "gathering information and setting teammates up with flashes, recon and stuns",
    "Sentinel": "anchoring sites and watching flanks with patience",
}

# Suggested picks per class and coaching group (patch-dependent; edit freely).
CLASS_PICKS: dict[str, dict[str, tuple[str, ...]]] = {
    "Duelist": {
        "iron_bronze": ("Phoenix", "Reyna", "Jett"),
        "silver_gold": ("Reyna", "Raze", "Jett"),
        "platinum_diamond": ("Jett", "Neon", "Raze"),
        "ascendant_immortal": ("Jett", "Neon", "Waylay"),
    },
    "Initiator": {
        "iron_bronze": ("Skye", "Sova", "KAY/O"),
        "silver_gold": ("Sova", "Skye", "Gekko"),
        "platinum_diamond": ("Fade", "Sova", "Gekko"),
        "ascendant_immortal": ("Fade", "Sova", "Breach"),
    },
    "Controller": {
        "iron_bronze": ("Brimstone", "Omen", "Viper"),
        "silver_gold": ("Omen", "Brimstone", "Clove"),
        "platinum_diamond": ("Omen", "Viper", "Clove"),
        "ascendant_immortal": ("Viper", "Astra", "Harbor"),
    },
    "Sentinel": {
        "iron_bronze": ("Sage", "Killjoy", "Cypher"),
        "silver_gold": ("Killjoy", "Cypher", "Sage"),
        "platinum_diamond": ("Cypher", "Killjoy", "Chamber"),
        "ascendant_immortal": ("Cypher", "Killjoy", "Vyse"),
    },
}
