"""Pure analytical functions: payload -> stats -> metrics -> coaching.

Nothing in this module performs I/O, touches Discord, or keeps state, so every
function can be unit-tested with a plain dict.

Pipeline
--------
1. ``extract_player_stats``  - pull your numbers out of a HenrikDev v3 match.
2. ``evaluate_metrics``      - compare them with your rank's benchmark.
3. ``build_findings``        - turn weak/strong metrics into diagnoses.
4. ``build_playstyle`` and ``evaluate_agent_fit`` - what kind of player you are
   and whether your agent suits that.
5. ``select_drills``         - rank-appropriate drills for your weak spots.
6. ``analyze_match``         - runs 2-5 and returns one ``MatchAnalysis``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

from engine import benchmarks as bm

# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlayerMatchStats:
    """Your raw numbers for one match (or a sum over several - see ``matches``)."""

    match_id: str
    map_name: str
    mode: str
    game_start: Optional[int]  # epoch seconds
    agent: str
    agent_icon_url: Optional[str]
    tier_id: Optional[int]
    tier_name: str
    result: str  # "Win" | "Loss" | "Draw"
    rounds_played: int
    rounds_won: int
    rounds_lost: int
    kills: int
    deaths: int
    assists: int
    score: int
    headshots: int
    bodyshots: int
    legshots: int
    damage_made: int
    first_bloods: int = 0
    first_deaths: int = 0
    first_engagement_available: bool = False
    team_fk_rounds: int = 0       # rounds where YOUR TEAM got the first kill
    team_fk_round_wins: int = 0   # ...and went on to win
    matches: int = 1

    # -- derived metrics ------------------------------------------------------ #
    @property
    def total_hits(self) -> int:
        return self.headshots + self.bodyshots + self.legshots

    @property
    def hs_pct(self) -> float:
        """Headshots / total hits * 100."""
        return self.headshots / self.total_hits * 100 if self.total_hits else 0.0

    @property
    def adr(self) -> float:
        """Total damage / rounds played."""
        return self.damage_made / self.rounds_played if self.rounds_played else 0.0

    @property
    def acs(self) -> float:
        return self.score / self.rounds_played if self.rounds_played else 0.0

    @property
    def kd(self) -> float:
        """Kills / deaths. With zero deaths the ratio is reported as the kill count."""
        return self.kills / self.deaths if self.deaths else float(self.kills)

    @property
    def apr(self) -> float:
        """Assists per round."""
        return self.assists / self.rounds_played if self.rounds_played else 0.0

    @property
    def dpr(self) -> float:
        """Deaths per round."""
        return self.deaths / self.rounds_played if self.rounds_played else 0.0

    @property
    def first_duels(self) -> int:
        return self.first_bloods + self.first_deaths

    @property
    def first_blood_diff(self) -> int:
        return self.first_bloods - self.first_deaths

    @property
    def first_duel_win_pct(self) -> Optional[float]:
        """First bloods / (first bloods + first deaths) * 100; None if no data."""
        if not self.first_engagement_available or self.first_duels == 0:
            return None
        return self.first_bloods / self.first_duels * 100

    @property
    def first_contact_rate(self) -> Optional[float]:
        """Share of rounds in which you were part of the opening kill."""
        if not self.first_engagement_available or not self.rounds_played:
            return None
        return self.first_duels / self.rounds_played

    @property
    def conversion_pct(self) -> Optional[float]:
        """Round win % in rounds where your team got the first kill."""
        if not self.team_fk_rounds:
            return None
        return self.team_fk_round_wins / self.team_fk_rounds * 100


@dataclass(frozen=True)
class MetricResult:
    key: str
    label: str
    value: Optional[float]
    target: float
    unit: str = ""
    decimals: int = 1
    ratio: Optional[float] = None
    status: str = "na"  # "great" | "ok" | "close" | "low" | "na"
    note: str = ""


@dataclass(frozen=True)
class Finding:
    key: str
    severity: str  # "issue" | "positive" | "info"
    title: str
    detail: str


@dataclass(frozen=True)
class PlaystyleProfile:
    archetype: str
    archetype_blurb: str
    class_scores: dict[str, float]
    best_class: str
    traits: tuple[str, ...]
    sample_matches: int


@dataclass(frozen=True)
class AgentFit:
    agent: str
    agent_class: Optional[str]
    level: str  # "strong" | "good" | "partial" | "mismatch" | "unknown"
    summary: str
    suggested_class: str
    suggested_agents: tuple[str, ...]


@dataclass(frozen=True)
class MatchAnalysis:
    stats: PlayerMatchStats
    rank: bm.RankBenchmark
    rank_assumed: bool
    metrics: list[MetricResult]
    findings: list[Finding]
    playstyle: PlaystyleProfile
    agent_fit: AgentFit
    drills: list[bm.Drill]
    group: bm.CoachingGroup
    sample_matches: int = 1


# --------------------------------------------------------------------------- #
# 1. Extraction
# --------------------------------------------------------------------------- #


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_player(
    players: Sequence[dict[str, Any]], puuid: Optional[str], name: Optional[str], tag: Optional[str]
) -> Optional[dict[str, Any]]:
    if puuid:
        for p in players:
            if p.get("puuid") == puuid:
                return p
    if name and tag:
        name_l, tag_l = name.strip().lower(), tag.strip().lower()
        for p in players:
            if str(p.get("name", "")).strip().lower() == name_l and str(p.get("tag", "")).strip().lower() == tag_l:
                return p
    return None


def _first_kills_by_round(match: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Map round index -> the first kill event of that round."""
    kills = match.get("kills")
    if not kills:  # fall back to per-round kill events
        kills = []
        for idx, rnd in enumerate(match.get("rounds") or []):
            for ps in (rnd or {}).get("player_stats") or []:
                for event in (ps or {}).get("kill_events") or []:
                    kills.append({**event, "round": idx})

    first: dict[int, tuple[float, dict[str, Any]]] = {}
    for kill in kills:
        rnd = kill.get("round")
        if not isinstance(rnd, int):
            continue
        t = kill.get("kill_time_in_round")
        t = float(t) if isinstance(t, (int, float)) else math.inf
        if rnd not in first or t < first[rnd][0]:
            first[rnd] = (t, kill)
    return {rnd: kill for rnd, (_, kill) in first.items()}


