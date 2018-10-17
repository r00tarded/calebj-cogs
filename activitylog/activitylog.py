import discord
from discord.ext import commands
from cogs.utils import checks
from cogs.utils.dataIO import dataIO
from datetime import datetime, timedelta
import os
import asyncio
import aiohttp
from functools import partial
from enum import Enum

__version__ = '1.6.0'

TIMESTAMP_FORMAT = '%Y-%m-%d %X'  # YYYY-MM-DD HH:MM:SS
PATH_LIST = ['data', 'activitylogger']
PATH = os.path.join(*PATH_LIST)
JSON = os.path.join(*PATH_LIST, "settings.json")
EDIT_TIMEDELTA = timedelta(seconds=3)

# 0 is Message object
AUTHOR_TEMPLATE = "@{0.author.name}#{0.author.discriminator}"
MESSAGE_TEMPLATE = AUTHOR_TEMPLATE + ": {0.clean_content}"

# 0 is Message object, 1 is attachment URL
ATTACHMENT_TEMPLATE = AUTHOR_TEMPLATE + ": {0.clean_content} (attachment url(s): {1})"

# 0 is Message object, 1 is attachment path
# TODO: support multiple attachments?
DOWNLOAD_TEMPLATE = AUTHOR_TEMPLATE + ": {0.clean_content} (attachment saved to {1})"

# 0 is before, 1 is after, 2 is formatted timestamp
EDIT_TEMPLATE = AUTHOR_TEMPLATE + " edited message from {2} ({0.clean_content}) to read: {1.clean_content}"

# 0 is deleted message, 1 is formatted timestamp
DELETE_TEMPLATE = AUTHOR_TEMPLATE + " deleted message from {1} ({0.clean_content})"


class FetchCookie(object):
    def __init__(self, ctx, start, status_msg, last_edit=None):
        self.ctx = ctx
        self.start = start
        self.status_msg = status_msg
        self.last_edit = last_edit
        self.total_messages = 0
        self.completed_messages = []


class FetchStatus(Enum):
    STARTING = 'starting'
    FETCHING = 'fetching'
    CANCELLED = 'cancelled'
    EXCEPTION = 'exception'
    COMPLETED = 'completed'


class LogHandle:
    """basic wrapper for logfile handles, used to keep track of stale handles"""
    def __init__(self, path, time=None, mode='a', buf=1):
        self.handle = open(path, mode, buf, errors='backslashreplace')
        self.lock = asyncio.Lock()

        if time:
            self.time = time
        else:
            self.time = datetime.fromtimestamp(os.path.getmtime(path))

    async def write(self, value):
        async with self.lock:
            self._write(value)

    def close(self):
        self.handle.close()

    def _write(self, value):
        self.time = datetime.utcnow()
        self.handle.write(value)


