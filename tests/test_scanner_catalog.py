"""Scanner catalog drops mocks and workspace duplicates."""
from app.scanners import catalog


def test_catalog_skips_mocks_and_workspace_duplicates(tmp_path, monkeypatch):
    harbor = tmp_path / "harbor"
    workspace = tmp_path / "ws"
    harbor.mkdir()
    workspace.mkdir()
    (harbor / "real_moltbook_scanner.js").write_text("fetch('https://moltbook.example')\n", encoding="utf-8")
    (harbor / "moltbook_continuous.js").write_text(
        "// Continuous scanning placeholder\nwhile(true){}\n", encoding="utf-8"
    )
    (harbor / "moltbook_4hr_swarm.js").write_text("Simulating posts\n", encoding="utf-8")
    (workspace / "real_moltbook_scanner.js").write_text("duplicate\n", encoding="utf-8")
    monkeypatch.setattr(catalog, "SEARCH_ROOTS", (str(harbor), str(workspace)))
    monkeypatch.setattr(catalog, "_SAFE_HARBOR", str(harbor))
    found = catalog._discover_scanners()
    assert "real_moltbook_scanner" in found
    assert "moltbook_continuous" not in found
    assert "moltbook_4hr_swarm" not in found
    paths = found["real_moltbook_scanner"].script_paths
    assert paths
    assert all(str(harbor) in p for p in paths)
    assert not any(str(workspace) in p for p in paths)
