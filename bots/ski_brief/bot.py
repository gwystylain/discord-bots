"""Discord bot that writes a weekend ski brief for the Paul Ridge / Round Mountain area.

Data sources:
  * mountain-forecast.com - 6-day forecast for Round Mountain (volcano) at 1646 m and 1000 m
  * Snower (app.snower.fi) - live snow depth and snowfall trend from the "Paul Ridge" station
  * Avalanche Canada - danger ratings and avalanche problems for the Paul Ridge location

The numbers go to Claude, which writes a short "is skiing good this weekend?" brief. Without an
ANTHROPIC_API_KEY (or if the call fails) the bot falls back to a plain numbers-only summary.

Usage (from the repo root):
    python -m bots.ski_brief --print   # fetch + write a brief and print it (no Discord needed)
    python -m bots.ski_brief           # run the bot: /skibrief command + weekly scheduled post

Config (env/ski-brief.env locally, the compose env_file in Docker, or environment variables):
    DISCORD_BOT_TOKEN   bot token from the Discord developer portal (required to run the bot)
    ANTHROPIC_API_KEY   Claude API key (optional; without it you get the numbers-only brief)
    SKI_CHANNEL_ID      channel for the weekly post (optional; no scheduled post if unset)
    SKI_GUILD_ID        your server's ID, makes /skibrief show up instantly (optional)
    SKI_POST_DAY        day of the weekly post, mon..sun (default thu)
    SKI_POST_TIME       time of the weekly post, HH:MM (default 18:00)
    SKI_TIMEZONE        default America/Vancouver
    SNOWER_STATION      default "Paul Ridge"

The forecast is pulled at two levels: 1646 m (Red Heather / Paul Ridge touring zone) and 1000 m
(Diamond Head parking lot), so the brief can say whether you skin from the car or walk the approach.
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import anthropic
import httpx
from bs4 import BeautifulSoup

from bots.common import load_env

load_env("ski-brief")

FORECAST_BASE = os.environ.get(
    "FORECAST_BASE", "https://www.mountain-forecast.com/peaks/Round-Mountain-volcano/forecasts/"
)
# Forecast levels: 1646 m ~ the Red Heather / Paul Ridge touring zone, 1000 m ~ the parking lot.
LEVELS = {1646: "touring zone (Red Heather / Paul Ridge)", 1000: "parking lot / approach"}
FORECAST_URL = FORECAST_BASE + "1646"
SNOWER_URL = "https://app.snower.fi/api/v1/public/devices"
# Avalanche Canada's forecast for the point at Paul Ridge (Sea-to-Sky region).
AVCAN_URL = "https://api.avalanche.ca/forecasts/en/products/point?lat=49.76868&long=-123.03055"
SNOWER_STATION = os.environ.get("SNOWER_STATION", "Paul Ridge")
TZ = ZoneInfo(os.environ.get("SKI_TIMEZONE", "America/Vancouver"))
MODEL = "claude-opus-5-5"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
CACHE_SECONDS = 30 * 60
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---------------------------------------------------------------------------- data fetching

def _num(text):
    """'-2' -> -2.0, '1300' -> 1300.0, a dash or empty -> None."""
    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(m.group()) if m else None


def parse_forecast(html, elevation):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="forecast-table__table--content")
    if table is None:
        raise ValueError("forecast table not found - mountain-forecast.com layout may have changed")

    def cells(key):
        tr = table.find("tr", attrs={"data-row": key})
        return tr.find_all("td") if tr else []

    dates = []
    for td in cells("days"):
        dates += [td["data-date"]] * int(td.get("colspan", 1))

    def col(key, i, how="num"):
        row = cells(key)
        if i >= len(row):
            return None
        c = row[i]
        if how == "text":
            return c.get_text(" ", strip=True) or None
        if how == "level":
            v = c.find(attrs={"data-value": True})
            return _num(v["data-value"]) if v else _num(c.get_text())
        return _num(c.get_text())

    periods = []
    for i, date in enumerate(dates):
        wind = cells("wind")[i] if i < len(cells("wind")) else None
        icon = wind.find(attrs={"data-speed": True}) if wind else None
        wind_text = wind.get_text(" ", strip=True) if wind else ""
        periods.append({
            "date": date,
            "day": dt.date.fromisoformat(date).strftime("%a"),
            "period": col("time", i, "text"),
            "weather": col("phrases", i, "text"),
            "snow_cm": col("snow", i) or 0.0,
            "rain_mm": col("rain", i) or 0.0,
            "temp_max_c": col("temperature-max", i),
            "temp_min_c": col("temperature-min", i),
            "wind_chill_c": col("temperature-chill", i),
            "wind_kmh": float(icon["data-speed"]) if icon else _num(wind_text),
            "wind_dir": wind_text.split()[-1] if wind_text else None,
            "freezing_level_m": col("freezing-level", i, "level"),
            "cloud_base_m": col("cloud-base", i),
        })

    summaries = [p.get_text(" ", strip=True) for p in table.find_all("p", class_="forecast-table__summary")]
    return {"elevation_m": elevation, "label": LEVELS[elevation], "summaries": summaries, "periods": periods}


def pick_station(devices, name):
    items = devices.get("devices") if isinstance(devices, dict) else devices
    if not isinstance(items, list):  # the API returns a dict; find the list of devices inside it
        items = next((v for v in devices.values() if isinstance(v, list)), [])
    for d in items:
        if d.get("name", "").strip().lower() == name.lower():
            v = d.get("values") or {}
            fresh = v.get("freshness") or {}
            return {
                "station": d["name"],
                "lat": d.get("lat"),
                "lon": d.get("lon"),
                "measured_at": v.get("ts") or d.get("last_reading_at"),
                "data_freshness": fresh.get("state"),
                "snow_depth_cm": v.get("snow_depth_cm"),
                "change_6h_cm": v.get("delta_6h"),
                "change_24h_cm": v.get("delta_24h"),
                "change_7d_cm": v.get("delta_7d"),
                "snow_surface_temp_c": v.get("surface_temp"),
                "air_temp_c": v.get("temperature"),
                "wind_kmh": v.get("wind_speed"),
                "wind_dir": v.get("wind_direction"),
                "humidity_pct": v.get("humidity"),
            }
    raise ValueError(f'Snower station "{name}" not found')


def _plain(o):
    """Flatten Avalanche Canada JSON: {value, display} -> display text, HTML -> plain text."""
    if isinstance(o, dict):
        if "display" in o and set(o) <= {"value", "display"}:
            return o["display"]
        return {k: _plain(v) for k, v in o.items() if v not in (None, [], {}, "")}
    if isinstance(o, list):
        return [_plain(v) for v in o]
    if isinstance(o, str) and "<" in o:
        text = BeautifulSoup(o, "html.parser").get_text(" ", strip=True)
        return re.sub(r"\s+([.,;:!?])", r"\1", text)
    return o


def parse_avalanche(data):
    r = data["report"]
    days = []
    for d in r.get("dangerRatings") or []:
        when = d.get("date", {}).get("value")
        local = dt.datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(TZ).date() if when else None
        days.append({
            "date": local.isoformat() if local else None,
            "day": d.get("date", {}).get("display"),
            **{v.get("display", k): v.get("rating", {}).get("display") for k, v in (d.get("ratings") or {}).items()},
        })
    return {
        "url": data.get("url"),
        "issued": r.get("dateIssued"),
        "valid_until": r.get("validUntil"),
        "highlights": _plain(r.get("highlights")),
        "confidence": _plain(r.get("confidence")),
        "danger_ratings": days,
        "problems": _plain(r.get("problems")),
        "travel_advice": _plain(r.get("terrainAndTravelAdvice")),
        "summaries": _plain(r.get("summaries")),
        "message": _plain(r.get("message")),
    }


async def fetch_all():
    """Returns (forecasts {elevation: parsed}, station reading, avalanche forecast, list of error strings)."""
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as http:
        *fcs, sn, av = await asyncio.gather(
            *(http.get(f"{FORECAST_BASE}{elev}") for elev in LEVELS), http.get(SNOWER_URL), http.get(AVCAN_URL),
            return_exceptions=True,
        )
    forecasts, station, avalanche, errors = {}, None, None, []
    try:
        if isinstance(av, Exception):
            raise av
        av.raise_for_status()
        avalanche = parse_avalanche(av.json())
    except Exception as e:
        errors.append(f"avalanche.ca: {e}")
    for elev, fc in zip(LEVELS, fcs):
        try:
            if isinstance(fc, Exception):
                raise fc
            fc.raise_for_status()
            forecasts[elev] = parse_forecast(fc.text, elev)
        except Exception as e:
            errors.append(f"mountain-forecast {elev} m: {e}")
    try:
        if isinstance(sn, Exception):
            raise sn
        sn.raise_for_status()
        station = pick_station(sn.json(), SNOWER_STATION)
    except Exception as e:
        errors.append(f"snower: {e}")
    return forecasts, station, avalanche, errors


def target_weekend(today):
    """Mon-Fri: the coming Sat+Sun. Sat: today+tomorrow. Sun: today."""
    if today.weekday() == 6:
        return [today]
    sat = today + dt.timedelta(days=(5 - today.weekday()))
    return [sat, sat + dt.timedelta(days=1)]


# ---------------------------------------------------------------------------- brief writing

SYSTEM_PROMPT = """You write a weekend ski brief for a small Discord group of backcountry skiers \
who tour at Red Heather / Paul Ridge in Garibaldi Provincial Park (Diamond Head area, near Squamish, \
BC). The trip starts at the Diamond Head parking lot at about 1000 m and climbs to the Red Heather / \
Paul Ridge touring zone at about 1600 m. Your input is two mountain-forecast.com forecasts for Round \
Mountain (the 1646 m level for the touring zone, the 1000 m level for the parking lot and approach) \
and live readings from the Snower "Paul Ridge" snow station, which sits in the touring zone.

