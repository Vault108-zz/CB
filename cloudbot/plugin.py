import asyncio
import importlib
import inspect
import logging
import re
import sys
import time
import warnings
from collections import defaultdict
from functools import partial
from itertools import chain
from operator import attrgetter
from pathlib import Path
from weakref import WeakValueDictionary

import sqlalchemy

from cloudbot.event import Event, PostHookEvent
from cloudbot.hook import Priority, Action
from cloudbot.util import database, async_util

logger = logging.getLogger("cloudbot")


def find_hooks(parent, module):
    """
    :type parent: Plugin
    :type module: object
    :rtype: dict
    """
    # set the loaded flag
    module._cloudbot_loaded = True
    hooks = defaultdict(list)
    for name, func in module.__dict__.items():
        if hasattr(func, "_cloudbot_hook"):
            # if it has cloudbot hook
            func_hooks = func._cloudbot_hook

            for hook_type, func_hook in func_hooks.items():
                hooks[hook_type].append(_hook_name_to_plugin[hook_type](parent, func_hook))

            # delete the hook to free memory
            del func._cloudbot_hook

    return hooks


def find_tables(code):
    """
    :type code: object
    :rtype: list[sqlalchemy.Table]
    """
    tables = []
    for name, obj in code.__dict__.items():
        if isinstance(obj, sqlalchemy.Table) and obj.metadata == database.metadata:
            # if it's a Table, and it's using our metadata, append it to the list
            tables.append(obj)

    return tables


