import asyncio
import io
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN")

HEART = "<:whiteheartawk:1566486532980740266>"
ROBUX = "<:robuxawk:1568714859678408745>"
COLOR = 0xFFDADA

# Railway sets RAILWAY_VOLUME_MOUNT_PATH when a volume is attached
DATA = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "data"))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "bot.db"
TIMERS_PATH = DATA / "timers.json"


# ───────────────────────── storage ─────────────────────────


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_one(sql, params=()):
    conn = get_db()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def db_all(sql, params=()):
    conn = get_db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def db_run(sql, params=()):
    conn = get_db()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def init_db() -> None:
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            vouch_channel_id TEXT,
            vouch_count_enabled TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vouches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            author_id INTEGER,
            author_name TEXT,
            author_avatar_url TEXT,
            content TEXT,
            created_at TEXT NOT NULL,
            origin_key TEXT,
            UNIQUE(guild_id, message_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vouch_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vouch_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            data BLOB NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vouches_guild ON vouches (guild_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vouch_files_vouch ON vouch_files (vouch_id)")
    conn.commit()
    conn.close()


def get_settings(guild_id: int):
    return db_one("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))


def upsert_settings(guild_id: int, **kwargs) -> None:
    allowed = {"vouch_channel_id", "vouch_count_enabled"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,))
    for key, value in updates.items():
        conn.execute(f"UPDATE settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
    conn.commit()
    conn.close()


def load_timers():
    try:
        return json.loads(TIMERS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_timers():
    TIMERS_PATH.write_text(json.dumps(timers, indent=2), encoding="utf-8")


timers = load_timers()


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        init_db()
        await self.tree.sync()
        timer_loop.start()
        update_vouch_counts.start()


bot = Bot()


def guild_only(interaction: discord.Interaction) -> discord.Guild:
    if interaction.guild is None:
        raise app_commands.CheckFailure("This command only works in a server.")
    return interaction.guild


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "you need Manage Server to use that"
    elif isinstance(error, app_commands.CheckFailure):
        msg = str(error) or "you can't use that here"
    else:
        print(f"command error: {error!r}")
        msg = "something went wrong, check the bot logs"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ───────────────────────── /24 and /48 ─────────────────────────


async def start_fp(interaction: discord.Interaction, hours: int):
    timers.append({
        "user_id": interaction.user.id,
        "channel_id": interaction.channel_id,
        "end": time.time() + hours * 3600,
        "reminded": False,
    })
    save_timers()
    await interaction.response.send_message(
        f"{HEART} : {hours}hrs fast past timer has started - "
        f"Reminder will occur 5 hours before time is up . ."
    )


@bot.tree.command(name="24", description="Start a 24hr fast pass timer")
async def fp24(interaction: discord.Interaction):
    await start_fp(interaction, 24)


@bot.tree.command(name="48", description="Start a 48hr fast pass timer")
async def fp48(interaction: discord.Interaction):
    await start_fp(interaction, 48)


@tasks.loop(seconds=20)
async def timer_loop():
    now = time.time()
    changed = False
    for t in timers[:]:
        channel = bot.get_channel(t["channel_id"])
        if channel is None:
            continue
        mention = f"<@{t['user_id']}>"
        if not t["reminded"] and now >= t["end"] - 5 * 3600:
            t["reminded"] = True
            changed = True
            if now < t["end"]:
                await channel.send(f"{HEART} Fast Pass Timer : 5 hours left! {mention}")
        if now >= t["end"]:
            await channel.send(f"{HEART} Fast Pass Timer : Time is up! {mention}")
            timers.remove(t)
            changed = True
    if changed:
        save_timers()


# ───────────────────────── vouch restorer ─────────────────────────

VOUCH_MAX_FILE_BYTES = 8 * 1024 * 1024
VOUCH_MAX_FILES = 10
VOUCH_COUNT_CHECK_SECONDS = 30
VOUCH_RENAME_COOLDOWN_SECONDS = 330  # Discord allows ~2 channel renames per 10 minutes
VOUCH_RESTORE_DELAY_SECONDS = 0.5

_vouch_count_dirty: set[int] = set()
_vouch_last_rename: dict[int, float] = {}
_vouch_busy: set[int] = set()
_avatar_cache: dict[int, str | None] = {}


def vouch_total(guild_id: int) -> int:
    row = db_one("SELECT COUNT(*) AS n FROM vouches WHERE guild_id = ?", (guild_id,))
    return row["n"] if row else 0


def request_vouch_count_update(guild_id: int) -> None:
    _vouch_count_dirty.add(guild_id)


def build_counted_channel_name(name: str, count: int) -> str:
    # swaps the first number in the channel name for the new count (adds one if there isn't any)
    if re.search(r"\d+", name):
        new_name = re.sub(r"\d+", str(count), name, count=1)
    else:
        new_name = f"{name}-{count}"
    return new_name[:100]


def save_vouch(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    author_id: int | None,
    author_name: str | None,
    author_avatar_url: str | None,
    content: str | None,
    created_at: str,
    files: list[tuple[str, bytes]],
    origin_key: str | None = None,
) -> bool:
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO vouches (guild_id, channel_id, message_id, author_id, author_name, author_avatar_url, content, created_at, origin_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, message_id, author_id, author_name, author_avatar_url, content, created_at, origin_key),
        )
        if cur.rowcount == 0:
            return False
        vouch_id = cur.lastrowid
        for filename, data in files:
            conn.execute("INSERT INTO vouch_files (vouch_id, filename, data) VALUES (?, ?, ?)", (vouch_id, filename, data))
        conn.commit()
        return True
    finally:
        conn.close()


async def store_vouch_message(message: discord.Message) -> bool:
    if message.guild is None or message.author.bot:
        return False
    if db_one("SELECT 1 FROM vouches WHERE guild_id = ? AND message_id = ?", (message.guild.id, message.id)):
        return False

    files: list[tuple[str, bytes]] = []
    for att in message.attachments[:VOUCH_MAX_FILES]:
        if att.size > VOUCH_MAX_FILE_BYTES:
            print(f"Vouch {message.id}: skipping attachment {att.filename} (too big to back up)")
            continue
        try:
            files.append((att.filename, await att.read()))
        except Exception as e:
            print(f"Vouch {message.id}: couldn't download attachment {att.filename}: {e!r}")

    content = message.content or ""
    if not content.strip() and not files:
        return False

    return save_vouch(
        guild_id=message.guild.id,
        channel_id=message.channel.id,
        message_id=message.id,
        author_id=message.author.id,
        author_name=message.author.display_name,
        author_avatar_url=message.author.display_avatar.url,
        content=content,
        created_at=message.created_at.isoformat(),
        files=files,
    )


def safe_webhook_name(name: str | None) -> str:
    name = (name or "").strip() or "unknown"
    # webhooks can't have "discord" or "clyde" in the name, so break those words up invisibly
    name = re.sub(r"(?i)(discord|clyde)", lambda m: m.group(0)[0] + "​" + m.group(0)[1:], name)
    return name[:80]


async def current_avatar_url(author_id, fallback):
    if not author_id:
        return fallback
    if author_id not in _avatar_cache:
        try:
            user = await bot.fetch_user(author_id)
            _avatar_cache[author_id] = user.display_avatar.url
        except discord.HTTPException:
            _avatar_cache[author_id] = None
    return _avatar_cache[author_id] or fallback


async def post_restored_vouch(channel, webhook, row, files: list[tuple[str, bytes]]):
    content = (row["content"] or "")[:2000]
    name = row["author_name"] or "unknown"
    avatar = await current_avatar_url(row["author_id"], row["author_avatar_url"]) or None
    no_mentions = discord.AllowedMentions.none()

    def make_files():
        return [discord.File(io.BytesIO(data), filename=fn) for fn, data in files]

    if webhook is not None:
        for with_files in (True, False):
            if not with_files and not files:
                break
            kwargs = dict(username=safe_webhook_name(name), allowed_mentions=no_mentions, wait=True)
            if avatar:
                kwargs["avatar_url"] = avatar
            if content:
                kwargs["content"] = content
            elif not (with_files and files):
                kwargs["content"] = "*(attachment unavailable)*"
            if with_files and files:
                kwargs["files"] = make_files()
            try:
                return await webhook.send(**kwargs)
            except discord.HTTPException as e:
                print(f"Vouch restore: webhook send failed ({'with' if with_files else 'without'} files): {e!r}")

    # no webhook (or it failed): fall back to an embed with the author's name/avatar on top
    embed = discord.Embed(description=content or "*(attachment unavailable)*", color=COLOR)
    embed.set_author(name=name[:256], icon_url=avatar)
    for with_files in (True, False):
        if not with_files and not files:
            break
        try:
            if with_files and files:
                return await channel.send(embed=embed, files=make_files(), allowed_mentions=no_mentions)
            return await channel.send(embed=embed, allowed_mentions=no_mentions)
        except discord.HTTPException as e:
            print(f"Vouch restore: embed fallback failed ({'with' if with_files else 'without'} files): {e!r}")
    return None


async def run_vouch_restore(interaction: discord.Interaction, source_gid: int, channel: discord.TextChannel) -> None:
    guild = interaction.guild
    _vouch_busy.add(guild.id)
    webhook = None
    posted = failed = skipped = 0
    try:
        _avatar_cache.clear()
        rows = db_all("SELECT * FROM vouches WHERE guild_id = ? ORDER BY created_at ASC, id ASC", (source_gid,))

        # purge restored copies whose channel was deleted
        conn = get_db()
        try:
            for r in conn.execute("SELECT id, channel_id FROM vouches WHERE guild_id = ? AND origin_key IS NOT NULL", (guild.id,)).fetchall():
                if guild.get_channel(r["channel_id"]) is None:
                    conn.execute("DELETE FROM vouch_files WHERE vouch_id = ?", (r["id"],))
                    conn.execute("DELETE FROM vouches WHERE id = ?", (r["id"],))
            conn.commit()
        finally:
            conn.close()

        # only skip ones already restored INTO THIS channel
        done_keys = {r["origin_key"] for r in db_all(
            "SELECT origin_key FROM vouches WHERE guild_id = ? AND channel_id = ? AND origin_key IS NOT NULL",
            (guild.id, channel.id))}
        todo = [r for r in rows if f"{source_gid}:{r['message_id']}" not in done_keys]
        skipped = len(rows) - len(todo)
        total = len(todo)

        if channel.permissions_for(guild.me).manage_webhooks:
            try:
                webhook = await channel.create_webhook(name="vouch restorer", reason=f"Vouch restore by {interaction.user}")
            except discord.HTTPException as e:
                print(f"Vouch restore: couldn't create webhook: {e!r}")

        for i, row in enumerate(todo, start=1):
            files = [(f["filename"], bytes(f["data"])) for f in db_all("SELECT filename, data FROM vouch_files WHERE vouch_id = ? ORDER BY id", (row["id"],))]
            msg = await post_restored_vouch(channel, webhook, row, files)
            if msg is None:
                failed += 1
            else:
                posted += 1
                save_vouch(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    message_id=msg.id,
                    author_id=row["author_id"],
                    author_name=row["author_name"],
                    author_avatar_url=row["author_avatar_url"],
                    content=row["content"],
                    created_at=row["created_at"],
                    files=files,
                    origin_key=f"{source_gid}:{row['message_id']}",
                )

            if i % 20 == 0:
                try:
                    await interaction.edit_original_response(content=f"Restoring vouches... **{i}/{total}**")
                except discord.HTTPException:
                    pass
            await asyncio.sleep(VOUCH_RESTORE_DELAY_SECONDS)
    except Exception as e:
        print(f"Vouch restore crashed: {e!r}")
        failed += 1
    finally:
        if webhook is not None:
            try:
                await webhook.delete(reason="Vouch restore finished")
            except discord.HTTPException:
                pass
        _vouch_busy.discard(guild.id)

    settings = get_settings(guild.id)
    if settings and settings["vouch_count_enabled"] == "1":
        request_vouch_count_update(guild.id)

    summary = f"{HEART} Restored **{posted}** vouch{'es' if posted != 1 else ''} into {channel.mention}."
    if skipped:
        summary += f"\nSkipped {skipped} that were already restored earlier."
    if failed:
        summary += f"\n{failed} couldn't be posted (check the bot logs). Running `/vouch_restore` again will pick up only the missing ones."
    try:
        await interaction.edit_original_response(content=summary)
    except discord.HTTPException:
        try:
            await channel.send(summary, delete_after=60)
        except discord.HTTPException:
            pass


class VouchRestoreConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, source_gid: int, channel: discord.TextChannel):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.source_gid = source_gid
        self.channel = channel

    @discord.ui.button(label="Restore vouches", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return
        if interaction.guild.id in _vouch_busy:
            await interaction.response.edit_message(content="A vouch backup/restore is already running here, wait for it to finish.", view=None)
            return
        await interaction.response.edit_message(content="Starting restore...", view=None)
        await run_vouch_restore(interaction, self.source_gid, self.channel)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Cancelled, nothing was posted.", view=None)


@tasks.loop(seconds=VOUCH_COUNT_CHECK_SECONDS)
async def update_vouch_counts():
    for gid in list(_vouch_count_dirty):
        if time.monotonic() - _vouch_last_rename.get(gid, 0) < VOUCH_RENAME_COOLDOWN_SECONDS:
            continue

        settings = get_settings(gid)
        guild = bot.get_guild(gid)
        if not guild or not settings or settings["vouch_count_enabled"] != "1" or not settings["vouch_channel_id"]:
            _vouch_count_dirty.discard(gid)
            continue

        channel = guild.get_channel(int(settings["vouch_channel_id"]))
        if channel is None:
            _vouch_count_dirty.discard(gid)
            continue

        _vouch_count_dirty.discard(gid)
        new_name = build_counted_channel_name(channel.name, vouch_total(gid))
        if new_name == channel.name:
            continue
        try:
            await channel.edit(name=new_name, reason="Vouch count updated")
            _vouch_last_rename[gid] = time.monotonic()
        except discord.Forbidden:
            print(f"Vouch count: missing Manage Channels for #{channel.name} in guild {gid}")
        except discord.HTTPException as e:
            print(f"Vouch count: couldn't rename #{channel.name} in guild {gid}: {e!r}")


@update_vouch_counts.before_loop
async def before_update_vouch_counts():
    await bot.wait_until_ready()


@bot.event
async def on_message(message: discord.Message):
    # live-save new vouches so they can be restored if the server ever gets deleted
    if message.guild is None or message.author.bot:
        return
    settings = get_settings(message.guild.id)
    if settings and settings["vouch_channel_id"] and message.channel.id == int(settings["vouch_channel_id"]):
        try:
            if await store_vouch_message(message) and settings["vouch_count_enabled"] == "1":
                request_vouch_count_update(message.guild.id)
        except Exception as e:
            print(f"Failed to back up vouch {message.id}: {e!r}")


@bot.event
async def on_guild_channel_delete(channel):
    # clean up restored vouch copies that lived in the deleted channel
    try:
        db_run("DELETE FROM vouch_files WHERE vouch_id IN (SELECT id FROM vouches WHERE guild_id = ? AND channel_id = ? AND origin_key IS NOT NULL)", (channel.guild.id, channel.id))
        db_run("DELETE FROM vouches WHERE guild_id = ? AND channel_id = ? AND origin_key IS NOT NULL", (channel.guild.id, channel.id))
    except Exception as e:
        print(f"Failed to clean up restored vouches for deleted channel {channel.id}: {e!r}")


@bot.tree.command(name="vouch_setup", description="Set your vouch channel (new vouches get backed up automatically)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="Channel where vouches get posted")
async def vouch_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    guild = guild_only(interaction)
    upsert_settings(guild.id, vouch_channel_id=str(channel.id))
    await interaction.response.send_message(
        f"{HEART} vouch channel set to {channel.mention}. Vouches posted there are backed up automatically, "
        f"run `/vouch_backup` once to save the ones that are already there.",
        ephemeral=True,
    )


@bot.tree.command(name="vouch_backup", description="Save every vouch already in the vouch channel to the backup")
@app_commands.checks.has_permissions(manage_guild=True)
async def vouch_backup(interaction: discord.Interaction):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    cid = settings["vouch_channel_id"] if settings else None
    found = guild.get_channel(int(cid)) if cid else None
    channel = found if isinstance(found, discord.TextChannel) else None
    if channel is None:
        await interaction.response.send_message("Run `/vouch_setup` first so I know which one is your vouch channel.", ephemeral=True)
        return
    if guild.id in _vouch_busy:
        await interaction.response.send_message("A vouch backup/restore is already running here, wait for it to finish.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    _vouch_busy.add(guild.id)
    scanned = new = 0
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author.bot:
                continue
            scanned += 1
            if await store_vouch_message(msg):
                new += 1
    except discord.Forbidden:
        await interaction.followup.send(f"I can't read the history of {channel.mention}, I need View Channel + Read Message History there.", ephemeral=True)
        return
    except Exception as e:
        print(f"Vouch backup crashed: {e!r}")
        await interaction.followup.send("The scan stopped early because of an error (it's been logged). Whatever was saved so far is kept, run it again to continue.", ephemeral=True)
        return
    finally:
        _vouch_busy.discard(guild.id)

    if settings and settings["vouch_count_enabled"] == "1":
        request_vouch_count_update(guild.id)

    await interaction.followup.send(
        f"{HEART} Scanned **{scanned}** message{'s' if scanned != 1 else ''} in {channel.mention} and saved **{new}** new vouch{'es' if new != 1 else ''}.\n"
        f"Total vouches in the backup: **{vouch_total(guild.id)}**",
        ephemeral=True,
    )


async def vouch_source_autocomplete(interaction: discord.Interaction, current: str):
    rows = db_all("SELECT guild_id, COUNT(*) AS n FROM vouches GROUP BY guild_id ORDER BY n DESC")
    choices = []
    for r in rows:
        if interaction.guild_id and r["guild_id"] == interaction.guild_id:
            continue
        g = bot.get_guild(r["guild_id"])
        label = f"{g.name if g else 'old server'} ({r['guild_id']}) - {r['n']} vouches"
        if current and current.lower() not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=str(r["guild_id"])))
    return choices[:25]


@bot.tree.command(name="vouch_restore", description="Re-post the backed-up vouches from your old server into a channel here")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    source="Which backup to restore (pick from the list, or paste the old server's ID)",
    channel="Channel to post the restored vouches in (becomes your vouch channel if none is set)",
)
@app_commands.autocomplete(source=vouch_source_autocomplete)
async def vouch_restore(interaction: discord.Interaction, source: str, channel: discord.TextChannel):
    guild = guild_only(interaction)
    try:
        source_gid = int(source.strip())
    except ValueError:
        await interaction.response.send_message("Pick a backup from the list or paste the old server's ID (numbers only).", ephemeral=True)
        return
    if source_gid == guild.id:
        await interaction.response.send_message("That's this server, pick the backup from your old one.", ephemeral=True)
        return
    if guild.id in _vouch_busy:
        await interaction.response.send_message("A vouch backup/restore is already running here, wait for it to finish.", ephemeral=True)
        return

    row = db_one("SELECT COUNT(*) AS n FROM vouches WHERE guild_id = ?", (source_gid,))
    total = row["n"] if row else 0
    if total == 0:
        await interaction.response.send_message(f"I don't have any vouches backed up for server `{source_gid}`. Pick one from the list when you type the `source` option.", ephemeral=True)
        return

    perms = channel.permissions_for(guild.me)
    if not perms.send_messages:
        await interaction.response.send_message(f"I can't send messages in {channel.mention}.", ephemeral=True)
        return

    settings = get_settings(guild.id)
    if not settings or not settings["vouch_channel_id"]:
        upsert_settings(guild.id, vouch_channel_id=str(channel.id))

    note = ""
    if not perms.manage_webhooks:
        note = "\n*I don't have Manage Webhooks in that channel, so vouches will post as embeds with the name/avatar on top instead of looking like the original messages.*"

    await interaction.response.send_message(
        f"Restore **{total}** vouch{'es' if total != 1 else ''} from server `{source_gid}` into {channel.mention}?\n"
        f"They'll post in their original order, one at a time (about {max(1, round(total * VOUCH_RESTORE_DELAY_SECONDS / 60))} min).{note}",
        view=VouchRestoreConfirmView(interaction.user.id, source_gid, channel),
        ephemeral=True,
    )


@bot.tree.command(name="vouch_count", description="Auto-update the number in your vouch channel's name as vouches come in")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(enabled="Turn the automatic channel-name count on or off")
async def vouch_count(interaction: discord.Interaction, enabled: bool = True):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    cid = settings["vouch_channel_id"] if settings else None
    channel = guild.get_channel(int(cid)) if cid else None
    if channel is None:
        await interaction.response.send_message("Set your vouch channel first with `/vouch_setup`.", ephemeral=True)
        return

    upsert_settings(guild.id, vouch_count_enabled="1" if enabled else "0")
    if not enabled:
        await interaction.response.send_message(f"{HEART} Auto vouch count is off.", ephemeral=True)
        return

    request_vouch_count_update(guild.id)
    total = vouch_total(guild.id)
    preview = build_counted_channel_name(channel.name, total)
    await interaction.response.send_message(
        f"{HEART} Auto vouch count is on. {channel.mention} will be renamed to `{preview}` and the number updates as new vouches come in (Discord only lets channels be renamed about twice every 10 minutes, so it can lag a few minutes).\n"
        f"The number in the name gets replaced with the backup total (**{total}**). If you've never run `/vouch_backup`, do that first so the count is right.",
        ephemeral=True,
    )


@bot.tree.command(name="vouch_remove", description="Remove one vouch from the backup (deleted vouches are NOT removed automatically)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(message="The vouch message's link or ID")
async def vouch_remove(interaction: discord.Interaction, message: str):
    guild = guild_only(interaction)
    ids = re.findall(r"\d+", message)
    if not ids:
        await interaction.response.send_message("Paste the vouch's message link or message ID.", ephemeral=True)
        return

    message_id = int(ids[-1])
    row = db_one("SELECT id FROM vouches WHERE guild_id = ? AND message_id = ?", (guild.id, message_id))
    if not row:
        await interaction.response.send_message("I don't have a vouch backed up with that message ID.", ephemeral=True)
        return

    conn = get_db()
    conn.execute("DELETE FROM vouch_files WHERE vouch_id = ?", (row["id"],))
    conn.execute("DELETE FROM vouches WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()

    settings = get_settings(guild.id)
    if settings and settings["vouch_count_enabled"] == "1":
        request_vouch_count_update(guild.id)
    await interaction.response.send_message(f"{HEART} Removed that vouch from the backup. (The message itself, if it still exists, wasn't touched.)", ephemeral=True)


# ───────────────────────── /tax ─────────────────────────


def parse_amount(s: str) -> int:
    s = s.lower().replace(",", "").strip()
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    return int(float(s) * mult)


@bot.tree.command(name="tax", description="Calculate robux tax")
@app_commands.describe(amount="Amount, like 1000 or 1k")
async def tax(interaction: discord.Interaction, amount: str):
    try:
        initial = parse_amount(amount)
    except ValueError:
        return await interaction.response.send_message("put a number, like 1000", ephemeral=True)
    if initial <= 0:
        return await interaction.response.send_message("put a number above 0", ephemeral=True)
    after_tax = math.floor(initial * 0.7)
    cover_tax = math.ceil(initial / 0.7)
    embed = discord.Embed(
        description=(
            f"♡ : Initial Amount\n## {ROBUX} {initial:,}\n\n"
            f"♡ : After Tax (30%)\n## {ROBUX} {after_tax:,}\n\n"
            f"♡ : Cost to Cover Tax\n## {ROBUX} {cover_tax:,}"
        ),
        color=COLOR,
    )
    await interaction.response.send_message(embed=embed)


# ───────────────────────── /showcase ─────────────────────────
# basic placeholder, send me your old one and I'll match it


@bot.tree.command(name="showcase", description="Post a showcase")
@app_commands.describe(text="Description", image="Image to show")
async def showcase(
    interaction: discord.Interaction,
    text: str = None,
    image: discord.Attachment = None,
):
    if not text and not image:
        return await interaction.response.send_message(
            "add some text or an image", ephemeral=True
        )
    embed = discord.Embed(description=text, color=COLOR)
    embed.set_author(
        name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
    )
    if image:
        embed.set_image(url=image.url)
    # reply privately, then post the embed as the bot so no "used /showcase" line shows
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.channel.send(embed=embed)
    except discord.HTTPException as e:
        return await interaction.followup.send(
            f"couldn't post here, make sure I have View Channel, Send Messages and "
            f"Embed Links in this channel ({e.status})",
            ephemeral=True,
        )
    await interaction.delete_original_response()


@bot.event
async def on_ready():
    for row in db_all("SELECT guild_id FROM settings WHERE vouch_count_enabled = '1'"):
        request_vouch_count_update(row["guild_id"])
    print(f"logged in as {bot.user}")


bot.run(TOKEN)