def extract_player_stats(
    match: dict[str, Any],
    *,
    puuid: Optional[str] = None,
    name: Optional[str] = None,
    tag: Optional[str] = None,
) -> Optional[PlayerMatchStats]:
    """Build ``PlayerMatchStats`` for the tracked player, or None if not in the match."""
    metadata = match.get("metadata") or {}
    players = (match.get("players") or {}).get("all_players") or []
    me = _find_player(players, puuid, name, tag)
    if me is None:
        return None

    stats = me.get("stats") or {}
    my_puuid = me.get("puuid")
    team = str(me.get("team") or "").lower()
    rounds = match.get("rounds") or []
    rounds_played = _int(metadata.get("rounds_played")) or len(rounds)

    team_info = (match.get("teams") or {}).get(team) or {}
    rounds_won = _int(team_info.get("rounds_won"))
    rounds_lost = _int(team_info.get("rounds_lost"))
    if rounds_won > rounds_lost:
        result = "Win"
    elif rounds_won < rounds_lost:
        result = "Loss"
    elif team_info.get("has_won") is True:
        result = "Win"
    elif rounds_won == rounds_lost and rounds_won > 0:
        result = "Draw"
    else:
        result = "Win" if team_info.get("has_won") else "Loss"

    # First blood / first death, plus how often your team converts a first kill.
    firsts = _first_kills_by_round(match)
    fb = fd = team_fk = team_fk_wins = 0
    for rnd_idx, kill in firsts.items():
        if my_puuid and kill.get("killer_puuid") == my_puuid:
            fb += 1
        if my_puuid and kill.get("victim_puuid") == my_puuid:
            fd += 1
        if str(kill.get("killer_team") or "").lower() == team:
            team_fk += 1
            if 0 <= rnd_idx < len(rounds) and str((rounds[rnd_idx] or {}).get("winning_team") or "").lower() == team:
                team_fk_wins += 1

    agent_assets = (me.get("assets") or {}).get("agent") or {}
    return PlayerMatchStats(
        match_id=str(metadata.get("matchid") or ""),
        map_name=str(metadata.get("map") or "Unknown map"),
        mode=str(metadata.get("mode") or metadata.get("mode_id") or "Unknown"),
        game_start=_int(metadata.get("game_start")) or None,
        agent=str(me.get("character") or "Unknown"),
        agent_icon_url=agent_assets.get("small") or agent_assets.get("bust"),
        tier_id=me.get("currenttier") if isinstance(me.get("currenttier"), int) else None,
        tier_name=str(me.get("currenttier_patched") or "Unrated"),
        result=result,
        rounds_played=rounds_played,
        rounds_won=rounds_won,
        rounds_lost=rounds_lost,
        kills=_int(stats.get("kills")),
        deaths=_int(stats.get("deaths")),
        assists=_int(stats.get("assists")),
        score=_int(stats.get("score")),
        headshots=_int(stats.get("headshots")),
        bodyshots=_int(stats.get("bodyshots")),
        legshots=_int(stats.get("legshots")),
        damage_made=_int(me.get("damage_made")),
        first_bloods=fb,
        first_deaths=fd,
        first_engagement_available=bool(firsts),
        team_fk_rounds=team_fk,
        team_fk_round_wins=team_fk_wins,
    )


