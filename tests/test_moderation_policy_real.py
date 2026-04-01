import importlib
import sys
import types

import pytest


def load_policy_module(monkeypatch):
    original_aiosqlite = sys.modules.get("aiosqlite")
    if 'aiosqlite' not in sys.modules:
        fake = types.ModuleType('aiosqlite')
        fake.Connection = object
        fake.Row = object
        fake.IntegrityError = RuntimeError
        sys.modules['aiosqlite'] = fake

    try:
        policy = importlib.import_module('retiboard.moderation.policy')
    finally:
        if original_aiosqlite is None:
            sys.modules.pop("aiosqlite", None)
        else:
            sys.modules["aiosqlite"] = original_aiosqlite
    return policy


@pytest.mark.asyncio
async def test_should_reject_post_precedence(monkeypatch):
    policy = load_policy_module(monkeypatch)

    async def fake_has_control(_db, *, scope, target_id, action):
        active = {
            ('thread', 't1', 'purge'),
            ('post', 'p1', 'purge'),
            ('post', 'p1', 'hide'),
        }
        return (scope, target_id, action) in active

    monkeypatch.setattr(policy, 'has_control', fake_has_control)

    post = policy.PostMetadata(
        post_id='p1',
        thread_id='t1',
        parent_id='t1',
        timestamp=1,
        expiry_timestamp=2,
        bump_flag=False,
        content_hash='c1',
        payload_size=10,
        identity_hash='id1',
    )

    decision = await policy.should_reject_post(None, post)
    assert decision.allowed is False
    assert decision.reason == 'purged_thread'


@pytest.mark.asyncio
async def test_should_serve_blob_allows_hidden_post(monkeypatch):
    policy = load_policy_module(monkeypatch)

    async def fake_get_blob_reference(_db, blob_hash):
        assert blob_hash == 'blob1'
        return {
            'blob_kind': 'text',
            'post_id': 'p1',
            'thread_id': 't1',
            'identity_hash': 'id1',
            'text_only': False,
        }

    async def fake_has_control(_db, *, scope, target_id, action):
        return (scope, target_id, action) == ('post', 'p1', 'hide')

    monkeypatch.setattr(policy, 'get_blob_reference', fake_get_blob_reference)
    monkeypatch.setattr(policy, 'has_control', fake_has_control)

    decision = await policy.should_serve_blob(None, 'blob1')
    assert decision.allowed is True
    assert decision.reason is None


@pytest.mark.asyncio
async def test_should_serve_blob_withheld_local_policy_for_purged_post(monkeypatch):
    policy = load_policy_module(monkeypatch)

    async def fake_get_blob_reference(_db, blob_hash):
        assert blob_hash == 'blob1'
        return {
            'blob_kind': 'text',
            'post_id': 'p1',
            'thread_id': 't1',
            'identity_hash': 'id1',
            'text_only': False,
        }

    async def fake_has_control(_db, *, scope, target_id, action):
        return (scope, target_id, action) == ('post', 'p1', 'purge')

    monkeypatch.setattr(policy, 'get_blob_reference', fake_get_blob_reference)
    monkeypatch.setattr(policy, 'has_control', fake_has_control)

    decision = await policy.should_serve_blob(None, 'blob1')
    assert decision.allowed is False
    assert decision.reason == 'withheld_local_policy'


@pytest.mark.asyncio
async def test_should_serve_blob_not_found(monkeypatch):
    policy = load_policy_module(monkeypatch)

    async def fake_get_blob_reference(_db, _blob_hash):
        return None

    monkeypatch.setattr(policy, 'get_blob_reference', fake_get_blob_reference)

    decision = await policy.should_serve_blob(None, 'missing')
    assert decision.allowed is False
    assert decision.reason == 'not_found'


@pytest.mark.asyncio
async def test_should_serve_blob_policy_rejected_for_text_only_media(monkeypatch):
    policy = load_policy_module(monkeypatch)

    async def fake_get_blob_reference(_db, blob_hash):
        assert blob_hash == 'blob2'
        return {
            'blob_kind': 'attachments',
            'post_id': 'p2',
            'thread_id': 't2',
            'identity_hash': 'id2',
            'text_only': True,
        }

    async def fake_has_control(_db, *, scope, target_id, action):
        return False

    monkeypatch.setattr(policy, 'get_blob_reference', fake_get_blob_reference)
    monkeypatch.setattr(policy, 'has_control', fake_has_control)

    decision = await policy.should_serve_blob(None, 'blob2')
    assert decision.allowed is False
    assert decision.reason == 'policy_rejected'


@pytest.mark.asyncio
async def test_should_replicate_post_allows_hidden_post(monkeypatch):
    policy = load_policy_module(monkeypatch)

    async def fake_has_control(_db, *, scope, target_id, action):
        return (scope, target_id, action) == ('post', 'p1', 'hide')

    monkeypatch.setattr(policy, 'has_control', fake_has_control)

    post = policy.PostMetadata(
        post_id='p1',
        thread_id='t1',
        parent_id='t1',
        timestamp=1,
        expiry_timestamp=2,
        bump_flag=False,
        content_hash='c1',
        payload_size=10,
        identity_hash='id1',
    )

    decision = await policy.should_replicate_post(None, post)
    assert decision.allowed is True
    assert decision.reason is None
