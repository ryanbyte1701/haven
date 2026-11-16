import logging
import os
import re
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.getenv("HAVEN_CONFIG", "/config/config.yaml"))
EXAMPLE_PATH = Path(os.getenv("HAVEN_EXAMPLE", "/srv/config.example.yaml"))
DATA_DIR = Path(os.getenv("HAVEN_DATA", "/data"))
DASH_TOKEN = os.getenv("DASH_TOKEN", "")

log = logging.getLogger("config")
_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")


def _substitute(text: str) -> str:
    """Replace ${VAR} / ${VAR:-default} with environment values so secrets stay in haven.env."""
    return _ENV.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), text)


def _walk(node):
    if isinstance(node, dict):
        return {k: _walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v) for v in node]
    if isinstance(node, str) and "${" in node:
        return _substitute(node)
    return node


def load_config() -> dict:
    path = CONFIG_PATH
    if not path.exists():
        if EXAMPLE_PATH.exists():
            log.warning("%s not found, starting with the bundled example (demo devices). "
                        "Copy config.example.yaml to config.yaml to configure your devices.", CONFIG_PATH)
            path = EXAMPLE_PATH
        else:
            log.warning("%s not found, no adapters enabled", CONFIG_PATH)
            return {}
    try:
        # Parse first, then fill in ${VARS} inside string values only, so secrets containing
        # YAML-special characters (#, *, :, quotes) can't break or truncate the file.
        return _walk(yaml.safe_load(path.read_text()) or {})
    except yaml.YAMLError as e:
        log.error("config.yaml has a formatting error (check indentation, use spaces not tabs): %s", e)
        return {}


def check_data_dir() -> None:
    """Fail loudly (but keep running) if /data isn't writable: the #1 Synology permission issue."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        log.error("Cannot write to %s (%s). Running as uid=%s gid=%s. On Synology, set `user:` in "
                  "docker-compose.yml to your DSM user's UID:GID (see SYNOLOGY.md) or give that user "
                  "read/write on the data folder.", DATA_DIR, e, os.getuid(), os.getgid())