class ActivityLogger(object):
    """Log activity seen by bot"""

    def __init__(self, bot):
        self.bot = bot
        self.settings = dataIO.load_json(JSON)
        self.handles = {}
        self.lock = False
        self.session = aiohttp.ClientSession(loop=self.bot.loop)
        self.fetch_handle = None

        try:
            self.analytics = CogAnalytics(self)
        except Exception as error:
            self.bot.logger.exception(error)
            self.analytics = None

    def __unload(self):
        self.lock = True
        self.session.close()

        for h in self.handles.values():
            h.close()

        if isinstance(self.fetch_handle, asyncio.Future):
            if not self.fetch_handle.cancelled():
                self.fetch_handle.cancel()

    async def _robust_edit(self, msg, content=None, embed=None):
        try:
            msg = await self.bot.edit_message(msg, new_content=content, embed=embed)
        except discord.errors.NotFound:
            msg = await self.bot.send_message(msg.channel, content=content, embed=embed)
        except Exception:
            raise

        return msg

    async def cookie_edit_task(self, cookie, **kwargs):
        cookie.status_msg = await self._robust_edit(cookie.status_msg, **kwargs)

    async def fetch_task(self, channels, subfolder, attachments=None, status_cb=None):
        channel = None
        completed_channels = []
        pending_channels = channels.copy()

        def update(count, last_msg, status, channel, exception=None):
            if not callable(status_cb):
                return
            elif type(last_msg) is not discord.Message:
                last_msg = None

            status_cb(count=count, channel=channel, subfolder=subfolder,
                      status=status, exception=exception, last_msg=last_msg,
                      completed_channels=completed_channels,
                      pending_channels=pending_channels)

        try:
            for channel in channels:
                pending_channels.remove(channel)
                count = 0
                fetch_begin = channel.created_at

                update(count, None, FetchStatus.STARTING, channel)

                while True:
                    last_count = count
                    async for message in self.bot.logs_from(channel, after=fetch_begin, reverse=True):
                        await self.message_handler(message, force=True, subfolder=subfolder,
                                                   force_attachments=attachments)
                        fetch_begin = message
                        update(count, fetch_begin, FetchStatus.FETCHING, channel)
                        count += 1

                    if count == last_count:
                        break

                update(count, fetch_begin, FetchStatus.COMPLETED, channel)
                completed_channels.append(channel)

        except asyncio.CancelledError:
            update(count, fetch_begin, FetchStatus.CANCELLED, channel)
        except Exception as e:
            update(count, fetch_begin, FetchStatus.EXCEPTION, channel, exception=e)
            raise

    def format_fetch_line(self, cookie, count, status, exception, channel, **kwargs):
        elapsed = datetime.now() - (cookie.last_edit or cookie.start)
        edit_to = None
        base = '#%s: ' % channel.name

        if status is FetchStatus.STARTING:
            edit_to = base + 'initializing...'
        elif status is FetchStatus.EXCEPTION:
            edit_to = base + 'error after %i messages.' % count

            if isinstance(exception, Exception):
                ename = type(exception).__name__
                estr = str(exception)
                edit_to += ': %s: %s' % (ename, estr)
        elif status is FetchStatus.CANCELLED:
            edit_to = base + 'cancelled after %i messages.' % count
        elif status is FetchStatus.COMPLETED:
            edit_to = base + 'fetched %i messages.' % count
        elif status is FetchStatus.FETCHING:
            if elapsed > EDIT_TIMEDELTA:
                edit_to = base + '%i messages retrieved so far...' % count

        return edit_to

    def fetch_callback(self, cookie, pending_channels, **kwargs):
        status = kwargs.get('status')
        count = kwargs.get('count')

        format_line = self.format_fetch_line(cookie, **kwargs)

        if format_line:
            rows = cookie.completed_messages + [format_line]
            rows.extend([('#%s: pending' % c.name) for c in pending_channels])
            cookie.last_edit = datetime.now()
            task = self.cookie_edit_task(cookie, content='\n'.join(rows))
            self.bot.loop.create_task(task)

        if status is FetchStatus.COMPLETED:
            cookie.total_messages += count
            cookie.completed_messages.append(format_line)

            if not pending_channels:
                dest = cookie.ctx.message.channel
                elapsed = datetime.now() - cookie.start
                msg = ('Fetched a total of %i messages in %s.' % (cookie.total_messages, elapsed))
                self.bot.loop.create_task(self.bot.send_message(dest, msg))

    @commands.group(pass_context=True)
    @checks.is_owner()
    async def logfetch(self, ctx):
        """
        Fetches logs from channel or server. Beware the disk usage.
        """
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @logfetch.command(pass_context=True, name='cancel')
    async def fetch_cancel(self, ctx):
        """
        Cancels a running fetch operation.
        """
        if isinstance(self.fetch_handle, asyncio.Future):
            if not self.fetch_handle.cancelled():
                self.fetch_handle.cancel()
                self.fetch_handle = None
                await self.bot.say('Fetch cancelled.')
                return

        await self.bot.say('Nothing to cancel.')

    @logfetch.command(pass_context=True, name='channel')
    async def fetch_channel(self, ctx, subfolder: str, channel: discord.Channel = None, attachments: bool = None):
        """
        Fetch complete logs for a channel. Defaults to the current one.
        """

        msg = await self.bot.say('Dispatching fetch task...')
        start = datetime.now()
        cookie = FetchCookie(ctx, start, msg)

        if channel is None:
            channel = ctx.message.channel

        callback = partial(self.fetch_callback, cookie)
        task = self.fetch_task([channel], subfolder, attachments=attachments, status_cb=callback)
        self.fetch_handle = self.bot.loop.create_task(task)

    @logfetch.command(pass_context=True, name='server', allow_dm=False)
    async def fetch_server(self, ctx, subfolder: str, attachments: bool = None):
        """
        Fetch complete logs for the current server.

        Respects current logging settings such as attachments and channels.
        Note that server events such as join/leave, ban etc can't be retrieved.
        """
        server = ctx.message.server

        def check(channel):
            if channel.type is not discord.ChannelType.text:
                return False

            return channel.permissions_for(server.me).read_message_history

        channels = [c for c in server.channels if check(c)]
        msg = await self.bot.say('Dispatching fetch task...')
        start = datetime.now()
        cookie = FetchCookie(ctx, start, msg)
        callback = partial(self.fetch_callback, cookie)
        task = self.fetch_task(channels, subfolder, attachments=attachments, status_cb=callback)
        self.fetch_handle = self.bot.loop.create_task(task)

    @logfetch.command(pass_context=True, name='remote-channel')
    async def fetch_rchannel(self, ctx, subfolder: str, channel_id: str, attachments: bool = None):
        """
        Fetch complete logs for any channel the bot can see.
        """

        msg = await self.bot.say('Dispatching fetch task...')
        start = datetime.now()

        cookie = FetchCookie(ctx, start, msg)

        channel = self.bot.get_channel(channel_id)
        if not channel:
            await self.bot.say('Could not find that server.')
            return
        elif not channel.permissions_for(channel.server.me).read_message_history:
            await self.bot.say('Missing the "read message history" permission in that channel.')
            return

        callback = partial(self.fetch_callback, cookie)
        task = self.fetch_task([channel], subfolder, attachments=attachments, status_cb=callback)

        self.fetch_handle = self.bot.loop.create_task(task)

    @logfetch.command(pass_context=True, name='remote-server')
    async def fetch_rserver(self, ctx, subfolder: str, server_id: str, attachments: bool = None):
        """
        Fetch complete logs for another server.

        Respects current logging settings such as attachments and channels.
        Note that server events such as join/leave, ban etc can't be retrieved.
        """

        server = self.bot.get_server(server_id)
        if not server:
            await self.bot.say('Could not find that server.')
            return

        def check(channel):
            if channel.type is not discord.ChannelType.text:
                return False

            return channel.permissions_for(server.me).read_message_history

        channels = [c for c in server.channels if check(c)]

        msg = await self.bot.say('Dispatching fetch task...')
        start = datetime.now()

        cookie = FetchCookie(ctx, start, msg)

        callback = partial(self.fetch_callback, cookie)
        task = self.fetch_task(channels, subfolder, attachments=attachments, status_cb=callback)

        self.fetch_handle = self.bot.loop.create_task(task)

    @commands.group(pass_context=True)
    @checks.is_owner()
    async def logset(self, ctx):
        """
        Change activity logging settings
        """
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @logset.command(name='everything', aliases=['global'])
    async def set_everything(self, on_off: bool = None):
        """
        Global override for all logging
        """
        if on_off is not None:
            self.settings['everything'] = on_off

        if self.settings.get('everything', False):
            await self.bot.say("Global logging override is enabled.")
        else:
            await self.bot.say("Global logging override is disabled.")

        self.save_json()

    @logset.command(name='default')
    async def set_default(self, on_off: bool = None):
        """
        Sets whether logging is on or off where unset

        Server overrides, global override, and attachments don't use this.
        """
        if on_off is not None:
            self.settings['default'] = on_off

        if self.settings.get('default', False):
            await self.bot.say("Logging is enabled by default.")
        else:
            await self.bot.say("Logging is disabled by default.")

        self.save_json()

    @logset.command(name='dm')
    async def set_direct(self, on_off: bool = None):
        """
        Log direct messages?
        """
        if on_off is not None:
            self.settings['direct'] = on_off

        default = self.settings.get('default', False)

        if self.settings.get('direct', default):
            await self.bot.say("Logging of direct messages is enabled.")
        else:
            await self.bot.say("Logging of direct messages is disabled.")

        self.save_json()

    @logset.command(name='attachments')
    async def set_attachments(self, on_off: bool = None):
        """
        Download message attachments?
        """
        if on_off is not None:
            self.settings['attachments'] = on_off

        if self.settings.get('attachments', False):
            await self.bot.say("Downloading of attachments is enabled.")
        else:
            await self.bot.say("Downloading of attachments is disabled.")

        self.save_json()

    @logset.command(pass_context=True, no_pm=True, name='channel')
    async def set_channel(self, ctx, on_off: bool, channel: discord.Channel = None):
        """
        Sets channel logging on or off (channel optional)

        To enable or disable all channels at once, use `logset server`.
        """
        if channel is None:
            channel = ctx.message.channel

        server = channel.server

        if server.id not in self.settings:
            self.settings[server.id] = {}

        self.settings[server.id][channel.id] = on_off

        if on_off:
            await self.bot.say('Logging enabled for %s' % channel.mention)
        else:
            await self.bot.say('Logging disabled for %s' % channel.mention)

        self.save_json()

    @logset.command(pass_context=True, no_pm=True, name='server')
    async def set_server(self, ctx, on_off: bool):
        """
        Sets logging on or off for all channels and server events
        """
        server = ctx.message.server

        if server.id not in self.settings:
            self.settings[server.id] = {}
        self.settings[server.id]['all'] = on_off

        if on_off:
            await self.bot.say('Logging enabled for %s' % server)
        else:
            await self.bot.say('Logging disabled for %s' % server)
        self.save_json()

    @logset.command(pass_context=True, no_pm=True, name='voice')
    async def set_voice(self, ctx, on_off: bool):
        """
        Sets logging on or off for ALL voice channel events
        """
        server = ctx.message.server

        if server.id not in self.settings:
            self.settings[server.id] = {}
        self.settings[server.id]['voice'] = on_off

        if on_off:
            await self.bot.say('Voice event logging enabled for %s' % server)
        else:
            await self.bot.say('Voice event logging disabled for %s' % server)

        self.save_json()

    @logset.command(pass_context=True, no_pm=True, name='events')
    async def set_events(self, ctx, on_off: bool):
        """
        Sets logging on or off for server events
        """
        server = ctx.message.server

        if server.id not in self.settings:
            self.settings[server.id] = {}

        self.settings[server.id]['events'] = on_off

        if on_off:
            await self.bot.say('Logging enabled for server events in %s' % server)
        else:
            await self.bot.say('Logging disabled for server events in %s' % server)

        self.save_json()

    @logset.command(pass_context=True, no_pm=True, name='rotation')
    async def set_rotation(self, ctx, freq: str = None):
        """
        Show, disable, or set the log rotation period

        Days start at 00:00 UTC. Attachment folders are still shared.
        When enabled, log filenames will be prepended with their ISO 8601 date and period.
        Example: if monthly, logs for July in channel ID 1234 would be in 20180701--P1M_1234.log

        Valid options are:
        - none: disable rotation
        - d: one log file per day (starts 00:00Z each day)
        - w: one log file per week (starts 00:00Z each Monday)
        - m: one log file per month (starts 00:00Z on first day of month)
        - y: one log file per year (starts 00:00Z Jan 1)
        """
        if freq:
            freq = freq.lower().strip('"\'` ')

        if freq in ('d', 'w', 'm', 'y', 'none', 'disable'):
            adj = 'now'

            if freq in ('none', 'disable'):
                freq = None

            self.settings['rotation'] = freq
            self.save_json()
        elif freq:
            await self.bot.send_cmd_help(ctx)
            return
        else:
            adj = 'currently'
            freq = self.settings.get('rotation').lower()

        if not freq:
            await self.bot.say("Log rotation is %s disabled." % adj)
        else:
            desc = {
                'd' : 'daily',
                'w' : 'weekly',
                'm' : 'monthly',
                'y' : 'yearly'
            }[freq]

            await self.bot.say('Log rotation period is %s %s.' % (adj, desc))

    def save_json(self):
        dataIO.save_json(JSON, self.settings)

    @staticmethod
    def format_rotation_string(timestamp, rotation_code, filename=None):
        kwargs = dict(hour=0, minute=0, second=0, microsecond=0)

        if not rotation_code:
            return filename or ''

        if rotation_code == 'y':
            kwargs.update(day=1, month=1)
        elif rotation_code == 'm':
            kwargs.update(day=1)
        elif rotation_code == 'w':
            kwargs.update(day=timestamp.day - timestamp.weekday())

        start = timestamp.replace(**kwargs)
        spec = start.strftime('%Y%m%d')

        if rotation_code == 'w':
            spec += '--P7D'
        else:
            spec += '--P1%c' % rotation_code.upper()

        if filename:
            return '%s_%s' % (spec, filename)
        else:
            return spec

    @staticmethod
    def get_voice_flags(member):
        flags = []
        for f in ('deaf', 'mute', 'self_deaf', 'self_mute'):
            if getattr(member, f, None):
                flags.append(f)

        return flags

    @staticmethod
    def format_overwrite(target, channel, before, after):
        target_str = 'Channel overwrites: {0.name} ({0.id}): '.format(channel)
        target_str += 'role' if isinstance(target, discord.Role) else 'member'
        target_str += ' {0.name} ({0.id})'.format(target)

        if before:
            bpair = [x.value for x in before.pair()]

        if after:
            apair = [x.value for x in after.pair()]

        if before and after:
            fmt = ' updated to values %i, %i (was %i, %i)'
            return target_str + fmt % tuple(apair + bpair)
        elif after:
            return target_str + ' added with values %i, %i' % tuple(apair)
        elif before:
            return target_str + ' removed (was %i, %i)' % tuple(bpair)

    def gethandle(self, path, mode='a'):
        """Manages logfile handles, culling stale ones and creating folders"""
        if path in self.handles:
            if os.path.exists(path):
                return self.handles[path]
            else:  # file was deleted?
                try:  # try to close, no guarantees tho
                    self.handles[path].close()
                except Exception:
                    pass

                del self.handles[path]
                return self.gethandle(path, mode)
        else:
            # Clean up excess handles before creating a new one
            if len(self.handles) >= 256:
                chrono = sorted(self.handles.items(), key=lambda x: x[1].time)
                oldest_path, oldest_handle = chrono[0]
                oldest_handle.close()
                del self.handles[oldest_path]

            dirname, _ = os.path.split(path)

            try:
                if not os.path.exists(dirname):
                    os.makedirs(dirname)

                handle = LogHandle(path, mode=mode)
            except Exception:
                raise

            self.handles[path] = handle
            return handle

    def should_log(self, location):
        if self.settings.get('everything', False):
            return True

        default = self.settings.get('default', False)

        if type(location) is discord.Server:
            if location.id in self.settings:
                loc = self.settings[location.id]
                return loc.get('all', False) or loc.get('events', default)

        elif type(location) is discord.Channel:
            if location.server.id in self.settings:
                loc = self.settings[location.server.id]
                opts = [loc.get('all', False), loc.get(location.id, default)]

                if location.type is discord.ChannelType.voice:
                    opts.append(loc.get('voice', False))

                return any(opts)

        elif type(location) is discord.PrivateChannel:
            return self.settings.get('direct', default)

        else:  # can't log other types
            return False

    def should_download(self, msg):
        return self.should_log(msg.channel) and \
            self.settings.get('attachments', False)

    def process_attachment(self, message):
        a = message.attachments[0]
        aid = a['id']
        aname = a['filename']
        url = a['url']
        channel = message.channel
        path = PATH_LIST.copy()

        if type(channel) is discord.Channel:
            serverid = channel.server.id
        elif type(channel) is discord.PrivateChannel:
            serverid = 'direct'

        path += [serverid, channel.id + '_attachments']
        path = os.path.join(*path)
        filename = aid + '_' + aname

        if len(filename) > 255:
            target_len = 255 - len(aid) - 4
            part_a = target_len // 2
            part_b = target_len - part_a
            filename = aid + '_' + aname[:part_a] + '...' + aname[-part_b:]
            truncated = True
        else:
            truncated = False

        return aid, url, path, filename, truncated

    async def log(self, location, text, timestamp=None, force=False, subfolder=None, mode='a'):
        if not timestamp:
            timestamp = datetime.utcnow()

        if self.lock or not (force or self.should_log(location)):
            return

        path = PATH_LIST.copy()
        entry = [timestamp.strftime(TIMESTAMP_FORMAT)]
        rotation = self.settings.get('rotation')

        if type(location) is discord.Server:
            path += [location.id, 'server.log']
        elif type(location) is discord.Channel:
            serverid = location.server.id
            entry.append('#' + location.name)
            path += [serverid, location.id + '.log']
        elif type(location) is discord.PrivateChannel:
            path += ['direct', location.id + '.log']
        else:
            return

        if subfolder:
            path.insert(-1, str(subfolder))

        text = text.replace('\n', '\\n')
        entry.append(text)

        if rotation:
            path[-1] = self.format_rotation_string(timestamp, rotation, path[-1])

        fname = os.path.join(*path)
        handle = self.gethandle(fname, mode=mode)
        await handle.write(' '.join(entry) + '\n')

    async def message_handler(self, message, *args, force_attachments=None, **kwargs):
        dl_attachment = self.should_download(message)
        if force_attachments is not None:
            dl_attachment = force_attachments

        if message.attachments and dl_attachment:
            aid, url, path, filename, trunc = self.process_attachment(message)
            entry = DOWNLOAD_TEMPLATE.format(message, filename)

            if trunc:
                entry += ' (filename truncated)'
        elif message.attachments:
            urls = ','.join(a['url'] for a in message.attachments)
            entry = ATTACHMENT_TEMPLATE.format(message, urls)
        else:
            entry = MESSAGE_TEMPLATE.format(message)

        await self.log(message.channel, entry, message.timestamp, *args, **kwargs)

        if message.attachments and dl_attachment:
            dl_path = os.path.join(path, filename)
            tmp_path = os.path.join(path, aid + '.tmp')

            if not os.path.exists(path):
                os.mkdir(path)

            if not os.path.exists(dl_path):  # don't redownload
                async with self.session.get(url) as r:
                    with open(tmp_path, 'wb') as f:
                        f.write(await r.read())

                    os.rename(tmp_path, dl_path)

    async def on_message(self, message):
        await self.message_handler(message)

    async def on_message_edit(self, before, after):
        timestamp = before.timestamp.strftime(TIMESTAMP_FORMAT)
        entry = EDIT_TEMPLATE.format(before, after, timestamp)
        await self.log(after.channel, entry, after.edited_timestamp)

    async def on_message_delete(self, message):
        timestamp = message.timestamp.strftime(TIMESTAMP_FORMAT)
        entry = DELETE_TEMPLATE.format(message, timestamp)
        await self.log(message.channel, entry)

    async def on_server_join(self, server):
        entry = 'this bot joined the server'
        await self.log(server, entry)

    async def on_server_remove(self, server):
        entry = 'this bot left the server'
        await self.log(server, entry)

    async def on_server_update(self, before, after):
        entries = []

        if before.owner != after.owner:
            entries.append('Server owner changed from {0.owner} (id {0.owner.id}) to {1.owner} (id {1.owner.id})')

        if before.region != after.region:
            entries.append('Server region changed from {0.region} to {1.region}')

        if before.name != after.name:
            entries.append('Server name changed from "{0.name}" to "{1.name}"')

        if before.icon_url != after.icon_url:
            entries.append('Server icon changed from {0.icon_url} to {1.icon_url}')

        for e in entries:
            await self.log(before, e.format(before, after))

    async def on_server_role_create(self, role):
        entry = "Role created: '{0}' (id {0.id})".format(role)
        await self.log(role.server, entry)

    async def on_server_role_delete(self, role):
        entry = "Role deleted: '{0}' (id {0.id})".format(role)
        await self.log(role.server, entry)

    async def on_server_role_update(self, before, after):
        if not self.should_log(before.server):
            return

        entries = []

        if before.name != after.name:
            entries.append('Role renamed: "{0.name}" to "{1.name}"')

        if before.color != after.color:
            entries.append('Role color: "{0}" (id {0.id}) changed from {0.color} to {1.color}')

        if before.mentionable != after.mentionable:
            if after.mentionable:
                entries.append('Role mentionable: "{1.name}" (id {1.id}) is now mentionable')
            else:
                entries.append('Role mentionable: "{1.name}" (id {1.id}) is no longer mentionable')

        if before.hoist != after.hoist:
            if after.hoist:
                entries.append('Role hoist: "{1.name}" (id {1.id}) is now shown seperately')
            else:
                entries.append('Role hoist: "{1.name}" (id {1.id}) is no longer shown seperately')

        if before.permissions != after.permissions:
            entries.append('Role permissions: "{1.name}" (id {1.id}) changed from {0.permissions.value} '
                           'to {1.permissions.value}')

        if before.position != after.position:
            entries.append('Role position: "{0}" changed from {0.position} to {1.position}')

        for e in entries:
            await self.log(before.server, e.format(before, after))

    async def on_member_join(self, member):
        entry = 'Member join: @{0} (id {0.id})'.format(member)
        await self.log(member.server, entry)

    async def on_member_remove(self, member):
        entry = 'Member leave: @{0} (id {0.id})'.format(member)
        await self.log(member.server, entry)

    async def on_member_ban(self, member):
        entry = 'Member ban: @{0} (id {0.id})'.format(member)
        await self.log(member.server, entry)

    async def on_member_unban(self, server, user):
        entry = 'Member unban: @{0} (id {0.id})'.format(user)
        await self.log(server, entry)

    async def on_member_update(self, before, after):
        if not self.should_log(before.server):
            return

        entries = []

        if before.nick != after.nick:
            entries.append('Member nickname: "@{0}" (id {0.id}) changed nickname from "{0.nick}" to "{1.nick}"')

        if before.name != after.name:
            entries.append('Member username: "@{0}" (id {0.id}) changed username from "{0.name}" to "{1.name}"')

        if before.roles != after.roles:
            broles = set(before.roles)
            aroles = set(after.roles)
            added = aroles - broles
            removed = broles - aroles

            for r in added:
                entries.append('Member role add: "{0}" (id {0.id}) role '
                               'was added to "@{{0}}" (id {{0.id}})'.format(r))

            for r in removed:
                entries.append('Member role remove: "{0}" (id {0.id}) role '
                               'was removed from "@{{0}}" (id {{0.id}})'.format(r))

        for e in entries:
            await self.log(before.server, e.format(before, after))

    async def on_channel_create(self, channel):
        if channel.is_private:
            return

        entry = 'Channel created: "{0.name}" (id {0.id})'.format(channel)
        await self.log(channel.server, entry)

    async def on_channel_delete(self, channel):
        if channel.is_private:
            return

        entry = 'Channel deleted: "{0.name}" (id {0.id})'.format(channel)
        await self.log(channel.server, entry)

    async def on_channel_update(self, before, after):
        if type(before) is discord.PrivateChannel:
            return
        elif not self.should_log(before.server):
            return

        entries = []

        if before.name != after.name:
            entries.append('Channel rename: "{0.name}" (id {0.id}) renamed to "{1.name}"')

        if before.topic != after.topic:
            entries.append('Channel topic: "{0.name}" (id {0.id}) topic was set to "{1.topic}"')

        if before.position != after.position:
            entries.append('Channel position: "{0.name}" (id {0.id}) moved from {0.position} to {1.position}')

        before_ow = dict(before.overwrites)
        after_ow = dict(after.overwrites)
        before_ow_set = set(before_ow)
        after_ow_set = set(after_ow)

        for old_ow in before_ow_set - after_ow_set:
            entries.append(self.format_overwrite(old_ow, before, before_ow[old_ow], None))

        for new_ow in after_ow_set - before_ow_set:
            entries.append(self.format_overwrite(new_ow, before, None, after_ow[new_ow]))

        for isect_ow in after_ow_set & before_ow_set:
            if before_ow[isect_ow].pair() == after_ow[isect_ow].pair():
                continue

            entries.append(self.format_overwrite(isect_ow, before, before_ow[isect_ow], after_ow[isect_ow]))

        for e in entries:
            await self.log(before.server, e.format(before, after))

    async def on_command(self, command, ctx):
        if ctx.cog is self and self.analytics:
            self.analytics.command(ctx)

    async def on_voice_state_update(self, before, after):
        if not self.should_log(before.server):
            return

        if before.voice_channel != after.voice_channel:
            if before.voice_channel:
                msg = "Voice channel leave: {0} (id {0.id})"

                if after.voice_channel:
                    msg += ' moving to {1.voice_channel}'

                await self.log(before.voice_channel, msg.format(before, after))

            if after.voice_channel:
                msg = "Voice channel join: {0} (id {0.id})"

                if before.voice_channel:
                    msg += ', moved from {0.voice_channel}'

                flags = self.get_voice_flags(after)

                if flags:
                    msg += ', flags: %s' % ','.join(flags)

                await self.log(after.voice_channel, msg.format(before, after))

        if before.deaf != after.deaf:
            verb = 'deafen' if after.deaf else 'undeafen'
            await self.log(before.voice_channel, 'Server {0}: {1} (id {1.id})'.format(verb, before))

        if before.mute != after.mute:
            verb = 'mute' if after.mute else 'unmute'
            await self.log(before.voice_channel, 'Server {0}: {1} (id {1.id})'.format(verb, before))

        if before.self_deaf != after.self_deaf:
            verb = 'deafen' if after.self_deaf else 'undeafen'
            await self.log(before.voice_channel, 'Server self-{0}: {1} (id {1.id})'.format(verb, before))

        if before.self_mute != after.self_mute:
            verb = 'mute' if after.self_mute else 'unmute'
            await self.log(before.voice_channel, 'Server self-{0}: {1} (id {1.id})'.format(verb, before))


def check_folders():
    if not os.path.exists(PATH):
        os.mkdir(PATH)


def check_files():
    if not dataIO.is_valid_json(JSON):
        defaults = {
            'everything': False,
            'attachments': False,
            'default': False
        }
        dataIO.save_json(JSON, defaults)


def setup(bot):
    check_folders()
    check_files()
    n = ActivityLogger(bot)
    bot.add_cog(n)
