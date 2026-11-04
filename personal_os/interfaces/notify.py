"""Windows notifications (toasts) for reminders and briefings.

Shown through PowerShell's registered app id, so they appear like any app's notification (with
sound, in the notification centre) without a new dependency. The tray balloon is the fallback.
"""

from __future__ import annotations

import os
import subprocess
import sys

from personal_os.core.logging import get_logger

_log = get_logger("notify")
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$t = [Security.SecurityElement]::Escape($env:POS_TITLE); $m = [Security.SecurityElement]::Escape($env:POS_TEXT)
$scenario = if ($env:POS_REMINDER -eq "1") { ' scenario="reminder"' } else { '' }
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast$scenario><visual><binding template=""ToastGeneric""><text>$t</text><text>$m</text></binding></visual><actions><action content=""Dismiss"" arguments=""dismiss"" activationType=""system""/></actions><audio src=""ms-winsoundevent:Notification.Reminder""/></toast>")
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:POS_APPID).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def toast(title: str, text: str, *, reminder: bool = False) -> bool:
    """Show a Windows notification. Text goes through environment variables, never the command
    line, and is XML-escaped by PowerShell. Returns False if it couldn't be shown."""
    if sys.platform != "win32":
        return False
    env = dict(
        os.environ, POS_TITLE=title[:120], POS_TEXT=text[:400], POS_APPID=POWERSHELL_APP_ID,
        POS_REMINDER="1" if reminder else "0",
    )  # fmt: skip
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
            env=env, capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        _log.warning("notification not shown")
        return False
    return True
