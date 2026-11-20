"""Install the git pre-push hook that runs the bench gate (docs/BENCH.md).

    uv run python scripts/install_git_hooks.py           # install
    uv run python scripts/install_git_hooks.py --remove  # take it out

A push is refused when the code that decides what Donna understands and does changed since
the last full bench run, or that run had failures. The hook lives in .git/hooks (not
committed); an existing pre-push hook is never overwritten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MARK = "# personal-os bench gate"
HOOK = f"""#!/bin/sh
{MARK}
uv run python -m personal_os.evals.gate || {{
  echo "Push refused: run the bench first (or push with --no-verify if you mean it)." >&2
  exit 1
}}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()
    hook = Path(__file__).resolve().parents[1] / ".git" / "hooks" / "pre-push"
    ours = hook.exists() and MARK in hook.read_text("utf-8", errors="replace")
    if args.remove:
        if ours:
            hook.unlink()
            print(f"Removed {hook}")
        else:
            print("No bench gate hook installed.")
        return 0
    if hook.exists() and not ours:
        print(f"{hook} exists and isn't ours; left as it is.", file=sys.stderr)
        return 1
    hook.write_text(HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print(f"Installed {hook}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
