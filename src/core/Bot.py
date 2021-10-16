from __future__ import annotations
from contextlib import suppress
import typing

if typing.TYPE_CHECKING:
    from ..cogs.reminder import Reminders

from discord import AllowedMentions, Intents
from colorama import Fore, init
from discord.ext import commands
from tortoise import Tortoise
from .Context import Context
from .Help import HelpCommand
from datetime import datetime
from constants import IST
from typing import NoReturn, Optional
from async_property import async_property
import aiohttp, asyncio, os
import config, constants
import itertools
import traceback
import discord
import mystbin
import dbl
import time

from .HttpHandler import QuoHttpHandler

init(autoreset=True)
intents = Intents.default()
intents.members = True


os.environ["JISHAKU_HIDE"] = "True"
os.environ["JISHAKU_NO_UNDERSCORE"] = "True"
os.environ["JISHAKU_NO_DM_TRACEBACK"] = "True"
print(Fore.RED + "-----------------------------------------------------")


class Quotient(commands.AutoShardedBot):
    def __init__(self, *args, **kwargs):
        super().__init__(
            command_prefix=self.get_prefix,
            intents=intents,
            max_messages=1000,
            strip_after_prefix=True,
            case_insensitive=True,
            help_command=HelpCommand(),
            chunk_guilds_at_startup=False,
            allowed_mentions=AllowedMentions(everyone=False, roles=False, replied_user=True, users=True),
            activity=discord.Activity(type=discord.ActivityType.listening, name="qsetup | qhelp"),
            **kwargs,
        )

        # cache stuff
        self.guild_data = {}
        self.eztagchannels = set()
        self.tagcheck = set()
        self.scrim_channels = set()
        self.tourney_channels = set()
        self.autopurge_channels = set()
        self.media_partner_channels = set()

        asyncio.get_event_loop().run_until_complete(self.init_quo())
        self.loop = asyncio.get_event_loop()
        self.color = config.COLOR
        self.start_time = datetime.now(tz=IST)
        self.cmd_invokes = 0
        self.seen_messages = 0
        self.binclient = mystbin.Client()
        self.lockdown = False
        self.persistent_views_added = False
        self.http_client = QuoHttpHandler(self)
        self.loop.create_task(self.http_client.handle())

        self.dblpy = dbl.DBLClient(self, self.config.DBL_TOKEN, autopost=True)

        self._BotBase__cogs = commands.core._CaseInsensitiveDict()

        for ext in self.config.EXTENSIONS:
            try:
                self.load_extension(ext)
                print(Fore.GREEN + f"[EXTENSION] {ext} was loaded successfully!")
            except Exception as e:
                tb = traceback.format_exception(type(e), e, e.__traceback__)
                tbe = "".join(tb) + ""
                print(Fore.RED + f"[WARNING] Could not load extension {ext}: {tbe}")
        print(Fore.RED + "-----------------------------------------------------")

    @property
    def config(self):
        """import and return config.py"""
        return __import__("config")

    @property
    def db(self):
        """to execute raw queries"""
        return Tortoise.get_connection("default")._pool

    async def init_quo(self):
        """Instantiating aiohttps ClientSession and asking ORM to create relations"""
        self.session = aiohttp.ClientSession(loop=self.loop)
        await Tortoise.init(config.TORTOISE)
        await Tortoise.generate_schemas(safe=True)

        from utils import cache

        await cache(self)

        # Initializing Models (Assigning Bot attribute to all models)
        for mname, model in Tortoise.apps.get("models").items():
            model.bot = self

    async def get_prefix(self, message: discord.Message) -> str:
        """Get a guild's prefix"""
        if not message.guild:
            return

        if self.user.id == 765159200204128266:  # its the beta bot
            prefix = ""
        else:
            guild = self.guild_data.get(message.guild.id)
            if guild:
                prefix = guild["prefix"]

            else:
                self.guild_data[message.guild.id] = {"prefix": "q", "color": self.color, "footer": config.FOOTER}
                prefix = "q"

        return tuple("".join(chars) for chars in itertools.product(*zip(prefix.lower(), prefix.upper())))

    async def close(self) -> NoReturn:
        await super().close()
        await self.session.close()

    def get_message(self, message_id: int) -> discord.Message:
        """Gets the message from the cache"""
        return self._connection._get_message(message_id)

    async def process_commands(self, message: discord.Message):
        if message.content:
            ctx = await self.get_context(message, cls=Context)

            if ctx.command is None:
                return

            await self.invoke(ctx)

    async def on_message(self, message: discord.Message):
        self.seen_messages += 1

        if not message.guild or message.author.bot:
            return

        await self.process_commands(message)

    async def on_command(self, ctx):
        self.cmd_invokes += 1
        await constants.show_tip(ctx)
        await self.db.execute("INSERT INTO user_data (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ctx.author.id)

    async def on_ready(self) -> NoReturn:  # yes we love colors and colorama
        print(Fore.RED + "------------------------------------------------------")
        print(Fore.BLUE + f"[Quotient] Logged in as {self.user.name}({self.user.id})")
        print(Fore.CYAN + f"[Quotient] Spawned {len(self.shards)} Shards")

        if not self.persistent_views_added:  # add persistent views
            from cogs.esports.views import SlotManagerView, TourneySlotManager
            from models import SlotManager, Tourney

            async for record in SlotManager.filter(toggle=True):
                self.add_view(SlotManagerView(self), message_id=record.message_id)

            async for tourney in Tourney.filter(slotm_message_id__isnull=False):
                self.add_view(TourneySlotManager(self, tourney=tourney), message_id=tourney.slotm_message_id)

            self.persistent_views_added = True

    def embed(self, ctx: Context, **kwargs) -> discord.Embed:
        """This is how we deliver features like custom footer and custom color :)"""
        embed_color = self.guild_data[ctx.guild.id]["color"]
        embed_footer = self.guild_data[ctx.guild.id]["footer"]

        if embed_footer.strip().lower() == "none":
            embed_footer = discord.Embed.Empty

        embed = discord.Embed(**kwargs)
        embed.color = embed_color
        embed.set_footer(text=embed_footer)
        return embed

    async def is_owner(self, user) -> bool:
        if await super().is_owner(user):
            return True

        return user.id in config.DEVS

    async def get_or_fetch_member(self, guild: discord.Guild, member_id: int):
        """Looks up a member in cache or fetches if not found."""
        member = guild.get_member(member_id)
        if member is not None:
            return member

        shard = self.get_shard(guild.shard_id)

        if shard.is_ws_ratelimited():
            try:
                member = await guild.fetch_member(member_id)
            except discord.HTTPException:
                return None
            else:
                return member

        members = await guild.query_members(limit=1, user_ids=(member_id,), cache=True)

        if len(members) > 0:
            return members[0]

    @property
    def server(self) -> Optional[discord.Guild]:
        return self.get_guild(746337818388987967)

    @property
    def reminders(self) -> Reminders:  # since we use it a lot
        return self.get_cog("Reminders")

    @async_property
    async def db_latency(self):
        t1 = time.perf_counter()
        await self.db.execute("SELECT 1;")
        t2 = time.perf_counter() - t1
        return f"{t2*1000:.2f} ms"

    @staticmethod
    async def getch(get_method, fetch_method, _id):  # why does c have all the fun?
        try:
            _result = get_method(_id) or await fetch_method(_id)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            return None
        else:
            return _result

    async def send_message(self, channel_id, content, **kwargs):
        await self.http.send_message(channel_id, content, **kwargs)

    async def on_ipc_error(self, endpoint, error):
        print(endpoint, "raised", error)
