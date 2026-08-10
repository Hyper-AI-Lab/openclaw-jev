from app.artifacts.store import ArtifactStore, sha256_bytes


def test_sha256_deterministic():
    data = b"proof of completion"
    assert sha256_bytes(data) == sha256_bytes(data)
    assert len(sha256_bytes(data)) == 64


def test_storage_path_layout():
    rel = ArtifactStore._storage_rel_path(sha256_bytes(b"x"), "proof.txt")
    assert rel.endswith("proof.txt")
    assert "/" in rel
