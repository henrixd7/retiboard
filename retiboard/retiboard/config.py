"""
RetiBoard configuration constants.

All values derive from the v3.6.4 spec. Section references in comments.

Design note: These are compile-time defaults. Board-specific overrides
(TTL, difficulty, max_payload_size, etc.) come from the board announce
schema (§3.3) and are stored per-board in meta.db at runtime — never here.
"""

import os
from pathlib import Path


# =============================================================================
# Paths (§2.2, §4, §15)
# =============================================================================

# User-local data root. Identity, boards, and all storage live here.
# This directory is sovereign — never transmitted, never shared.
RETIBOARD_HOME = Path(os.environ.get(
    "RETIBOARD_HOME",
    os.path.expanduser("~/.retiboard"),
))

# RNS identity file (§18.1). One per node.
IDENTITY_PATH = RETIBOARD_HOME / "identity"

# Unified process log file. Runtime logs stay local to the sovereign node.
LOG_PATH = RETIBOARD_HOME / "retiboard.log"

# Per-board storage root. Layout per §4:
#   boards/<board_id>/meta.db
#   boards/<board_id>/payloads/<content_hash>.bin
BOARDS_DIR = RETIBOARD_HOME / "boards"

# Rotating process log settings.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


# =============================================================================
# Network & Server (§2.2, §15)
# =============================================================================

# FastAPI binds to localhost only — the trust boundary is the local machine.
# The only network interface is RNS/LXMF, not HTTP.
API_HOST = "127.0.0.1"
API_PORT = int(os.environ.get("RETIBOARD_PORT", "8787"))

# Frontend static files (built Vue SPA).
# In production this is embedded; during dev it's the Vite output dir.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


# =============================================================================
# Ephemeral Defaults (§1, §3.3, §4)
# =============================================================================

# Default thread start TTL if not overridden by board announce.
# Spec §3.3: default_ttl_seconds = 43200 (12 hours).
DEFAULT_TTL_SECONDS = 43_200

# Background pruning interval in seconds (§4: "every 15 min").
PRUNE_INTERVAL_SECONDS = 900

# Default per-bump thread TTL refill in seconds (§3.3).
# Each bump refills up to one hour, capped by the 12h thread TTL window.
DEFAULT_BUMP_DECAY_RATE = 3_600

# Maximum active threads stored locally per board (§3.3).
DEFAULT_MAX_ACTIVE_THREADS_LOCAL = 50


# =============================================================================
# Gossip & Sync Tuning (§7.1)
# =============================================================================

# HAVE announce interval range in seconds (§7.1 Tier 2).
HAVE_INTERVAL_MIN = 300     # 5 minutes
HAVE_INTERVAL_MAX = 900     # 15 minutes

# HAVE caps: max threads included per announcement.
HAVE_MAX_THREADS_NORMAL = 20
HAVE_MAX_THREADS_LORA = 10

# Low-bandwidth HAVE interval (§7.1, §14.4).
HAVE_INTERVAL_LORA_MIN = 1_800   # 30 minutes
HAVE_INTERVAL_LORA_MAX = 3_600   # 60 minutes

# Delta gossip limits (§7.1 Tier 3).
DELTA_MAX_RECORDS = 50
DELTA_MAX_BYTES = 16_384     # 16 KB

# Max concurrent thread syncs per board (§7.2).
MAX_CONCURRENT_SYNCS = 5
MAX_CONCURRENT_SYNCS_LORA = 2

# Opportunistic replication fan-out (§7.3).
REPLICATION_FANOUT = 3

# Parallel payload fetch peers (§7.1 Tier 3).
PAYLOAD_FETCH_PEERS = 3

# Metadata sync resource caps (§15).
# Prevent "HAVE bombing" (processing 1000s of threads in one packet).
MAX_HAVE_THREADS_IN_PACKET = 250
# Max threads to sync in a single delta request.
MAX_DELTA_BATCH_SIZE = 50

# RNS Announce app_data limit (§3.3).
# Reticulum MTU is 500. Accounting for headers and signature,
# app_data should stay below ~384 bytes to avoid OSError.
MAX_ANNOUNCE_APP_DATA = 384

# =============================================================================
# Transport & Bandwidth (§14, §7.1 Tier 2)
# =============================================================================

# Threshold for "low bandwidth" classification in bits per second.
# §7.1: "detected via RNS link speed < 10 kbit/s"
LOW_BANDWIDTH_THRESHOLD_BPS = 10_000

