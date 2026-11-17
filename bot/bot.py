"""Demon Announcer bot: gets demon wins from the Geode mod and posts them in Discord."""
import os
import secrets
import sqlite3
import time

import discord
from aiohttp import web
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
PORT = int(os.environ.get("PORT", "8080"))
DB = sqlite3.connect(os.environ.get("DB_PATH", "demons.db"))
DB.executescript("""
CREATE TABLE IF NOT EXISTS players (discord_id INTEGER PRIMARY KEY, key TEXT UNIQUE, gd_name TEXT);
CREATE TABLE IF NOT EXISTS channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER);
CREATE TABLE IF NOT EXISTS beats (
    discord_id INTEGER, level_id INTEGER, level_name TEXT, difficulty TEXT,
    attempts INTEGER, platformer INTEGER, ts INTEGER,
    PRIMARY KEY (discord_id, level_id));
""")

DIFFS = {  # key -> (name, color, emoji)
    "easy": ("Easy Demon", 0x00C8FF, "🟦"),
    "medium": ("Medium Demon", 0x00E676, "🟩"),
    "hard": ("Hard Demon", 0xFFD600, "🟨"),
    "insane": ("Insane Demon", 0xFF8C00, "🟧"),
    "extreme": ("Extreme Demon", 0xFF1744, "🟥"),
}
ORDER = list(DIFFS)  # easiest -> hardest

client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)


# ---------- Discord commands ----------

@tree.command(description="Get your key to paste into the Demon Announcer mod")
async def link(inter: discord.Interaction):
    key = "DA-" + secrets.token_hex(8).upper()
    DB.execute("INSERT INTO players (discord_id, key) VALUES (?, ?) "
               "ON CONFLICT(discord_id) DO UPDATE SET key = excluded.key",
               (inter.user.id, key))
    DB.commit()
    await inter.response.send_message(
        f"Your key: `{key}`\nPaste it into the mod's **Bot Key** setting in GD.\n"
        "Keep it secret! Run /link again any time to get a new one (the old one stops working).",
        ephemeral=True)


@app_commands.guild_only()
@tree.command(description="Pick the channel where demon wins get posted")
@app_commands.default_permissions(manage_guild=True)
async def setchannel(inter: discord.Interaction, channel: discord.TextChannel):
    DB.execute("INSERT OR REPLACE INTO channels VALUES (?, ?)", (inter.guild_id, channel.id))
    DB.commit()
    await inter.response.send_message(f"Demon wins will be posted in {channel.mention} 🔥")


@app_commands.guild_only()
@tree.command(description="See how many demons someone has beaten")
async def demons(inter: discord.Interaction, member: discord.Member | None = None):
    member = member or inter.user
    rows = DB.execute("SELECT difficulty, COUNT(*) FROM beats WHERE discord_id = ? GROUP BY difficulty",
                      (member.id,)).fetchall()
    counts = dict(rows)
    if not counts:
        await inter.response.send_message(f"{member.display_name} hasn't announced any demons yet.")
        return
    lines = [f"{DIFFS[d][2]} {DIFFS[d][0]}s: **{counts.get(d, 0)}**" for d in ORDER]
    recent = DB.execute("SELECT level_name, difficulty FROM beats WHERE discord_id = ? ORDER BY ts DESC LIMIT 3",
                        (member.id,)).fetchall()
    if recent:
        lines.append("\nLatest: " + ", ".join(f"{n} ({DIFFS[d][0]})" for n, d in recent))
    embed = discord.Embed(title=f"{member.display_name}'s demons — {sum(counts.values())} total",
                          description="\n".join(lines), color=0xFF1744)
    await inter.response.send_message(embed=embed)


@app_commands.guild_only()
@tree.command(description="Top demon beaters in this server")
async def leaderboard(inter: discord.Interaction):
    rows = DB.execute("SELECT discord_id, COUNT(*) AS n FROM beats GROUP BY discord_id ORDER BY n DESC").fetchall()
    await inter.response.defer()
    lines = []
    for discord_id, n in rows:
        try:
            member = inter.guild.get_member(discord_id) or await inter.guild.fetch_member(discord_id)
        except discord.NotFound:
            continue  # not in this server
        lines.append(f"**{len(lines) + 1}.** {member.display_name} — {n} demons")
        if len(lines) == 10:
            break
    await inter.followup.send(embed=discord.Embed(
        title="🏆 Demon Leaderboard", description="\n".join(lines) or "Nobody yet!", color=0xFFD600))


# ---------- Web server the mod talks to ----------

async def handle_beat(request: web.Request):
    try:
        data = await request.json()
        key = str(data["key"])
        level_id = int(data["levelId"])
        level_name = str(data["levelName"])[:100]
        difficulty = str(data["difficulty"])
        attempts = int(data.get("attempts", 0))
        platformer = bool(data.get("platformer", False))
        gd_name = str(data.get("gdName", ""))[:30]
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad request"}, status=400)
    if difficulty not in DIFFS:
        return web.json_response({"error": "unknown difficulty"}, status=400)

    row = DB.execute("SELECT discord_id FROM players WHERE key = ?", (key,)).fetchone()
    if not row:
        return web.json_response({"error": "unknown key, run /link in Discord"}, status=401)
    discord_id = row[0]

    cur = DB.execute("INSERT OR IGNORE INTO beats VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (discord_id, level_id, level_name, difficulty, attempts, int(platformer), int(time.time())))
    DB.execute("UPDATE players SET gd_name = ? WHERE discord_id = ?", (gd_name, discord_id))
    DB.commit()
    if cur.rowcount == 0:  # already announced this level before
        return web.json_response({"ok": True, "posted": 0})

    name, color, _ = DIFFS[difficulty]
    total = DB.execute("SELECT COUNT(*) FROM beats WHERE discord_id = ?", (discord_id,)).fetchone()[0]
    posted = 0
    for guild_id, channel_id in DB.execute("SELECT guild_id, channel_id FROM channels").fetchall():
        guild = client.get_guild(guild_id)
        channel = guild and guild.get_channel(channel_id)
        if not channel:
            continue
        try:
            member = guild.get_member(discord_id) or await guild.fetch_member(discord_id)
        except discord.NotFound:
            continue  # this player isn't in that server
        embed = discord.Embed(
            title=f"🔥 {member.display_name} beat {level_name}!",
            description=(f"**{name}**{' (Platformer)' if platformer else ''}\n"
                         f"Attempts: {attempts:,}\nLevel ID: {level_id}\n"
                         f"That's demon #{total}!"),
            color=color)
        if gd_name:
            embed.set_footer(text=f"GD: {gd_name}")
        try:
            await channel.send(embed=embed)
            posted += 1
        except discord.HTTPException:
            pass
    return web.json_response({"ok": True, "posted": posted})


async def start_web():
    app = web.Application()
    app.router.add_post("/api/beat", handle_beat)
    app.router.add_get("/", lambda _: web.Response(text="Demon Announcer is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"Web server listening on port {PORT}")


@client.event
async def setup_hook():
    await start_web()
    await tree.sync()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


client.run(TOKEN)
