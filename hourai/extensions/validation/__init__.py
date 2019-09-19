import discord
import asyncio
import traceback
from .rejectors import *
from .approvers import *
from .raid import *
from .storage import BanStorage
from discord.ext import tasks, commands
from datetime import datetime, timedelta
from hourai import bot, utils
from hourai.db import models, proxies
from hourai.utils import format

PURGE_LOOKBACK = timedelta(hours=6)
PURGE_DM = """
You have been kicked from {} due to not being verified within sufficient time.
If you feel this is in error, please contact a mod regarding this.
"""
BATCH_SIZE = 10
MINIMUM_GUILD_SIZE = 150

# TODO(james7132): Add per-server validation configuration.
# TODO(james7132): Add filter for pornographic or violent avatars
# Validators are applied in order from first to last. If a later validator has an
# approval reason, it overrides all previous rejection reasons.
VALIDATORS = (# -----------------------------------------------------------------
              # Suspicion Level Validators
              #     Validators here are mostly for suspicious characteristics.
              #     These are designed with a high-recall, low precision
              #     methdology. False positives from these are more likely.
              #     These are low severity checks.
              # -----------------------------------------------------------------

              # New user accounts are commonly used for alts of banned users.
              NewAccountRejector(lookback=timedelta(days=30)),
              # Low effort user bots and alt accounts tend not to set an avatar.
              NoAvatarRejector(),
              # Deleted accounts shouldn't be able to join new servers. A user
              # joining that is seemingly deleted is suspicious.
              DeletedAccountRejector(),

              # Filter likely user bots based on usernames.
              StringFilterRejector(
                  prefix='Likely user bot. ',
                  filters=['discord\.gg', 'twitter\.com', 'twitch\.tv',
                           'youtube\.com', 'youtu\.be',
                           '@everyone', '@here', 'admin', 'mod']),
              StringFilterRejector(
                  prefix='Likely user bot. ',
                  full_match=True,
                  filters=['[0-9a-fA-F]+', # Full Hexadecimal name
                            '\d+',         # Full Decimal name
                          ]),

              # If a user has Nitro, they probably aren't an alt or user bot.
              NitroApprover(),

              # -----------------------------------------------------------------
              # Questionable Level Validators
              #     Validators here are mostly for red flags of unruly or
              #     potentially troublesome.  These are designed with a
              #     high-recall, high-precision methdology. False positives from
              #     these are more likely to occur.
              # -----------------------------------------------------------------

              # Filter usernames and nicknames that match moderator users.
              NameMatchRejector(prefix='Username matches moderator\'s. ',
                                filter_func=utils.is_moderator),
              NameMatchRejector(prefix='Username matches moderator\'s. ',
                                filter_func=utils.is_moderator,
                                member_selector=lambda m: m.nick),

              # Filter usernames and nicknames that match bot users.
              NameMatchRejector(prefix='Username matches bot\'s. ',
                                filter_func=lambda m: m.bot),
              NameMatchRejector(prefix='Username matches bot\'s. ',
                                filter_func=lambda m: m.bot,
                                member_selector=lambda m: m.nick),

              # Filter offensive usernames.
              StringFilterRejector(
                  prefix='Offensive username. ',
                  filters=['nigger', 'nigga', 'faggot', 'cuck', 'retard']),

              # Filter sexually inapproriate usernames.
              StringFilterRejector(
                  prefix='Sexually inapproriate username. ',
                  filters=['anal', 'cock', 'vore', 'scat', 'fuck', 'pussy',
                           'penis', 'piss', 'shit', 'cum']),

              # -----------------------------------------------------------------
              # Malicious Level Validators
              #     Validators here are mostly for known offenders.
              #     These are designed with a low-recall, high precision
              #     methdology. False positives from these are far less likely to
              #     occur.
              # -----------------------------------------------------------------

              # Make sure the user is not banned on other servers.
              BannedUserRejector(min_guild_size=150),

              # Check the username against known banned users from the current
              # server.
              # BannedUserNameMatchRejector(min_guild_size=150)

              # -----------------------------------------------------------------
              # Raid Level Validators
              #     Validators here operate on more tha just one user, and look
              #     at the overall rate of users joining the server.
              # ----------------------------------------------------------------

              # TODO(james7132): Add the raid validators

              # -----------------------------------------------------------------
              # Override Level Validators
              #     Validators here are made to explictly override previous
              #     validators. These are specifically targetted at a small
              #     specific group of individiuals. False positives and negatives
              #     at this level are not possible.
              # -----------------------------------------------------------------
              BotApprover(),
              BotOwnerApprover(),
              BotTeamApprover(),
              )

def _get_validation_config(ctx):
    return ctx.session.query(models.GuildValidationConfig).get(ctx.guild.id)

