#!/usr/bin/env python3
"""Publish absent releases only. Existing versioned bytes are never replaced."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def publish(inspect, create, expected, verified):
    before = inspect()
    if before is None:
        create()
        before = inspect()
    if before != expected:
        raise ValueError("partial/differing release assets; explicit recovery required; never overwrite")
    verified(before)


def main():
    # Only trusted automation code; no producer source or catalogue code executes here.
    from release_automation import API, REPOSITORY, asset_names, optional, release_state, require, version
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    version(args.tag)
    names = asset_names(args.tag)
    require(sorted(p.name for p in args.assets.iterdir()) == sorted(names), "unexpected local assets")
    expected = {name: (args.assets / name).read_bytes() for name in names}
    api = API()
    def check_tag():
        obj = api.call(f"/repos/{REPOSITORY}/git/ref/tags/{args.tag}")["object"]
        for _ in range(5):
            if obj["type"] == "commit":
                require(obj["sha"] == args.sha, "source tag moved")
                return
            require(obj["type"] == "tag", "invalid source ref")
            obj = api.call(f"/repos/{REPOSITORY}/git/tags/{obj['sha']}")["object"]
        raise ValueError("tag nesting exceeded")
    def inspect():
        check_tag()
        release = optional(api.call, f"/repos/{REPOSITORY}/releases/tags/{args.tag}")
        if release_state(release, args.tag) == "build":
            return None
        with tempfile.TemporaryDirectory() as temp:
            cmd = ["gh", "release", "download", args.tag, "--repo", REPOSITORY, "--dir", temp]
            result = subprocess.run(cmd, capture_output=True)
            require(result.returncode == 0, "public asset download failed")
            return {name: (Path(temp) / name).read_bytes() for name in names}
    def create():
        # GitHub refuses a concurrent existing release or asset name; no clobber flag.
        check_tag()
        result = subprocess.run(["gh", "release", "create", args.tag, "--repo", REPOSITORY,
            "--verify-tag", "--title", args.tag, "--notes", "NOMAD plugin wheel and matching ZIP bundle.",
            "--latest=false", *(str(args.assets / name) for name in names)], capture_output=True)
        require(result.returncode == 0, "release create failed; inspect partial state before retry")
    publish(inspect, create, expected, lambda _: check_tag())
    print("Verified published release assets match exact tested build bytes")


if __name__ == "__main__":
    main()
