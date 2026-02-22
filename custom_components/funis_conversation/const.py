"""Constants for Funis conversation agent."""

DOMAIN = "funis_conversation"

CONF_NAME = "name"
CONF_RELAY_URL = "relay_url"
CONF_RELAY_TOKEN = "relay_token"
CONF_WAIT_TIMEOUT = "wait_timeout"
CONF_WAIT_POLL = "wait_poll"
CONF_CWD = "cwd"
CONF_MODEL = "model"
CONF_APPROVAL_POLICY = "approval_policy"
CONF_SANDBOX_MODE = "sandbox_mode"

DEFAULT_NAME = "Funis"
DEFAULT_RELAY_URL = "http://127.0.0.1:8765"
DEFAULT_WAIT_TIMEOUT = 120
DEFAULT_WAIT_POLL = 1.0
DEFAULT_APPROVAL_POLICY = "never"
DEFAULT_SANDBOX_MODE = "danger-full-access"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_conversation_map"
