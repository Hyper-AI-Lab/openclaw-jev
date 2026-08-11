"""Config path attr resolution via PEP 562 __getattr__."""
import app.config as config


def test_config_getattr_resolves_deleted_path_attr():
    # Reproduce the stale-module ImportError class: name missing from module dict.
    had = "RMP_DATA_DIR" in config.__dict__
    old = config.__dict__.pop("RMP_DATA_DIR", None)
    try:
        from app.config import RMP_DATA_DIR

        assert isinstance(RMP_DATA_DIR, str)
        assert RMP_DATA_DIR.endswith("data") or "data" in RMP_DATA_DIR
    finally:
        if had and old is not None:
            config.__dict__["RMP_DATA_DIR"] = old
        elif "RMP_DATA_DIR" not in config.__dict__:
            config.__dict__["RMP_DATA_DIR"] = config._rmp_data_dir()