class PluginManager:
    """
    PluginManager is the core of CloudBot plugin loading.

    PluginManager loads Plugins, and adds their Hooks to easy-access dicts/lists.

    Each Plugin represents a file, and loads hooks onto itself using find_hooks.

    Plugins are the lowest level of abstraction in this class. There are four different plugin types:
    - CommandPlugin is for bot commands
    - RawPlugin hooks onto irc_raw irc lines
    - RegexPlugin loads a regex parameter, and executes on irc lines which match the regex
    - SievePlugin is a catch-all sieve, which all other plugins go through before being executed.

    :type bot: cloudbot.bot.CloudBot
    :type plugins: dict[str, Plugin]
    :type commands: dict[str, CommandHook]
    :type raw_triggers: dict[str, list[RawHook]]
    :type catch_all_triggers: list[RawHook]
    :type event_type_hooks: dict[cloudbot.event.EventType, list[EventHook]]
    :type regex_hooks: list[(re.__Regex, RegexHook)]
    :type sieves: list[SieveHook]
    """

    def __init__(self, bot):
        """
        Creates a new PluginManager. You generally only need to do this from inside cloudbot.bot.CloudBot
        :type bot: cloudbot.bot.CloudBot
        """
        self.bot = bot

        self.plugins = {}
        self._plugin_name_map = WeakValueDictionary()
        self.commands = {}
        self.raw_triggers = {}
        self.catch_all_triggers = []
        self.event_type_hooks = {}
        self.regex_hooks = []
        self.sieves = []
        self.cap_hooks = {"on_available": defaultdict(list), "on_ack": defaultdict(list)}
        self.connect_hooks = []
        self.out_sieves = []
        self.hook_hooks = defaultdict(list)
        self.perm_hooks = defaultdict(list)
        self._hook_waiting_queues = {}

    def find_plugin(self, title):
        """
        Finds a loaded plugin and returns its Plugin object
        :param title: the title of the plugin to find
        :return: The Plugin object if it exists, otherwise None
        """
        return self._plugin_name_map.get(title)

    @asyncio.coroutine
    def load_all(self, plugin_dir):
        """
        Load a plugin from each *.py file in the given directory.

        Won't load any plugins listed in "disabled_plugins".

        :type plugin_dir: str
        """
        plugin_dir = Path(plugin_dir)
        # Load all .py files in the plugins directory and any subdirectory
        # But ignore files starting with _
        path_list = plugin_dir.rglob("[!_]*.py")
        # Load plugins asynchronously :O
        yield from asyncio.gather(*[self.load_plugin(path) for path in path_list], loop=self.bot.loop)

    @asyncio.coroutine
    def unload_all(self):
        yield from asyncio.gather(
            *[self.unload_plugin(path) for path in self.plugins.keys()], loop=self.bot.loop
        )

    @asyncio.coroutine
    def load_plugin(self, path):
        """
        Loads a plugin from the given path and plugin object, then registers all hooks from that plugin.

        Won't load any plugins listed in "disabled_plugins".

        :type path: str | Path
        """

        path = Path(path)
        file_path = path.resolve()
        file_name = file_path.name
        # Resolve the path relative to the current directory
        plugin_path = file_path.relative_to(self.bot.base_dir)
        title = '.'.join(plugin_path.parts[1:]).rsplit('.', 1)[0]

        if "plugin_loading" in self.bot.config:
            pl = self.bot.config.get("plugin_loading")

            if pl.get("use_whitelist", False):
                if title not in pl.get("whitelist", []):
                    logger.info('Not loading plugin module "{}": plugin not whitelisted'.format(title))
                    return
            else:
                if title in pl.get("blacklist", []):
                    logger.info('Not loading plugin module "{}": plugin blacklisted'.format(title))
                    return

        # make sure to unload the previously loaded plugin from this path, if it was loaded.
        if file_path in self.plugins:
            yield from self.unload_plugin(file_path)

        module_name = "plugins.{}".format(title)
        try:
            plugin_module = importlib.import_module(module_name)
            # if this plugin was loaded before, reload it
            if hasattr(plugin_module, "_cloudbot_loaded"):
                importlib.reload(plugin_module)
        except Exception:
            logger.exception("Error loading {}:".format(title))
            return

        # create the plugin
        plugin = Plugin(str(file_path), file_name, title, plugin_module)

        # proceed to register hooks

        # create database tables
        yield from plugin.create_tables(self.bot)

        # run on_start hooks
        for on_start_hook in plugin.hooks["on_start"]:
            success = yield from self.launch(on_start_hook, Event(bot=self.bot, hook=on_start_hook))
            if not success:
                logger.warning("Not registering hooks from plugin {}: on_start hook errored".format(plugin.title))

                # unregister databases
                plugin.unregister_tables(self.bot)
                return

        self.plugins[plugin.file_path] = plugin
        self._plugin_name_map[plugin.title] = plugin

        for on_cap_available_hook in plugin.hooks["on_cap_available"]:
            for cap in on_cap_available_hook.caps:
                self.cap_hooks["on_available"][cap.casefold()].append(on_cap_available_hook)
            self._log_hook(on_cap_available_hook)

        for on_cap_ack_hook in plugin.hooks["on_cap_ack"]:
            for cap in on_cap_ack_hook.caps:
                self.cap_hooks["on_ack"][cap.casefold()].append(on_cap_ack_hook)
            self._log_hook(on_cap_ack_hook)

        for periodic_hook in plugin.hooks["periodic"]:
            task = async_util.wrap_future(self._start_periodic(periodic_hook))
            plugin.tasks.append(task)
            self._log_hook(periodic_hook)

        # register commands
        for command_hook in plugin.hooks["command"]:
            for alias in command_hook.aliases:
                if alias in self.commands:
                    logger.warning(
                        "Plugin {} attempted to register command {} which was already registered by {}. "
                        "Ignoring new assignment.".format(plugin.title, alias, self.commands[alias].plugin.title))
                else:
                    self.commands[alias] = command_hook
            self._log_hook(command_hook)

        # register raw hooks
        for raw_hook in plugin.hooks["irc_raw"]:
            if raw_hook.is_catch_all():
                self.catch_all_triggers.append(raw_hook)
            else:
                for trigger in raw_hook.triggers:
                    if trigger in self.raw_triggers:
                        self.raw_triggers[trigger].append(raw_hook)
                    else:
                        self.raw_triggers[trigger] = [raw_hook]
            self._log_hook(raw_hook)

        # register events
        for event_hook in plugin.hooks["event"]:
            for event_type in event_hook.types:
                if event_type in self.event_type_hooks:
                    self.event_type_hooks[event_type].append(event_hook)
                else:
                    self.event_type_hooks[event_type] = [event_hook]
            self._log_hook(event_hook)

        # register regexps
        for regex_hook in plugin.hooks["regex"]:
            for regex_match in regex_hook.regexes:
                self.regex_hooks.append((regex_match, regex_hook))
            self._log_hook(regex_hook)

        # register sieves
        for sieve_hook in plugin.hooks["sieve"]:
            self.sieves.append(sieve_hook)
            self._log_hook(sieve_hook)

        # register connect hooks
        for connect_hook in plugin.hooks["on_connect"]:
            self.connect_hooks.append(connect_hook)
            self._log_hook(connect_hook)

        for out_hook in plugin.hooks["irc_out"]:
            self.out_sieves.append(out_hook)
            self._log_hook(out_hook)

        for post_hook in plugin.hooks["post_hook"]:
            self.hook_hooks["post"].append(post_hook)
            self._log_hook(post_hook)

        for perm_hook in plugin.hooks["perm_check"]:
            for perm in perm_hook.perms:
                self.perm_hooks[perm].append(perm_hook)

            self._log_hook(perm_hook)

        # sort sieve hooks by priority
        self.sieves.sort(key=lambda x: x.priority)
        self.connect_hooks.sort(key=attrgetter("priority"))

        # Sort hooks
        self.regex_hooks.sort(key=lambda x: x[1].priority)
        dicts_of_lists_of_hooks = (self.event_type_hooks, self.raw_triggers, self.perm_hooks, self.hook_hooks)
        lists_of_hooks = [self.catch_all_triggers, self.sieves, self.connect_hooks, self.out_sieves]
        lists_of_hooks.extend(chain.from_iterable(d.values() for d in dicts_of_lists_of_hooks))

        for lst in lists_of_hooks:
            lst.sort(key=attrgetter("priority"))

        # we don't need this anymore
        del plugin.hooks["on_start"]

    @asyncio.coroutine
    def unload_plugin(self, path):
        """
        Unloads the plugin from the given path, unregistering all hooks from the plugin.

        Returns True if the plugin was unloaded, False if the plugin wasn't loaded in the first place.

        :type path: str | Path
        :rtype: bool
        """
        path = Path(path)
        file_path = path.resolve()

        # make sure this plugin is actually loaded
        if str(file_path) not in self.plugins:
            return False

        # get the loaded plugin
        plugin = self.plugins[str(file_path)]

        for task in plugin.tasks:
            task.cancel()

        for on_cap_available_hook in plugin.hooks["on_cap_available"]:
            available_hooks = self.cap_hooks["on_available"]
            for cap in on_cap_available_hook.caps:
                cap_cf = cap.casefold()
                available_hooks[cap_cf].remove(on_cap_available_hook)
                if not available_hooks[cap_cf]:
                    del available_hooks[cap_cf]

        for on_cap_ack in plugin.hooks["on_cap_ack"]:
            ack_hooks = self.cap_hooks["on_ack"]
            for cap in on_cap_ack.caps:
                cap_cf = cap.casefold()
                ack_hooks[cap_cf].remove(on_cap_ack)
                if not ack_hooks[cap_cf]:
                    del ack_hooks[cap_cf]

        # unregister commands
        for command_hook in plugin.hooks["command"]:
            for alias in command_hook.aliases:
                if alias in self.commands and self.commands[alias] == command_hook:
                    # we need to make sure that there wasn't a conflict, so we don't delete another plugin's command
                    del self.commands[alias]

        # unregister raw hooks
        for raw_hook in plugin.hooks["irc_raw"]:
            if raw_hook.is_catch_all():
                self.catch_all_triggers.remove(raw_hook)
            else:
                for trigger in raw_hook.triggers:
                    assert trigger in self.raw_triggers  # this can't be not true
                    self.raw_triggers[trigger].remove(raw_hook)
                    if not self.raw_triggers[trigger]:  # if that was the last hook for this trigger
                        del self.raw_triggers[trigger]

        # unregister events
        for event_hook in plugin.hooks["event"]:
            for event_type in event_hook.types:
                assert event_type in self.event_type_hooks  # this can't be not true
                self.event_type_hooks[event_type].remove(event_hook)
                if not self.event_type_hooks[event_type]:  # if that was the last hook for this event type
                    del self.event_type_hooks[event_type]

        # unregister regexps
        for regex_hook in plugin.hooks["regex"]:
            for regex_match in regex_hook.regexes:
                self.regex_hooks.remove((regex_match, regex_hook))

        # unregister sieves
        for sieve_hook in plugin.hooks["sieve"]:
            self.sieves.remove(sieve_hook)

        # unregister connect hooks
        for connect_hook in plugin.hooks["on_connect"]:
            self.connect_hooks.remove(connect_hook)

        for out_hook in plugin.hooks["irc_out"]:
            self.out_sieves.remove(out_hook)

        for post_hook in plugin.hooks["post_hook"]:
            self.hook_hooks["post"].remove(post_hook)

        for perm_hook in plugin.hooks["perm_check"]:
            for perm in perm_hook.perms:
                self.perm_hooks[perm].remove(perm_hook)

        # Run on_stop hooks
        for on_stop_hook in plugin.hooks["on_stop"]:
            event = Event(bot=self.bot, hook=on_stop_hook)
            yield from self.launch(on_stop_hook, event)

        # unregister databases
        plugin.unregister_tables(self.bot)

        # remove last reference to plugin
        del self.plugins[plugin.file_path]

        if self.bot.config.get("logging", {}).get("show_plugin_loading", True):
            logger.info("Unloaded all plugins from {}".format(plugin.title))

        return True

    def _log_hook(self, hook):
        """
        Logs registering a given hook

        :type hook: Hook
        """
        if self.bot.config.get("logging", {}).get("show_plugin_loading", True):
            logger.info("Loaded {}".format(hook))
            logger.debug("Loaded {}".format(repr(hook)))

    def _prepare_parameters(self, hook, event):
        """
        Prepares arguments for the given hook

        :type hook: cloudbot.plugin.Hook
        :type event: cloudbot.event.Event
        :rtype: list
        """
        parameters = []
        for required_arg in hook.required_args:
            if hasattr(event, required_arg):
                value = getattr(event, required_arg)
                parameters.append(value)
            else:
                logger.error("Plugin {} asked for invalid argument '{}', cancelling execution!"
                             .format(hook.description, required_arg))
                logger.debug("Valid arguments are: {} ({})".format(dir(event), event))
                return None
        return parameters

    def _execute_hook_threaded(self, hook, event):
        """
        :type hook: Hook
        :type event: cloudbot.event.Event
        """
        event.prepare_threaded()

        parameters = self._prepare_parameters(hook, event)
        if parameters is None:
            return None

        try:
            return hook.function(*parameters)
        finally:
            event.close_threaded()

    @asyncio.coroutine
    def _execute_hook_sync(self, hook, event):
        """
        :type hook: Hook
        :type event: cloudbot.event.Event
        """
        yield from event.prepare()

        parameters = self._prepare_parameters(hook, event)
        if parameters is None:
            return None

        try:
            return (yield from hook.function(*parameters))
        finally:
            yield from event.close()

    @asyncio.coroutine
    def internal_launch(self, hook, event):
        """
        Launches a hook with the data from [event]
        :param hook: The hook to launch
        :param event: The event providing data for the hook
        :return: a tuple of (ok, result) where ok is a boolean that determines if the hook ran without error and result is the result from the hook
        """
        try:
            if hook.threaded:
                out = yield from self.bot.loop.run_in_executor(None, self._execute_hook_threaded, hook, event)
            else:
                out = yield from self._execute_hook_sync(hook, event)
        except Exception as e:
            logger.exception("Error in hook {}".format(hook.description))
            return False, e

        return True, out

    @asyncio.coroutine
    def _execute_hook(self, hook, event):
        """
        Runs the specific hook with the given bot and event.

        Returns False if the hook errored, True otherwise.

        :type hook: cloudbot.plugin.Hook
        :type event: cloudbot.event.Event
        :rtype: bool
        """
        ok, out = yield from self.internal_launch(hook, event)
        result, error = None, None
        if ok is True:
            result = out
        else:
            error = out

        post_event = partial(
            PostHookEvent, launched_hook=hook, launched_event=event, bot=event.bot,
            conn=event.conn, result=result, error=error
        )
        for post_hook in self.hook_hooks["post"]:
            success, res = yield from self.internal_launch(post_hook, post_event(hook=post_hook))
            if success and res is False:
                break

        return ok

    @asyncio.coroutine
    def _sieve(self, sieve, event, hook):
        """
        :type sieve: cloudbot.plugin.Hook
        :type event: cloudbot.event.Event
        :type hook: cloudbot.plugin.Hook
        :rtype: cloudbot.event.Event
        """
        try:
            if sieve.threaded:
                result = yield from self.bot.loop.run_in_executor(None, sieve.function, self.bot, event, hook)
            else:
                result = yield from sieve.function(self.bot, event, hook)
        except Exception:
            logger.exception("Error running sieve {} on {}:".format(sieve.description, hook.description))
            return None
        else:
            return result

    @asyncio.coroutine
    def _start_periodic(self, hook):
        interval = hook.interval
        initial_interval = hook.initial_interval
        yield from asyncio.sleep(initial_interval)

        while True:
            event = Event(bot=self.bot, hook=hook)
            yield from self.launch(hook, event)
            yield from asyncio.sleep(interval)

    @asyncio.coroutine
    def launch(self, hook, event):
        """
        Dispatch a given event to a given hook using a given bot object.

        Returns False if the hook didn't run successfully, and True if it ran successfully.

        :type event: cloudbot.event.Event | cloudbot.event.CommandEvent
        :type hook: cloudbot.plugin.Hook | cloudbot.plugin.CommandHook
        :rtype: bool
        """

        if hook.type not in ("on_start", "on_stop", "periodic"):  # we don't need sieves on on_start hooks.
            for sieve in self.bot.plugin_manager.sieves:
                event = yield from self._sieve(sieve, event, hook)
                if event is None:
                    return False

        if hook.single_thread:
            # There should only be one running instance of this hook, so let's wait for the last event to be processed
            # before starting this one.

            key = (hook.plugin.title, hook.function_name)
            if key in self._hook_waiting_queues:
                queue = self._hook_waiting_queues[key]
                if queue is None:
                    # there's a hook running, but the queue hasn't been created yet, since there's only one hook
                    queue = asyncio.Queue()
                    self._hook_waiting_queues[key] = queue
                assert isinstance(queue, asyncio.Queue)
                # create a future to represent this task
                future = asyncio.Future()
                queue.put_nowait(future)
                # wait until the last task is completed
                yield from future
            else:
                # set to None to signify that this hook is running, but there's no need to create a full queue
                # in case there are no more hooks that will wait
                self._hook_waiting_queues[key] = None

            # Run the plugin with the message, and wait for it to finish
            result = yield from self._execute_hook(hook, event)

            queue = self._hook_waiting_queues[key]
            if queue is None or queue.empty():
                # We're the last task in the queue, we can delete it now.
                del self._hook_waiting_queues[key]
            else:
                # set the result for the next task's future, so they can execute
                next_future = yield from queue.get()
                next_future.set_result(None)
        else:
            # Run the plugin with the message, and wait for it to finish
            result = yield from self._execute_hook(hook, event)

        # Return the result
        return result


