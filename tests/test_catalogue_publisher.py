"""Write transport tests: all remote effects mocked, no live mutation."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "scripts/publish_catalogue.py"
    assert path.exists(), "narrow catalogue publisher missing"
    spec = importlib.util.spec_from_file_location("publisher", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Remote:
    def __init__(self, draft=None):
        self.calls = []
        self.sha = "a" * 40
        self.pr = None if draft is None else {"number": 1, "draft": draft,
            "state": "open", "title": "old", "body": "old", "base": {"ref": "main"},
            "head": {"ref": "automation/openhop-nomad-v0.1.2", "sha": "b" * 40}}

    def __call__(self, path, method="GET", data=None):
        self.calls.append((path, method, data))
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": self.sha}}
        if "?state=open" in path:
            return [] if self.pr is None else [self.pr.copy()]
        if path.endswith("/pulls") and method == "POST":
            self.pr = {**data, "number": 1, "state": "open", "head": {"ref": data["head"], "sha": "c" * 40},
                       "base": {"ref": data["base"]}}
            return self.pr.copy()
        if path.endswith("/pulls/1"):
            if method == "PATCH":
                self.pr.update(data)
                self.pr["head"]["sha"] = "c" * 40
            return self.pr.copy()
        raise AssertionError(path)


def receipt():
    return {"changed": True, "tag": "v0.1.2", "branch": "automation/openhop-nomad-v0.1.2",
            "base_sha": "a" * 40, "remote_sha": "b" * 40,
            "origin_run_url": "https://github.com/openhop-dev/openhop-nomad-plugin/actions/runs/10",
            "fields": {"version": "0.1.2", "source_revision": "d" * 40,
                       "wheel_url": "https://github.com/openhop-dev/openhop-nomad-plugin/releases/download/v0.1.2/openhop_nomad_plugin-0.1.2-py3-none-any.whl",
                       "sha256": "e" * 64}}


@pytest.mark.parametrize("draft", [None, True, False])
def test_publish_ready_once_preserves_draft_on_retry_and_reads_back(draft):
    m = module()
    api = Remote(draft)
    pushed = []
    def push(data):
        pushed.append(data)
        return "c" * 40
    m.publish(api, push, receipt())
    assert pushed == [receipt()]
    assert api.pr["draft"] == (False if draft is None else draft)
    writes = [c for c in api.calls if c[1] != "GET"]
    assert len(writes) == 1
    assert writes[0][1] == ("POST" if draft is None else "PATCH")
    assert api.calls[-1][1] == "GET"
    assert "actions/runs/10" in api.pr["body"]
    if draft is not None:
        assert "draft" not in writes[0][2]


def test_main_moved_fails_before_push():
    m = module()
    api = Remote()
    api.sha = "f" * 40
    with pytest.raises(ValueError):
        m.publish(api, lambda _: pytest.fail("must not push"), receipt())
    assert all(c[1] == "GET" for c in api.calls)


def test_noop_does_not_touch_remote():
    module().publish(lambda *a, **kw: pytest.fail("no-op API"),
                     lambda _: pytest.fail("no-op push"), {"changed": False})


def test_explicit_lease_even_when_branch_absent():
    m = module()
    for sha in ("", "b" * 40):
        cmd = m.push_args("automation/openhop-nomad-v0.1.2", sha)
        assert "--force-with-lease=refs/heads/automation/openhop-nomad-v0.1.2:" + sha in cmd
        assert "--force" not in cmd


def test_failed_pr_readback_is_not_success():
    m = module()
    api = Remote()
    def changed(path, method="GET", data=None):
        result = api(path, method, data)
        if path.endswith("/pulls/1") and method == "GET":
            result["head"]["sha"] = "f" * 40
        return result
    with pytest.raises(ValueError):
        m.publish(changed, lambda _: "c" * 40, receipt())
