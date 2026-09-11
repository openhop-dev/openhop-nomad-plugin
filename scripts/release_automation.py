#!/usr/bin/env python3
"""Trusted-main NOMAD release preparation. No remote writes in this module.

Catalogue main owns wheel/profile validation; never import producer release code
or run this verifier in the catalogue installation-token step.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
import zipfile

REPOSITORY = "openhop-dev/openhop-nomad-plugin"
CATALOGUE = "openhop-dev/openhop-plugin-catalogue"
PLUGIN = "openhop.nomad"
FIELDS = {"version", "source_revision", "wheel_url", "sha256"}
SHA = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def version(tag):
    require(isinstance(tag, str) and TAG.fullmatch(tag), "tag must be vMAJOR.MINOR.PATCH")
    return tag[1:]


def check_versions(tag, root):
    v = version(tag)
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    manifest = json.loads((root / "openhop-plugin.json").read_text())
    require(project["version"] == manifest["version"] == v, "source versions differ from tag")
    require(project["name"] == "openhop-nomad-plugin" and manifest["id"] == PLUGIN,
            "wrong source package")


def asset_names(tag):
    return (f"openhop_nomad_plugin-{version(tag)}-py3-none-any.whl",
            f"openhop-nomad-plugin-{tag}-wheel.zip")


def release_state(release, tag):
    names = asset_names(tag)
    if release is None:
        return "build"
    require(release["tag_name"] == tag and not release["draft"] and not release["prerelease"],
            "release must be final")
    actual = [a["name"] for a in release["assets"]]
    require(sorted(actual) == sorted(names), "partial/unexpected assets: deliberate recovery required")
    return "verify"


def verify_bundle(raw, name, wheel):
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        require(bundle.namelist() == [name], "bundle must contain exactly the wheel")
        info = bundle.getinfo(name)
        require(info.file_size == len(wheel), "bundle size mismatch")
        require(bundle.read(name) == wheel, "bundle bytes differ from public wheel")


def upsert(base, fields):
    require(set(fields) == FIELDS, "only four release fields permitted")
    version("v" + fields["version"])
    require(SHA.fullmatch(fields["source_revision"]), "invalid source SHA")
    require(re.fullmatch(r"[0-9a-f]{64}", fields["sha256"]), "invalid digest")
    result = copy.deepcopy(base)
    entries = [p for p in result["plugins"] if p["id"] == PLUGIN]
    require(len(entries) == 1, "exactly one registered NOMAD catalogue entry required")
    entry = entries[0]
    old = tuple(map(int, version("v" + entry["version"]).split(".")))
    new = tuple(map(int, fields["version"].split(".")))
    require(new >= old, "refusing downgrade")
    if new == old:
        require(all(entry.get(k) == v for k, v in fields.items()),
                "equal version metadata/bytes are immutable")
    entry.update(fields)
    return result


def valid_origin(run, tag, sha):
    branch = run.get("head_branch")
    bound = branch == tag or (run.get("event") == "workflow_dispatch" and branch == "main"
                             and run.get("display_title") == f"Build Wheel {tag}")
    return bool(bound and run.get("head_sha") == sha
                and run.get("event") in {"push", "release", "workflow_dispatch"}
                and run.get("status") == "completed" and run.get("conclusion") == "success"
                and run.get("path") == ".github/workflows/build-wheel.yml"
                and run.get("name") == "Build Wheel"
                and run.get("repository", {}).get("full_name") == REPOSITORY
                and run.get("head_repository", {}).get("full_name") == REPOSITORY)


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub API failed ({status}); no write attempted")


def optional(call, path):
    try:
        return call(path)
    except APIError as exc:
        if exc.status == 404:
            return None
        raise


class API:
    """Read-only GitHub transport. Never infer absence from CLI error text."""
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN", "")
        if not self.token:
            result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
            require(result.returncode == 0, "read-only GitHub authentication required")
            self.token = result.stdout.strip()

    def call(self, path):
        require(path.startswith("/repos/") and not any(c in path for c in "\r\n"), "invalid API path")
        request = urllib.request.Request("https://api.github.com" + path, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nomad-release-verifier"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                require(len(raw) <= 4 * 1024 * 1024, "oversized API response")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code) from None

    def pages(self, path, key=None):
        result = []
        for page in range(1, 11):
            data = self.call(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
            rows = data[key] if key else data
            require(isinstance(rows, list), "invalid API listing")
            result.extend(rows)
            if len(rows) < 100:
                if key and "total_count" in data:
                    require(len(result) == data["total_count"], "incomplete API enumeration")
                return result
        raise ValueError("pagination limit exceeded")


def load_policy(root):
    spec = importlib.util.spec_from_file_location("catalogue_policy", root / "scripts/publishing_policy.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    c = policy.registration(PLUGIN)
    require(c["source_repository"] == c["artifact_repository"] == REPOSITORY
            and c["package_profile"] == "python-service-v1"
            and c["branch_prefix"] == "automation/openhop-nomad-v"
            and c["source_verification"] == "public-tag"
            and c["release_assets"] == "wheel-and-zip", "NOMAD registration mismatch")
    return policy


def trusted_source(api, policy, tag):
    version(tag)
    sha = policy.resolve_tag(api, REPOSITORY, tag, PLUGIN)
    compare = api.call(f"/repos/{REPOSITORY}/compare/{sha}...main")
    require(compare["status"] in {"ahead", "identical"}
            and compare["merge_base_commit"]["sha"] == sha, "release source is not on trusted main")
    def source(path):
        blob = api.call(f"/repos/{REPOSITORY}/contents/{path}?ref={sha}")
        require(blob["type"] == "file" and blob["encoding"] == "base64", "source must be regular file")
        return base64.b64decode(blob["content"])
    project = tomllib.loads(source("pyproject.toml").decode())["project"]
    manifest = json.loads(source("openhop-plugin.json"))
    require(project["version"] == manifest["version"] == version(tag), "source versions differ from tag")
    require(project["name"] == "openhop-nomad-plugin" and manifest["id"] == PLUGIN, "wrong source package")
    return sha


def origin(api, tag, sha):
    runs = api.pages(f"/repos/{REPOSITORY}/actions/workflows/build-wheel.yml/runs?head_sha={sha}&status=success",
                     "workflow_runs")
    matches = [r for r in runs if valid_origin(r, tag, sha)]
    # A main-dispatched build checks out the tag separately: Actions head_sha is
    # automation main, NOT the built source. Only our tag-bound main workflow
    # contract can supply this fallback; never relabel the proposal retry run.
    def manual_origin(run):
        head = run.get("head_sha", "")
        if not (run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main"
                and SHA.fullmatch(head) and valid_origin(run, tag, head)):
            return False
        for base, target in ((sha, head), (head, "main")):
            compare = api.call(f"/repos/{REPOSITORY}/compare/{base}...{target}")
            if not (compare["status"] in {"ahead", "identical"}
                    and compare["merge_base_commit"]["sha"] == base):
                return False
        return True
    if not matches:
        manual = api.pages(f"/repos/{REPOSITORY}/actions/workflows/build-wheel.yml/runs?event=workflow_dispatch&status=success",
                           "workflow_runs")
        matches = [r for r in manual if manual_origin(r)]
    require(matches, "no successful Build Wheel origin bound to tag/source")
    # Prefer the actual tag publication rather than a later retry.
    selected = min(matches, key=lambda r: r["id"])
    live = api.call(f"/repos/{REPOSITORY}/actions/runs/{selected['id']}")
    require(valid_origin(live, tag, sha) or manual_origin(live), "origin changed")
    return f"https://github.com/{REPOSITORY}/actions/runs/{live['id']}"


def event_tag(api, event, event_name, ref, manual_tag):
    if event_name == "workflow_dispatch":
        require(ref == "refs/heads/main", "manual proposal must run on main")
        version(manual_tag)
        return manual_tag
    require(event_name == "workflow_run", "unsupported proposal event")
    supplied = event["workflow_run"]
    require(type(supplied["id"]) is int and supplied["id"] > 0, "invalid run ID")
    live = api.call(f"/repos/{REPOSITORY}/actions/runs/{supplied['id']}")
    require(all(live.get(k) == supplied.get(k) for k in
                ("head_sha", "head_branch", "event", "conclusion", "path")), "event/run mismatch")
    tag = live["head_branch"]
    if live["event"] == "workflow_dispatch" and tag == "main":
        title = live.get("display_title", "")
        require(title.startswith("Build Wheel "), "manual build missing tag binding")
        tag = title.removeprefix("Build Wheel ")
    version(tag)
    require(valid_origin(live, tag, live["head_sha"]), "untrusted triggering Build Wheel run")
    return tag


def public_release(api, policy, tag, sha, entry):
    item = copy.deepcopy(entry)
    item.update(version=version(tag), source_revision=sha, wheel_url=policy.wheel_url(version(tag), PLUGIN))
    evidence = policy.verify_release(api, item)
    raw = policy.download(item["wheel_url"])
    require(len(raw) == evidence["asset_size"], "public wheel size mismatch")
    item["sha256"] = hashlib.sha256(raw).hexdigest()
    policy.verify_wheel(raw, item)
    wheel_name, bundle_name = asset_names(tag)
    bundle_asset = next(a for a in evidence["assets"] if a["name"] == bundle_name)
    bundle = policy.download(bundle_asset["browser_download_url"])
    require(len(bundle) == bundle_asset["size"], "public bundle size mismatch")
    verify_bundle(bundle, wheel_name, raw)
    # Read back mutable release metadata/tag after downloading.
    require(policy.verify_release(api, item) == evidence, "release changed during verification")
    return {k: item[k] for k in FIELDS}


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    require(result.returncode == 0, "git read/preparation failed")
    return result.stdout.strip()


def prepare(api, catalogue, tag, output, run_origin=True):
    """Read remote state and write local candidate/evidence only; no branch or token mint."""
    policy = load_policy(catalogue)
    main_sha = api.call(f"/repos/{CATALOGUE}/git/ref/heads/main")["object"]["sha"]
    require(git(catalogue, "rev-parse", "HEAD") == main_sha, "catalogue checkout is not current remote main")
    require(not git(catalogue, "status", "--porcelain", "--untracked-files=no"), "dirty catalogue checkout")
    sha = trusted_source(api, policy, tag)
    origin_url = origin(api, tag, sha) if run_origin else None
    base = policy.strict_json((catalogue / "catalogue.json").read_bytes())
    entries = [p for p in base["plugins"] if p["id"] == PLUGIN]
    require(len(entries) == 1, "NOMAD entry missing/duplicated")
    fields = public_release(api, policy, tag, sha, entries[0])
    candidate = upsert(base, fields)
    branch = "automation/openhop-nomad-" + tag
    remote = optional(api.call, f"/repos/{CATALOGUE}/git/ref/heads/{branch}")
    remote_sha = remote["object"]["sha"] if remote else ""
    require(not remote_sha or SHA.fullmatch(remote_sha), "invalid observed branch SHA")
    changed = candidate != base
    output.mkdir(parents=True, exist_ok=True)
    (output / "catalogue.json").write_text(json.dumps(candidate, indent=2) + "\n")
    receipt = dict(tag=tag, branch=branch, base_sha=main_sha, remote_sha=remote_sha,
                   fields=fields, origin_run_url=origin_url, changed=changed,
                   candidate_sha256=hashlib.sha256((output / "catalogue.json").read_bytes()).hexdigest())
    (output / "proposal.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(f"changed={str(changed).lower()}\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "inspect-build", "verify-existing"])
    parser.add_argument("--catalogue", type=Path, required=True)
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path, default=Path("proposal"))
    args = parser.parse_args()
    api = API()
    tag = args.tag
    if not tag:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        tag = event_tag(api, event, os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"],
                        os.environ.get("MANUAL_TAG", ""))
    version(tag)
    if args.mode == "prepare":
        print(json.dumps(prepare(api, args.catalogue.resolve(), tag, args.output), indent=2))
        return
    policy = load_policy(args.catalogue.resolve())
    sha = trusted_source(api, policy, tag)
    release = optional(api.call, f"/repos/{REPOSITORY}/releases/tags/{tag}")
    state = release_state(release, tag)
    if state == "verify":
        base = policy.strict_json((args.catalogue / "catalogue.json").read_bytes())
        entry = next(p for p in base["plugins"] if p["id"] == PLUGIN)
        public_release(api, policy, tag, sha, entry)
    require(args.mode != "verify-existing" or state == "verify", "release absent")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(f"state={state}\nsha={sha}\ntag={tag}\n")
    print(json.dumps(dict(state=state, sha=sha, tag=tag)))


if __name__ == "__main__":
    main()