class Plugin:
    """
    Each Plugin represents a plugin file, and contains loaded hooks.

    :type file_path: str
    :type file_name: str
    :type title: str
    :type hooks: dict
    :type tables: list[sqlalchemy.Table]
    """

    def __init__(self, filepath, filename, title, code):
        """
        :type filepath: str
        :type filename: str
        :type code: object
        """
        self.tasks = []
        self.file_path = filepath
        self.file_name = filename
        self.title = title
        self.hooks = find_hooks(self, code)
        # we need to find tables for each plugin so that they can be unloaded from the global metadata when the
        # plugin is reloaded
        self.tables = find_tables(code)
        # Keep a reference to this in case another plugin needs to access it
        self.code = code

    @asyncio.coroutine
    def create_tables(self, bot):
        """
        Creates all sqlalchemy Tables that are registered in this plugin

        :type bot: cloudbot.bot.CloudBot
        """
        if self.tables:
            # if there are any tables

            logger.info("Registering tables for {}".format(self.title))

            for table in self.tables:
                if not (yield from bot.loop.run_in_executor(None, table.exists, bot.db_engine)):
                    yield from bot.loop.run_in_executor(None, table.create, bot.db_engine)

    def unregister_tables(self, bot):
        """
        Unregisters all sqlalchemy Tables registered to the global metadata by this plugin
        :type bot: cloudbot.bot.CloudBot
        """
        if self.tables:
            # if there are any tables
            logger.info("Unregistering tables for {}".format(self.title))

            for table in self.tables:
                bot.db_metadata.remove(table)


