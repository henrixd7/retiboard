"""
RetiBoard gossip synchronization engine.

Implements the §7 three-tier sync strategy:
  Tier 1 — LXMF Broadcast: new metadata sent as LXMF messages
  Tier 2 — HAVE Announcements: periodic compact thread summaries
  Tier 3 — Delta Gossip: on-demand metadata + payload fetch

Spec: §7.1, §6.2, §8.4
"""

# Protocol message type tags embedded in LXMF fields or request paths.
MSG_TYPE_METADATA = "retiboard.metadata"      # Tier 1: post metadata
MSG_TYPE_HAVE = "retiboard.have"              # Tier 2: HAVE announcement
MSG_TYPE_HAVE_REQ = "retiboard.have_req"      # Tier 2: catch-up HAVE request
MSG_TYPE_DELTA_REQ = "retiboard.delta_req"    # Tier 3: delta request
MSG_TYPE_DELTA_RES = "retiboard.delta_res"    # Tier 3: delta response
MSG_TYPE_PAYLOAD_REQ = "retiboard.payload_req"  # Tier 3: payload request
MSG_TYPE_PAYLOAD_RES = "retiboard.payload_res"  # Tier 3: payload response
MSG_TYPE_BOARD_ANNOUNCE = "retiboard.board_ann"  # Board announce push (peer discovery)
MSG_TYPE_CHUNK_MANIFEST_REQ = "retiboard.chunk_manifest_req"
MSG_TYPE_CHUNK_MANIFEST_RES = "retiboard.chunk_manifest_res"
MSG_TYPE_CHUNK_MANIFEST_UNAV = "retiboard.chunk_manifest_unav"
MSG_TYPE_CHUNK_REQ = "retiboard.chunk_req"
MSG_TYPE_CHUNK_CANCEL = "retiboard.chunk_cancel"
MSG_TYPE_CHUNK_OFFER = "retiboard.chunk_offer"
MSG_TYPE_BOARD_LIST_REQ = "retiboard.board_list_req"
MSG_TYPE_BOARD_LIST_RES = "retiboard.board_list_res"

# Request handler paths (registered on RNS Destination)
PATH_DELTA = "/retiboard/delta"
PATH_PAYLOAD = "/retiboard/payload"
