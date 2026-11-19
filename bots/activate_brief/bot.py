"""Discord bot that posts the Activate tracker's roast of our crew and their Hoops Trivia rankings.

The tracker (activateTracker) does all the work and this bot posts its `text` (Discord Markdown,
at most 2000 chars) as is. For now it always asks about the same four players (PLAYERS below).
    /roast    GET /api/brief?players=...&days=N       who's been showing up, and who hasn't
    /trivia   GET /api/rankings?players=...&location=  Hoops Trivia standings per location

Usage (from the repo root):
    python -m bots.activate_brief --print            # fetch the roast and print it (no Discord needed)
    python -m bots.activate_brief --print --trivia   # same for the Hoops Trivia rankings
    python -m bots.activate_brief                    # run the bot: /roast, /trivia + optional weekly post

Config (env/activate-brief.env locally, the compose env_file in Docker, or environment variables):
    DISCORD_BOT_TOKEN        bot token from the Discord developer portal (required to run the bot)
    ACTIVATE_URL             the tracker's base URL (default https://activate.noobatron.duckdns.org)
    ACTIVATE_DAYS            window for "last N days", 1-365 (default 30)
    ACTIVATE_CHANNEL_ID      channel for the weekly post (optional; no scheduled post if unset)
    ACTIVATE_GUILD_ID        your server's ID, makes /roast show up instantly (optional)
    ACTIVATE_POST_DAY        day of the weekly post, mon..sun (default mon)
    ACTIVATE_POST_TIME       time of the weekly post, HH:MM (default 18:00)
    ACTIVATE_TIMEZONE        default America/Vancouver
"""
import argparse
import asyncio
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

import httpx

from bots.common import load_env

load_env("activate-brief")

PLAYERS = ["FaffKing", "GMEBagholder", "Roboterson", "xXNoobBotXx420"]
BASE_URL = (os.environ.get("ACTIVATE_URL") or "https://activate.noobatron.duckdns.org").rstrip("/")
DAYS = int(os.environ.get("ACTIVATE_DAYS", "30"))
TZ = ZoneInfo(os.environ.get("ACTIVATE_TIMEZONE", "America/Vancouver"))
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DISCORD_LIMIT = 2000


class BriefError(Exception):
    """Something to say in the channel instead of the brief."""


async def _get(path, params):
    """GET one of the tracker's /api/ endpoints for PLAYERS; its 404s become BriefErrors."""
    url = f"{BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.get(url, params={"players": ",".join(PLAYERS), **params})
    except httpx.HTTPError as e:
        raise BriefError(f"Couldn't reach the Activate tracker at {BASE_URL} ({type(e).__name__}).") from e
    if r.status_code == 404:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if body.get("unknown"):
            raise BriefError("The tracker doesn't know " + ", ".join(f"**{n}**" for n in body["unknown"]) + ".")
        if body.get("error") == "unknown location":
            raise BriefError(f"The tracker doesn't track a location called **{body.get('location')}**.")
        if body.get("error"):
            raise BriefError(f"The tracker says: {body['error']}.")
        raise BriefError(f"The tracker has nothing at {url} - is it running a version with {path}?")
    if r.status_code != 200:
        raise BriefError(f"The tracker answered HTTP {r.status_code}.")
    return r.json()


async def fetch_brief(days=DAYS):
    """The tracker's roast of PLAYERS over the last `days` days."""
    return await _get("/api/brief", {"days": days})


async def fetch_trivia(location=None):
    """PLAYERS' Hoops Trivia standings at every tracked location, or just `location`."""
    return await _get("/api/rankings", {"location": location} if location else {})


def message_text(brief):
    text = (brief.get("text") or "").strip() or "Nothing to report. Suspicious."
    return text if len(text) <= DISCORD_LIMIT else text[: DISCORD_LIMIT - 1] + "…"


# ---------------------------------------------------------------------------- Discord

def run_bot():
    import discord
    from discord import app_commands
    from discord.ext import tasks

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set (see env/activate-brief.env.example)")
    channel_id = int(os.environ["ACTIVATE_CHANNEL_ID"]) if os.environ.get("ACTIVATE_CHANNEL_ID") else None
    guild_id = int(os.environ["ACTIVATE_GUILD_ID"]) if os.environ.get("ACTIVATE_GUILD_ID") else None
    post_day = WEEKDAYS.index(os.environ.get("ACTIVATE_POST_DAY", "mon").strip().lower()[:3])
    hh, mm = (int(x) for x in os.environ.get("ACTIVATE_POST_TIME", "18:00").split(":"))

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    # The brief bolds names but never pings; make sure a stray @ in a display name can't either.
    quiet = discord.AllowedMentions.none()

    async def text_or_error(fetch):
        try:
            return message_text(await fetch)
        except BriefError as e:
            return f"⚠️ {e}"

    @tree.command(name="roast", description="Who's been showing up at Activate, and who hasn't")
    @app_commands.describe(days="How many days back to look (default from the bot's settings)")
    async def roast(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = DAYS):
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(await text_or_error(fetch_brief(days)), allowed_mentions=quiet)

    @tree.command(name="trivia", description="Hoops Trivia rankings at each Activate location")
    @app_commands.describe(location="Just one location, e.g. Langley or Coquitlam (default: all of them)")
    async def trivia(interaction: discord.Interaction, location: str | None = None):
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(await text_or_error(fetch_trivia(location)), allowed_mentions=quiet)

    @tasks.loop(time=dt.time(hour=hh, minute=mm, tzinfo=TZ))
    async def weekly_post():
        if dt.datetime.now(TZ).weekday() != post_day:
            return
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            await channel.send(message_text(await fetch_brief()), allowed_mentions=quiet)
        except Exception as e:  # a failed post shouldn't kill the loop; try again next week
            print(f"Weekly post failed: {e!r}", file=sys.stderr)

    def invite_url():
        perms = discord.Permissions(send_messages=True, view_channel=True)
        return discord.utils.oauth_url(client.application_id, permissions=perms,
                                       scopes=("bot", "applications.commands"))

    state = {"command": "ready"}

    @client.event
    async def setup_hook():
        try:
            if guild_id:
                guild = discord.Object(id=guild_id)
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
            else:
                await tree.sync()  # global commands can take up to an hour to appear
        except discord.Forbidden:
            state["command"] = "NOT registered (see above)"
            print(f"Can't register /roast and /trivia in server {guild_id}: the bot isn't in that server, or was "
                  f"invited without the applications.commands scope. Invite it with:\n  {invite_url()}\n"
                  "then restart the bot.", file=sys.stderr)
        if channel_id:
            weekly_post.start()

    @client.event
    async def on_ready():
        sched = (f"weekly post {WEEKDAYS[post_day]} {hh:02d}:{mm:02d} to #{channel_id}"
                 if channel_id else "no weekly post")
        print(f"Logged in as {client.user} - /roast + /trivia {state['command']}, {sched}, tracker {BASE_URL}\n"
              f"Invite link: {invite_url()}")

    client.run(token)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="print the brief to the console instead of running the bot")
    ap.add_argument("--days", type=int, default=DAYS, help=f"with --print, the window in days (default {DAYS})")
    ap.add_argument("--trivia", action="store_true", help="with --print, print the Hoops Trivia rankings instead")
    ap.add_argument("--location", help="with --trivia, just this location (default: all)")
    args = ap.parse_args()
    if args.print:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        try:
            data = asyncio.run(fetch_trivia(args.location) if args.trivia else fetch_brief(args.days))
        except BriefError as e:
            sys.exit(str(e))
        print(message_text(data))
    else:
        run_bot()


if __name__ == "__main__":
    main()