# Maximum payload size (encrypted blob) in bytes.
# Normal (TCP/IP): 10 MB — generous for attachment-capable boards.
# LoRa/slow: 64 KB — text posts are fine, small files possible,
#   but large attachments are impractical on constrained links.
# Board creators can further restrict this via max_payload_size in
# their announce schema (sovereignty — the board creator decides).
MAX_PAYLOAD_SIZE_NORMAL = 10 * 1024 * 1024   # 10 MB
MAX_PAYLOAD_SIZE_LORA = 64 * 1024            # 64 KB


# =============================================================================
# Anti-Spam (§17)
# =============================================================================

# Default PoW difficulty. 0 = disabled (trusted/private boards).
DEFAULT_POW_DIFFICULTY = 0

# Local PoW difficulty cap (§14.5).
# If a board's declared difficulty exceeds this, the client warns the
# user and refuses to post. 0 = no local cap (unlimited).
# Board operators targeting LoRa audiences should set low difficulty.
# Override via RETIBOARD_POW_DIFFICULTY_CAP environment variable.
LOCAL_POW_DIFFICULTY_CAP = int(os.environ.get(
    "RETIBOARD_POW_DIFFICULTY_CAP", "0"
))


# =============================================================================
# Application Metadata
# =============================================================================

APP_NAME = "RetiBoard"
APP_VERSION = "0.1.0"
SPEC_VERSION = "3.6.4"

# =============================================================================
# Default RNS Config Template
# =============================================================================

DEFAULT_RNS_CONFIG = """# This is the default Reticulum config file.
# You should probably edit it to include any additional,
# interfaces and settings you might need.

# Only the most basic options are included in this default
# configuration. To see a more verbose, and much longer,
# configuration example, you can run the command:
# rnsd --exampleconfig


[reticulum]
  
  # If you enable Transport, your system will route traffic
  # for other peers, pass announces and serve path requests.
  # This should only be done for systems that are suited to
  # act as transport nodes, ie. if they are stationary and
  # always-on. This directive is optional and can be removed
  # for brevity.
  
  enable_transport = False
  
  
  # By default, the first program to launch the Reticulum
  # Network Stack will create a shared instance, that other
  # programs can communicate with. Only the shared instance
  # opens all the configured interfaces directly, and other
  # local programs communicate with the shared instance over
  # a local socket. This is completely transparent to the
  # user, and should generally be turned on. This directive
  # is optional and can be removed for brevity.
  
  share_instance = Yes
  
  
  # If you want to run multiple *different* shared instances
  # on the same system, you will need to specify different
  # instance names for each. On platforms supporting domain
  # sockets, this can be done with the instance_name option:
  
  instance_name = default


# Some platforms don't support domain sockets, and if that
# is the case, you can isolate different instances by
# specifying a unique set of ports for each:

# shared_instance_port = 37428
# instance_control_port = 37429


# If you want to explicitly use TCP for shared instance
# communication, instead of domain sockets, this is also
# possible, by using the following option:

# shared_instance_type = tcp


# You can configure whether Reticulum should discover
# available interfaces from other Transport Instances over
# the network. If this option is enabled, Reticulum will
# collect interface information discovered from the network.

# discover_interfaces = No


# You can configure Reticulum to panic and forcibly close
# if an unrecoverable interface error occurs, such as the
# hardware device for an interface disappearing. This is
# an optional directive, and can be left out for brevity.
# This behaviour is disabled by default.

# panic_on_interface_error = No


# If you're connecting to a large external network, you
# can use one or more external blackhole list to block
# spammy and excessive announces onto your network. This
# funtionality is especially useful if you're hosting public
# entrypoints or gateways. The list source below provides a
# functional example, but better, more timely maintained
# lists probably exist in the community.

# blackhole_sources = 521c87a83afb8f29e4455e77930b973b


[logging]
  # Valid log levels are 0 through 7:
  #   0: Log only critical information
  #   1: Log errors and lower log levels
  #   2: Log warnings and lower log levels
  #   3: Log notices and lower log levels
  #   4: Log info and lower (this is the default)
  #   5: Verbose logging
  #   6: Debug logging
  #   7: Extreme logging
  
  loglevel = 4


# The interfaces section defines the physical and virtual
# interfaces Reticulum will use to communicate on. This
# section will contain examples for a variety of interface
# types. You can modify these or use them as a basis for
# your own config, or simply remove the unused ones.

[interfaces]
  
  # This interface enables communication with other
  # link-local Reticulum nodes over UDP. It does not
  # need any functional IP infrastructure like routers
  # or DHCP servers, but will require that at least link-
  # local IPv6 is enabled in your operating system, which
  # should be enabled by default in almost any OS. See
  # the Reticulum Manual for more configuration options.
  
  [[Default Interface]]
    type = AutoInterface
    enabled = No

  [[TCP Transport]]
    type = TCPClientInterface
    enabled = yes
    target_host = nomad.truewall.eu
    target_port = 4242
"""
