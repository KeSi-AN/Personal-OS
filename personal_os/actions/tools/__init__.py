"""The built-in tools (DESIGN.md §6.3)."""

from __future__ import annotations

from personal_os.actions.base import Registry, Tool
from personal_os.actions.tools import agent, commands, desktop, files, google_tools, notify, read
from personal_os.config.schema import Config


def build_registry(config: Config | None = None) -> Registry:
    """The built-in tools, plus plugins when `actions.plugins_enabled` (needs the config)."""
    tools: list[Tool] = [
        agent.Now(),
        agent.Recall(),
        agent.ListReminders(),
        agent.Memory(),
        read.SearchContext(),
        read.ReadFile(),
        read.ListFolder(),
        read.SystemStatus(),
        desktop.OpenPath(),
        desktop.OpenUrl(),
        desktop.OpenApp(),
        desktop.CreateNote(),
        desktop.CopyToClipboard(),
        desktop.Media(),
        desktop.Screenshot(),
        desktop.Window(),
        desktop.RemoveCreated(),
        notify.SetReminder(),
        notify.UpdateReminder(),
        notify.CancelReminder(),
        notify.SetBrightness(),
        notify.SetVolume(),
        notify.LockScreen(),
        notify.RevealPath(),
        files.CreateFile(),
        files.EditFile(),
        files.RestoreFile(),
        files.MovePath(),
        files.DeletePath(),
        commands.RunCommand(),
        commands.RunShell(),
        commands.GitCommit(),
        commands.GitUncommit(),
        commands.GitSwitch(),
        commands.GitStash(),
        commands.GitStashPop(),
        google_tools.CalendarToday(),
        google_tools.MailUnread(),
    ]
    if config is not None and config.actions.plugins_enabled:
        from personal_os.actions.plugins import load_plugins

        extra, _report = load_plugins(config.data_dir, {t.name for t in tools})
        tools += extra
    return Registry(tools)