async def _validate_member(bot, member):
    approval = True
    approval_reasons = []
    rejection_reasons = []
    for validator in VALIDATORS:
        try:
            async for reason in validator.get_rejection_reasons(bot, member):
                if reason is None:
                    continue
                rejection_reasons.append(reason)
                approval = False
            async for reason in validator.get_approval_reasons(bot, member):
                if reason is None:
                    continue
                approval_reasons.append(reason)
                approval = True
        except:
            # TODO(james7132) Handle the error
            traceback.print_exc()
    return approval, approval_reasons, rejection_reasons

def _chunk_iter(src, chunk_size):
    chunk = []
    for val in src:
        chunk.append(val)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    yield chunk

class Validation(bot.BaseCog):

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.ban_storage = BanStorage(bot, timeout=300)
        self.purge_unverified.start()
        self.reload_bans.start()

    def cog_unload(self):
        self.purge_unverified.cancel()
        self.reload_bans.cancel()

    @tasks.loop(seconds=5)
    async def reload_bans(self):
        try:
            self.bot.logger.info('RELOADING BANS')
            for guild in self.bot.guilds:
                await self.ban_storage.save_bans(guild)
            self.bot.logger.info('BANS RELOADED')
        except:
            self.bot.logger.exception("Exception while reloading bans")

    @reload_bans.before_loop
    async def before_reload_bans(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=5.0)
    async def purge_unverified(self):
        session = self.bot.create_db_session()
        configs = session.query(models.GuildValidationConfig) \
                        .filter_by(is_propogated=True) \
                        .all()
        guilds = ((conf, self.bot.get_guild(conf.guild_id)) for conf in configs)
        check_time = datetime.utcnow() - PURGE_LOOKBACK
        def _is_kickable(member):
            # Does not kick
            #  * Bots
            #  * Nitro Boosters
            #  * Verified users
            #  * Unverified users who have joined less than 6 hours ago.
            checks = (not member.bot,
                      member.joined_at is not None,
                      member.joined_at <= check_time)
            return all(checks)
        async def _kick_member(member):
            try:
                await utils.send_dm(member, PURGE_DM.format(member.guild.name))
            except:
                pass
            await member.kick(reason='Unverified in sufficient time.')
            self.bot.logger.info('Purged {} from {} for not being verified in time.'.format(
                  utils.pretty_print(member), utils.pretty_print(member.guild)))
        tasks = list()
        for conf, guild in guilds:
            role = guild.get_role(conf.validation_role_id)
            if role is None or not guild.me.guild_permissions.kick_members:
                continue
            if not guild.chunked:
                await self.bot.request_offline_members(guild)
            unvalidated_members = utils.all_without_roles(guild.members, (role,))
            kickable_members = filter(_is_kickable, unvalidated_members)
            tasks.extend(_kick_member(member) for member in kickable_members)
        await asyncio.gather(*tasks)

    @purge_unverified.before_loop
    async def before_purge_unverified(self):
        await self.bot.wait_until_ready()

    # async def _update_ban_list(self):
        # session = self.bot.create_db_session()
        # async def _get_bans(guild):
            # if not guild.me.guild_permissions.ban_members:
                # return list()
            # try:
                # return [models.Ban(guild_id=guild.id, user_id=b.user.id,
                                            # reason=b.reason)
                                # for b in await guild.bans()]
            # except discord.Forbidden as e:
                # print('Failed to fetch {}\'s bans'.format(guild.name))
                # return list()
        # bans = await asyncio.gather(*[_get_bans(g) for g in self.bot.guilds])
        # session.query(models.Ban).delete()
        # for ban_list in bans:
            # session.add_all(ban_list)
        # session.commit()
        # self.bot.logger.info('Updated ban list')

    @commands.command(name="setmodlog")
    @commands.guild_only()
    async def setmodlog(self, ctx, channel: discord.TextChannel=None):
        # TODO(jame7132): Update this so it's in a different cog.
        channel = channel or ctx.channel
        proxy = ctx.get_guild_proxy()
        proxy.set_modlog_channel(channel)
        proxy.save()
        ctx.session.commit()
        await ctx.send(":thumbsup: Set {}'s modlog to {}.".format(
            ctx.guild.name, channel.mention))

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def validation(self, ctx):
        pass

    @validation.command(name="setup")
    async def validation_setup(self, ctx, role: discord.Role, channel: discord.TextChannel):
        config = _get_validation_config(ctx) or models.GuildValidationConfig()
        config.guild_id = ctx.guild.id
        config.validation_role_id = role.id
        config.validation_channel_id = channel.id
        ctx.session.add(config)
        ctx.session.commit()
        await ctx.send('Validation configuration complete! Please run `~validation propagate` then `~validation lockdown` to complete setup.')

    @validation.command(name="propagate")
    @commands.bot_has_permissions(manage_roles=True)
    async def validation_propagate(self, ctx):
        config = _get_validation_config(ctx)
        if config is None:
            await ctx.send('No validation config was found. Please run `~valdiation setup`')
            return
        msg = await ctx.send('Propagating validation role...!')
        if not ctx.guild.chunked:
            await ctx.bot.request_offline_members(ctx.guild)
        role = ctx.guild.get_role(config.validation_role_id)
        if role is None:
            await ctx.send("Verification role not found.")
            config.is_propogated = False
            session.add(config)
            session.commit()
            return
        while True:
            filtered_members = [m for m in guild.members if role not in m.roles]
            member_count = len(filtered_members)
            total_processed = 0
            async def add_role(member, role):
                if role in member.roles:
                    return
                try:
                    async_iter = _get_rejection_reasons(member)
                    reasons = await utils.collect(async_iter)
                    if len(reasons) < 0:
                        await member.add_roles(role)
                except discord.errors.Forbidden:
                    pass
            for chunk in _chunk_iter(ctx.guild.members, BATCH_SIZE):
                await asyncio.gather(*[add_role(mem, role) for mem in chunk])
                total_processed += len(chunk)
                await msg.edit(content=f'Propagation Ongoing ({total_processed}/{member_count})...')
            await msg.edit(content=f'Propagation conplete!')

            members_with_role = [m for m in guild.members if role in m.roles]
            if float(len(members_with_role)) / float(member_count) > 0.99:
                config.is_propogated = True
                session.add(config)
                session.commit()
                return


    @validation.command(name="lockdown")
    @commands.bot_has_permissions(manage_channels=True)
    async def validation_lockdown(self, ctx):
        config = _get_validation_config(ctx)
        if config is None:
            await ctx.send('No validation config was found. Please run `~valdiation setup`')
            return
        msg = await ctx.send('Locking down all channels!')
        everyone_role = ctx.guild.default_role
        validation_role = ctx.guild.get_role(config.validation_role_id)

        def update_overwrites(channel, role, read=True):
            overwrites = dict(channel.overwrites)
            validation = overwrites.get(role) or discord.PermissionOverwrite()
            validation.update(read_messages=read, connect=read)
            return validation

        everyone_perms = everyone_role.permissions
        everyone_perms.update(read_messages=False, connect=False)

        tasks = []
        tasks += [ch.set_permissions(validation_role,
                                     update_overwrites(ch, validation_role))
                  for ch in ctx.guild.channels
                  if ch.id != config.validation_channel_id]
        tasks.append(validation_channel.set_permissions(role, update_overwrites(valdiation_channel, everyone_role, read=True)))
        tasks.append(validation_channel.set_permissions(role, update_overwrites(valdiation_channel, validation_role, read=False)))
        tasks.append(everyone_role.edit(permissions=everyone_perms))

        await asyncio.gather(*tasks)
        await msg.edit(f'Lockdown complete! Make sure your mods can read the validation channel!')

    @commands.Cog.listener()
    async def on_member_join(self, member):
        print('{} ({}) joined {} ({})'.format(member.name.encode('utf-8'), member.id,
            member.guild.name.encode('utf-8'), member.guild.id))
        session = self.bot.create_db_session()
        proxy = proxies.GuildProxy(member.guild, session)
        if not proxy.validation_config.is_valid:
            return
        approved, reasons_a, reasons_r = await _validate_member(self.bot, member)
        if approved:
            message = f"Verified user: {member.metion} ({member.id})."
        else:
            message = (f"{utils.mention_random_online_mod(member.guild)}. "
                       f"User {member.name} ({member.id}) requires manual "
                       f"verification.")
        if len(reasons_a) > 0:
            message += ("\nApproved for the following reaasons: \n"
                        f"{ormat.bullet_list(reasons_a)}")
        if len(reasons_r) > 0:
            message += ("\nRejected for the following reaasons: \n"
                        f"{format.bullet_list(reasons_r)}")
        await proxy.send_modlog_message(content=response)
        if approved:
            role_id = proxy.validation_config.validation_role_id
            role = member.guild.get_role(role_id)
            await member.add_roles(role)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        try:
            ban_info = await guild.fetch_ban(user)
            await self.ban_storage.save_ban(guild, ban_info)
        except discord.Forbidden:
            pass

        if guild.member_count >= MINIMUM_GUILD_SIZE:
            # TODO(james7132): Enable this after adding deduplication.
            # await self.report_bans(ban)
            pass

    async def report_bans(self, ban_info):
        session = self.bot.create_db_session()
        guild_proxies = []
        for guild in self.bot.guilds:
            member = guild.get_member(user.id)
            if member is not None:
                guild_proxies.append(proxies.GuildProxy(guild, session))

        contents = None
        if ban_info.reason is None:
            contents = ("User {} ({}) has been banned from another server.".format(
                user.mention, user.id))
        else:
            contents = ("User {} ({}) has been banned from another server for "
                        "the following reason: `{}`").format(
                            user.mention, user.id, ban_info.reason)

        await asyncio.gather(*[proxy.send_modlog_message(contents)
                               for proxy in guild_proxies])
def setup(bot):
    bot.add_cog(Validation(bot))