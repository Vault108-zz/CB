import asyncio
import re

from cloudbot import hook
from cloudbot.util import formatting

logchannel = ""

@asyncio.coroutine
@hook.command("groups", "listgroups", "permgroups", permissions=["permissions_users"], autohelp=False)
def get_permission_groups(conn):
    """- lists all valid groups
    :type conn: cloudbot.client.Client
    """
    return "Valid groups: {}".format(conn.permissions.get_groups())


@asyncio.coroutine
@hook.command("gperms", permissions=["permissions_users"])
def get_group_permissions(text, conn, notice):
    """<group> - lists permissions given to <group>
    :type text: str
    :type conn: cloudbot.client.Client
    """
    group = text.strip()
    permission_manager = conn.permissions
    group_users = permission_manager.get_group_users(group.lower())
    group_permissions = permission_manager.get_group_permissions(group.lower())
    if group_permissions:
        return "Group {} has permissions {}".format(group, group_permissions)
    elif group_users:
        return "Group {} exists, but has no permissions".format(group)
    else:
        notice("Unknown group '{}'".format(group))


@asyncio.coroutine
@hook.command("gusers", permissions=["permissions_users"])
def get_group_users(text, conn, notice):
    """<group> - lists users in <group>
    :type text: str
    :type conn: cloudbot.client.Client
    """
    group = text.strip()
    permission_manager = conn.permissions
    group_users = permission_manager.get_group_users(group.lower())
    group_permissions = permission_manager.get_group_permissions(group.lower())
    if group_users:
        return "Group {} has members: {}".format(group, group_users)
    elif group_permissions:
        return "Group {} exists, but has no members".format(group, group_permissions)
    else:
        notice("Unknown group '{}'".format(group))


@asyncio.coroutine
@hook.command("uperms", autohelp=False)
def get_user_permissions(text, conn, mask, has_permission, notice):
    """[user] - lists all permissions given to [user], or the caller if no user is specified
    :type text: str
    :type conn: cloudbot.client.Client
    :type mask: str
    """
    if text:
        if not has_permission("permissions_users"):
            notice("Sorry, you are not allowed to use this command on another user")
            return
        user = text.strip()
    else:
        user = mask

    permission_manager = conn.permissions

    user_permissions = permission_manager.get_user_permissions(user.lower())
    if user_permissions:
        return "User {} has permissions: {}".format(user, user_permissions)
    else:
        return "User {} has no elevated permissions".format(user)


@asyncio.coroutine
@hook.command("ugroups", autohelp=False)
def get_user_groups(text, conn, mask, has_permission, notice):
    """[user] - lists all permissions given to [user], or the caller if no user is specified
    :type text: str
    :type conn: cloudbot.client.Client
    :type mask: str
    """
    if text:
        if not has_permission("permissions_users"):
            notice("Sorry, you are not allowed to use this command on another user")
            return
        user = text.strip()
    else:
        user = mask

    permission_manager = conn.permissions

    user_groups = permission_manager.get_user_groups(user.lower())
    if user_groups:
        return "User {} is in groups: {}".format(user, user_groups)
    else:
        return "User {} is in no permission groups".format(user)