class Hook:
    """
    Each hook is specific to one function. This class is never used by itself, rather extended.

    :type type; str
    :type plugin: Plugin
    :type function: callable
    :type function_name: str
    :type required_args: list[str]
    :type threaded: bool
    :type permissions: list[str]
    :type single_thread: bool
    """

    def __init__(self, _type, plugin, func_hook):
        """
        :type _type: str
        :type plugin: Plugin
        :type func_hook: hook._Hook
        """
        self.type = _type
        self.plugin = plugin
        self.function = func_hook.function
        self.function_name = self.function.__name__

        sig = inspect.signature(self.function)

        # don't process args starting with "_"
        self.required_args = [arg for arg in sig.parameters.keys() if not arg.startswith('_')]
        if sys.version_info < (3, 7, 0):
            if "async" in self.required_args:
                logger.warning("Use of deprecated function 'async' in %s", self.description)
                time.sleep(1)
                warnings.warn(
                    "event.async() is deprecated, use event.async_call() instead.",
                    DeprecationWarning, stacklevel=2
                )

        if asyncio.iscoroutine(self.function) or asyncio.iscoroutinefunction(self.function):
            self.threaded = False
        else:
            self.threaded = True

        self.permissions = func_hook.kwargs.pop("permissions", [])
        self.single_thread = func_hook.kwargs.pop("singlethread", False)
        self.action = func_hook.kwargs.pop("action", Action.CONTINUE)
        self.priority = func_hook.kwargs.pop("priority", Priority.NORMAL)

        if func_hook.kwargs:
            # we should have popped all the args, so warn if there are any left
            logger.warning("Ignoring extra args {} from {}".format(func_hook.kwargs, self.description))

    @property
    def description(self):
        return "{}:{}".format(self.plugin.title, self.function_name)

    def __repr__(self):
        return "type: {}, plugin: {}, permissions: {}, single_thread: {}, threaded: {}".format(
            self.type, self.plugin.title, self.permissions, self.single_thread, self.threaded
        )


