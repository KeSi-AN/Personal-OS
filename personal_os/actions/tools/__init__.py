"""The built-in tools (DESIGN.md §6.3)."""

from __future__ import annotations

from personal_os.actions.base import Registry
from personal_os.actions.tools import commands, desktop, files, read


def build_registry() -> Registry:
    return Registry(
        [
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
        ]
    )
