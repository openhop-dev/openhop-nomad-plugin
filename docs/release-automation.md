# Release and catalogue automation

## Boundaries

`Build Wheel` handles canonical `vMAJOR.MINOR.PATCH` tag pushes, published-release events, and manual retries (select **main**, supply `release_tag`). Trusted main automation resolves the source tag, requires it to be on main, and checks both source version files. A new build checks out that exact commit, tests it, and uses the existing Python wheel packaging with a matching single-wheel ZIP. It does not change the package layout, manifest schema, source tags, or existing published bytes.

All events for one tag serialize. An existing final release with the exact wheel/ZIP asset contract is downloaded and verified, not rebuilt. Missing/extra assets, differing bytes, moved tags, and ambiguous API errors fail closed. Only a confirmed 404 means a release is absent. The isolated publication job creates absent releases without clobbering and reads back the exact bytes. Do not delete authoritative source tags to recover a partial release; investigate deliberately instead.

`Propose NOMAD catalogue update` runs from trusted main after a successful `Build Wheel`, or manually from main with an existing release tag. It verifies public wheel bytes with the catalogue's registered Python-service profile (metadata, manifest, safe paths, RECORD), verifies the ZIP contains identical bytes, and calculates the public wheel checksum. It records an actual successful originating Build Wheel run, not the proposal retry run. Main-dispatched builds have an automation `head_sha` distinct from the tag checkout; fallback provenance requires a tag-bound run title and verifies tag-to-automation and automation-to-main ancestry.

The proposal starts from current remote catalogue main, rejects downgrades and equal-version differing metadata, and changes only NOMAD's `version`, `source_revision`, `wheel_url`, and `sha256`. Compatibility, category, logo, unrelated entries, and top-level metadata are preserved. Exact already-approved metadata is a no-op.

Destination validation and the full destination test suite run before the catalogue write token is minted. Checkouts do not persist credentials. The token step uses existing `OPENHOP_CATALOGUE_APP_ID` and `OPENHOP_CATALOGUE_APP_PRIVATE_KEY` secrets, scoped to `openhop-dev/openhop-plugin-catalogue` with contents/pull-request write and metadata read. Secret presence is not proof of successful authentication.

The narrow isolated publisher imports neither destination nor release-source code. It uses Git plumbing without hooks/filters, an explicit observed-SHA force-with-lease, and readback of branch bytes and PR state. New PRs are ready; retries preserve an existing human-selected draft state. It does not request a merge or certify itself: catalogue-owned approved-app policy and required checks own trusted merging.

## Read-only public-release rehearsal

Install automation dependencies in a venv and use a **separate, clean current-main clone** of the catalogue; do not repurpose a maintainer's checkout. These commands only read GitHub and write local temporary files:

```bash
python3 -m venv .automation-venv
.automation-venv/bin/pip install -r scripts/automation-requirements.txt
catalogue_dir=$(mktemp -d)
proposal_dir=$(mktemp -d)
git clone --depth 1 --branch main https://github.com/openhop-dev/openhop-plugin-catalogue.git "$catalogue_dir"
.automation-venv/bin/python scripts/release_automation.py prepare \
  --catalogue "$catalogue_dir" --tag v0.1.2 --output "$proposal_dir"
python_bin="$PWD/.automation-venv/bin/python"
(cd "$catalogue_dir" && "$python_bin" -m pytest tests)
cp "$proposal_dir/catalogue.json" "$catalogue_dir/catalogue.json"
"$python_bin" "$catalogue_dir/scripts/validate_catalogue.py"
(cd "$catalogue_dir" && "$python_bin" -m pytest tests)
```

`prepare` is read-only remotely, using `GH_TOKEN` or the existing `gh auth token` credential for GET requests. It emits `proposal.json` and candidate `catalogue.json`. Do **not** invoke either `publish_*.py` script during a rehearsal. A stale catalogue checkout is rejected; repeat with a new current-main clone if main advances.

## Verified rehearsal and outstanding gate

A public v0.1.2 rehearsal against catalogue main `7424198a8e4e7df92660febcd2ad46d687e30feb` resolved source `4f9d10131b2b58ddf6e2027c9206f8c8ce377eb3`, originating run `34355945706`, and public wheel SHA-256 `8f6f75073be3cdbc598e9de1f22dc1d558d7b5c079f3e5e79b3013033fbe043c`. Candidate schema validation passed. The unchanged trusted catalogue passed all 160 tests.

**Not deployment-ready yet:** that catalogue revision's candidate tests hard-code NOMAD 0.1.1 and fixture transitions to 0.1.2. Running its full suite with the valid 0.1.2 candidate produces 155 passes and 5 failures (`test_third_python_app_is_registration_only`, `test_nomad_certification`, `test_nomad_receipt_binds_selected_app`, `test_repository_catalogue_metadata_validates`, and `test_wheel_filename_version_must_match_approved_version`). Correct those catalogue-owned test fixtures through a separately authorized catalogue change before enabling this producer workflow. The producer deliberately retains the failing gate rather than skipping tests or rewriting destination code.

Actionlint verification was blocked by a denied tool-install/download command in the local rehearsal and must still be completed. No workflow dispatch, release mutation, branch push, PR creation, or merge was exercised by this rehearsal.
