from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_board_selector_groups_discovered_name_collisions_and_shows_transparent_metrics():
    content = (ROOT / "frontend/src/components/BoardSelector.vue").read_text()
    assert "const discoveredGroups = computed(() => {" in content
    assert "group.items.length > 1 ? 'Subscribe best' : 'Subscribe'" in content
    assert "verified {{ group.best.verified_peer_count }}" in content
    assert "advertising {{ group.best.advertising_peer_count }}" in content
    assert "announces {{ group.best.announce_seen_count }}" in content
    assert "toggleDiscoveredGroup(group.key)" in content
    assert "class=\"btn-dim btn-sm\"" in content
    assert "v-if=\"isDiscoveredExpanded(group.key)\"" in content


def test_catalog_and_home_views_surface_network_status_transparency():
    catalog = (ROOT / "frontend/src/views/CatalogView.vue").read_text()
    home = (ROOT / "frontend/src/views/HomeView.vue").read_text()
    assert "NetworkStatusIndicator" in catalog
    assert "NetworkStatusIndicator" in home
