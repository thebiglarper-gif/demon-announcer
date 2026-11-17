"""Demon Announcer bot: gets demon wins from the Geode mod and posts them in Discord."""
import hashlib
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.utils import escape_markdown

TOKEN = os.environ["DISCORD_TOKEN"]
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")  # 127.0.0.1 = only this PC can reach the bot
# Discord user IDs (comma separated) allowed to use /removedemon and /block
ADMINS = {int(x) for x in os.environ.get("BOT_ADMINS", "").split(",") if x.strip()}

DB = sqlite3.connect(os.environ.get("DB_PATH", "demons.db"))
DB.executescript("""
CREATE TABLE IF NOT EXISTS players (discord_id INTEGER PRIMARY KEY, key_hash TEXT UNIQUE,
                                    gd_name TEXT, blocked INTEGER DEFAULT 0, last_post INTEGER DEFAULT 0);
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
GD_DEMON_DIFF = {"3": "easy", "4": "medium", "0": "hard", "5": "insane", "6": "extreme"}
MAIN_LEVEL_DEMONS = {14: "Clubstep", 18: "Theory of Everything 2", 20: "Deadlocked"}  # not on GD servers

POST_COOLDOWN = 30          # seconds between announcements for one player
IP_LIMIT = (20, 60)         # max 20 requests per 60 s from one address
BAD_KEY_LIMIT = (5, 600)    # max 5 wrong keys per 10 min from one address

# Never let a message ping @everyone, @here, roles or users
client = discord.Client(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
tree = app_commands.CommandTree(client)
http: aiohttp.ClientSession | None = None


def hash_key(key: str) -> str:
    """Keys are only stored hashed, so a leaked database doesn't leak working keys."""
    return hashlib.sha256(key.encode()).hexdigest()


def is_admin(inter: discord.Interaction) -> bool:
    return inter.user.id in ADMINS


# ---------- Discord commands ----------

@tree.command(description="Get your key to paste into the Demon Announcer mod")
async def link(inter: discord.Interaction):
    key = "DA-" + secrets.token_hex(16).upper()
    DB.execute("INSERT INTO players (discord_id, key_hash) VALUES (?, ?) "
               "ON CONFLICT(discord_id) DO UPDATE SET key_hash = excluded.key_hash",
               (inter.user.id, hash_key(key)))
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
    counts = dict(DB.execute("SELECT difficulty, COUNT(*) FROM beats WHERE discord_id = ? GROUP BY difficulty",
                             (member.id,)).fetchall())
    if not counts:
        await inter.response.send_message(f"{member.display_name} hasn't announced any demons yet.")
        return
    lines = [f"{DIFFS[d][2]} {DIFFS[d][0]}s: **{counts.get(d, 0)}**" for d in ORDER]
    recent = DB.execute("SELECT level_name, difficulty FROM beats WHERE discord_id = ? ORDER BY ts DESC LIMIT 3",
                        (member.id,)).fetchall()
    if recent:
        lines.append("\nLatest: " + ", ".join(f"{escape_markdown(n)} ({DIFFS[d][0]})" for n, d in recent))
    embed = discord.Embed(title=f"{member.display_name}'s demons — {sum(counts.values())} total",
                          description="\n".join(lines), color=0xFF1744)
    await inter.response.send_message(embed=embed)


@app_commands.guild_only()
@tree.command(description="Top demon beaters in this server")
async def leaderboard(inter: discord.Interaction):
    rows = DB.execute("SELECT b.discord_id, COUNT(*) AS n FROM beats b JOIN players p USING (discord_id) "
                      "WHERE p.blocked = 0 GROUP BY b.discord_id ORDER BY n DESC").fetchall()
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


@tree.command(description="(Bot admins) Remove a fake demon from someone's record")
async def removedemon(inter: discord.Interaction, member: discord.User, level_id: int):
    if not is_admin(inter):
        await inter.response.send_message("Only bot admins can do that.", ephemeral=True)
        return
    cur = DB.execute("DELETE FROM beats WHERE discord_id = ? AND level_id = ?", (member.id, level_id))
    DB.commit()
    await inter.response.send_message(f"Removed {cur.rowcount} demon(s) from {member.mention}.", ephemeral=True)


@tree.command(description="(Bot admins) Block or unblock someone from announcing demons")
async def block(inter: discord.Interaction, member: discord.User, blocked: bool = True):
    if not is_admin(inter):
        await inter.response.send_message("Only bot admins can do that.", ephemeral=True)
        return
    DB.execute("INSERT INTO players (discord_id, blocked) VALUES (?, ?) "
               "ON CONFLICT(discord_id) DO UPDATE SET blocked = excluded.blocked", (member.id, int(blocked)))
    DB.commit()
    await inter.response.send_message(f"{member.mention} is now {'blocked' if blocked else 'unblocked'}.",
                                      ephemeral=True)


# ---------- Checking levels with the real GD servers ----------

async def lookup_level(level_id: int):
    """Returns (name, difficulty, platformer) if the level is a real rated demon, otherwise None."""
    if level_id in MAIN_LEVEL_DEMONS:
        return MAIN_LEVEL_DEMONS[level_id], "hard", False
    async with http.post("https://www.boomlings.com/database/getGJLevels21.php",
                         data={"str": str(level_id), "type": "0", "secret": "Wmfd2893gb7"},
                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
        text = await resp.text()
    if not text or text.startswith("-1"):
        return None
    parts = text.split("#")[0].split("|")[0].split(":")
    info = dict(zip(parts[::2], parts[1::2]))
    if info.get("1") != str(level_id) or info.get("17") != "1" or info.get("18") != "10":
        return None  # not a rated demon
    return info.get("2", "Unknown"), GD_DEMON_DIFF.get(info.get("43", "0"), "hard"), info.get("15") == "5"


# ---------- Web server the mod talks to ----------

hits: dict[str, deque] = defaultdict(deque)
bad_keys: dict[str, deque] = defaultdict(deque)


def over_limit(bucket: dict, ip: str, limit: tuple[int, int]) -> bool:
    now, (count, window) = time.time(), limit
    q = bucket[ip]
    while q and q[0] < now - window:
        q.popleft()
    return len(q) >= count


async def handle_beat(request: web.Request):
    ip = request.remote or "?"
    if over_limit(hits, ip, IP_LIMIT) or over_limit(bad_keys, ip, BAD_KEY_LIMIT):
        return web.json_response({"error": "slow down"}, status=429)
    hits[ip].append(time.time())

    try:
        data = await request.json()
        key = str(data["key"]).strip().upper()
        if key.startswith("DA") and not key.startswith("DA-"):
            key = "DA-" + key[2:]  # the dash sometimes gets lost when pasting
        level_id = int(data["levelId"])
        attempts = max(0, min(int(data.get("attempts", 0)), 10_000_000))
        gd_name = escape_markdown(str(data.get("gdName", ""))[:30])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad request"}, status=400)

    row = DB.execute("SELECT discord_id, blocked, last_post FROM players WHERE key_hash = ?",
                     (hash_key(key),)).fetchone()
    if not row:
        bad_keys[ip].append(time.time())
        return web.json_response({"error": "unknown key, run /link in Discord"}, status=401)
    discord_id, blocked, last_post = row
    if blocked:
        return web.json_response({"error": "blocked"}, status=403)
    if time.time() - last_post < POST_COOLDOWN:
        return web.json_response({"error": "slow down"}, status=429)

    # Don't trust the level name or difficulty from the mod: ask the GD servers
    try:
        level = await lookup_level(level_id)
    except (aiohttp.ClientError, TimeoutError):
        return web.json_response({"error": "couldn't check the level with GD servers"}, status=503)
    if not level:
        return web.json_response({"error": "that level isn't a rated demon"}, status=400)
    level_name, difficulty, platformer = level
    level_name = level_name[:100]

    cur = DB.execute("INSERT OR IGNORE INTO beats VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (discord_id, level_id, level_name, difficulty, attempts, int(platformer), int(time.time())))
    DB.execute("UPDATE players SET gd_name = ?, last_post = ? WHERE discord_id = ?",
               (gd_name, int(time.time()), discord_id))
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
            title=f"🔥 {member.display_name} beat {escape_markdown(level_name)}!",
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
    app = web.Application(client_max_size=4096)  # requests bigger than 4 KB are refused
    app.router.add_post("/api/beat", handle_beat)
    app.router.add_get("/", lambda _: web.Response(text="Demon Announcer is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    print(f"Web server listening on http://{HOST}:{PORT}")


@client.event
async def setup_hook():
    global http
    http = aiohttp.ClientSession(skip_auto_headers=["User-Agent"])  # GD servers block most user agents
    await start_web()
    await tree.sync()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


client.run(TOKEN)
