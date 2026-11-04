"""User-written tools (brief §7: a plugin API). Off until `actions.plugins_enabled`.

A plugin is a .py file in `plugins/` in the data folder defining `TOOLS = [MyTool(), ...]`,
each a `personal_os.actions.base.Tool` (args model, tier, preview, run). Loading a plugin runs
its code, so only add files you wrote or read. A plugin's tier is honoured; anything claiming
tier 3 is blocked like the built-in tier 3 tools, and plugin tools are marked in the log.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from personal_os.actions.base import Tool
from personal_os.core.logging import get_logger

_log = get_logger("plugins")


@dataclass
class PluginReport:
    loaded: list[str]
    errors: list[str]


def plugin_dir(data_dir: Path) -> Path:
    return data_dir / "plugins"


def load_plugins(data_dir: Path, reserved: set[str]) -> tuple[list[Tool], PluginReport]:
    tools: list[Tool] = []
    report = PluginReport([], [])
    folder = plugin_dir(data_dir)
    if not folder.is_dir():
        return tools, report
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"personal_os_plugin_{path.stem}", path)
            if spec is None or spec.loader is None:
                raise ImportError("can't load")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            found = getattr(module, "TOOLS", [])
            for tool in found:
                if not isinstance(tool, Tool):
                    raise TypeError(f"{tool!r} isn't a Tool")
                if tool.name in reserved:
                    raise ValueError(f"{tool.name} would replace a built-in tool")
                type(tool).plugin = True  # type: ignore[attr-defined]
                tools.append(tool)
                reserved.add(tool.name)
                report.loaded.append(f"{path.name}:{tool.name}")
        except Exception as exc:  # one broken plugin mustn't break the rest
            report.errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            _log.warning("plugin not loaded", extra={"fields": {"file": path.name}})
    return tools, report


EXAMPLE = '''"""An example plugin: copy it to plugins/ and set actions.plugins_enabled = true."""

from pydantic import Field

from personal_os.actions.base import Args, Result, Tier, Tool, ToolContext


class WordCountArgs(Args):
    text: str = Field(max_length=20000)


class WordCount(Tool):
    name = "word_count"
    summary = "count the words in a piece of text"
    tier = Tier.READ
    args_model = WordCountArgs

    def preview(self, args: WordCountArgs, ctx: ToolContext) -> str:
        return f"count the words in {len(args.text)} characters of text"

    def run(self, args: WordCountArgs, ctx: ToolContext) -> Result:
        n = len(args.text.split())
        return Result(f"{n} words", data=str(n))


TOOLS = [WordCount()]
'''