def aggregate_stats(items: Sequence[PlayerMatchStats]) -> PlayerMatchStats:
    """Sum several matches into one pseudo-match (ratios stay correctly weighted)."""
    if not items:
        raise ValueError("aggregate_stats() needs at least one match")
    if len(items) == 1:
        return items[0]

    last = items[-1]
    most_played = Counter(i.agent for i in items).most_common(1)[0][0]
    return replace(
        last,
        match_id=f"aggregate:{len(items)}",
        agent=most_played,
        rounds_played=sum(i.rounds_played for i in items),
        rounds_won=sum(i.rounds_won for i in items),
        rounds_lost=sum(i.rounds_lost for i in items),
        kills=sum(i.kills for i in items),
        deaths=sum(i.deaths for i in items),
        assists=sum(i.assists for i in items),
        score=sum(i.score for i in items),
        headshots=sum(i.headshots for i in items),
        bodyshots=sum(i.bodyshots for i in items),
        legshots=sum(i.legshots for i in items),
        damage_made=sum(i.damage_made for i in items),
        first_bloods=sum(i.first_bloods for i in items),
        first_deaths=sum(i.first_deaths for i in items),
        first_engagement_available=any(i.first_engagement_available for i in items),
        team_fk_rounds=sum(i.team_fk_rounds for i in items),
        team_fk_round_wins=sum(i.team_fk_round_wins for i in items),
        matches=sum(i.matches for i in items),
    )


# --------------------------------------------------------------------------- #
# 2. Metrics vs benchmarks
# --------------------------------------------------------------------------- #


def resolve_rank(tier_id: Optional[int]) -> tuple[bm.RankBenchmark, bool]:
    """Return (benchmark, assumed). ``assumed`` is True when the rank was unavailable."""
    name = bm.tier_to_rank_name(tier_id)
    if name is None:
        return bm.RANK_BENCHMARKS[bm.DEFAULT_RANK], True
    return bm.RANK_BENCHMARKS[name], False


def _metric(
    key: str,
    value: Optional[float],
    target: float,
    *,
    unit: str = "",
    decimals: int = 1,
    note: str = "",
) -> MetricResult:
    label = bm.METRIC_LABELS[key]
    if value is None:
        return MetricResult(key, label, None, target, unit, decimals, None, "na", note)
    ratio = value / target if target else 0.0
    if ratio >= bm.GREAT_RATIO:
        status = "great"
    elif ratio >= 1.0:
        status = "ok"
    elif ratio >= bm.LOW_RATIO:
        status = "close"
    else:
        status = "low"
    return MetricResult(key, label, value, target, unit, decimals, ratio, status, note)