Judge whether skiing will be good on the target weekend days. Weigh, in roughly this order:
- Coverage: current snow depth at the station (under ~50 cm early season means thin, rocks and \
bushes likely).
- New snow: what fell in the last 24 h / 7 d (station) and what is forecast before and during the weekend.
- Snow quality: freezing level vs. ~1600 m, rain at elevation, warm spells or crusts, wind chill.
- The approach: compare the freezing level and 1000 m forecast with the parking lot. Freezing level \
below ~1000 m with snow means you can likely skin from the car; above it, expect rain or slush low \
down and a walk or carry until the snow starts. Rain at the lot while it snows up high is common, so \
call it out.
- Wind: strong wind (40+ km/h) means wind-affected snow and wind slabs; also comfort.
- Visibility: clear vs. cloudy/snowing (flat light above treeline).

- Avalanche danger: the Avalanche Canada forecast for this point. Red Heather / Paul Ridge is \
mostly treeline and below-treeline terrain, with alpine above. Considerable or higher danger lowers \
the score, and High or Extreme means you should recommend staying in simple, low-angle terrain or \
staying home.

The station's snow-depth changes come from a depth sensor and can be noisy (e.g. a 24 h gain bigger \
than the 7 d gain). If they look inconsistent, say so briefly and lean on the forecast.

