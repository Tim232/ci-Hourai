import discord
from discord.ext import commands
from hourai.bot import cogs
from hourai.utils import embed as embed_utils
from hourai.utils import format, success, checks


class ModLogging(cogs.BaseCog):
    """ Cog for logging Discord and bot events to a servers' modlog channels.
    """

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None:
            return
        proxy = self.bot.get_guild_proxy(guild)
        logging_config = await proxy.config.get('logging')
        if logging_config is None or not logging_config.log_deleted_messages:
            return
        content = 'Message deleted in <#{}>.'.format(payload.channel_id)
        modlog = await proxy.get_modlog()
        msg = payload.cached_message
        embed = embed_utils.message_to_embed(msg or payload.message_id)
        embed.color = discord.Color.dark_red()
        if msg is None:
            await modlog.send(content=content, embed=embed)
            return
        elif msg.author.bot:
            return
        content = 'Message by {} deleted in {}.'.format(
            msg.author.mention, msg.channel.mention)
        if len(msg.attachments) > 0:
            attachments = (attach.url for attach in msg.attachments)
            field = format.vertical_list(attachments)
            embed.add_field(name='Attachments', value=field)
        await modlog.send(content=content, embed=embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None:
            return
        proxy = self.bot.get_guild_proxy(guild)
        logging_config = await proxy.config.get('logging')
        if logging_config is None or not logging_config.log_deleted_messages:
            return
        content = '{} messages bulk deleted in <#{}>.'.format(
            len(payload.message_ids), payload.channel_id)
        modlog = await proxy.get_modlog()
        await modlog.send(content=content)

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    @checks.is_moderator()
    async def log(self, ctx):
        pass

    @log.command(name='deleted')
    async def log_deleted(self, ctx):
        config = await ctx.guild_proxy.config.get('logging')
        config.log_deleted_messages = not config.log_deleted_messages
        change = ('enabled' if config.log_deleted_messages else 'disabled.')
        await ctx.guild_proxy.config.set('logging', config)
        await success(f'Logging of deleted messages has been {change}')