def evaluate_metrics(stats: PlayerMatchStats, rank: bm.RankBenchmark) -> list[MetricResult]:
    """Score each metric against the rank's target."""
    hs_note = ""
    if 0 < stats.total_hits < bm.MIN_HITS_FOR_HS:
        hs_note = f"small sample ({stats.total_hits} hits)"

    fd_value = stats.first_duel_win_pct if stats.first_duels >= bm.MIN_FIRST_DUELS else None
    fd_note = "" if fd_value is not None else f"only {stats.first_duels} opening duel(s)"

    conv_value = stats.conversion_pct if stats.team_fk_rounds >= bm.MIN_TEAM_FK_ROUNDS else None
    conv_note = "" if conv_value is not None else f"only {stats.team_fk_rounds} team first-kill round(s)"

    return [
        _metric("hs_pct", stats.hs_pct if stats.total_hits else None, rank.hs_pct, unit="%", note=hs_note),
        _metric("adr", stats.adr if stats.rounds_played else None, rank.adr, decimals=0),
        _metric("kd", stats.kd if stats.kills or stats.deaths else None, rank.kd, decimals=2),
        _metric("first_duel", fd_value, rank.first_duel_win_pct, unit="%", note=fd_note),
        _metric("conversion", conv_value, rank.conversion_pct, unit="%", note=conv_note),
    ]


# --------------------------------------------------------------------------- #
# 3. Diagnoses
# --------------------------------------------------------------------------- #


def build_findings(stats: PlayerMatchStats, metrics: Sequence[MetricResult], rank: bm.RankBenchmark) -> list[Finding]:
    """Explain *why* weak metrics are weak and call out what went well."""
    by_key = {m.key: m for m in metrics}
    group = rank.group
    findings: list[Finding] = []

    def fmt(m: MetricResult) -> str:
        return f"{m.value:.{m.decimals}f}{m.unit} vs {m.target:.{m.decimals}f}{m.unit} target"

    m = by_key["hs_pct"]
    if m.status == "low":
        detail = bm.HS_DIAGNOSIS[group]
        if m.note:
            detail += f" ({m.note}, so treat this as a hint, not a verdict.)"
        findings.append(Finding("hs_pct", "issue", f"Headshot % is low ({fmt(m)})", detail))

    m = by_key["adr"]
    if m.status == "low":
        kd_ok = by_key["kd"].ratio is not None and by_key["kd"].ratio >= 0.95
        findings.append(
            Finding("adr", "issue", f"ADR is low ({fmt(m)})", bm.ADR_EXIT_FRAGS if kd_ok else bm.ADR_PASSIVE)
        )

    m = by_key["kd"]
    if m.status == "low":
        early_share = stats.first_deaths / stats.deaths if stats.deaths and stats.first_engagement_available else 0.0
        findings.append(
            Finding(
                "kd",
                "issue",
                f"K/D is low ({fmt(m)})",
                bm.KD_EARLY_DEATHS if early_share >= 0.30 else bm.KD_LATE_DEATHS,
            )
        )

    m = by_key["first_duel"]
    if m.status == "low":
        findings.append(
            Finding("first_duel", "issue", f"Losing opening duels ({stats.first_bloods} FB vs {stats.first_deaths} FD)", bm.FIRST_DUEL_DIAGNOSIS[group])
        )

    m = by_key["conversion"]
    if m.status == "low":
        findings.append(Finding("conversion", "issue", f"First-kill conversion is low ({fmt(m)})", bm.CONVERSION_LOW))

    # One combined line of strengths so the report isn't only criticism.
    strengths = sorted((x for x in metrics if x.status == "great" and x.ratio is not None), key=lambda x: -x.ratio)[:3]
    if strengths:
        listed = ", ".join(f"{s.label} {s.value:.{s.decimals}f}{s.unit} (target {s.target:.{s.decimals}f}{s.unit})" for s in strengths)
        findings.append(Finding("strengths", "positive", "What went well", f"Well above target: {listed}. Keep leaning on these."))

    if not findings:
        findings.append(Finding("overall", "info", "No major red flags", "Your numbers are in range for your rank. Keep stacking consistent games."))
    return findings


# --------------------------------------------------------------------------- #
# 4. Playstyle and agent fit
# --------------------------------------------------------------------------- #


