"""Discord embed generation for post-match reports.

Two embeds are produced per match so each stays well inside Discord's limits
(4096 chars description, 1024 per field, 6000 total per embed):

1. **Match summary**  - result, core stats, and metrics vs rank benchmarks.
2. **Coaching report** - diagnoses, playstyle, agent fit, rank focus, drills.

Both are colour coded: green for a win, red for a loss, gold for a draw.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

import discord

from engine.analyzer import Finding, MatchAnalysis, MetricResult, PlayerMatchStats, TopicAdvice

COLOR_WIN = 0x2ECC71
COLOR_LOSS = 0xE74C3C
COLOR_DRAW = 0xF1C40F

STATUS_EMOJI = {"great": "🔥", "ok": "✅", "close": "🟡", "low": "🔻", "na": "➖"}
FIT_EMOJI = {"strong": "✅", "good": "👍", "partial": "⚠️", "mismatch": "❌", "unknown": "❔"}

FIELD_LIMIT = 1024
DESCRIPTION_LIMIT = 4096


def _clip(text: str, limit: int = FIELD_LIMIT) -> str:
    """Trim text to a Discord limit without cutting mid-word where possible."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _color_for(result: str) -> int:
    return {"Win": COLOR_WIN, "Loss": COLOR_LOSS}.get(result, COLOR_DRAW)


def _metric_line(m: MetricResult) -> str:
    if m.value is None:
        reason = f" ({m.note})" if m.note else ""
        return f"{STATUS_EMOJI['na']} **{m.label}:** n/a{reason}"
    suffix = f" _{m.note}_" if m.note else ""
    return (
        f"{STATUS_EMOJI[m.status]} **{m.label}:** {m.value:.{m.decimals}f}{m.unit} "
        f"(target {m.target:.{m.decimals}f}{m.unit}){suffix}"
    )


def _finding_line(f: Finding) -> str:
    icon = {"issue": "🔻", "positive": "🔥", "info": "ℹ️"}[f.severity]
    return f"{icon} **{f.title}**\n{f.detail}"


