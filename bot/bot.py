"""Demon Announcer bot: gets demon wins from the Geode mod and posts them in Discord."""
import asyncio
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
# Discord user IDs (comma separated) allowed to use /setchannel, /removedemon and /block
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


def clean_key(key) -> str:
    key = str(key).strip().upper()
    if key.startswith("DA") and not key.startswith("DA-"):
        key = "DA-" + key[2:]  # the dash sometimes gets lost when pasting
    return key


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
@app_commands.describe(channel="The text channel for demon wins")
async def setchannel(inter: discord.Interaction,
                     channel: app_commands.AppCommandChannel):
    if not is_admin(inter):
        await inter.response.send_message("Only the bot's owners can do that.", ephemeral=True)
        return
    real = inter.guild.get_channel(channel.id)
    if not isinstance(real, discord.TextChannel) or not real.permissions_for(inter.guild.me).send_messages:
        await inter.response.send_message(
            f"I can't post in {channel.mention}. Pick a text channel where I can see and send messages "
            "(check the channel's permissions for my role).", ephemeral=True)
        return
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


class PageButtons(discord.ui.View):
    """◀ Back / Next ▶ buttons that flip through pages of a list."""

    def __init__(self, owner_id: int, make_page, pages: int):
        super().__init__(timeout=300)  # buttons stop working after 5 minutes
        self.owner_id, self.make_page, self.pages, self.page = owner_id, make_page, pages, 1
        self.message: discord.Message | None = None
        self.refresh()

    def refresh(self):
        self.back.disabled = self.page <= 1
        self.forward.disabled = self.page >= self.pages

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message("Only the person who used the command can flip the pages.",
                                              ephemeral=True)
            return False
        return True

    async def flip(self, inter: discord.Interaction, step: int):
        self.page = max(1, min(self.page + step, self.pages))
        self.refresh()
        await inter.response.edit_message(embed=self.make_page(self.page), view=self)

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary)
    async def back(self, inter: discord.Interaction, button: discord.ui.Button):
        await self.flip(inter, -1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def forward(self, inter: discord.Interaction, button: discord.ui.Button):
        await self.flip(inter, 1)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@app_commands.guild_only()
@tree.command(description="List every demon someone has beaten, with attempts")
@app_commands.describe(member="Whose demons to show (leave empty for yours)",
                       difficulty="Which demon difficulty to show")
@app_commands.choices(difficulty=[app_commands.Choice(name="All demons", value="all")] +
                      [app_commands.Choice(name=DIFFS[d][0], value=d) for d in ORDER])
async def completions(inter: discord.Interaction, member: discord.Member | None = None,
                      difficulty: app_commands.Choice[str] | None = None):
    member = member or inter.user
    diff = difficulty.value if difficulty else "all"
    rows = DB.execute("SELECT level_name, difficulty, attempts, platformer, ts FROM beats WHERE discord_id = ?",
                      (member.id,)).fetchall()
    if diff != "all":
        rows = [r for r in rows if r[1] == diff]
    rows.sort(key=lambda r: (-ORDER.index(r[1]), -r[4]))  # hardest first, then newest
    label = "demons" if diff == "all" else DIFFS[diff][0] + "s"
    if not rows:
        await inter.response.send_message(f"{member.display_name} hasn't announced any {label} yet.")
        return

    per_page = 20
    pages = (len(rows) + per_page - 1) // per_page
    total_attempts = sum(r[2] for r in rows)

    def make_page(page: int) -> discord.Embed:
        lines = [f"{DIFFS[d][2]} **{escape_markdown(n)}**{' (Platformer)' if p else ''} — "
                 f"{a:,} attempts{f' · <t:{ts}:d>' if ts else ''}"
                 for n, d, a, p, ts in rows[(page - 1) * per_page: page * per_page]]
        embed = discord.Embed(title=f"{member.display_name}'s {label} — {len(rows)} beaten",
                              description="\n".join(lines),
                              color=0xFF1744 if diff == "all" else DIFFS[diff][1])
        footer = f"Total attempts: {total_attempts:,}"
        if pages > 1:
            footer += f" · Page {page}/{pages}"
        embed.set_footer(text=footer)
        return embed

    if pages == 1:
        await inter.response.send_message(embed=make_page(1))
        return
    view = PageButtons(inter.user.id, make_page, pages)
    await inter.response.send_message(embed=make_page(1), view=view)
    view.message = await inter.original_response()


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
        await inter.response.send_message("Only the bot's owners can do that.", ephemeral=True)
        return
    cur = DB.execute("DELETE FROM beats WHERE discord_id = ? AND level_id = ?", (member.id, level_id))
    DB.commit()
    await inter.response.send_message(f"Removed {cur.rowcount} demon(s) from {member.mention}.", ephemeral=True)


@tree.command(description="(Bot admins) Block or unblock someone from announcing demons")
async def block(inter: discord.Interaction, member: discord.User, blocked: bool = True):
    if not is_admin(inter):
        await inter.response.send_message("Only the bot's owners can do that.", ephemeral=True)
        return
    DB.execute("INSERT INTO players (discord_id, blocked) VALUES (?, ?) "
               "ON CONFLICT(discord_id) DO UPDATE SET blocked = excluded.blocked", (member.id, int(blocked)))
    DB.commit()
    await inter.response.send_message(f"{member.mention} is now {'blocked' if blocked else 'unblocked'}.",
                                      ephemeral=True)


# ---------- Checking levels with the real GD servers ----------

GD_URL = "https://www.boomlings.com/database/getGJLevels21.php"
GD_SECRET = "Wmfd2893gb7"


def parse_demon(level_text: str):
    """Turns one level from the GD servers into (id, name, difficulty, platformer), or None if not a rated demon."""
    parts = level_text.split(":")
    info = dict(zip(parts[::2], parts[1::2]))
    if info.get("17") != "1" or info.get("18") != "10" or not info.get("1", "").isdigit():
        return None
    return int(info["1"]), info.get("2", "Unknown"), GD_DEMON_DIFF.get(info.get("43", "0"), "hard"), info.get("15") == "5"


async def lookup_levels(level_ids: list[int]) -> dict[int, tuple]:
    """Asks the GD servers about many levels at once. Returns {id: (name, difficulty, platformer)} for real demons."""
    found = {i: (MAIN_LEVEL_DEMONS[i], "hard", False) for i in level_ids if i in MAIN_LEVEL_DEMONS}
    rest = [i for i in level_ids if i not in MAIN_LEVEL_DEMONS]
    for start in range(0, len(rest), 50):
        chunk = rest[start:start + 50]
        async with http.post(GD_URL, data={"str": ",".join(map(str, chunk)), "type": "10", "secret": GD_SECRET},
                             timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
        if text and not text.startswith("-1"):
            for level_text in text.split("#")[0].split("|"):
                demon = parse_demon(level_text)
                if demon and demon[0] in chunk:
                    found[demon[0]] = demon[1:]
        if start + 50 < len(rest):
            await asyncio.sleep(1)  # be gentle with the GD servers
    return found


async def lookup_level(level_id: int):
    """Returns (name, difficulty, platformer) if the level is a real rated demon, otherwise None."""
    return (await lookup_levels([level_id])).get(level_id)


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
        key = clean_key(data["key"])
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


SYNC_COOLDOWN = 600  # seconds between old-demon syncs for one player
last_sync: dict[int, float] = {}


async def handle_sync(request: web.Request):
    """The mod sends every demon the player already beat before installing it. Saved quietly, no announcements."""
    ip = request.remote or "?"
    if over_limit(hits, ip, IP_LIMIT) or over_limit(bad_keys, ip, BAD_KEY_LIMIT):
        return web.json_response({"error": "slow down"}, status=429)
    hits[ip].append(time.time())
    try:
        data = await request.json()
        key = clean_key(data["key"])
        levels = {int(l["levelId"]): max(0, min(int(l.get("attempts", 0)), 10_000_000))
                  for l in data["levels"][:3000]}
    except (ValueError, KeyError, TypeError, AttributeError):
        return web.json_response({"error": "bad request"}, status=400)

    row = DB.execute("SELECT discord_id, blocked FROM players WHERE key_hash = ?", (hash_key(key),)).fetchone()
    if not row:
        bad_keys[ip].append(time.time())
        return web.json_response({"error": "unknown key, run /link in Discord"}, status=401)
    discord_id, blocked = row
    if blocked:
        return web.json_response({"error": "blocked"}, status=403)
    if time.time() - last_sync.get(discord_id, 0) < SYNC_COOLDOWN:
        return web.json_response({"error": "slow down"}, status=429)
    last_sync[discord_id] = time.time()

    try:
        demons = await lookup_levels(list(levels))
    except (aiohttp.ClientError, TimeoutError):
        return web.json_response({"error": "couldn't check the levels with GD servers"}, status=503)
    imported = 0
    for level_id, (name, difficulty, platformer) in demons.items():
        cur = DB.execute("INSERT OR IGNORE INTO beats VALUES (?, ?, ?, ?, ?, ?, 0)",  # ts 0 = beaten before the bot
                         (discord_id, level_id, name[:100], difficulty, levels[level_id], int(platformer)))
        imported += cur.rowcount
    DB.commit()
    print(f"Synced {imported} old demons for {discord_id}")
    return web.json_response({"ok": True, "imported": imported})


async def start_web():
    app = web.Application(client_max_size=256 * 1024)  # requests bigger than 256 KB are refused
    app.router.add_post("/api/beat", handle_beat)
    app.router.add_post("/api/sync", handle_sync)
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
