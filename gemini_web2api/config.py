"""Configuration management."""
import json
import os

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
    "sync_token": None,
}

CONFIG = dict(DEFAULT_CONFIG)
_CONFIG_PATH = None


def load_config(path: str = None):
    """Load config from JSON file."""
    global _CONFIG_PATH
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
        _CONFIG_PATH = os.path.abspath(path)
    return CONFIG


def get_config_path():
    """Absolute path of the config file that was loaded, or None."""
    return _CONFIG_PATH


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