Write for Discord, under 1400 characters:
- Line 1: a bold one-line verdict with a score, e.g. **Verdict: 3/5 - decent Saturday, skip Sunday**
- Then short bullets covering each weekend day (and the key lead-up conditions).
- Finish with one line on the best window or a suggestion.
Use **bold** and "- " bullets only: no headings, no tables. Quote real numbers from the data. \
If a weekend day is outside the forecast range, say so rather than guessing. If a data source is \
missing, say what you could not check.

Include one **Avalanche** bullet: the danger ratings (alpine / treeline / below treeline) for the \
weekend days if the forecast covers them, plus the main avalanche problems in a few words. If the \
weekend is past the forecast's dates, say ratings aren't out yet. If it's an early-season bulletin \
with no ratings, say so and pass on its key advice (e.g. when daily forecasts start). End with the \
link to the full avalanche forecast from the data, written as [Full avalanche forecast](<url>)."""


def build_prompt(forecasts, station, avalanche, errors, weekend, now):
    weekend_iso = [d.isoformat() for d in weekend]
    lines = [
        f"Now: {now:%A %Y-%m-%d %H:%M} ({TZ.key})",
        f"Target weekend days: {', '.join(f'{d:%A} {d.isoformat()}' for d in weekend)}",
    ]
    if forecasts:
        covered = {p["date"] for f in forecasts.values() for p in f["periods"]}
        missing = [d for d in weekend_iso if d not in covered]
        if missing:
            lines.append(f"NOTE: these weekend days are beyond the forecast range: {', '.join(missing)}")
    for elev, forecast in forecasts.items():
        lines.append(f"\n## mountain-forecast.com, Round Mountain at {elev} m = {forecast['label']} "
                     "(AM/PM/night periods)")
        lines += [f"Summary: {s}" for s in forecast["summaries"]]
        for p in forecast["periods"]:
            tag = "  <-- WEEKEND" if p["date"] in weekend_iso else ""
            lines.append(json.dumps(p, ensure_ascii=False) + tag)
    if station:
        lines.append(f"\n## Snower station reading\n{json.dumps(station, ensure_ascii=False)}")
    if avalanche:
        lines.append(f"\n## Avalanche Canada forecast\n{json.dumps(avalanche, ensure_ascii=False)}")
    if errors:
        lines.append("\n## Data sources that failed\n" + "\n".join(errors))
    lines.append("\nWrite the weekend ski brief.")
    return "\n".join(lines)