@asyncio.coroutine
@hook.command("deluser", permissions=["permissions_users"])
def remove_permission_user(text, nick, bot, message, conn, notice, reply):
    """<user> [group] - removes <user> from [group], or from all groups if no group is specified
    :type text: str
    :type bot: cloudbot.bot.CloudBot
    :type conn: cloudbot.client.Client
    """
    split = text.split()
    if len(split) > 2:
        notice("Too many arguments")
        return
    elif len(split) < 1:
        notice("Not enough arguments")
        return

    if len(split) > 1:
        user = split[0]
        group = split[1]
    else:
        user = split[0]
        group = None

    permission_manager = conn.permissions
    changed = False
    if group is not None:
        if not permission_manager.group_exists(group.lower()):
            notice("Unknown group '{}'".format(group))
            return
        changed_masks = permission_manager.remove_group_user(group.lower(), user.lower())
        if changed_masks:
            changed = True
        if len(changed_masks) > 1:
            reply("Removed {} and {} from {}".format(", ".join(changed_masks[:-1]), changed_masks[-1], group))
            if logchannel:
                message("{} used deluser remove {} and {} from {}.".format(nick, ", ".join(changed_masks[:-1]), changed_masks[-1], group), logchannel)
        elif changed_masks:
            reply("Removed {} from {}".format(changed_masks[0], group))
            if logchannel:
                message("{} used deluser remove {} from {}.".format(nick, ", ".join(changed_masks[0]), group), logchannel)
        else:
            reply("No masks in {} matched {}".format(group, user))
    else:
        groups = permission_manager.get_user_groups(user.lower())
        for group in groups:
            changed_masks = permission_manager.remove_group_user(group.lower(), user.lower())
            if changed_masks:
                changed = True
            if len(changed_masks) > 1:
                reply("Removed {} and {} from {}".format(", ".join(changed_masks[:-1]), changed_masks[-1], group))
                if logchannel:
                    message("{} used deluser remove {} and {} from {}.".format(nick, ", ".join(changed_masks[:-1]), changed_masks[-1], group), logchannel)
            elif changed_masks:
                reply("Removed {} from {}".format(changed_masks[0], group))
                if logchannel:
                    message("{} used deluser remove {} from {}.".format(nick, ", ".join(changed_masks[0]), group), logchannel)
        if not changed:
            reply("No masks with elevated permissions matched {}".format(group, user))

    if changed:
        bot.config.save_config()
        permission_manager.reload()


@asyncio.coroutine
@hook.command("adduser", permissions=["permissions_users"])
def add_permissions_user(text, nick, message, conn, bot, notice, reply):
    """<user> <group> - adds <user> to <group>
    :type text: str
    :type conn: cloudbot.client.Client
    :type bot: cloudbot.bot.CloudBot
    """
    split = text.split()
    if len(split) > 2:
        notice("Too many arguments")
        return
    elif len(split) < 2:
        notice("Not enough arguments")
        return

    user = split[0]
    group = split[1]

    if not re.search('.+!.+@.+', user):
        # TODO: When we have presence tracking, check if there are any users in the channel with the nick given
        notice("The user must be in the format 'nick!user@host'")
        return

    permission_manager = conn.permissions

    group_exists = permission_manager.group_exists(group)

    changed = permission_manager.add_user_to_group(user.lower(), group.lower())

    if not changed:
        reply("User {} is already matched in group {}".format(user, group))
    elif group_exists:
        reply("User {} added to group {}".format(user, group))
        if logchannel:
                message("{} used adduser to add {} to {}.".format(nick, user, group), logchannel)
    else:
        reply("Group {} created with user {}".format(group, user))
        if logchannel:
                message("{} used adduser to create group {} and add {} to it.".format(nick, group, user), logchannel)

    if changed:
        bot.config.save_config()
        permission_manager.reload()


@asyncio.coroutine
@hook.command("stopthebot", permissions=["botcontrol"])
def stop(text, bot):
    """[reason] - stops me with [reason] as its quit message.
    :type text: str
    :type bot: cloudbot.bot.CloudBot
    """
    if text:
        yield from bot.stop(reason=text)
    else:
        yield from bot.stop()


@asyncio.coroutine
@hook.command(permissions=["botcontrol"])
def restart(text, bot):
    """[reason] - restarts me with [reason] as its quit message.
    :type text: str
    :type bot: cloudbot.bot.CloudBot
    """
    if text:
        yield from bot.restart(reason=text)
    else:
        yield from bot.restart()


@asyncio.coroutine
@hook.command(permissions=["botcontrol", "snoonetstaff"])
def join(text, conn, nick, message, notice):
    """<channel> - joins <channel>
    :type text: str
    :type conn: cloudbot.client.Client
    """
    for target in text.split():
        if not target.startswith("#"):
            target = "#{}".format(target)
        if logchannel:
            message("{} used JOIN to make me join {}.".format(nick, target), logchannel)
        notice("Attempting to join {}...".format(target))
        conn.join(target)


