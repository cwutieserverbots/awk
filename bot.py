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
    for col in ("showcase_text", "showcase_image2_url", "showcase_image3_url", "queue_channel_id"):
        try:
            conn.execute(f"ALTER TABLE settings ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vouches_guild ON vouches (guild_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vouch_files_vouch ON vouch_files (vouch_id)")
    conn.commit()
    conn.close()


def get_settings(guild_id: int):
    return db_one("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))


def upsert_settings(guild_id: int, **kwargs) -> None:
    allowed = {
        "vouch_channel_id", "vouch_count_enabled",
        "showcase_text", "showcase_image2_url", "showcase_image3_url",
        "queue_channel_id",
    }
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
        self.add_dynamic_items(OrderButton)  # keeps the order buttons working after a restart
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


def vouch_total(guild_id: int, channel_id: int | None = None) -> int:
    # with a channel, only count vouches that live in that channel (so old + restored copies don't double up)
    if channel_id is not None:
        row = db_one("SELECT COUNT(*) AS n FROM vouches WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
    else:
        row = db_one("SELECT COUNT(*) AS n FROM vouches WHERE guild_id = ?", (guild_id,))
    return row["n"] if row else 0


# a restored copy is skipped when its original is also in the same backup, so nothing posts twice
_RESTORABLE_WHERE = (
    "v.guild_id = ? AND NOT (v.origin_key IS NOT NULL AND EXISTS ("
    "SELECT 1 FROM vouches o WHERE o.guild_id = v.guild_id AND o.origin_key IS NULL "
    "AND (CAST(o.guild_id AS TEXT) || ':' || CAST(o.message_id AS TEXT)) = v.origin_key))"
)


def restorable_count(source_gid: int, exclude_channel_id: int | None = None) -> int:
    sql = f"SELECT COUNT(*) AS n FROM vouches v WHERE {_RESTORABLE_WHERE}"
    params: tuple = (source_gid,)
    if exclude_channel_id is not None:
        sql += " AND v.channel_id != ?"
        params += (exclude_channel_id,)
    row = db_one(sql, params)
    return row["n"] if row else 0


def restorable_rows(source_gid: int, exclude_channel_id: int | None = None):
    sql = f"SELECT v.* FROM vouches v WHERE {_RESTORABLE_WHERE}"
    params: tuple = (source_gid,)
    if exclude_channel_id is not None:
        sql += " AND v.channel_id != ?"
        params += (exclude_channel_id,)
    sql += " ORDER BY v.created_at ASC, v.id ASC"
    return db_all(sql, params)


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
    name = re.sub(r"(?i)(discord|clyde)", lambda m: m.group(0)[0] + "\u200b" + m.group(0)[1:], name)
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
        # same-server restore also skips anything already sitting in the target channel
        rows = restorable_rows(source_gid, channel.id if source_gid == guild.id else None)

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
        new_name = build_counted_channel_name(channel.name, vouch_total(gid, channel.id))
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
        f"Vouches saved from {channel.mention}: **{vouch_total(guild.id, channel.id)}**",
        ephemeral=True,
    )


async def vouch_source_autocomplete(interaction: discord.Interaction, current: str):
    rows = db_all(
        "SELECT v.guild_id AS guild_id, COUNT(*) AS n FROM vouches v WHERE NOT (v.origin_key IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM vouches o WHERE o.guild_id = v.guild_id AND o.origin_key IS NULL "
        "AND (CAST(o.guild_id AS TEXT) || ':' || CAST(o.message_id AS TEXT)) = v.origin_key)) "
        "GROUP BY v.guild_id ORDER BY n DESC"
    )
    choices = []
    for r in rows:
        if r["guild_id"] == interaction.guild_id:
            label = f"this server ({r['guild_id']}) - {r['n']} vouches"
        else:
            g = bot.get_guild(r["guild_id"])
            label = f"{g.name if g else 'old server'} ({r['guild_id']}) - {r['n']} vouches"
        if current and current.lower() not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=str(r["guild_id"])))
    return choices[:25]


