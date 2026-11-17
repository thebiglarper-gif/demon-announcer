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

The bot also starts a small web server on port 8080 (change it with the `PORT` setting).
The mod sends demon wins there, so the bot needs a web address the mod can reach,
for example from a hosting service that gives your app a public URL.

## 3. Use it
- `/setchannel #channel`: where wins get posted (admins only)
- `/link`: get your key for the mod (only you can see it)
- `/demons [@someone]`: demon counts by difficulty
- `/leaderboard`: top 10 in the server
