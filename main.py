"""Valorant Coaching Bot - entry point.

Lifecycle
---------
1. Load and validate settings (fail fast with a readable error list).
2. ``setup_hook`` opens the HTTP client, resolves the target channel and starts
   the background poller.
3. The poller's *first* successful run only **seeds** the cache with your recent
   matches (no posts), so restarting never spams old games.
4. Every later run posts a report for each match it has not seen before.

Run with:  python main.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import OrderedDict
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import tasks

from config.settings import ConfigError, Settings, load_settings
from engine import analyzer
from engine.analyzer import PlayerMatchStats
from services.valorant_api import AuthError, HenrikAPIError, NotFoundError, RateLimitError, ValorantAPI
from utils.embed_builder import build_match_embeds, build_recent_embeds, build_topic_embed

log = logging.getLogger("coach")

FETCH_SIZE = 5            # matches requested per poll / command
MAX_CATCH_UP = 3          # most reports posted in a single poll
COMMAND_COOLDOWN = 15.0   # seconds between uses of a command, per user


class CoachBot(discord.Client):
    """Discord client that polls for finished matches and posts coaching reports."""

    def __init__(self, settings: Settings) -> None:
        # No privileged intents needed: the bot only sends messages.
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.api = ValorantAPI(settings.henrik_api_key)
        self.channel: Optional[discord.abc.Messageable] = None

        self.last_match_id: Optional[str] = None            # cached newest match
        self._seen: set[str] = set()                         # every match already handled
        self._seeded = False                                 # False until the first successful poll
        self._history: OrderedDict[str, PlayerMatchStats] = OrderedDict()  # oldest -> newest

        # Slash commands (/lastmatch, /recent, /coach) for on-demand analysis.
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self._on_command_error
        self._command_lock = asyncio.Lock()  # one API-backed command at a time
        self._register_commands()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def setup_hook(self) -> None:
        await self.api.start()

        try:
            self.channel = await self.fetch_channel(self.settings.channel_id)  # type: ignore[assignment]
        except discord.NotFound as exc:
            raise RuntimeError(
                f"Channel {self.settings.channel_id} not found. Check DISCORD_CHANNEL_ID and that the bot is in that server."
            ) from exc
        except discord.Forbidden as exc:
            raise RuntimeError(
                f"No access to channel {self.settings.channel_id}. Give the bot View Channel permission."
            ) from exc

        await self._sync_commands()

        # The interval comes from settings rather than the decorator's default.
        self.poll_matches.change_interval(seconds=self.settings.poll_interval)
        self.poll_matches.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s. Tracking %s (%s) every %ss.",
                 self.user, self.settings.riot_id, self.settings.region, self.settings.poll_interval)

    async def close(self) -> None:
        if self.poll_matches.is_running():
            self.poll_matches.cancel()
        await self.api.close()
        await super().close()

    # ------------------------------------------------------------------ #
    # Background poller
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=180)
    async def poll_matches(self) -> None:
        """Runs forever; every failure is handled so the loop never dies."""
        try:
            await self._poll_once()
        except AuthError as exc:
            log.critical("%s. Fix HENRIK_API_KEY and restart the bot. Polling stopped.", exc)
            self.poll_matches.cancel()
        except RateLimitError as exc:
            log.warning("Still rate limited (%s); will try again next cycle.", exc)
        except NotFoundError as exc:
            log.error("%s. Check VALORANT_REGION / VALORANT_NAME / VALORANT_TAG. Will retry.", exc)
        except HenrikAPIError as exc:
            log.warning("API problem: %s. Will retry next cycle.", exc)
        except Exception:  # noqa: BLE001 - last line of defence for a long-running task
            log.exception("Unexpected error in poll cycle; will retry next cycle.")

    @poll_matches.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    async def _poll_once(self) -> None:
        s = self.settings
        matches = await self.api.get_recent_matches(
            s.region, s.riot_name, s.riot_tag, mode=s.match_mode, size=FETCH_SIZE
        )
        log.debug("Fetched %d usable match(es).", len(matches))

        if not self._seeded:
            self._seed(matches)
            return

        new_matches = self._find_new_matches(matches)
        for match in new_matches:
            if not await self._handle_match(match):
                break  # post failed; retry this match next cycle

    # ------------------------------------------------------------------ #
    # Match bookkeeping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _match_id(match: dict[str, Any]) -> str:
        return str(match["metadata"]["matchid"])

    def _extract(self, match: dict[str, Any]) -> Optional[PlayerMatchStats]:
        return analyzer.extract_player_stats(match, name=self.settings.riot_name, tag=self.settings.riot_tag)

    def _remember(self, stats: PlayerMatchStats) -> None:
        """Add a match to the rolling history used for playstyle analysis."""
        self._history[stats.match_id] = stats
        while len(self._history) > self.settings.history_size:
            self._history.popitem(last=False)

    def _seed(self, matches: list[dict[str, Any]]) -> None:
        """First run: remember what already exists without posting anything."""
        for match in reversed(matches):  # oldest first so history stays chronological
            self._seen.add(self._match_id(match))
            stats = self._extract(match)
            if stats:
                self._remember(stats)
        if matches:
            self.last_match_id = self._match_id(matches[0])
        self._seeded = True
        log.info("Seeded with %d existing match(es); newest = %s. Only newer matches will be posted.",
                 len(matches), self.last_match_id)

    def _find_new_matches(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Matches newer than anything seen, oldest first (matches arrive newest first)."""
        new: list[dict[str, Any]] = []
        for match in matches:
            if self._match_id(match) in self._seen:
                break  # everything after this is older and already handled
            new.append(match)

        if len(new) > MAX_CATCH_UP:
            log.warning("%d unseen matches found; posting only the newest %d.", len(new), MAX_CATCH_UP)
            for skipped in new[MAX_CATCH_UP:]:
                self._seen.add(self._match_id(skipped))
            new = new[:MAX_CATCH_UP]
        return list(reversed(new))

    async def _handle_match(self, match: dict[str, Any]) -> bool:
        """Analyse and post one match. Returns False if posting should be retried."""
        match_id = self._match_id(match)
        stats = self._extract(match)

        if stats is None:
            log.warning("Tracked player not found in match %s; skipping it.", match_id)
            self._seen.add(match_id)
            return True

        self._remember(stats)
        analysis = analyzer.analyze_match(stats, list(self._history.values()))
        embeds = build_match_embeds(analysis, self.settings.riot_id)

        assert self.channel is not None
        try:
            await self.channel.send(embeds=embeds)
        except discord.Forbidden:
            log.error("Missing permission to post in the channel. The bot needs Send Messages and Embed Links.")
            return False
        except discord.HTTPException as exc:
            log.error("Discord rejected the report for %s: %s", match_id, exc)
            return False

        self._seen.add(match_id)
        self.last_match_id = match_id
        log.info("Posted report for %s (%s on %s, %s).", match_id, stats.agent, stats.map_name, stats.result)
        return True

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #
    def _register_commands(self) -> None:
        topic_choices = [app_commands.Choice(name=label, value=key) for key, label in analyzer.TOPICS.items()]

        @self.tree.command(name="lastmatch", description="Full coaching report for your most recent competitive match")
        @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
        async def lastmatch(interaction: discord.Interaction) -> None:
            await self._run_command(interaction, self._cmd_lastmatch)

        @self.tree.command(name="recent", description="Analysis and tips from your last few competitive matches")
        @app_commands.describe(count="How many recent matches to analyse (2-5, default 5)")
        @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
        async def recent(interaction: discord.Interaction, count: app_commands.Range[int, 2, 5] = 5) -> None:
            await self._run_command(interaction, self._cmd_recent, count)

        @self.tree.command(name="coach", description="Ask your coach about one area of your game")
        @app_commands.describe(topic="What do you want advice on?")
        @app_commands.choices(topic=topic_choices)
        @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
        async def coach(interaction: discord.Interaction, topic: str = "overall") -> None:
            await self._run_command(interaction, self._cmd_coach, topic)

    async def _sync_commands(self) -> None:
        """Register the commands in the report channel's server (appears instantly)."""
        guild = getattr(self.channel, "guild", None)
        try:
            if guild is not None:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            else:
                synced = await self.tree.sync()
            log.info("Registered slash commands: %s", ", ".join(f"/{c.name}" for c in synced))
        except discord.HTTPException as exc:
            log.error(
                "Could not register slash commands (%s). Re-invite the bot with the 'applications.commands' "
                "scope. Automatic match reports still work.", exc,
            )

    async def _on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down: try again in {error.retry_after:.0f}s."
        else:
            log.error("Slash command failed: %s", error, exc_info=error)
            message = "Something went wrong running that command. Check the bot's logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _run_command(self, interaction: discord.Interaction, handler: Any, *args: Any) -> None:
        """Defer, run an embed-producing handler, and reply with friendly errors."""
        await interaction.response.defer(thinking=True)
        try:
            async with self._command_lock:
                embeds = await handler(*args)
        except AuthError:
            await interaction.followup.send("The HenrikDev API key was rejected. Check HENRIK_API_KEY.")
            return
        except NotFoundError:
            await interaction.followup.send("Couldn't find that Riot ID. Check VALORANT_REGION, VALORANT_NAME and VALORANT_TAG.")
            return
        except RateLimitError:
            await interaction.followup.send("The Valorant API is rate limiting the bot right now. Try again in a minute.")
            return
        except HenrikAPIError as exc:
            await interaction.followup.send(f"The Valorant API had a problem: {exc}")
            return

        if not embeds:
            await interaction.followup.send("No competitive matches found for this account yet.")
            return
        await interaction.followup.send(embeds=embeds)

    async def _fetch_stats(self) -> list[PlayerMatchStats]:
        """Your recent matches as stats, oldest first."""
        s = self.settings
        matches = await self.api.get_recent_matches(s.region, s.riot_name, s.riot_tag, mode=s.match_mode, size=FETCH_SIZE)
        stats = [st for m in matches if (st := self._extract(m))]
        return list(reversed(stats))

    async def _cmd_lastmatch(self) -> list[discord.Embed]:
        items = await self._fetch_stats()
        if not items:
            return []
        analysis = analyzer.analyze_match(items[-1], items)
        return build_match_embeds(analysis, self.settings.riot_id)

    async def _cmd_recent(self, count: int) -> list[discord.Embed]:
        items = (await self._fetch_stats())[-count:]
        if not items:
            return []
        return build_recent_embeds(items, analyzer.analyze_recent(items), self.settings.riot_id)

    async def _cmd_coach(self, topic: str) -> list[discord.Embed]:
        # Reuse the poller's rolling history (no API call) when it has data.
        items = list(self._history.values()) or await self._fetch_stats()
        if not items:
            return []
        analysis = analyzer.analyze_recent(items)
        advice = analyzer.topic_advice(topic, analysis)
        return [build_topic_embed(advice, analysis, self.settings.riot_id)]


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)

    bot = CoachBot(settings)
    try:
        bot.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.critical("Discord rejected the bot token. Check DISCORD_BOT_TOKEN.")
        sys.exit(1)
    except RuntimeError as exc:
        log.critical("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
