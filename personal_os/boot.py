"""Bootstrap for background processes (launcher, daemon): run a module from this project's
site-packages under the base interpreter's real pythonw.exe.

Why: uv's venv `pythonw.exe` is a small launcher that starts a *console* `python.exe`, so a
"windowless" start still opened a terminal window. The base pythonw.exe never creates one.

Usage (built by personal_os.core.spawn): pythonw.exe -I boot.py <site-packages> <module> [args]
This file is run as a script, so it must not import anything from personal_os.
"""

import runpy
import site
import sys

site_dir, module = sys.argv[1], sys.argv[2]
before = len(sys.path)
site.addsitedir(site_dir)  # also processes .pth files (the editable install of this project)
sys.path[:] = sys.path[before:] + sys.path[:before]  # project packages win over the base's
sys.argv = [module, *sys.argv[3:]]
runpy.run_module(module, run_name="__main__", alter_sys=True)