class CommandHook(Hook):
    """
    :type name: str
    :type aliases: list[str]
    :type doc: str
    :type auto_help: bool
    """

    def __init__(self, plugin, cmd_hook):
        """
        :type plugin: Plugin
        :type cmd_hook: cloudbot.util.hook._CommandHook
        """
        self.auto_help = cmd_hook.kwargs.pop("autohelp", True)

        self.name = cmd_hook.main_alias.lower()
        self.aliases = [alias.lower() for alias in cmd_hook.aliases]  # turn the set into a list
        self.aliases.remove(self.name)
        self.aliases.insert(0, self.name)  # make sure the name, or 'main alias' is in position 0
        self.doc = cmd_hook.doc

        super().__init__("command", plugin, cmd_hook)

    def __repr__(self):
        return "Command[name: {}, aliases: {}, {}]".format(self.name, self.aliases[1:], Hook.__repr__(self))

    def __str__(self):
        return "command {} from {}".format("/".join(self.aliases), self.plugin.file_name)


class RegexHook(Hook):
    """
    :type regexes: set[re.__Regex]
    """

    def __init__(self, plugin, regex_hook):
        """
        :type plugin: Plugin
        :type regex_hook: cloudbot.util.hook._RegexHook
        """
        self.run_on_cmd = regex_hook.kwargs.pop("run_on_cmd", False)
        self.only_no_match = regex_hook.kwargs.pop("only_no_match", False)

        self.regexes = regex_hook.regexes

        super().__init__("regex", plugin, regex_hook)

    def __repr__(self):
        return "Regex[regexes: [{}], {}]".format(", ".join(regex.pattern for regex in self.regexes),
                                                 Hook.__repr__(self))

    def __str__(self):
        return "regex {} from {}".format(self.function_name, self.plugin.file_name)