async def claude_brief(prompt):
    client = anthropic.AsyncAnthropic()
    # Server-side fallbacks: if the model declines, the API re-runs the request on a fallback model.
    resp = await client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write the brief")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"empty response (stop_reason={resp.stop_reason})")
    return text


def avalanche_line(avalanche, weekend):
    """One-bullet avalanche summary for the numbers-only brief."""
    wanted = {d.isoformat() for d in weekend}
    days = [d for d in avalanche["danger_ratings"] if d["date"] in wanted]
    if days:
        ratings = "; ".join(
            f"{d['day']}: " + ", ".join(f"{k} {v}" for k, v in d.items() if k not in ("date", "day"))
            for d in days
        )
    elif avalanche["danger_ratings"]:
        ratings = "weekend ratings not out yet"
    else:
        ratings = "no ratings"
    problems = [p.get("type") for p in avalanche["problems"] or [] if isinstance(p, dict) and p.get("type")]
    if problems:
        ratings += f". Problems: {', '.join(problems)}"
    if not days and avalanche.get("highlights"):
        ratings += f". {avalanche['highlights']}"
    return f"- **Avalanche**: {ratings}\n[Full avalanche forecast]({avalanche['url']})"


def plain_brief(forecasts, station, avalanche, weekend):
    """Numbers-only brief used when Claude isn't available."""
    out = ["**Weekend ski data** - Michael has yet to secure a 2080 Ti for our local LLM, "
           "so you'll need to parse the numbers yourself like a caveman 🦴"]
    if station:
        out.append(
            f"- **{station['station']}**: {station['snow_depth_cm']} cm on the ground "
            f"({station['change_24h_cm'] or 0:+} cm / 24 h, {station['change_7d_cm'] or 0:+} cm / 7 d), "
            f"snow surface {station['snow_surface_temp_c']} °C"
        )
    top, lot = forecasts.get(1646), forecasts.get(1000)
    for d in weekend:
        ps = [p for p in (top or lot or {"periods": []})["periods"] if p["date"] == d.isoformat()]
        if not ps:
            out.append(f"- **{d:%A}**: beyond the 6-day forecast")
            continue
        snow = sum(p["snow_cm"] for p in ps)
        rain = sum(p["rain_mm"] for p in ps)
        temps = [t for p in ps for t in (p["temp_min_c"], p["temp_max_c"]) if t is not None]
        wind = max((p["wind_kmh"] or 0) for p in ps)
        fl = max((p["freezing_level_m"] or 0) for p in ps)
        sky = ", ".join(dict.fromkeys(p["weather"] for p in ps if p["weather"]))
        line = (f"- **{d:%A}** ({1646 if top else 1000} m): {sky}; snow {snow:g} cm, rain {rain:g} mm, "
                f"{min(temps):g} to {max(temps):g} °C, wind up to {wind:g} km/h, freezing level up to {fl:g} m")
        if lot and top:
            lp = [p for p in lot["periods"] if p["date"] == d.isoformat()]
            lot_temps = [p["temp_max_c"] for p in lp if p["temp_max_c"] is not None]
            line += (f". Parking lot (1000 m): snow {sum(p['snow_cm'] for p in lp):g} cm, "
                     f"rain {sum(p['rain_mm'] for p in lp):g} mm, max {max(lot_temps, default=0):g} °C")
        out.append(line)
    if avalanche:
        out.append(avalanche_line(avalanche, weekend))
    else:
        out.append("Couldn't load the avalanche forecast - check avalanche.ca (Sea-to-Sky) before you go.")
    return "\n".join(out)


_cache = {"at": 0.0, "brief": None}