def _norm(x: float, lo: float, hi: float) -> float:
    """Linearly map x from [lo, hi] onto [0, 1], clamped. Works when lo > hi (inverse)."""
    if lo == hi:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def build_playstyle(stats: PlayerMatchStats, rank: bm.RankBenchmark) -> PlaystyleProfile:
    """Classify the player's style from their (ideally multi-match) stat profile."""
    hs_ratio = stats.hs_pct / rank.hs_pct if stats.total_hits else 1.0
    adr_ratio = stats.adr / rank.adr if rank.adr else 1.0
    kd_ratio = stats.kd / rank.kd if rank.kd else 1.0
    fcr = stats.first_contact_rate        # None when telemetry is missing
    fdw = stats.first_duel_win_pct        # percent, or None
    apr, dpr = stats.apr, stats.dpr
    fcr_n = fcr if fcr is not None else 0.15  # neutral assumption

    # How well the stat profile matches each agent class (0..1). Heuristic weights.
    scores = {
        "Duelist": 0.35 * _norm(fcr_n, 0.10, 0.30)
        + 0.25 * _norm((fdw if fdw is not None else 50.0) / 100, 0.40, 0.60)
        + 0.25 * _norm(hs_ratio, 0.8, 1.2)
        + 0.15 * _norm(kd_ratio, 0.8, 1.2),
        "Initiator": 0.40 * _norm(apr, 0.15, 0.40)
        + 0.25 * _norm(adr_ratio, 0.85, 1.10)
        + 0.20 * (1.0 - abs(_norm(fcr_n, 0.08, 0.28) - 0.5) * 2)  # mid first-contact is ideal
        + 0.15 * _norm(kd_ratio, 0.8, 1.1),
        "Controller": 0.35 * _norm(fcr_n, 0.20, 0.08)
        + 0.25 * _norm(apr, 0.15, 0.35)
        + 0.25 * _norm(adr_ratio, 0.85, 1.05)
        + 0.15 * _norm(dpr, 0.80, 0.55),
        "Sentinel": 0.35 * _norm(fcr_n, 0.18, 0.06)
        + 0.30 * _norm(kd_ratio, 0.9, 1.2)
        + 0.20 * _norm(hs_ratio, 0.9, 1.2)
        + 0.15 * _norm(dpr, 0.80, 0.55),
    }
    best_class = max(scores, key=scores.get)

    # Archetype: first matching rule wins.
    if fcr is not None and fcr >= 0.22 and (fdw or 0) >= 50:
        archetype = "Entry Fragger"
    elif fcr is not None and fcr >= 0.22:
        archetype = "Over-extending Entry"
    elif fcr is not None and fcr < 0.14 and hs_ratio >= 1.0 and kd_ratio >= 1.0:
        archetype = "Precision Anchor"
    elif apr >= 0.30 and adr_ratio < 1.05:
        archetype = "Utility Support"
    elif adr_ratio >= 1.0 and hs_ratio < 0.85:
        archetype = "Damage Dealer"
    elif fcr is not None and fcr < 0.12 and adr_ratio < 0.9:
        archetype = "Passive Survivor"
    elif kd_ratio >= 1.0:
        archetype = "Balanced Slayer"
    else:
        archetype = "Developing Player"

    traits = [
        f"HS {stats.hs_pct:.0f}% (target {rank.hs_pct:.0f}%)",
        f"ADR {stats.adr:.0f} (target {rank.adr:.0f})",
        f"{apr:.2f} assists/round",
    ]
    if fcr is not None:
        traits.insert(0, f"In the opening duel {fcr * 100:.0f}% of rounds")

    return PlaystyleProfile(
        archetype=archetype,
        archetype_blurb=bm.ARCHETYPES[archetype],
        class_scores=scores,
        best_class=best_class,
        traits=tuple(traits),
        sample_matches=stats.matches,
    )


