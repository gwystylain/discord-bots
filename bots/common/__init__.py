"""Helpers shared by every bot in this repo."""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_env(bot_name):
    """Load env/<bot_name>.env for local runs.

    In Docker the variables come from the compose `env_file` instead, and anything already set in
    the environment wins over the file.
    """
    path = os.path.join(REPO_ROOT, "env", f"{bot_name}.env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