async def make_brief(use_cache=True):
    if use_cache and _cache["brief"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["brief"]
    now = dt.datetime.now(TZ)
    weekend = target_weekend(now.date())
    forecasts, station, avalanche, errors = await fetch_all()
    if not forecasts and not station:
        raise RuntimeError("Weather and snow data sources all failed: " + "; ".join(errors))

    text, by_ai = None, False
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        try:
            prompt = build_prompt(forecasts, station, avalanche, errors, weekend, now)
            text, by_ai = await claude_brief(prompt), True
        except Exception as e:
            print(f"Claude brief failed, using plain brief: {e!r}", file=sys.stderr)
    if text is None:
        text = plain_brief(forecasts, station, avalanche, weekend)

    brief = {
        "title": f"Ski brief: weekend of {weekend[0]:%b %d}",
        "text": text,
        "footer": " · ".join(filter(None, [
            f"Snower {station['station']} @ {dt.datetime.fromisoformat(station['measured_at']).astimezone(TZ):%a %H:%M}"
            if station and station.get("measured_at") else None,
            "mountain-forecast.com " + " + ".join(f"{e} m" for e in forecasts) if forecasts else None,
            "Avalanche Canada" if avalanche else None,
            "written by Claude" if by_ai else None,
        ])),
        "errors": errors,
    }
    _cache.update(at=time.time(), brief=brief)
    return brief


# ---------------------------------------------------------------------------- Discord

def run_bot():
    import discord
    from discord import app_commands
    from discord.ext import tasks

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set (see env/ski-brief.env.example)")
    channel_id = int(os.environ["SKI_CHANNEL_ID"]) if os.environ.get("SKI_CHANNEL_ID") else None
    guild_id = int(os.environ["SKI_GUILD_ID"]) if os.environ.get("SKI_GUILD_ID") else None
    post_day = DAYS.index(os.environ.get("SKI_POST_DAY", "thu").strip().lower()[:3])
    hh, mm = (int(x) for x in os.environ.get("SKI_POST_TIME", "18:00").split(":"))

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)

    def to_embed(brief):
        embed = discord.Embed(
            title=brief["title"], description=brief["text"][:4096], color=0x4A90D9, url=FORECAST_URL
        )
        embed.set_footer(text=brief["footer"])
        return embed

    @tree.command(name="skibrief", description="Is skiing at Paul Ridge / Round Mountain good this weekend?")
    @app_commands.describe(fresh="Ignore the 30-minute cache and refetch everything")
    async def skibrief(interaction: discord.Interaction, fresh: bool = False):
        await interaction.response.defer(thinking=True)  # fetching + writing takes a few seconds
        try:
            brief = await make_brief(use_cache=not fresh)
            await interaction.followup.send(embed=to_embed(brief))
        except Exception as e:
            await interaction.followup.send(f"Couldn't build the ski brief: {e}")

    @tasks.loop(time=dt.time(hour=hh, minute=mm, tzinfo=TZ))
    async def weekly_post():
        if dt.datetime.now(TZ).weekday() != post_day:
            return
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        try:
            await channel.send(embed=to_embed(await make_brief(use_cache=False)))
        except Exception as e:
            print(f"Weekly post failed: {e!r}", file=sys.stderr)

    def invite_url():
        perms = discord.Permissions(send_messages=True, embed_links=True, view_channel=True)
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
            print(f"Can't register /skibrief in server {guild_id}: the bot isn't in that server, or was "
                  f"invited without the applications.commands scope. Invite it with:\n  {invite_url()}\n"
                  "then restart the bot.", file=sys.stderr)
        if channel_id:
            weekly_post.start()

    @client.event
    async def on_ready():
        sched = f"weekly post {DAYS[post_day]} {hh:02d}:{mm:02d} to #{channel_id}" if channel_id else "no weekly post"
        print(f"Logged in as {client.user} - /skibrief {state['command']}, {sched}\nInvite link: {invite_url()}")

    client.run(token)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="print a brief to the console instead of running the bot")
    ap.add_argument("--data", action="store_true", help="with --print, also dump the raw data sent to Claude")
    args = ap.parse_args()
    if args.print:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if args.data:
            forecasts, station, avalanche, errors = asyncio.run(fetch_all())
            now = dt.datetime.now(TZ)
            print(build_prompt(forecasts, station, avalanche, errors, target_weekend(now.date()), now),
                  "\n" + "-" * 60)
        brief = asyncio.run(make_brief(use_cache=False))
        print(f"{brief['title']}\n\n{brief['text']}\n\n[{brief['footer']}]")
        for e in brief["errors"]:
            print(f"warning: {e}")
    else:
        run_bot()


if __name__ == "__main__":
    main()