def evaluate_agent_fit(agent: str, profile: PlaystyleProfile, rank: bm.RankBenchmark) -> AgentFit:
    """Compare the agent you played with the class your stats suggest."""
    agent_class = bm.AGENT_CLASSES.get(bm.normalize_agent_name(agent))
    best = profile.best_class
    picks = bm.CLASS_PICKS[best][rank.group]
    suggested = tuple(p for p in picks if bm.normalize_agent_name(p) != bm.normalize_agent_name(agent))[:3]
    n = profile.sample_matches
    provisional = f" (provisional: based on only {n} match{'es' if n != 1 else ''})" if n < 3 else ""

    if agent_class is None:
        return AgentFit(
            agent, None, "unknown",
            f"{agent} isn't in the agent database yet (add it to AGENT_CLASSES). Your stats read as **{profile.archetype}**, which suits **{best}**s{provisional}.",
            best, suggested,
        )

    gap = profile.class_scores[best] - profile.class_scores[agent_class]
    if agent_class == best:
        level = "strong"
        text = f"**{agent}** ({agent_class}) matches your profile. Your stats read as **{profile.archetype}**, and the {agent_class} role is built around {bm.CLASS_BLURBS[agent_class]}. Keep it."
    elif gap <= 0.08:
        level = "good"
        text = f"**{agent}** ({agent_class}) is a good fit; your stats lean only slightly toward **{best}**."
    elif gap <= 0.20:
        level = "partial"
        text = f"**{agent}** ({agent_class}) is a partial fit. Your stats read as **{profile.archetype}**, which suits **{best}** better."
    else:
        level = "mismatch"
        text = f"**{agent}** ({agent_class}) doesn't match how you play. Your **{profile.archetype}** profile suits **{best}**, a role built around {bm.CLASS_BLURBS[best]}."
    return AgentFit(agent, agent_class, level, text + provisional, best, suggested)


# --------------------------------------------------------------------------- #
# 5. Drills
# --------------------------------------------------------------------------- #


def select_drills(
    group_key: str,
    match_metrics: Sequence[MetricResult],
    trend_metrics: Sequence[MetricResult] = (),
    limit: int = 3,
) -> list[bm.Drill]:
    """Pick drills for the weakest metrics (this match first, then recent trend)."""

    def lows(metrics: Sequence[MetricResult]) -> list[str]:
        ranked = sorted((m for m in metrics if m.status == "low" and m.ratio is not None), key=lambda m: m.ratio)
        return [m.key for m in ranked]

    weak_keys = lows(match_metrics) + [k for k in lows(trend_metrics) if k not in lows(match_metrics)]
    all_drills = bm.COACHING_GROUPS[group_key].drills

    chosen: list[bm.Drill] = []
    for key in weak_keys:
        for drill in all_drills:
            if key in drill.fixes and drill not in chosen:
                chosen.append(drill)
                break
        if len(chosen) >= limit:
            return chosen
    for drill in all_drills:  # top up with the group's defaults
        if drill not in chosen:
            chosen.append(drill)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


# --------------------------------------------------------------------------- #
# 6. Orchestration
# --------------------------------------------------------------------------- #


def analyze_match(stats: PlayerMatchStats, history: Sequence[PlayerMatchStats] = ()) -> MatchAnalysis:
    """Full analysis of one match.

    ``history`` is your recent matches (may include ``stats``). Playstyle and
    agent fit use the history because a single match is too noisy to judge a
    playstyle; the per-match metrics and findings use only ``stats``.
    """
    rank, assumed = resolve_rank(stats.tier_id)
    metrics = evaluate_metrics(stats, rank)
    findings = build_findings(stats, metrics, rank)

    sample = [h for h in history if h.match_id != stats.match_id] + [stats]
    aggregate = aggregate_stats(sample)
    profile = build_playstyle(aggregate, rank)
    fit = evaluate_agent_fit(stats.agent, profile, rank)

    trend_metrics = evaluate_metrics(aggregate, rank) if len(sample) > 1 else []
    drills = select_drills(rank.group, metrics, trend_metrics)

    return MatchAnalysis(
        stats=stats,
        rank=rank,
        rank_assumed=assumed,
        metrics=metrics,
        findings=findings,
        playstyle=profile,
        agent_fit=fit,
        drills=drills,
        group=bm.COACHING_GROUPS[rank.group],
        sample_matches=len(sample),
    )


