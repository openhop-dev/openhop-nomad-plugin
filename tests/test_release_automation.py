"""Release safety contracts. Network/process side effects are injected only."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_automation", ROOT / "scripts/release_automation.py")


def module():
    assert SPEC.origin and Path(SPEC.origin).exists(), "release automation helper is missing"
    m = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(m)
    return m


@pytest.mark.parametrize("tag", ["v01.1", "v01.2.3", "1.2.3", "v1.2.3\n", "v1.2.3-rc1", "v1.2.3;id"])
def test_reject_noncanonical_tags(tag):
    with pytest.raises(ValueError):
        module().version(tag)


def test_exact_versions_required(tmp_path):
    m = module()
    assert m.version("v1.2.3") == "1.2.3"
    (tmp_path / "pyproject.toml").write_text('[project]\nname="openhop-nomad-plugin"\nversion="0.1.2"\n')
    (tmp_path / "openhop-plugin.json").write_text(json.dumps({"id": "openhop.nomad", "version": "0.1.2"}))
    m.check_versions("v0.1.2", tmp_path)
    with pytest.raises(ValueError):
        m.check_versions("v0.1.3", tmp_path)


def base():
    return {"schema": 2, "extra": True, "plugins": [
        {"id": "openhop.nomad", "version": "0.1.1", "source_revision": "a" * 40,
         "sha256": "b" * 64, "wheel_url": "old", "category": "integration", "logo": "image",
         "min_repeater_version": "0.3.0", "unknown": [1, 2]},
        {"id": "other", "version": "9.9.9"}]}


def fields():
    return dict(version="0.1.2", source_revision="c" * 40, sha256="d" * 64,
                wheel_url="https://github.com/openhop-dev/openhop-nomad-plugin/releases/download/v0.1.2/openhop_nomad_plugin-0.1.2-py3-none-any.whl")


def test_upsert_changes_only_four_fields_without_mutating_input():
    m = module()
    old = base()
    result = m.upsert(old, fields())
    expected = copy.deepcopy(old)
    expected["plugins"][0].update(fields())
    assert result == expected
    assert old == base()
    assert m.upsert(result, fields()) == result


@pytest.mark.parametrize("change", [{"version": "0.1.0"}, {"version": "0.1.1"},
                                   {"category": "other"}])
def test_downgrade_equal_changed_bytes_or_extra_fields_rejected(change):
    data = fields() | change
    with pytest.raises(ValueError):
        module().upsert(base(), data)


def run(**changes):
    return dict(id=10, name="Build Wheel", path=".github/workflows/build-wheel.yml",
                event="push", status="completed", conclusion="success", head_branch="v0.1.2",
                head_sha="c" * 40, repository={"full_name": "openhop-dev/openhop-nomad-plugin"},
                head_repository={"full_name": "openhop-dev/openhop-nomad-plugin"},
                html_url="https://github.com/openhop-dev/openhop-nomad-plugin/actions/runs/10",
                **changes)


@pytest.mark.parametrize("event", ["push", "release", "workflow_dispatch"])
def test_origin_run_binds_exact_source_repository_tag_and_workflow(event):
    r = run()
    r["event"] = event
    assert module().valid_origin(r, "v0.1.2", "c" * 40)


@pytest.mark.parametrize("key,value", [("conclusion", "failure"), ("head_sha", "d" * 40),
    ("head_branch", "main"), ("event", "pull_request"), ("path", ".github/workflows/propose-plugin-catalogue.yml"),
    ("head_repository", {"full_name": "attacker/fork"}), ("repository", {"full_name": "attacker/fork"})])
def test_wrong_origin_rejected(key, value):
    r = run()
    r[key] = value
    assert not module().valid_origin(r, "v0.1.2", "c" * 40)


def test_main_dispatch_origin_requires_title_binding():
    r = run()
    r.update(event="workflow_dispatch", head_branch="main", name="Build Wheel v0.1.2",
             display_title="Build Wheel v0.1.2")
    assert module().valid_origin(r, "v0.1.2", "c" * 40)
    r["name"] = "Build Wheel v0.1.3"
    assert not module().valid_origin(r, "v0.1.2", "c" * 40)
    r["name"] = "Build Wheel v0.1.2"
    r["display_title"] = "Build Wheel v0.1.3"
    assert not module().valid_origin(r, "v0.1.2", "c" * 40)


@pytest.mark.parametrize("trusted_ancestry", [True, False])
def test_manual_origin_uses_actual_main_run_with_older_tag_source(trusted_ancestry):
    m = module()
    r = run()
    r.update(event="workflow_dispatch", head_branch="main", head_sha="d" * 40,
             display_title="Build Wheel v0.1.2")
    class API:
        def pages(self, path, key):
            return [r] if "event=workflow_dispatch" in path else []
        def call(self, path):
            if "/actions/runs/" in path:
                return r
            if "/compare/" in path:
                sha = path.split("/compare/")[1].split("...")[0]
                return {"status": "ahead" if trusted_ancestry else "diverged", "merge_base_commit": {"sha": sha}}
            raise AssertionError(path)
    if trusted_ancestry:
        assert m.origin(API(), "v0.1.2", "c" * 40).endswith("/runs/10")
    else:
        with pytest.raises(ValueError, match="no successful Build Wheel origin"):
            m.origin(API(), "v0.1.2", "c" * 40)


def test_workflow_run_event_accepts_tag_bound_manual_name():
    m = module()
    live = run()
    live.update(name="Build Wheel v0.1.2", display_title="Build Wheel v0.1.2",
                head_branch="main", event="workflow_dispatch")
    class API:
        def call(self, path):
            assert path == "/repos/openhop-dev/openhop-nomad-plugin/actions/runs/10"
            return live
    assert m.event_tag(API(), {"workflow_run": live}, "workflow_run", "refs/heads/main", "") == "v0.1.2"


def test_only_confirmed_404_is_absence():
    m = module()
    for code in (401, 403, 429, 500):
        def api(_path, code=code):
            raise m.APIError(code)
        with pytest.raises(m.APIError):
            m.optional(api, "/release")
    def absent(_path):
        raise m.APIError(404)
    assert m.optional(absent, "/release") is None


def test_existing_assets_are_never_rebuilt_or_overwritten():
    m = module()
    names = m.asset_names("v0.1.2")
    assert m.release_state(None, "v0.1.2") == "build"
    assert m.release_state({"tag_name": "v0.1.2", "draft": False, "prerelease": False,
                            "assets": [{"name": n} for n in names]}, "v0.1.2") == "verify"
    assert m.release_state({"tag_name": "v0.1.2", "draft": False,
                            "prerelease": False, "assets": []}, "v0.1.2") == "build"
    for assets in ([{"name": names[0]}], [{"name": "unexpected"}]):
        with pytest.raises(ValueError, match="partial/unexpected assets"):
            m.release_state({"tag_name": "v0.1.2", "draft": False, "prerelease": False,
                             "assets": assets}, "v0.1.2")
    for release in ({"tag_name": "v0.1.2", "draft": True, "prerelease": False, "assets": []},
                    {"tag_name": "v0.1.2", "draft": False, "prerelease": True, "assets": []},
                    {"tag_name": "v0.1.3", "draft": False, "prerelease": False, "assets": []}):
        with pytest.raises(ValueError, match="release must be final"):
            m.release_state(release, "v0.1.2")


def test_bundle_must_contain_identical_wheel():
    import io
    import zipfile
    m = module()
    name = m.asset_names("v0.1.2")[0]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name, b"public wheel")
    m.verify_bundle(out.getvalue(), name, b"public wheel")
    with pytest.raises(ValueError):
        m.verify_bundle(out.getvalue(), name, b"changed wheel")


def test_workflow_security_and_retry_contracts():
    import yaml
    build = yaml.safe_load((ROOT / ".github/workflows/build-wheel.yml").read_text())
    proposal = ROOT / ".github/workflows/propose-plugin-catalogue.yml"
    assert proposal.exists(), "proposal workflow missing"
    propose = yaml.safe_load(proposal.read_text())
    assert build["concurrency"]["cancel-in-progress"] is False
    assert propose["permissions"] == {"contents": "read", "actions": "read"}
    for workflow in (build, propose):
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert len(step["uses"].split("@")[1]) == 40
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False
    steps = propose["jobs"]["propose"]["steps"]
    mint = next(i for i, s in enumerate(steps) if "create-github-app-token@" in s.get("uses", ""))
    assert any("prepare" in s.get("run", "") for s in steps[:mint])
    assert any("pytest" in s.get("run", "") for s in steps[:mint])
    assert "changed == 'true'" in steps[mint]["if"]
    assert all("GH_TOKEN" not in s.get("env", {}) or i > mint or s["env"]["GH_TOKEN"] == "${{ github.token }}"
               for i, s in enumerate(steps))
    validation = next(s["run"] for s in steps if "cp proposal/catalogue.json" in s.get("run", ""))
    assert validation.count('(cd plugin-catalogue && ../.automation-venv/bin/python -m pytest tests)') == 2