@asyncio.coroutine
@hook.command(permissions=["botcontrol", "snoonetstaff"], autohelp=False)
def part(text, conn, nick, message, chan, notice):
    """[#channel] - parts [#channel], or the caller's channel if no channel is specified
    :type text: str
    :type conn: cloudbot.client.Client
    :type chan: str
    """
    if text:
        targets = text
    else:
        targets = chan
    for target in targets.split():
        if not target.startswith("#"):
            target = "#{}".format(target)
        if logchannel:
            message("{} used PART to make me leave {}.".format(nick, target), logchannel)
        notice("Attempting to leave {}...".format(target))
        conn.part(target)


@asyncio.coroutine
@hook.command(autohelp=False, permissions=["botcontrol"])
def cycle(text, conn, chan, notice):
    """[#channel] - cycles [#channel], or the caller's channel if no channel is specified
    :type text: str
    :type conn: cloudbot.client.Client
    :type chan: str
    """
    if text:
        targets = text
    else:
        targets = chan
    for target in targets.split():
        if not target.startswith("#"):
            target = "#{}".format(target)
        notice("Attempting to cycle {}...".format(target))
        conn.part(target)
        conn.join(target)


@asyncio.coroutine
@hook.command(permissions=["botcontrol"])
def nick(text, conn, notice, is_nick_valid):
    """<nick> - changes my nickname to <nick>
    :type text: str
    :type conn: cloudbot.client.Client
    """
    if not is_nick_valid(text):
        notice("Invalid username '{}'".format(text))
        return

    notice("Attempting to change nick to '{}'...".format(text))
    conn.set_nick(text)


@asyncio.coroutine
@hook.command(permissions=["botcontrol"])
def raw(text, conn, notice):
    """<command> - sends <command> as a raw IRC command
    :type text: str
    :type conn: cloudbot.client.Client
    """
    notice("Raw command sent.")
    conn.send(text)


@asyncio.coroutine
@hook.command(permissions=["botcontrol", "snoonetstaff"])
def say(text, conn, chan, nick, message):
    """[#channel] <message> - says <message> to [#channel], or to the caller's channel if no channel is specified
    :type text: str
    :type conn: cloudbot.client.Client
    :type chan: str
    """
    text = text.strip()
    if text.startswith("#"):
        split = text.split(None, 1)
        channel = split[0]
        text = split[1]
    else:
        channel = chan
        text = text
    if logchannel:
            message("{} used SAY to make me SAY \"{}\" in {}.".format(nick, text, channel), logchannel)
    conn.message(channel, text)


@asyncio.coroutine
@hook.command("message", "sayto", permissions=["botcontrol", "snoonetstaff"])
def message(text, conn, nick, message):
    """<name> <message> - says <message> to <name>
    :type text: str
    :type conn: cloudbot.client.Client
    """
    split = text.split(None, 1)
    channel = split[0]
    text = split[1]
    if logchannel:
            message("{} used MESSAGE to make me SAY \"{}\" in {}.".format(nick, text, channel), logchannel)
    conn.message(channel, text)


@asyncio.coroutine
@hook.command("me", "act", permissions=["botcontrol", "snoonetstaff"])
def me(text, conn, chan, message, nick):
    """[#channel] <action> - acts out <action> in a [#channel], or in the current channel of none is specified
    :type text: str
    :type conn: cloudbot.client.Client
    :type chan: str
    """
    text = text.strip()
    if text.startswith("#"):
        split = text.split(None, 1)
        channel = split[0]
        text = split[1]
    else:
        channel = chan
        text = text
    if logchannel:
            message("{} used ME to make me ACT \"{}\" in {}.".format(nick, text, channel), logchannel)
    conn.ctcp(channel, "ACTION", text)

@asyncio.coroutine
@hook.command(autohelp=False, permissions=["botcontrol"])
def listchans(conn, chan, message, notice):
    """-- Lists the current channels the bot is in"""
    chans = ', '.join(sorted(conn.channels, key=lambda x: x.strip('#').lower()))
    lines = formatting.chunk_str("I am currently in: {}".format(chans))
    for line in lines:
        if chan[:1] == "#":
            notice(line)
        else:
            message(line)