# --------------------------------------------------------------------------- #
# 7. On-demand analysis (used by the slash commands)
# --------------------------------------------------------------------------- #

# Topic key -> label shown in the /coach command.
TOPICS: dict[str, str] = {
    "overall": "Overall: biggest issues right now",
    "aim": "Aim: headshots and mechanics",
    "openings": "Opening duels and first contact",
    "deaths": "Deaths and damage",
    "agent": "Agent fit and picks",
    "drills": "Priority drills",
}


@dataclass(frozen=True)
class TopicAdvice:
    topic: str
    headline: str
    points: tuple[str, ...]
    drills: tuple[bm.Drill, ...]
    metric_keys: tuple[str, ...]


def analyze_recent(items: Sequence[PlayerMatchStats]) -> MatchAnalysis:
    """Analyse several matches (oldest first) as one combined sample.

    Uses the summed stats, so ratios are weighted by rounds played and the rank
    is taken from the newest match.
    """
    if not items:
        raise ValueError("analyze_recent() needs at least one match")
    return analyze_match(aggregate_stats(items))


def topic_advice(topic: str, analysis: MatchAnalysis) -> TopicAdvice:
    """Focused advice on one topic, based on an analysis (usually ``analyze_recent``)."""
    rank, group = analysis.rank, analysis.group
    metrics = {m.key: m for m in analysis.metrics}
    findings = {f.key: f for f in analysis.findings}

    def drills_for(*keys: str, limit: int = 2) -> tuple[bm.Drill, ...]:
        return tuple([d for d in group.drills if any(k in d.fixes for k in keys)][:limit])

    def status_point(key: str, low_text: str, ok_text: str) -> str:
        m = metrics[key]
        if m.status == "na":
            return f"Not enough data yet for {m.label} ({m.note or 'no data'})."
        return low_text if m.status == "low" else ok_text

    if topic == "aim":
        points = (
            status_point(
                "hs_pct",
                bm.HS_DIAGNOSIS[rank.group],
                "Your headshot rate is at or near target. Protect it: pre-aim at head height and don't trade consistency for flashy flicks.",
            ),
            *group.focus[:2],
        )
        return TopicAdvice(topic, "Aim and mechanics", points, drills_for("hs_pct"), ("hs_pct",))

    if topic == "openings":
        points = (
            status_point(
                "first_duel",
                bm.FIRST_DUEL_DIAGNOSIS[rank.group],
                "You win opening duels at or above the expected rate. Keep taking them with a plan.",
            ),
            status_point(
                "conversion",
                bm.CONVERSION_LOW,
                "Your team usually converts first kills into round wins. Keep trading fast after the opener.",
            ),
        )
        return TopicAdvice(topic, "Opening duels and conversion", points, drills_for("first_duel", "conversion"), ("first_duel", "conversion"))

    if topic == "deaths":
        points = tuple(f.detail for key in ("kd", "adr") if (f := findings.get(key)))
        if not points:
            points = ("Your K/D and damage are in range for your rank. Nothing stands out; keep playing close enough to trade.",)
        return TopicAdvice(topic, "Deaths and damage", points, drills_for("kd", "adr"), ("kd", "adr"))

    if topic == "agent":
        fit = analysis.agent_fit
        points = (fit.summary,)
        if fit.suggested_agents:
            points += (f"Worth trying in {rank.name}: {', '.join(fit.suggested_agents)} ({fit.suggested_class}).",)
        return TopicAdvice(topic, "Agent fit", points, (), ())

    if topic == "drills":
        return TopicAdvice(topic, "Your priority drills", (), tuple(analysis.drills), ())

    # "overall" and any unknown topic
    issues = [f for f in analysis.findings if f.severity == "issue"][:3]
    points = tuple(f"**{f.title}**: {f.detail}" for f in issues)
    if not points:
        points = (analysis.findings[0].detail,)
    points += (f"Playstyle: **{analysis.playstyle.archetype}**. {analysis.playstyle.archetype_blurb}",)
    return TopicAdvice("overall", "Biggest issues right now", points, tuple(analysis.drills[:2]), ("hs_pct", "adr", "kd", "first_duel"))
