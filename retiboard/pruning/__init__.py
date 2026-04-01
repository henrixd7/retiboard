"""
RetiBoard pruning module.

The heartbeat of ephemerality. Runs every 15 minutes (§4) to enforce
thread abandonment and local thread caps.

Spec: §4, §2.2 (relay identical pruning)
"""