class PeriodicHook(Hook):
    """
    :type interval: int
    """

    def __init__(self, plugin, periodic_hook):
        """
        :type plugin: Plugin
        :type periodic_hook: cloudbot.util.hook._PeriodicHook
        """

        self.interval = periodic_hook.interval
        self.initial_interval = periodic_hook.kwargs.pop("initial_interval", self.interval)

        super().__init__("periodic", plugin, periodic_hook)

    def __repr__(self):
        return "Periodic[interval: [{}], {}]".format(self.interval, Hook.__repr__(self))

    def __str__(self):
        return "periodic hook ({} seconds) {} from {}".format(self.interval, self.function_name, self.plugin.file_name)


class RawHook(Hook):
    """
    :type triggers: set[str]
    """

    def __init__(self, plugin, irc_raw_hook):
        """
        :type plugin: Plugin
        :type irc_raw_hook: cloudbot.util.hook._RawHook
        """
        super().__init__("irc_raw", plugin, irc_raw_hook)

        self.triggers = irc_raw_hook.triggers

    def is_catch_all(self):
        return "*" in self.triggers

    def __repr__(self):
        return "Raw[triggers: {}, {}]".format(list(self.triggers), Hook.__repr__(self))

    def __str__(self):
        return "irc raw {} ({}) from {}".format(self.function_name, ",".join(self.triggers), self.plugin.file_name)


