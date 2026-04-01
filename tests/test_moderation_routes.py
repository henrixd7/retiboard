import pytest

pytest.importorskip("aiosqlite")


def test_create_moderation_router_imports():
    from retiboard.api.routes.moderation import create_moderation_router

    router = create_moderation_router()
    paths = {route.path for route in router.routes}
    assert "/api/boards/{board_id}/control/state" in paths
    assert "/api/boards/{board_id}/control/hide-thread" in paths
    assert "/api/boards/{board_id}/control/hide-post" in paths
    assert "/api/boards/{board_id}/control/purge-post" in paths
    assert "/api/boards/{board_id}/control/purge-thread" in paths
