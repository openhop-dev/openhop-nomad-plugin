import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "scripts/publish_release.py"
    assert path.exists(), "immutable release transport missing"
    spec = importlib.util.spec_from_file_location("publish_release", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_existing_release_compares_bytes_without_upload():
    m = module()
    assets = {"wheel.whl": b"wheel", "bundle.zip": b"bundle"}
    seen = []
    m.publish(lambda: assets, lambda: pytest.fail("must not upload"), assets,
              lambda state: seen.append(state))
    assert seen == [assets]


def test_different_or_partial_existing_release_fails_closed():
    m = module()
    for old in ({"wheel.whl": b"changed"}, {"wheel.whl": b"wheel"}):
        with pytest.raises(ValueError):
            m.publish(lambda: old, lambda: pytest.fail("must not upload"),
                      {"wheel.whl": b"wheel", "bundle.zip": b"bundle"}, lambda _: None)


def test_new_release_creation_reads_back_exact_bytes():
    m = module()
    assets = {"wheel": b"bytes"}
    states = iter([None, assets])
    calls = []
    m.publish(lambda: next(states), lambda: calls.append("create"), assets,
              lambda _: calls.append("verified"))
    assert calls == ["create", "verified"]


def test_creation_readback_mismatch_fails():
    m = module()
    states = iter([None, {"wheel": b"different"}])
    with pytest.raises(ValueError):
        m.publish(lambda: next(states), lambda: None, {"wheel": b"bytes"}, lambda _: None)
