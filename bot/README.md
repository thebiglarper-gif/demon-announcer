# Demon Announcer Bot

## 1. Make the bot in Discord
1. Go to https://discord.com/developers/applications and click **New Application**. Name it "Demon Announcer".
2. Click **Bot** on the left, then **Reset Token**, and copy the token. Keep it secret!
3. Click **OAuth2** > **URL Generator**. Tick **bot** and **applications.commands**.
   Under bot permissions, tick **Send Messages** and **Embed Links**.
4. Open the link at the bottom and add the bot to your server.

## 2. Run the bot
You need Python 3.10 or newer.

```
pip install -r requirements.txt
```

Windows (PowerShell):
```
$env:DISCORD_TOKEN="paste-your-token-here"
python bot.py
```

Optional: set `BOT_ADMINS` to your Discord user ID (several IDs separated by commas)
so you can use `/removedemon` and `/block` to clean up fake wins:
```
$env:BOT_ADMINS="123456789012345678"
```

The bot also starts a small web server on port 8080 (change it with the `PORT` setting).
The mod sends demon wins there, so the bot needs a web address the mod can reach,
for example from a hosting service that gives your app a public URL.

## 3. Use it
- `/setchannel #channel`: where wins get posted (admins only)
- `/link`: get your key for the mod (only you can see it)
- `/demons [@someone]`: demon counts by difficulty
- `/completions [@someone] [difficulty] [page]`: every demon beaten, with attempts and date (pick All or one difficulty)
- `/leaderboard`: top 10 in the server
- `/removedemon @someone level_id`: delete a fake win (bot admins only)
- `/block @someone`: stop someone from announcing (bot admins only)

Demons a player beat before installing the mod are added automatically the first time GD opens
(no announcements for those; they show as "before the bot" in `/completions`).

## Security
- Never share the bot token or put it in GitHub. If it leaks, click **Reset Token** right away.
- Turn on two-factor login (2FA) for the Discord account that owns the bot.
- The bot checks every win with the real GD servers, so the level name and difficulty can't be faked.
- Player keys are stored scrambled (hashed), wrong keys and spam get blocked for a while,
  and the bot's messages can never ping @everyone, @here, roles or people.
