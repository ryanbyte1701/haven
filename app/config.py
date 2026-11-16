import logging
import os
import re
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.getenv("HAVEN_CONFIG", "/config/config.yaml"))
EXAMPLE_PATH = Path(os.getenv("HAVEN_EXAMPLE", "/srv/config.example.yaml"))
DATA_DIR = Path(os.getenv("HAVEN_DATA", "/data"))
# Configuration you make in Haven (settings, accounts, layout, automations, trusted gateways) lives next to
# config.yaml in the config folder; DATA_DIR keeps history, sessions, sign-in tokens and certificates.
# Without a config folder (tests, running from source) both are DATA_DIR.
_conf = os.getenv("HAVEN_CONF_DIR") or (str(CONFIG_PATH.parent) if CONFIG_PATH.parent.is_dir() else "")
CONF_DIR: Path | None = Path(_conf) if _conf else None
CONFIG_FILES = ("settings.json", "users.json", "layout.json", "overrides.json", "automations.json",
                "ecowitt_gateways.json")


def conf_dir() -> Path:
    return CONF_DIR if CONF_DIR is not None else DATA_DIR

log = logging.getLogger("config")
_told = False
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
            global _told
            if not _told:
                _told = True
                log.info("%s not found, using the bundled defaults (MQTT broker \"mosquitto\", Ecowitt, history). "
                         "Add your accounts in Settings.", CONFIG_PATH)
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


STORAGE_PROBLEM = ""      # shown in the dashboard when a folder can't be written


def storage_hint(path) -> str:
    return (f"Haven can't save to {path} (running as uid {os.getuid()}, gid {os.getgid()}). "
            "Use the current docker-compose.yml (it starts with user \"0:0\" and fixes folder ownership itself), "
            "or in File Station give that user Read & Write on docker/haven.")


def check_data_dir() -> str:
    """Can Haven write its folders? Logs loudly and returns a message if not (keeps running)."""
    global STORAGE_PROBLEM
    problems = []
    for d in dict.fromkeys([DATA_DIR, conf_dir()]):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as e:
            log.error("Cannot write to %s (%s). %s", d, e, storage_hint(d))
            problems.append(storage_hint(d))
    STORAGE_PROBLEM = " ".join(problems)
    return STORAGE_PROBLEM


def write_private(path, data, binary: bool = False) -> None:
    """Atomically write a secret file that is readable only by the container user (0600 from
    creation, never world-readable even briefly)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb" if binary else "w") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
