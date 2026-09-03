# Telegram Multi-User File Store Bot

A compact Telegram file store bot built with Python 3.11+, `python-telegram-bot`,
and SQLite. Telegram keeps the uploaded bytes; the bot stores Telegram message/file
references and metadata locally, so it does not unnecessarily download files.

## Features

- Upload videos, documents, audio, photos, animations, and voice messages.
- Secure random deep links such as `https://t.me/YourBot?start=file_TOKEN`.
- Users can view, disable, enable, and delete only their own files.
- Up to six personal Force-Subscribe channels per user.
- Up to six global Force-Subscribe channels managed by the owner.
- Real `getChatMember` checks before every file delivery. Pending join requests
  do not count as membership.
- Owner panel with statistics, user management, upload/file search, blocking,
  broadcast, channel management, settings, and maintenance mode.
- SQLite database created automatically.
- Telegram polling plus a lightweight HTTP health endpoint for Render.

## 1. Create the bot with BotFather

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, choose a display name and username, and copy the token.
3. Never commit or print the token.

To find your Telegram owner ID, send a message to [@userinfobot](https://t.me/userinfobot)
or another trusted ID utility and copy the numeric ID.

## 2. Configure environment variables

Required:

- `BOT_TOKEN`: the token from BotFather.
- `OWNER_ID`: your numeric Telegram user ID.

Optional:

- `DB_PATH`: SQLite file path; defaults to `filestore.db`.
- `PORT`: health server port; defaults to `8080`.

Copy `.env.example` for reference. Do not commit a real `.env` file.

### Replit

Add `BOT_TOKEN` and `OWNER_ID` in the Replit Secrets/environment variables panel.
Install and run:

```bash
pip install -r requirements.txt
python main.py
```

The bot uses polling, so no webhook URL is needed. Keep the Repl running while
you want the bot online. `filestore.db` is created in the project directory.

### Render

Create a Render **Web Service**, connect this project, and set:

```text
Build Command: pip install -r requirements.txt
Start Command: python main.py
```

Add `BOT_TOKEN` and `OWNER_ID` as Render environment variables. Render supplies
`PORT`; the bot starts an HTTP health server on `0.0.0.0:$PORT` while polling
Telegram. Use `/health` as the health-check path if you configure one.

SQLite is local to the service. Use a persistent disk if your Render plan and
deployment setup provide one; otherwise the database can be reset when the
service is recreated.

## 3. Configure Force-Subscribe channels

For every channel used for Force-Subscribe:

1. Add the bot to the channel.
2. Promote the bot to administrator. It needs permission to read membership.
3. In the bot, open **My Channels** (personal) or **Owner Panel → Global Force
   Sub** (global).
4. Send a public `@channelusername` or numeric chat ID.

For private channels, send:

```text
-1001234567890|https://t.me/+your_invite_link
```

Only configured channels are shown to users. A disabled channel is not checked.
Telegram membership is checked with `getChatMember` each time; pressing the
check button by itself never unlocks a file.

## 4. Use the bot

1. Send `/start`.
2. Send a supported file directly, or press **Upload File**.
3. Share the generated link.
4. A visitor must join every required channel and press **Check Membership**.
5. Once verified, the bot copies the original Telegram message/file to them.

The owner can open **Owner Panel** from `/start` and manage users, uploads,
channels, broadcasts, statistics, and maintenance mode. Normal users never see
the owner upload section and all file changes are checked against ownership.

## Database tables

The first run creates `users`, `files`, `user_channels`, `global_channels`,
`settings`, `statistics`, and `blocked_users` in SQLite.

## Troubleshooting

- If startup prints the setup message, add both required environment variables.
- If a channel cannot be added, confirm the ID is correct and the bot is an
  administrator in that channel.
- If a visitor remains locked, they must join every enabled global and file
  owner's enabled personal channel. A pending join request is intentionally not
  treated as membership.
- If a source message was deleted, the bot attempts to send the stored Telegram
  `file_id`; if Telegram has also invalidated that reference, the file must be
  uploaded again.