@bot.tree.command(name="vouch_restore", description="Re-post backed-up vouches (from an old server, or this one) into a channel here")
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
    if guild.id in _vouch_busy:
        await interaction.response.send_message("A vouch backup/restore is already running here, wait for it to finish.", ephemeral=True)
        return

    total = restorable_count(source_gid, channel.id if source_gid == guild.id else None)
    if total == 0:
        await interaction.response.send_message(f"I don't have any vouches backed up for server `{source_gid}`. Pick one from the list when you type the `source` option.", ephemeral=True)
        return

    perms = channel.permissions_for(guild.me)
    if not perms.send_messages:
        await interaction.response.send_message(f"I can't send messages in {channel.mention}.", ephemeral=True)
        return

    settings = get_settings(guild.id)
    saved_id = settings["vouch_channel_id"] if settings else None
    if not saved_id or guild.get_channel(int(saved_id)) is None:
        # no vouch channel yet, or the old one was deleted
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
    total = vouch_total(guild.id, channel.id)
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


# ───────────────────────── showcase ─────────────────────────


def build_showcase_embeds(text, image2, image3, main_image=None):
    first = discord.Embed(description=text, color=COLOR)
    if main_image:
        first.set_image(url=main_image)
    embeds = [first]
    for url in (image2, image3):
        if url:
            e = discord.Embed(color=COLOR)
            e.set_image(url=url)
            embeds.append(e)
    return embeds


class ShowcaseSetupModal(discord.ui.Modal, title="Showcase Template"):
    showcase_text = discord.ui.TextInput(
        label="Fixed text (top embed description)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
        placeholder="The header text reused on every showcase post",
    )
    image2_url = discord.ui.TextInput(
        label="2nd embed image URL (fixed)", required=False, max_length=1000,
        placeholder="e.g.  https://i.imgur.com/abc123.gif",
    )
    image3_url = discord.ui.TextInput(
        label="3rd embed image URL (fixed, optional)", required=False, max_length=1000,
        placeholder="e.g.  https://i.imgur.com/xyz456.gif",
    )

    def __init__(self, prefill: dict | None = None):
        super().__init__()
        if prefill:
            if prefill.get("showcase_text"):
                self.showcase_text.default = prefill["showcase_text"]
            if prefill.get("showcase_image2_url"):
                self.image2_url.default = prefill["showcase_image2_url"]
            if prefill.get("showcase_image3_url"):
                self.image3_url.default = prefill["showcase_image3_url"]

    async def on_submit(self, interaction: discord.Interaction):
        guild = guild_only(interaction)
        text = str(self.showcase_text).replace("\\n", "\n")
        img2 = str(self.image2_url).strip()
        img3 = str(self.image3_url).strip()
        upsert_settings(guild.id, showcase_text=text, showcase_image2_url=img2, showcase_image3_url=img3)
        await interaction.response.send_message(
            f"{HEART} Showcase template saved! When you run `/showcase`, this text goes in the 1st embed "
            f"alongside your photo, followed by the fixed image(s) below.",
            embeds=build_showcase_embeds(text, img2, img3),
            ephemeral=True,
        )


def safe_filename(name: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "") or "image.png"


@bot.tree.command(name="showcase_setup", description="Set the fixed showcase template (text + the 2nd/3rd banner images)")
@app_commands.checks.has_permissions(manage_guild=True)
async def showcase_setup(interaction: discord.Interaction):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    prefill = None
    if settings:
        prefill = {
            "showcase_text": settings["showcase_text"],
            "showcase_image2_url": settings["showcase_image2_url"],
            "showcase_image3_url": settings["showcase_image3_url"],
        }
    await interaction.response.send_modal(ShowcaseSetupModal(prefill=prefill))


