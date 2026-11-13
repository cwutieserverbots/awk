import asyncio
import json
import math
import os
import time
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN", "PASTE_TOKEN_HERE")

HEART = "<:whiteheartawk:1566486532980740266>"
ROBUX = "<:robuxawk:1568714859678408745>"

# Railway sets RAILWAY_VOLUME_MOUNT_PATH when a volume is attached
DATA = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "data"))
FILES = DATA / "vouch_files"
FILES.mkdir(parents=True, exist_ok=True)
TIMERS_PATH = DATA / "timers.json"
VOUCHES_PATH = DATA / "vouches.json"
SETTINGS_PATH = DATA / "settings.json"


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


timers = load(TIMERS_PATH, [])
vouches = load(VOUCHES_PATH, [])
settings = load(SETTINGS_PATH, {"vouch_channel_id": 0})


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        timer_loop.start()


bot = Bot()


# ───────────────────────── /fp ─────────────────────────


@bot.tree.command(name="fp", description="Start a fast pass timer")
@app_commands.describe(hours="How long the fast pass lasts")
@app_commands.choices(hours=[
    app_commands.Choice(name="24 hours", value=24),
    app_commands.Choice(name="48 hours", value=48),
    app_commands.Choice(name="72 hours", value=72),
])
async def fp(interaction: discord.Interaction, hours: app_commands.Choice[int]):
    timers.append({
        "user_id": interaction.user.id,
        "channel_id": interaction.channel_id,
        "end": time.time() + hours.value * 3600,
        "reminded": False,
    })
    save(TIMERS_PATH, timers)
    await interaction.response.send_message(
        f"{HEART} : {hours.value}hrs fast past timer has started - "
        f"Reminder will occur 5 hours before time is up . ."
    )


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
        save(TIMERS_PATH, timers)


# ───────────────────────── vouch restorer ─────────────────────────


async def store_message(msg: discord.Message, session: aiohttp.ClientSession):
    if any(v["id"] == msg.id for v in vouches):
        return False
    saved_files = []
    for a in msg.attachments:
        dest = FILES / f"{msg.id}_{a.filename}"
        try:
            async with session.get(a.url) as r:
                if r.status == 200:
                    dest.write_bytes(await r.read())
                    saved_files.append(str(dest))
        except aiohttp.ClientError:
            pass
    vouches.append({
        "id": msg.id,
        "author_name": msg.author.display_name,
        "author_avatar": msg.author.display_avatar.url,
        "content": msg.content,
        "timestamp": int(msg.created_at.timestamp()),
        "files": saved_files,
    })
    return True


@bot.event
async def on_message(msg: discord.Message):
    # live-save new vouches
    if msg.author.bot or msg.channel.id != settings["vouch_channel_id"]:
        return
    async with aiohttp.ClientSession() as s:
        if await store_message(msg, s):
            save(VOUCHES_PATH, vouches)


@bot.tree.command(name="vouchchannel", description="Set the vouch channel to back up")
@app_commands.default_permissions(administrator=True)
async def vouchchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    settings["vouch_channel_id"] = channel.id
    save(SETTINGS_PATH, settings)
    await interaction.response.send_message(
        f"{HEART} vouch channel set to {channel.mention}", ephemeral=True
    )


@bot.tree.command(name="vouchbackup", description="Save every vouch from the vouch channel")
@app_commands.default_permissions(administrator=True)
async def vouchbackup(interaction: discord.Interaction):
    channel = bot.get_channel(settings["vouch_channel_id"])
    if channel is None:
        return await interaction.response.send_message(
            "set the channel first with /vouchchannel", ephemeral=True
        )
    await interaction.response.defer(ephemeral=True)
    added = 0
    async with aiohttp.ClientSession() as s:
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author.bot:
                continue
            if await store_message(msg, s):
                added += 1
    vouches.sort(key=lambda v: v["timestamp"])
    save(VOUCHES_PATH, vouches)
    await interaction.followup.send(
        f"{HEART} backed up {added} new vouches ({len(vouches)} total)"
    )


@bot.tree.command(name="vouchrestore", description="Repost all saved vouches in this channel")
@app_commands.default_permissions(administrator=True)
async def vouchrestore(interaction: discord.Interaction):
    if not vouches:
        return await interaction.response.send_message("no vouches saved", ephemeral=True)
    await interaction.response.send_message(f"restoring {len(vouches)} vouches here . .")
    hook = await interaction.channel.create_webhook(name="Vouch Restore")
    try:
        for v in sorted(vouches, key=lambda x: x["timestamp"]):
            files = [discord.File(p) for p in v["files"] if os.path.exists(p)]
            text = (v["content"] or "") + f"\n-# originally posted <t:{v['timestamp']}:d>"
            await hook.send(
                content=text,
                username=v["author_name"],
                avatar_url=v["author_avatar"],
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await asyncio.sleep(1.2)
    finally:
        await hook.delete()
    await interaction.channel.send(f"{HEART} vouches restored!")


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
        color=0xFFDADA,
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
    embed = discord.Embed(description=text, color=0xFFDADA)
    embed.set_author(
        name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
    )
    if image:
        embed.set_image(url=image.url)
    await interaction.response.send_message(embed=embed)


@bot.event
async def on_ready():
    print(f"logged in as {bot.user}")


bot.run(TOKEN)