class SieveHook(Hook):
    def __init__(self, plugin, sieve_hook):
        """
        :type plugin: Plugin
        :type sieve_hook: cloudbot.util.hook._SieveHook
        """
        super().__init__("sieve", plugin, sieve_hook)

    def __repr__(self):
        return "Sieve[{}]".format(Hook.__repr__(self))

    def __str__(self):
        return "sieve {} from {}".format(self.function_name, self.plugin.file_name)


class EventHook(Hook):
    """
    :type types: set[cloudbot.event.EventType]
    """

    def __init__(self, plugin, event_hook):
        """
        :type plugin: Plugin
        :type event_hook: cloudbot.util.hook._EventHook
        """
        super().__init__("event", plugin, event_hook)

        self.types = event_hook.types

    def __repr__(self):
        return "Event[types: {}, {}]".format(list(self.types), Hook.__repr__(self))

    def __str__(self):
        return "event {} ({}) from {}".format(self.function_name, ",".join(str(t) for t in self.types),
                                              self.plugin.file_name)


class OnStartHook(Hook):
    def __init__(self, plugin, on_start_hook):
        """
        :type plugin: Plugin
        :type on_start_hook: cloudbot.util.hook._On_startHook
        """
        super().__init__("on_start", plugin, on_start_hook)

    def __repr__(self):
        return "On_start[{}]".format(Hook.__repr__(self))

    def __str__(self):
        return "on_start {} from {}".format(self.function_name, self.plugin.file_name)