@bot.tree.command(name="showcase", description="Post a new showcase with your saved template")
@app_commands.describe(image="The photo to showcase")
async def showcase(interaction: discord.Interaction, image: discord.Attachment):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    if not settings or not settings["showcase_text"]:
        await interaction.response.send_message("Run `/showcase_setup` first to set the fixed template text.", ephemeral=True)
        return
    if not image.content_type or not image.content_type.startswith("image/"):
        await interaction.response.send_message("Please attach an image file.", ephemeral=True)
        return

    # reply privately, then post as the bot so no "used /showcase" line shows
    await interaction.response.defer(ephemeral=True)
    file = await image.to_file(filename=safe_filename(image.filename))
    embeds = build_showcase_embeds(
        settings["showcase_text"], settings["showcase_image2_url"], settings["showcase_image3_url"],
        main_image=f"attachment://{file.filename}",
    )
    try:
        await interaction.channel.send(embeds=embeds, file=file)
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"couldn't post here, make sure I have View Channel, Send Messages, Embed Links and "
            f"Attach Files in this channel ({e.status})",
            ephemeral=True,
        )
        return
    await interaction.delete_original_response()


# ───────────────────────── orders ─────────────────────────
# /queue posts the order in the queue channel. The Working on / Done buttons
# message the customer in their ticket channel.

# The divider image shown above and below the order. Save your image as divider.png
# right next to bot.py (commit it with the bot) and it gets attached automatically.
# Discord attachment links expire after a while, so the file is the safe option.
# If you'd rather use a permanent link (like an imgur one), paste it into DIVIDER_URL.
# If neither exists, the card falls back to the old emoji border.
DIVIDER_FILE = Path(__file__).with_name("divider.png")
DIVIDER_URL = ""

ORDER_BORDER = (
    "<:dnsawk:1566483123775086812><:dnsawk2:1566483152313258125><:dnsawk:1566483123775086812>"
    "<:dnsawk2:1566483152313258125><:dnsawk:1566483123775086812><:dnsawk:1566483123775086812>"
    "<:dnsawk2:1566483152313258125><:dnsawk:1566483123775086812><:dnsawk2:1566483152313258125>"
    "<:dnsawk:1566483123775086812>"
)
ORDER_HEADER = "જ⁀➴  ׁ ׅ  . . <:pinkoutlineheartawk:1566486491440357396>  . . ﹒  ◡◡"
ORDER_DOT = "<:whitedotawk:1564404632934154391>"

# key -> (label, emoji), in the order they show on the message
ORDER_BUTTONS = {
    "noted": ("Noted", "<:whiteribbonawk:1566486396259008553>"),
    "working": ("Working on", "<:whiteheartawk:1566486532980740266>"),
    "done": ("Done", "<:pinkribbonawk:1566486362360782990>"),
}


def build_order_text(user_mention: str, items: str, fastpass: str, payment: str, emoji_border: bool = False) -> str:
    border = f"{ORDER_BORDER}\n" if emoji_border else ""
    return (
        f"{border}"
        f"{ORDER_HEADER}\n"
        f": ✐ : New Order\n"
        f"{ORDER_DOT}  . . ﹒ User : {user_mention}\n\n"
        f"{ORDER_DOT}  . . ﹒ Item(s) : {items}\n\n"
        f"{ORDER_DOT}  . . ﹒ Fast Pass : {fastpass}\n\n"
        f"{ORDER_DOT}  . . ﹒ Payment : {payment}\n\n"
        f"{ORDER_HEADER}"
        f"{chr(10) + ORDER_BORDER if emoji_border else ''}"
    )


def build_order_message(user_mention: str, items: str, fastpass: str, payment: str):
    # returns (embeds, file or None). Divider image goes above and below the order card.
    file = None
    if DIVIDER_FILE.exists():
        file = discord.File(DIVIDER_FILE, filename="divider.png")
        divider = "attachment://divider.png"
    elif DIVIDER_URL:
        divider = DIVIDER_URL
    else:
        divider = None

    main = discord.Embed(
        description=build_order_text(user_mention, items, fastpass, payment, emoji_border=divider is None),
        color=COLOR,
    )
    if divider is None:
        return [main], None

    top = discord.Embed(color=COLOR)
    top.set_image(url=divider)
    bottom = discord.Embed(color=COLOR)
    bottom.set_image(url=divider)
    return [top, main, bottom], file


class OrderButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"order:(?P<key>noted|working|done):(?P<ticket>\d+)",
):
    # the ticket channel id lives in the button's custom_id, so it survives restarts
    def __init__(self, key: str, ticket_id: int, disabled: bool = False):
        label, emoji = ORDER_BUTTONS[key]
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"order:{key}:{ticket_id}",
                disabled=disabled,
            )
        )
        self.key = key
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["key"], int(match["ticket"]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("only staff can use these buttons", ephemeral=True)
            return

        # rebuild the button states from the message so every order keeps its own state
        disabled = set()
        for row in interaction.message.components:
            for child in getattr(row, "children", []):
                cid = getattr(child, "custom_id", None) or ""
                if getattr(child, "disabled", False) and cid.startswith("order:"):
                    disabled.add(cid.split(":")[1])
        disabled.add(self.key)
        if self.key == "done":
            disabled |= set(ORDER_BUTTONS)
        await interaction.response.edit_message(view=OrderView(self.ticket_id, disabled))

        if self.key == "noted":
            return

        # the divider embeds have no text, so look through all embeds for the order text
        desc = next((e.description for e in interaction.message.embeds if e.description), "")
        m = re.search(r"<@!?(\d+)>", desc or "")
        ticket = interaction.guild.get_channel(self.ticket_id)
        if not m or ticket is None:
            await interaction.followup.send("I can't find that customer's ticket anymore, so nothing was sent.", ephemeral=True)
            return

        mention = f"<@{m.group(1)}>"
        if self.key == "working":
            msg = f"♡ : {mention} your order is being worked on! . ."
        else:
            msg = f"♡ : {mention} your order has been completed, please vouch and wait for delivery! . ."
        try:
            await ticket.send(msg)
        except discord.HTTPException:
            await interaction.followup.send(f"I couldn't post in {ticket.mention}, check my permissions there.", ephemeral=True)
            return
        await interaction.followup.send(f"{HEART} sent in {ticket.mention}", ephemeral=True)


class OrderView(discord.ui.View):
    def __init__(self, ticket_id: int, disabled: set[str] | None = None):
        super().__init__(timeout=None)
        disabled = disabled or set()
        for key in ORDER_BUTTONS:
            self.add_item(OrderButton(key, ticket_id, disabled=key in disabled))


@bot.tree.command(name="queue_setup", description="Set the queue channel where new orders get posted")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="The queue channel")
async def queue_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    guild = guild_only(interaction)
    upsert_settings(guild.id, queue_channel_id=str(channel.id))
    await interaction.response.send_message(f"{HEART} queue channel set to {channel.mention}", ephemeral=True)


@bot.tree.command(name="queue", description="Post a new order in the queue channel")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    user="The customer",
    items="What they ordered, like 4x necklaces",
    fastpass="Do they have a fast pass?",
    payment="How they're paying",
)
@app_commands.choices(fastpass=[
    app_commands.Choice(name="yes", value="yes"),
    app_commands.Choice(name="no", value="no"),
])
async def order(
    interaction: discord.Interaction,
    user: discord.Member,
    items: str,
    fastpass: app_commands.Choice[str],
    payment: str,
):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    qid = settings["queue_channel_id"] if settings else None
    queue = guild.get_channel(int(qid)) if qid else None
    if not isinstance(queue, discord.TextChannel):
        await interaction.response.send_message("Set the queue channel first with `/queue_setup`.", ephemeral=True)
        return

    ticket_channel = interaction.channel  # the ticket is wherever /queue was used
    await interaction.response.defer(ephemeral=True)
    embeds, file = build_order_message(user.mention, items, fastpass.value, payment)
    view = OrderView(ticket_channel.id, disabled={"noted"})  # Noted starts greyed out
    send_kwargs = dict(embeds=embeds, view=view)
    if file is not None:
        send_kwargs["file"] = file
    try:
        await queue.send(**send_kwargs)
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"couldn't post in {queue.mention}, make sure I have View Channel, Send Messages, "
            f"Embed Links and Attach Files there ({e.status})",
            ephemeral=True,
        )
        return
    await interaction.followup.send(f"{HEART} order posted in {queue.mention}", ephemeral=True)


@bot.event
async def on_ready():
    for row in db_all("SELECT guild_id FROM settings WHERE vouch_count_enabled = '1'"):
        request_vouch_count_update(row["guild_id"])
    print(f"logged in as {bot.user}")


bot.run(TOKEN)
