from __future__ import annotations

from dataclasses import dataclass

from retiboard.db.database import (
    CONTROL_ACTION_BLOCK,
    CONTROL_ACTION_HIDE,
    CONTROL_ACTION_PURGE,
    CONTROL_SCOPE_ATTACHMENT,
    CONTROL_SCOPE_IDENTITY,
    CONTROL_SCOPE_POST,
    CONTROL_SCOPE_THREAD,
    get_blob_reference,
    get_control_state as db_get_control_state,
    has_control,
)
from retiboard.db.models import PostMetadata


@dataclass(frozen=True)
class ModerationDecision:
    allowed: bool
    reason: str | None = None


async def get_control_state(db):
    return await db_get_control_state(db)


async def is_identity_blocked(db, identity_hash: str) -> bool:
    return bool(identity_hash) and await has_control(
        db, scope=CONTROL_SCOPE_IDENTITY, target_id=identity_hash, action=CONTROL_ACTION_BLOCK,
    )


async def is_identity_hidden(db, identity_hash: str) -> bool:
    return bool(identity_hash) and await has_control(
        db, scope=CONTROL_SCOPE_IDENTITY, target_id=identity_hash, action=CONTROL_ACTION_HIDE,
    )


async def is_attachment_banned(db, attachment_content_hash: str) -> bool:
    return bool(attachment_content_hash) and await has_control(
        db, scope=CONTROL_SCOPE_ATTACHMENT, target_id=attachment_content_hash,
        action=CONTROL_ACTION_BLOCK,
    )


async def is_thread_hidden(db, thread_id: str) -> bool:
    return bool(thread_id) and await has_control(db, scope=CONTROL_SCOPE_THREAD, target_id=thread_id, action=CONTROL_ACTION_HIDE)


async def is_post_hidden(db, post_id: str) -> bool:
    return bool(post_id) and await has_control(db, scope=CONTROL_SCOPE_POST, target_id=post_id, action=CONTROL_ACTION_HIDE)


async def is_thread_purged(db, thread_id: str) -> bool:
    return bool(thread_id) and await has_control(db, scope=CONTROL_SCOPE_THREAD, target_id=thread_id, action=CONTROL_ACTION_PURGE)


async def is_post_purged(db, post_id: str) -> bool:
    return bool(post_id) and await has_control(db, scope=CONTROL_SCOPE_POST, target_id=post_id, action=CONTROL_ACTION_PURGE)


async def should_reject_post(db, post: PostMetadata) -> ModerationDecision:
    if await is_identity_blocked(db, post.identity_hash):
        return ModerationDecision(False, 'blocked_identity')
    if await is_thread_purged(db, post.thread_id):
        return ModerationDecision(False, 'purged_thread')
    if await is_post_purged(db, post.post_id):
        return ModerationDecision(False, 'purged_post')
    return ModerationDecision(True, None)


async def should_replicate_post(db, post: PostMetadata) -> ModerationDecision:
    if post.is_abandoned:
        return ModerationDecision(False, 'abandoned')
    decision = await should_reject_post(db, post)
    if not decision.allowed:
        return decision
    return ModerationDecision(True, None)


async def should_serve_blob(db, blob_hash: str) -> ModerationDecision:
    ref = await get_blob_reference(db, blob_hash)
    if ref is None:
        return ModerationDecision(False, 'not_found')
    # Identity block: refuse to serve any blob from a banned identity.
    identity_hash = str(ref.get('identity_hash') or '')
    if identity_hash and await is_identity_blocked(db, identity_hash):
        return ModerationDecision(False, 'withheld_local_policy')
    blob_kind = str(ref.get('blob_kind') or '')
    # Attachment ban: refuse to serve banned attachment payloads.
    if blob_kind == 'attachments' and await is_attachment_banned(db, blob_hash):
        return ModerationDecision(False, 'withheld_local_policy')
    if blob_kind == 'attachments' and bool(ref.get('text_only')):
        return ModerationDecision(False, 'policy_rejected')
    post_id = str(ref.get('post_id') or '')
    thread_id = str(ref.get('thread_id') or '')
    if thread_id and await is_thread_purged(db, thread_id):
        return ModerationDecision(False, 'withheld_local_policy')
    if post_id and await is_post_purged(db, post_id):
        return ModerationDecision(False, 'withheld_local_policy')
    return ModerationDecision(True, None)