class OnStopHook(Hook):
    def __init__(self, plugin, on_stop_hook):
        super().__init__("on_stop", plugin, on_stop_hook)

    def __repr__(self):
        return "On_stop[{}]".format(Hook.__repr__(self))

    def __str__(self):
        return "on_stop {} from {}".format(self.function_name, self.plugin.file_name)


class CapHook(Hook):
    def __init__(self, _type, plugin, base_hook):
        self.caps = base_hook.caps
        super().__init__("on_cap_{}".format(_type), plugin, base_hook)

    def __repr__(self):
        return "{name}[{caps} {base!r}]".format(name=self.type, caps=self.caps, base=super())

    def __str__(self):
        return "{name} {func} from {file}".format(name=self.type, func=self.function_name, file=self.plugin.file_name)


class OnCapAvaliableHook(CapHook):
    def __init__(self, plugin, base_hook):
        super().__init__("available", plugin, base_hook)


class OnCapAckHook(CapHook):
    def __init__(self, plugin, base_hook):
        super().__init__("ack", plugin, base_hook)


class OnConnectHook(Hook):
    def __init__(self, plugin, sieve_hook):
        """
        :type plugin: Plugin
        :type sieve_hook: cloudbot.util.hook._Hook
        """
        super().__init__("on_connect", plugin, sieve_hook)

    def __repr__(self):
        return "{name}[{base!r}]".format(name=self.type, base=super())

    def __str__(self):
        return "{name} {func} from {file}".format(name=self.type, func=self.function_name, file=self.plugin.file_name)


class IrcOutHook(Hook):
    def __init__(self, plugin, out_hook):
        super().__init__("irc_out", plugin, out_hook)

    def __repr__(self):
        return "Irc_Out[{}]".format(Hook.__repr__(self))

    def __str__(self):
        return "irc_out {} from {}".format(self.function_name, self.plugin.file_name)


class PostHookHook(Hook):
    def __init__(self, plugin, out_hook):
        super().__init__("post_hook", plugin, out_hook)

    def __repr__(self):
        return "Post_hook[{}]".format(Hook.__repr__(self))

    def __str__(self):
        return "post_hook {} from {}".format(self.function_name, self.plugin.file_name)


class PermHook(Hook):
    def __init__(self, plugin, perm_hook):
        self.perms = perm_hook.perms
        super().__init__("perm_check", plugin, perm_hook)

    def __repr__(self):
        return "PermHook[{}]".format(Hook.__repr__(self))

    def __str__(self):
        return "perm hook {} from {}".format(self.function_name, self.plugin.file_name)


_hook_name_to_plugin = {
    "command": CommandHook,
    "regex": RegexHook,
    "irc_raw": RawHook,
    "sieve": SieveHook,
    "event": EventHook,
    "periodic": PeriodicHook,
    "on_start": OnStartHook,
    "on_stop": OnStopHook,
    "on_cap_available": OnCapAvaliableHook,
    "on_cap_ack": OnCapAckHook,
    "on_connect": OnConnectHook,
    "irc_out": IrcOutHook,
    "post_hook": PostHookHook,
    "perm_check": PermHook,
}