def _match_timestamp(game_start: Optional[int]) -> Optional[datetime]:
    if not game_start:
        return None
    try:
        return datetime.fromtimestamp(game_start, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def build_match_embeds(analysis: MatchAnalysis, player_label: str) -> list[discord.Embed]:
    """Build the [summary, coaching] embeds for one analysed match."""
    s = analysis.stats
    color = _color_for(s.result)
    headline = {"Win": "🏆 Victory", "Loss": "💀 Defeat", "Draw": "🤝 Draw"}[s.result]
    timestamp = _match_timestamp(s.game_start)
    footer = f"Match {s.match_id[:8]} • benchmarked vs {analysis.rank.name}"
    if analysis.rank_assumed:
        footer += " (rank unavailable, assumed)"

    # ---- Embed 1: match summary --------------------------------------------- #
    summary = discord.Embed(
        title=f"{headline} on {s.map_name}",
        description=(
            f"**{player_label}** · **{s.agent}** · {s.tier_name}\n"
            f"Score **{s.rounds_won}-{s.rounds_lost}** · {s.rounds_played} rounds · {s.mode}"
        ),
        color=color,
        timestamp=timestamp,
    )
    if s.agent_icon_url:
        summary.set_thumbnail(url=s.agent_icon_url)

    summary.add_field(name="⚔️ K / D / A", value=f"**{s.kills} / {s.deaths} / {s.assists}**", inline=True)
    summary.add_field(name="📊 K/D", value=f"**{s.kd:.2f}**", inline=True)
    summary.add_field(name="💥 ADR · ACS", value=f"**{s.adr:.0f}** · {s.acs:.0f}", inline=True)
    summary.add_field(
        name="🎯 Headshot %",
        value=f"**{s.hs_pct:.1f}%**\n{s.headshots}H / {s.bodyshots}B / {s.legshots}L",
        inline=True,
    )

    if s.first_engagement_available:
        duel_pct = f" ({s.first_duel_win_pct:.0f}% won)" if s.first_duel_win_pct is not None else ""
        opening = f"**{s.first_bloods} FB · {s.first_deaths} FD** ({s.first_blood_diff:+d}){duel_pct}"
    else:
        opening = "n/a (no kill telemetry)"
    summary.add_field(name="🩸 Opening duels", value=opening, inline=True)

    if s.conversion_pct is not None:
        conversion = f"**{s.conversion_pct:.0f}%**\n{s.team_fk_round_wins}/{s.team_fk_rounds} rounds"
    else:
        conversion = "n/a"
    summary.add_field(name="🔄 First-kill conversion", value=conversion, inline=True)

    summary.add_field(
        name=f"📏 Vs {analysis.rank.name} targets",
        value=_clip("\n".join(_metric_line(m) for m in analysis.metrics)),
        inline=False,
    )
    summary.set_footer(text=footer)

    return [summary, _coaching_embed(analysis, color)]


def _coaching_embed(analysis: MatchAnalysis, color: int, title: str = "🧠 Coaching report") -> discord.Embed:
    """Diagnoses, playstyle, agent fit, rank focus and drills."""
    findings_text = "\n\n".join(_finding_line(f) for f in analysis.findings[:4])
    coaching = discord.Embed(
        title=f"{title} · {analysis.group.title}",
        description=_clip(findings_text, DESCRIPTION_LIMIT),
        color=color,
    )

    p = analysis.playstyle
    sample_note = (
        f"last {p.sample_matches} matches" if p.sample_matches > 1 else "this match only (provisional)"
    )
    coaching.add_field(
        name=f"🧬 Playstyle: {p.archetype}",
        value=_clip(f"{p.archetype_blurb}\n_{' · '.join(p.traits)}_\n_Based on {sample_note}._"),
        inline=False,
    )

    fit = analysis.agent_fit
    fit_text = fit.summary
    if fit.level in ("partial", "mismatch", "unknown") and fit.suggested_agents:
        fit_text += f"\n**Try:** {', '.join(fit.suggested_agents)}"
    elif fit.level in ("strong", "good") and fit.suggested_agents:
        fit_text += f"\n_Backups in the same lane: {', '.join(fit.suggested_agents)}_"
    coaching.add_field(
        name=f"{FIT_EMOJI[fit.level]} Agent fit: {fit.level.capitalize()}",
        value=_clip(fit_text),
        inline=False,
    )

    coaching.add_field(
        name=f"🔧 {analysis.rank.name} mechanical focus",
        value=_clip("\n".join(f"• {line}" for line in analysis.group.focus)),
        inline=False,
    )

    drill_text = "\n".join(f"**{i}. {d.name}**\n{d.how}" for i, d in enumerate(analysis.drills, start=1))
    coaching.add_field(name="🏋️ Drills for your weak spots", value=_clip(drill_text), inline=False)
    coaching.set_footer(text="Targets are rough climbing benchmarks, not official Riot data.")
    return coaching


def build_recent_embeds(items: Sequence[PlayerMatchStats], analysis: MatchAnalysis, player_label: str) -> list[discord.Embed]:
    """Embeds for ``/recent``: one line per match, averages vs targets, then trend coaching.

    ``items`` must be oldest first; ``analysis`` comes from ``analyze_recent(items)``.
    """
    wins = sum(1 for m in items if m.result == "Win")
    losses = sum(1 for m in items if m.result == "Loss")
    draws = len(items) - wins - losses
    color = COLOR_WIN if wins > losses else COLOR_LOSS if losses > wins else COLOR_DRAW

    record = f"{wins}W-{losses}L" + (f"-{draws}D" if draws else "")
    result_emoji = {"Win": "🟢", "Loss": "🔴", "Draw": "🟡"}
    lines = [
        f"{result_emoji[m.result]} **{m.map_name}** · {m.agent} · {m.rounds_won}-{m.rounds_lost} · "
        f"{m.kills}/{m.deaths}/{m.assists} · ADR {m.adr:.0f} · HS {m.hs_pct:.0f}%"
        for m in reversed(items)  # newest first
    ]

    summary = discord.Embed(
        title=f"📈 Last {len(items)} competitive matches · {record}",
        description=_clip(f"**{player_label}** · {analysis.stats.tier_name}\n\n" + "\n".join(lines), DESCRIPTION_LIMIT),
        color=color,
    )
    summary.add_field(
        name=f"📏 Combined stats vs {analysis.rank.name} targets",
        value=_clip("\n".join(_metric_line(m) for m in analysis.metrics)),
        inline=False,
    )
    summary.set_footer(text=f"Averages are weighted by rounds played • benchmarked vs {analysis.rank.name}")
    return [summary, _coaching_embed(analysis, color, title="🧠 Trend coaching")]


def build_topic_embed(advice: TopicAdvice, analysis: MatchAnalysis, player_label: str) -> discord.Embed:
    """Embed for ``/coach``: focused advice on one topic."""
    embed = discord.Embed(
        title=f"🎓 Coach: {advice.headline}",
        description=_clip("\n\n".join(advice.points), DESCRIPTION_LIMIT) if advice.points else None,
        color=0x5865F2,
    )
    wanted = {m.key: m for m in analysis.metrics}
    metric_lines = [_metric_line(wanted[k]) for k in advice.metric_keys if k in wanted]
    if metric_lines:
        embed.add_field(name=f"📏 Vs {analysis.rank.name} targets", value=_clip("\n".join(metric_lines)), inline=False)
    if advice.drills:
        drill_text = "\n".join(f"**{i}. {d.name}**\n{d.how}" for i, d in enumerate(advice.drills, start=1))
        embed.add_field(name="🏋️ Try this", value=_clip(drill_text), inline=False)
    n = analysis.playstyle.sample_matches
    embed.set_footer(text=f"{player_label} • based on your last {n} match{'es' if n != 1 else ''}")
    return embed
