"""Tests for the CI/publish pipeline definition itself.

This story cannot run GitHub Actions locally, so the workflow file has to be
"right by construction" instead of proven by execution. These tests read
`.github/workflows/ci.yml` as plain YAML/text and check the handful of
properties that would otherwise only surface on the first real push: that
publishing cannot happen without the tests passing, that the image name
can never drift from the install files, and that the smoke test actually
exercises the port and mount the rest of the project agreed on.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from marrquee.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_README_PATH = _REPO_ROOT / "README.md"
_COMPOSE_PATH = _REPO_ROOT / "compose.install.yaml"

_IMAGE_NAME = "ghcr.io/purplefox-07/marrquee"
_SOCKET_MOUNT = "/var/run/docker.sock:/var/run/docker.sock"


def _workflow_text() -> str:
    return _WORKFLOW_PATH.read_text()


def _workflow_doc() -> dict[str, Any]:
    doc = yaml.safe_load(_workflow_text())
    assert isinstance(doc, dict)
    return doc


def _jobs() -> dict[str, Any]:
    jobs = _workflow_doc()["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _image_job() -> dict[str, Any]:
    return _jobs()["image"]  # type: ignore[no-any-return]


def _stack_smoke_job() -> dict[str, Any]:
    return _jobs()["stack-smoke"]  # type: ignore[no-any-return]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return steps  # type: ignore[return-value]


def _step_named(job: dict[str, Any], *needles: str) -> dict[str, Any]:
    """The first step whose name contains every one of `needles` (case-insensitive)."""
    for step in _steps(job):
        name = str(step.get("name", "")).lower()
        if all(needle.lower() in name for needle in needles):
            return step
    raise AssertionError(f"no step named with all of {needles!r} found")


def _text_between(text: str, start: str, end: str) -> str:
    """The text strictly between the first `start` and the next `end` after it."""
    start_index = text.index(start) + len(start)
    end_index = text.index(end, start_index)
    return text[start_index:end_index]


def _heredoc_body(run: str, filename: str) -> str:
    """The body of one `cat > ".../<filename>" <<'PY'` heredoc inside a
    step's own `run` text, up to the next bare `PY` line - real, parseable
    Python source. Scoped to one named heredoc rather than the step's whole
    text, so a line that happens to read identically inside a DIFFERENT
    step's own heredoc elsewhere in this same file (there is another
    `if not result.ok:` in the qBittorrent bring-up) can never make a
    mutation inside THIS heredoc look caught when it wasn't.
    """
    marker = f"cat > \"$RUNNER_TEMP/{filename}\" <<'PY'\n"
    start = run.index(marker) + len(marker)
    end = run.index("\nPY\n", start)
    return run[start:end]


def _shell_case_arms(case_block: str) -> dict[str, str]:
    """Each `label) body ;;` arm of a one-arm-per-line shell `case`, keyed by
    its label with the body trimmed - so a body that was hollowed out to a
    no-op, or a no-op that grew a body, both show up as a plain string
    diff instead of a substring match that either would still satisfy.
    """
    arms: dict[str, str] = {}
    for line in case_block.splitlines():
        match = re.match(r"\s*(\S+)\)\s*(.*?)\s*;;\s*$", line)
        if match:
            arms[match.group(1)] = match.group(2)
    return arms


def _assert_post_loop_verdict(run: str, guard: str, error_substring: str) -> None:
    """A poll loop's own final verdict: the exact `if [ ... ]; then` guard
    line, immediately followed by its own `::error::` line and a bare
    `exit 1` - not merely present somewhere in the step. `if false; then`
    (or `if true; then`) swapped in for the real comparison would still
    leave every substring this step's other assertions check for sitting
    right there in the step's text, just never reached (or always
    reached) - only pinning the guard's own exact text, and what sits on
    the two lines immediately after it, catches that.
    """
    lines = run.splitlines()
    matches = [index for index, line in enumerate(lines) if line.strip() == guard]
    assert matches, f"guard {guard!r} not found verbatim in the step"
    guard_index = matches[0]
    error_line = lines[guard_index + 1].strip()
    exit_line = lines[guard_index + 2].strip()
    assert "::error::" in error_line and error_substring in error_line, (
        f"{guard!r} is not immediately followed by its own ::error:: line (got {error_line!r})"
    )
    assert exit_line == "exit 1", (
        f"{guard!r} is not immediately followed by exit 1 (got {exit_line!r})"
    )


def _last_nonblank_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _publish_step(job: dict[str, Any]) -> dict[str, Any]:
    """The build-push-action step that actually pushes (push: true)."""
    for step in _steps(job):
        uses = step.get("uses", "")
        if (
            isinstance(uses, str)
            and uses.startswith("docker/build-push-action")
            and step.get("with", {}).get("push")
        ):
            return step
    raise AssertionError("no build-push-action step with push: true found")


def test_workflow_is_valid_yaml_and_declares_packages_write() -> None:
    doc = _workflow_doc()

    permissions = doc["permissions"]
    assert permissions["packages"] == "write"
    assert permissions["contents"] == "read"


def test_workflow_triggers_on_main_pushes_tags_and_pull_requests() -> None:
    doc = _workflow_doc()
    # PyYAML 1.1 parses a bare `on:` key as the boolean True; this workflow
    # quotes it (`"on":`) specifically to avoid that trap, so it reads back
    # as the string "on".
    triggers = doc["on"]

    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["tags"] == ["v*"]
    assert "pull_request" in triggers


def test_only_one_workflow_file_exists() -> None:
    """Two files cannot express "do not publish unless the tests passed"."""
    workflow_files = sorted(p.name for p in _WORKFLOW_PATH.parent.glob("*.yml"))

    assert workflow_files == ["ci.yml"]


def test_publishing_is_gated_on_the_test_job() -> None:
    image_job = _image_job()

    needs = image_job["needs"]
    assert needs == "test" or needs == ["test"]


def test_workflow_builds_both_platforms_before_publishing() -> None:
    publish_step = _publish_step(_image_job())

    platforms = publish_step["with"]["platforms"]
    assert "linux/amd64" in platforms
    assert "linux/arm64" in platforms
    assert publish_step["with"]["push"] is True


def test_publish_step_is_gated_on_a_real_push_not_a_pull_request() -> None:
    publish_step = _publish_step(_image_job())

    assert publish_step.get("if") == "github.event_name == 'push'"


def test_workflow_publishes_the_image_name_the_install_files_use() -> None:
    metadata_step = _step_named(_image_job(), "metadata")

    assert metadata_step["with"]["images"] == _IMAGE_NAME
    # The same literal string, never `github.repository` - GHCR rejects the
    # uppercase casing that expression would actually evaluate to.
    assert "github.repository" not in _workflow_text()

    compose_doc = yaml.safe_load(_COMPOSE_PATH.read_text())
    assert f"{_IMAGE_NAME}:latest" == compose_doc["services"]["marrquee"]["image"]
    assert _IMAGE_NAME in _README_PATH.read_text()


def test_published_image_is_labelled_with_the_project_page_it_came_from() -> None:
    # This label is what makes the package's page on GitHub link back to the
    # project page. It is stated outright rather than left for the metadata
    # action to derive, and it must actually reach the build step.
    job = _image_job()
    metadata_step = _step_named(job, "metadata")
    expected = "org.opencontainers.image.source=https://github.com/PurpleFox-07/marrquee"

    assert expected in metadata_step["with"]["labels"]
    assert _publish_step(job)["with"]["labels"] == "${{ steps.meta.outputs.labels }}"


def test_smoke_step_mounts_the_docker_socket_and_the_port_settings_uses() -> None:
    step = _step_named(_image_job(), "start", "amd64")
    run = step["run"]

    assert _SOCKET_MOUNT in run
    port = Settings().port
    assert f"-p {port}:{port}" in run


def test_compose_binary_smoke_proves_it_runs_without_a_docker_cli() -> None:
    """This is the only way to answer whether the standalone binary needs a
    `docker` CLI - it runs the built image with no CLI installed, against the
    runner's real socket.
    """
    job = _image_job()
    steps = _steps(job)

    version_step = _step_named(job, "prove", "compose binary")
    assert "--entrypoint /usr/local/bin/docker-compose" in version_step["run"]
    assert "marrquee:smoke-amd64" in version_step["run"]
    assert "version" in version_step["run"]

    up_step = _step_named(job, "bring", "compose")
    assert _SOCKET_MOUNT in up_step["run"]
    assert "--entrypoint /usr/local/bin/docker-compose" in up_step["run"]
    assert "up -d" in up_step["run"]

    assert_step = _step_named(job, "assert", "compose-binary smoke")
    assert "marrquee-compose-smoke" in assert_step["run"]

    cleanup_step = _step_named(job, "remove", "compose-binary smoke")
    assert cleanup_step.get("if") == "always()"
    assert "docker rm -f marrquee-compose-smoke" in cleanup_step["run"]

    # The compose-binary smoke happens before the arm64 build and the
    # publish gate, alongside the amd64 smoke - not as a late add-on that
    # could silently be skipped.
    names = [str(step.get("name", "")) for step in steps]
    arm64_index = names.index("Build the arm64 image for the smoke test")
    assert names.index(version_step["name"]) < arm64_index

    # No `if:` guard of its own, so a real failure here fails the `image` job
    # like any other step, and the publish steps later in that same job never
    # run - this smoke depends only on our own image, not an external
    # registry, so it is allowed to be a hard gate.
    for step in (version_step, up_step, assert_step):
        assert "if" not in step


def test_compose_binary_smoke_does_not_gate_publishing() -> None:
    """Publishing stays gated on the `test` job alone, exactly as before -
    this smoke reports a real finding but never blocks `:latest`.
    """
    image_job = _image_job()

    assert image_job["needs"] in ("test", ["test"])


def test_test_job_uses_uvs_official_installer_not_a_third_party_action() -> None:
    test_job = _jobs()["test"]

    assert "actions/setup-uv" not in _workflow_text()
    install_step = _step_named(test_job, "install", "uv")
    assert "astral.sh/uv/install.sh" in install_step["run"]


def test_action_versions_match_the_pinned_contract() -> None:
    text = _workflow_text()

    assert "actions/checkout@v7" in text
    assert "docker/setup-qemu-action@v4" in text
    assert "docker/setup-buildx-action@v4" in text
    assert "docker/login-action@v4" in text
    assert "docker/build-push-action@v7" in text


def test_verification_step_inspects_the_published_manifest_for_both_platforms() -> None:
    step = _step_named(_image_job(), "verify")

    assert "docker buildx imagetools inspect" in step["run"]
    assert _IMAGE_NAME in step["run"]


def test_readme_tells_the_owner_to_make_the_package_public() -> None:
    readme = _README_PATH.read_text().lower()

    assert "public" in readme
    assert "package" in readme
    assert "danger zone" in readme or "change visibility" in readme


def test_amd64_smoke_step_checks_diagnostics_not_root() -> None:
    step = _step_named(_image_job(), "talking to", "docker daemon")

    assert "127.0.0.1:7788/diagnostics" in step["run"]
    assert "127.0.0.1:7788/ |" not in step["run"]


def test_readme_sends_the_owner_to_the_wizard_and_names_diagnostics() -> None:
    readme = _README_PATH.read_text()

    assert "What do you want on your media server" in readme
    assert "/diagnostics" in readme


def test_readme_replaces_the_curl_walk_with_a_browser_only_update_section() -> None:
    readme = _README_PATH.read_text()

    assert "## Try the deploy engine" not in readme
    assert "## Update Marrquee and deploy from your browser" in readme
    # The whole user path - everything before the developer section - is
    # the surface the owner rule ("no command-line steps") applies to.
    user_path = readme.split("## Developing Marrquee", 1)[0]
    assert "/api/deploy" not in user_path
    assert "/api/install" not in user_path


def test_readme_wiring_check_is_look_only_and_adds_no_command_line_step() -> None:
    readme = _README_PATH.read_text()

    section = readme[
        readme.index("## Update Marrquee and deploy from your browser") : readme.index(
            "## Developing Marrquee"
        )
    ]
    assert "Settings -> Apps" in section
    # The whole owner walk is GUI-only, start to finish - no pasted command
    # anywhere in it, not even an optional one.
    assert "```bash" not in section
    assert "curl" not in section.lower()


def test_readme_walkthrough_mentions_signing_in_and_choosing_the_login() -> None:
    readme = _README_PATH.read_text()
    section = readme[
        readme.index("## Update Marrquee and deploy from your browser") : readme.index(
            "## Developing Marrquee"
        )
    ]
    assert "Choose your login" in section
    assert "sign in with the username and password you chose" in section.lower()
    assert "Remember me" in section


# --- stack-smoke: the real-Docker proof, and the guarantee it never gates ----


def test_stack_smoke_job_exists_and_needs_only_test() -> None:
    job = _stack_smoke_job()

    assert job["needs"] in ("test", ["test"])


def test_publish_job_does_not_depend_on_the_stack_smoke_job() -> None:
    """`image` (which publishes) must never wait on a job that pulls three
    external images and can fail for reasons unrelated to this repository.
    """
    image_needs = _image_job()["needs"]
    normalized = image_needs if isinstance(image_needs, list) else [image_needs]

    assert "stack-smoke" not in normalized


def test_stack_smoke_job_is_not_needed_by_any_other_job() -> None:
    for job_name, job in _jobs().items():
        if job_name == "stack-smoke":
            continue
        needs = job.get("needs", [])
        normalized = needs if isinstance(needs, list) else [needs]
        assert "stack-smoke" not in normalized, f"{job_name} must not depend on stack-smoke"


def test_stack_smoke_starts_the_image_with_the_socket_and_the_host_mount() -> None:
    step = _step_named(_stack_smoke_job(), "start", "built image", "install file")

    assert _SOCKET_MOUNT in step["run"]
    assert '-v "${RUNNER_TEMP}:/host${RUNNER_TEMP}"' in step["run"]
    assert "-v /:/host" not in step["run"]
    port = Settings().port
    assert f"-p {port}:{port}" in step["run"]


def test_stack_smoke_installs_two_apps_and_starts_a_deploy() -> None:
    """Radarr is deliberately left out of the first install - this job's
    whole point is proving it arrives later, through the Hub's own add
    endpoint, onto a stack that is already running.
    """
    job = _stack_smoke_job()

    install_step = _step_named(job, "install two apps")
    run = install_step["run"]
    assert "/api/install" in run
    assert "prowlarr" in run
    assert "sonarr" in run
    assert "radarr" not in run

    start_step = _step_named(job, "start the deploy")
    assert "POST http://127.0.0.1:7788/api/deploy" in start_step["run"]


def test_stack_smoke_generates_a_masked_throwaway_login_before_installing() -> None:
    """`/api/install` now requires a login - generated fresh for this one
    run, masked before it can ever reach the job log, and carried into the
    install body through the environment rather than a literal in the
    step's own text.
    """
    job = _stack_smoke_job()

    login_step = _step_named(job, "throwaway login")
    run = login_step["run"]
    assert "openssl rand -hex 16" in run
    assert "::add-mask::" in run
    assert "MARRQUEE_CI_PASSWORD=" in run
    assert "$GITHUB_ENV" in run

    install_step = _step_named(job, "install two apps")
    assert "login" in install_step["run"]
    assert "marrquee-ci" in install_step["run"]
    assert "${MARRQUEE_CI_PASSWORD}" in install_step["run"]

    steps = _steps(job)
    login_index = steps.index(login_step)
    install_index = steps.index(install_step)
    assert login_index < install_index


def test_stack_smoke_polls_for_finale_with_a_bounded_loop() -> None:
    step = _step_named(_stack_smoke_job(), "poll", "finale")

    assert "/api/deploy" in step["run"]
    assert "finale" in step["run"]
    # Bounded - `seq 1 N`, never an unconditional `while true`.
    assert "seq 1 " in step["run"]
    assert "while true" not in step["run"]


def test_stack_smoke_puts_the_failure_code_and_headline_in_a_public_annotation() -> None:
    """The public annotations API is readable without a login; the job log
    is not - this is what cost real time diagnosing the first-ever red run,
    so the snapshot's own (already plain-language, secret-free) `code` and
    `headline` land in the `::error::` itself instead of only the log.
    """
    run = _step_named(_stack_smoke_job(), "poll", "finale")["run"]

    assert ".failure.code" in run
    assert ".headline" in run
    assert "::error::" in run


def test_stack_smoke_annotation_escapes_percent_cr_lf_and_double_colon() -> None:
    """GitHub's workflow-command escaping for annotation text, applied
    before the message ever reaches `::error::` - and `::` is also
    collapsed so the text can never be mistaken for a second command.
    """
    run = _step_named(_stack_smoke_job(), "poll", "finale")["run"]

    assert "%25" in run  # escapes a literal '%'
    assert "%0D" in run  # escapes a literal carriage return
    assert "%0A" in run  # escapes a literal newline
    assert "': :'" in run  # collapses a literal '::' in the message body


def test_stack_smoke_job_is_named_for_the_add_flow() -> None:
    assert _stack_smoke_job()["name"] == (
        "Real-Docker stack smoke (two arr apps, then add one from the Hub, "
        "then a VPN with a throwaway login)"
    )


def test_stack_smoke_records_prowlarr_and_sonarr_before_the_add() -> None:
    step = _step_named(_stack_smoke_job(), "record", "before the add")
    run = step["run"]

    assert "docker inspect -f '{{.Id}} {{.State.StartedAt}}' prowlarr" in run
    assert "docker inspect -f '{{.Id}} {{.State.StartedAt}}' sonarr" in run
    assert "PROWLARR_BEFORE=" in run
    assert "SONARR_BEFORE=" in run
    assert "$GITHUB_ENV" in run


def test_stack_smoke_adds_radarr_through_the_hub_install_endpoint() -> None:
    step = _step_named(_stack_smoke_job(), "add radarr", "hub's install endpoint")
    run = step["run"]

    assert "/api/hub/apps/radarr/install" in run
    assert '{"answers":{}}' in run
    assert "202" in run
    assert "::error::" in run


def test_stack_smoke_polls_hub_status_until_radarr_finishes_adding() -> None:
    step = _step_named(_stack_smoke_job(), "poll", "radarr finishes adding")
    run = step["run"]

    assert "/api/hub/status" in run
    assert 'app_id == "radarr"' in run
    assert ".add_state" in run
    assert ".busy" in run
    # Bounded - `seq 1 N`, never an unconditional `while true`.
    assert "seq 1 " in run
    assert "while true" not in run
    # An add_state of "error" fails loudly instead of spinning until the
    # loop's own timeout hides the real reason.
    assert '"$add_state" = "error"' in run
    assert run.count("::error::") >= 2


def test_stack_smoke_polls_hub_status_annotation_escapes_percent_cr_lf_and_double_colon() -> None:
    run = _step_named(_stack_smoke_job(), "poll", "radarr finishes adding")["run"]

    assert "%25" in run
    assert "%0D" in run
    assert "%0A" in run
    assert "': :'" in run


def test_stack_smoke_asserts_prowlarr_and_sonarr_were_not_recreated_by_the_add() -> None:
    """A real daemon is the only way to prove that `--no-recreate` really
    does leave a running container's id and start time untouched.
    """
    step = _step_named(_stack_smoke_job(), "were not recreated")
    run = step["run"]

    assert "docker inspect -f '{{.Id}} {{.State.StartedAt}}' prowlarr" in run
    assert "docker inspect -f '{{.Id}} {{.State.StartedAt}}' sonarr" in run
    assert "$PROWLARR_BEFORE" in run
    assert "$SONARR_BEFORE" in run
    assert "::error::Prowlarr or Sonarr was recreated by adding Radarr" in run


def test_stack_smoke_add_path_steps_run_between_the_first_finale_and_the_container_check() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    first_finale_index = names.index(_step_named(job, "poll", "it reaches finale")["name"])
    record_index = names.index(_step_named(job, "record", "before the add")["name"])
    add_index = names.index(_step_named(job, "add radarr", "hub's install endpoint")["name"])
    add_poll_index = names.index(_step_named(job, "poll", "radarr finishes adding")["name"])
    recreate_index = names.index(_step_named(job, "were not recreated")["name"])
    containers_index = names.index(_step_named(job, "all three containers are running")["name"])

    assert (
        first_finale_index
        < record_index
        < add_index
        < add_poll_index
        < recreate_index
        < containers_index
    )


def test_stack_smoke_asserts_the_marrquee_network_lists_every_app_and_marrquee_itself() -> None:
    """The exact regression this job exists to catch: a network of the right
    name existing is not the same as every container actually being on it.
    """
    step = _step_named(_stack_smoke_job(), "network", "lists all three apps")

    assert "docker network inspect marrquee" in step["run"]
    for name in ("prowlarr", "sonarr", "radarr", "marrquee-stack-smoke"):
        assert name in step["run"]


def test_stack_smoke_asserts_the_compose_file_is_readable_before_reading_keys_from_it() -> None:
    """`write_compose` chowns this file to the drive's owner instead of root
    specifically so it can be read - checked here on its own, before the key
    extraction step, so a regression reads as exactly that rather than a
    mysterious 401 two steps later.
    """
    job = _stack_smoke_job()
    readable_step = _step_named(job, "compose file is readable")

    assert "compose.yaml" in readable_step["run"]
    assert "-r " in readable_step["run"] or "! -r" in readable_step["run"]
    assert readable_step["run"].count("::error::") >= 1

    steps = _steps(job)
    names = [str(step.get("name", "")) for step in steps]
    keys_step = _step_named(job, "answers", "system/status")
    assert names.index(readable_step["name"]) < names.index(keys_step["name"])


def test_stack_smoke_reads_api_keys_from_the_generated_compose_file_not_the_api() -> None:
    step = _step_named(_stack_smoke_job(), "answers", "system/status")
    run = step["run"]

    assert "compose.yaml" in run
    assert "PROWLARR__AUTH__APIKEY" in run
    assert "SONARR__AUTH__APIKEY" in run
    assert "RADARR__AUTH__APIKEY" in run
    assert "X-Api-Key" in run
    assert "api/v1/system/status" in run
    assert "api/v3/system/status" in run


def test_stack_smoke_fails_loudly_on_an_empty_key_instead_of_sending_one() -> None:
    step = _step_named(_stack_smoke_job(), "answers", "system/status")
    run = step["run"]

    # Every extracted key has to be checked non-empty before it's ever used
    # in a curl call - sending an empty X-Api-Key would just be a confusing
    # 401 instead of a named failure.
    for key_var in ("prowlarr_key", "sonarr_key", "radarr_key"):
        assert f'-z "${key_var}"' in run or f'-z "${{{key_var}}}"' in run
    assert run.count("::error::") >= 3


def test_stack_smoke_never_echoes_an_api_key() -> None:
    step = _step_named(_stack_smoke_job(), "answers", "system/status")
    run = step["run"]

    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or "::error::" in stripped:
            assert "_key" not in stripped, f"a key variable appears in an echoed line: {line!r}"


def test_stack_smoke_dumps_diagnostics_and_logs_only_on_failure() -> None:
    step = _step_named(_stack_smoke_job(), "dump diagnostics")

    assert step.get("if") == "failure()"
    assert "/api/deploy/diagnostics" in step["run"]
    assert "docker logs marrquee-stack-smoke" in step["run"]
    assert "docker logs recyclarr" in step["run"]
    assert "docker logs plex" in step["run"]


def test_stack_smoke_also_puts_diagnostics_in_a_public_annotation_truncated_and_escaped() -> None:
    """Same reasoning as the polling step's annotation: the public
    annotations API needs no login, the job log does. Truncated to ~1500
    characters and escaped the same way, since this text is `_redact_secrets`
    output rather than curated plain-language copy.
    """
    run = _step_named(_stack_smoke_job(), "dump diagnostics")["run"]

    assert "[:1500]" in run
    assert "::error::" in run
    assert "%25" in run
    assert "%0D" in run
    assert "%0A" in run
    assert "': :'" in run
    # The plain log dump stays too - the annotation is additional, not a
    # replacement for it.
    assert 'echo "$diagnostics"' in run


def test_stack_smoke_asserts_prowlarr_sonarr_and_radarr_are_wired_together() -> None:
    """The proof the whole story exists for: two applications, both
    `fullSync`, and both apps' own root folder - asserted straight against
    the real, running containers, plus Marrquee's own report agreeing.
    """
    step = _step_named(_stack_smoke_job(), "assert", "wired together")
    run = step["run"]

    assert "api/v1/applications" in run
    assert "Sonarr" in run
    assert "Radarr" in run
    assert "fullSync" in run
    assert "/data/media/tv" in run
    assert "/data/media/movies" in run
    assert ".wiring[]" in run
    assert "done" in run


def test_stack_smoke_wiring_assertion_never_echoes_a_key() -> None:
    step = _step_named(_stack_smoke_job(), "assert", "wired together")
    run = step["run"]

    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or "::error::" in stripped:
            assert "_key" not in stripped, f"a key variable appears in an echoed line: {line!r}"


def test_stack_smoke_deploys_a_second_time_and_polls_it_to_finale() -> None:
    job = _stack_smoke_job()

    second_deploy_step = _step_named(job, "second", "deploy")
    assert "POST http://127.0.0.1:7788/api/deploy" in second_deploy_step["run"]

    # Two "poll ... finale" steps: the first deploy's and the second's.
    poll_steps = [
        step
        for step in _steps(job)
        if "poll" in str(step.get("name", "")).lower()
        and "finale" in str(step.get("name", "")).lower()
    ]
    assert len(poll_steps) == 2


def test_stack_smoke_re_asserts_the_same_counts_after_the_second_deploy() -> None:
    step = _step_named(_stack_smoke_job(), "second deploy changed nothing")
    run = step["run"]

    assert "length == 2" in run  # still exactly two applications
    assert "length == 1" in run  # still exactly one root folder each
    assert ".wiring[]" in run
    assert "done" in run


def test_stack_smoke_wiring_and_second_deploy_steps_run_in_the_right_order() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    wiring_index = names.index(_step_named(job, "assert", "wired together")["name"])
    second_deploy_index = names.index(_step_named(job, "second", "deploy")["name"])
    reassert_index = names.index(_step_named(job, "second deploy changed nothing")["name"])
    dump_index = names.index(_step_named(job, "dump diagnostics")["name"])

    assert wiring_index < second_deploy_index < reassert_index < dump_index


def test_stack_smoke_asserts_every_app_asks_for_the_one_login() -> None:
    """The FIRST proof this job cannot get from reading source: real Sonarr,
    Radarr and Prowlarr images actually take Marrquee's login through
    `config/host`, and their own `/login` actually enforces it.
    """
    step = _step_named(_stack_smoke_job(), "assert every app asks for the one login")
    run = step["run"]

    assert "api/${api_version}/config/host" in run
    assert 'assert_app_asks_for_the_login "Prowlarr" 9696 v1' in run
    assert 'assert_app_asks_for_the_login "Sonarr" 8989 v3' in run
    assert 'assert_app_asks_for_the_login "Radarr" 7878 v3' in run
    assert 'authenticationMethod=="forms"' in run
    assert 'authenticationRequired=="enabled"' in run
    assert 'username=="marrquee-ci"' in run
    assert "/login?returnUrl=%2F" in run
    assert "loginFailed=true" in run
    assert "MARRQUEE_CI_PASSWORD" in run
    assert "--no-recreate" not in run
    assert "did not take the one login" in run
    assert run.count("::error::") >= 1


def test_stack_smoke_login_assertion_never_echoes_the_password() -> None:
    step = _step_named(_stack_smoke_job(), "assert every app asks for the one login")
    run = step["run"]

    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or "::error::" in stripped:
            assert "MARRQUEE_CI_PASSWORD" not in stripped, (
                f"the CI password appears in an echoed line: {line!r}"
            )


def test_stack_smoke_login_assertion_runs_after_the_add_and_wiring_steps() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    add_poll_index = names.index(_step_named(job, "poll", "radarr finishes adding")["name"])
    wiring_index = names.index(_step_named(job, "assert", "wired together")["name"])
    login_index = names.index(_step_named(job, "assert every app asks for the one login")["name"])
    second_deploy_index = names.index(_step_named(job, "second", "deploy")["name"])

    assert add_poll_index < wiring_index < login_index < second_deploy_index


def test_stack_smoke_cycle1_switch_over_step_exists_between_login_and_second_deploy() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    login_index = names.index(_step_named(job, "assert every app asks for the one login")["name"])
    switch_index = names.index(_step_named(job, "cycle-1 app switches over")["name"])
    second_deploy_index = names.index(_step_named(job, "second", "deploy")["name"])

    assert login_index < switch_index < second_deploy_index


def test_stack_smoke_cycle1_switch_over_never_edits_marrquees_own_compose_file() -> None:
    """A `sed` output to a runner temp copy - Marrquee's real compose.yaml is
    read from, never written to, by this step.
    """
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    assert "cycle1-compose.yaml" in run
    assert '"$cycle1_compose"' in run
    assert 'SONARR__AUTH__METHOD: "Forms"' in run
    assert 'SONARR__AUTH__METHOD: "External"' in run
    assert 'SONARR__AUTH__REQUIRED: "Enabled"' in run
    assert 'SONARR__AUTH__REQUIRED: "DisabledForLocalAddresses"' in run
    assert "docker compose -p marrquee-apps -f" in run
    assert "up -d sonarr" in run
    assert "--no-recreate" not in run


def test_stack_smoke_cycle1_switch_over_deletes_login_json_and_reasserts_the_banner() -> None:
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    assert "docker exec marrquee-stack-smoke rm -f /config/login.json" in run
    assert "login_banner" in run
    assert '"choose"' in run
    assert "/hub/login" in run
    assert "marrquee-ci" in run
    assert "MARRQUEE_CI_PASSWORD" in run


def test_stack_smoke_cycle1_switch_over_polls_bounded_for_the_run_to_finish() -> None:
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    # 5 minute budget at a 2s cadence.
    assert "seq 1 150" in run
    assert "while true" not in run
    assert ".busy" in run
    assert '"pending"' in run
    assert run.count("::error::") >= 2


def test_stack_smoke_cycle1_switch_over_compares_all_three_container_ids() -> None:
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    assert run.count("docker inspect -f '{{.Id}}'") >= 6  # before and after, x3 apps
    assert "::error::Choosing the login recreated Prowlarr" in run
    assert "::error::Choosing the login recreated Radarr" in run
    assert "sonarr_before" in run
    assert "sonarr_after" in run


def test_stack_smoke_cycle1_switch_over_waits_for_sonarr_before_choosing_the_login() -> None:
    """A freshly recreated Sonarr can take 10-30s to answer its own API -
    far longer than the login applier's ~4s retry budget. Without a wait
    here, phase 1 of the login run would race a container that just
    restarted and hasn't come back yet, and the run would report Sonarr as
    never having accepted the login.
    """
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    up_index = run.index("up -d sonarr")
    login_post_index = run.index("http://127.0.0.1:7788/hub/login")
    assert up_index < login_post_index

    segment = run[up_index:login_post_index]
    assert "api/v3/system/status" in segment
    assert "::error::Sonarr never came back after the Cycle-1 recreate" in segment
    assert "seq 1 " in segment  # bounded, never an unconditional `while true`
    assert "while true" not in segment


def test_stack_smoke_cycle1_switch_over_extracts_sonarr_key_only_once() -> None:
    """The key is read once, right after the recreate, and reused for both
    the readiness wait and the final config/host check - never re-extracted.
    """
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    assert run.count("SONARR__AUTH__APIKEY") == 1


def test_stack_smoke_cycle1_switch_over_confirms_the_runner_compose_recreated_sonarr() -> None:
    """A quick sanity check that the runner's own `docker compose up -d
    sonarr` really did treat the env change as a diff, so the "before" id
    used for the later switch-back comparison is genuinely the Cycle-1
    container and not a stale read of the one before it.
    """
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    assert "sonarr_id_pre_switch" in run
    assert "::error::Sonarr was not recreated by the Cycle-1 switch-over" in run


def test_stack_smoke_cycle1_switch_over_checks_sonarr_reads_forms_again() -> None:
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    assert "api/v3/config/host" in run
    assert 'authenticationMethod=="forms"' in run


def test_stack_smoke_cycle1_switch_over_never_echoes_the_password() -> None:
    step = _step_named(_stack_smoke_job(), "cycle-1 app switches over")
    run = step["run"]

    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or "::error::" in stripped:
            assert "MARRQUEE_CI_PASSWORD" not in stripped, (
                f"the CI password appears in an echoed line: {line!r}"
            )


def test_stack_smoke_hub_steps_run_last_after_the_second_deploy_reassert() -> None:
    """Placed after everything that still needs Radarr running, and before
    the failure-only diagnostics dump - stopping Radarr on purpose must
    never make an earlier assertion read as a regression. The VPN steps run
    later still, after Radarr is already stopped.
    """
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    reassert_index = names.index(_step_named(job, "second deploy changed nothing")["name"])
    hub_up_index = names.index(_step_named(job, "hub shows every app up")["name"])
    stop_radarr_index = names.index(_step_named(job, "stop radarr")["name"])
    vpn_add_index = names.index(_step_named(job, "add the vpn", "throwaway login")["name"])
    vpn_cancel_index = names.index(_step_named(job, "cancel the vpn add")["name"])
    dump_index = names.index(_step_named(job, "dump diagnostics")["name"])

    assert (
        reassert_index
        < hub_up_index
        < stop_radarr_index
        < vpn_add_index
        < vpn_cancel_index
        < dump_index
    )


def test_stack_smoke_hub_up_step_checks_the_page_the_status_endpoint_and_a_real_link() -> None:
    """The Pitch's unverifiable-by-reading condition, first half: on real
    Docker, the Hub really does read every app Up, and a poster's link
    really does answer.
    """
    step = _step_named(_stack_smoke_job(), "hub shows every app up")
    run = step["run"]

    assert "data-hub" in run
    assert "/api/hub/status" in run
    assert 'all(. == "up")' in run
    assert "8989" in run  # Sonarr's own port, read back from its poster url
    assert "%{http_code}" in run
    assert "000" in run  # curl's own "nothing answered at all" code


def test_stack_smoke_stop_radarr_step_polls_for_down_with_a_last_seen_line() -> None:
    """The condition's second half: a real `FinishedAt` really does become
    an honest "Down - last seen ..." within the Hub's own polling budget.
    """
    step = _step_named(_stack_smoke_job(), "stop radarr")
    run = step["run"]

    assert "docker stop radarr" in run
    assert "/api/hub/status" in run
    assert '"down"' in run
    assert "last seen" in run
    assert "seq 1 15" in run  # bounded - never an unconditional `while true`
    assert "while true" not in run
    assert "::error::" in run


def test_stack_smoke_hub_steps_never_appear_in_the_readmes_user_path() -> None:
    """These prove real Docker only in CI - the owner rule ("no command-line
    steps in the user path") never lets a curl walk near the README.
    """
    readme = _README_PATH.read_text()
    user_path = readme.split("## Developing Marrquee", 1)[0]

    assert "/api/hub/status" not in user_path
    assert "docker stop radarr" not in user_path


def test_stack_smoke_always_cleans_up_containers_network_and_temp_files() -> None:
    step = _step_named(_stack_smoke_job(), "clean up")

    assert step.get("if") == "always()"
    run = step["run"]
    for name in (
        "prowlarr",
        "sonarr",
        "radarr",
        "gluetun",
        "qbittorrent",
        "recyclarr",
        "plex",
        "marrquee-stack-smoke",
    ):
        assert name in run
    assert "docker network rm marrquee" in run
    # `sudo`, not a plain `rm -rf`: some of what's under the temp folder can
    # be created by the Docker daemon as root, which would otherwise block
    # cleanup as the unprivileged runner user.
    assert "sudo rm -rf" in run
    assert '"$RUNNER_TEMP/marrquee-smoke"' in run


# --- stack-smoke: Gluetun on a real daemon - tunnel device, control-server
# auth, the kill switch and a plain failure for a throwaway login ----------


def test_stack_smoke_generates_a_masked_throwaway_vpn_login_before_adding_it() -> None:
    """A second throwaway credential, separate from the one every arr app
    shares - so a leak of one can never be mistaken for a leak of the other.
    """
    job = _stack_smoke_job()

    login_step = _step_named(job, "throwaway", "vpn", "login")
    run = login_step["run"]
    assert "openssl rand -hex 16" in run
    assert "::add-mask::" in run
    assert "MARRQUEE_CI_VPN_PASSWORD=" in run
    assert "$GITHUB_ENV" in run

    add_step = _step_named(job, "add the vpn", "throwaway login")
    assert "${MARRQUEE_CI_VPN_PASSWORD}" in add_step["run"]

    steps = _steps(job)
    assert steps.index(login_step) < steps.index(add_step)


def test_stack_smoke_adds_the_vpn_through_the_hub_install_endpoint() -> None:
    step = _step_named(_stack_smoke_job(), "add the vpn", "throwaway login")
    run = step["run"]

    assert "/api/hub/apps/gluetun/install" in run
    assert "protonvpn" in run
    assert "openvpn" in run
    assert "marrquee-ci-not-a-user" in run
    assert '"202"' in run
    assert "::error::" in run


def test_stack_smoke_vpn_add_step_waits_for_the_container_then_checks_the_tunnel_device() -> None:
    """The FIRST assertion this chunk owns: the tunnel device and capability
    really do land on the container Gluetun's compose branch describes.
    Bounded, because the container is created asynchronously by the add run
    - a bare `docker inspect` right after the 202 would race it.
    """
    step = _step_named(_stack_smoke_job(), "add the vpn", "throwaway login")
    run = step["run"]

    assert "seq 1 " in run
    assert "while true" not in run
    assert "docker inspect gluetun" in run
    assert "gluetun_ready" in run
    assert "HostConfig.CapAdd" in run
    assert "NET_ADMIN" in run
    assert "HostConfig.Devices" in run
    assert "/dev/net/tun" in run
    assert "::error::Gluetun did not get the tunnel device" in run


def test_stack_smoke_vpn_login_steps_never_echo_the_password() -> None:
    """The generation step's own `echo "...=$pw" >> "$GITHUB_ENV"` line is not
    a leak - it's redirected straight to the environment file, never printed
    to the log, and `$pw` is masked from that point on regardless. What must
    never happen is a LATER step echoing the password once it's a named
    variable.
    """
    step = _step_named(_stack_smoke_job(), "add the vpn", "throwaway login")
    for line in step["run"].splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or "::error::" in stripped:
            assert "MARRQUEE_CI_VPN_PASSWORD" not in stripped, (
                f"the VPN password appears in an echoed line: {line!r}"
            )


def test_stack_smoke_polls_hub_status_until_the_vpn_add_fails_with_a_plain_sentence() -> None:
    step = _step_named(_stack_smoke_job(), "poll", "vpn's add", "plain failure")
    run = step["run"]

    assert "/api/hub/status" in run
    assert 'app_id == "gluetun"' in run
    assert ".add_state" in run
    assert '"$add_state" = "error"' in run
    assert '"$state" = "up"' in run
    assert "::error::A fake VPN login was reported as connected" in run
    assert "seq 1 " in run
    assert "while true" not in run
    assert "GLUETUN_LINE=" in run
    assert "$GITHUB_ENV" in run


def test_stack_smoke_asserts_the_vpn_failure_line_against_words_read_inside_the_container() -> None:
    """The tile's `line` is `headline + " " + what_to_do` (hub.py's
    `_add_failure_line`) - split the two candidate sentences the same way
    inside the container, so this compares against exactly what the running
    code would say rather than a copy pasted into the workflow.
    """
    step = _step_named(_stack_smoke_job(), "vpn failure is one of the two")
    run = step["run"]

    assert "_split_failure_text" in run
    assert "failure_vpn_refused" in run
    assert "failure_vpn_not_connected" in run
    assert "find_provider" in run
    assert "TUNNEL_NEVER_UP_AFTER_SECONDS" in run
    assert "$GLUETUN_LINE" in run
    assert "::error::" in run
    assert "::notice::" in run


def test_stack_smoke_asserts_the_control_server_requires_the_api_key() -> None:
    """ "All routes are now private by default" (gluetun-wiki) - proven here
    with no VPN account at all: a request with no key is refused, and the
    key Marrquee generated and wrote into its own auth file is accepted.
    """
    step = _step_named(_stack_smoke_job(), "control server wants marrquee's key")
    run = step["run"]

    assert "http://gluetun:8000/v1/vpn/status" in run
    assert "401" in run
    assert "X-API-Key" in run
    assert "install.json" in run
    assert "api_keys" in run
    assert "200" in run
    assert "::error::" in run


def test_stack_smoke_waits_for_gluetun_running_before_the_kill_switch_check() -> None:
    """Without this wait, `--network container:gluetun` could fail for an
    unrelated reason (no such container yet) and the kill-switch check would
    pass vacuously.
    """
    job = _stack_smoke_job()
    step = _step_named(job, "gluetun's container is running", "kill-switch")
    run = step["run"]

    assert ".State.Running" in run
    assert "seq 1 " in run
    assert "while true" not in run
    assert "::error::" in run

    steps = _steps(job)
    kill_switch_step = _step_named(job, "nothing gets out without the tunnel")
    assert steps.index(step) < steps.index(kill_switch_step)


def test_stack_smoke_kill_switch_pair_blocks_traffic_without_the_tunnel() -> None:
    step = _step_named(_stack_smoke_job(), "nothing gets out without the tunnel")
    run = step["run"]

    assert "1.1.1.1" in run
    assert "--network container:gluetun" in run
    assert "wget" in run
    assert "::error::Traffic left through the kill switch" in run
    assert "::error::the runner itself could not reach 1.1.1.1" in run


def test_stack_smoke_kill_switch_check_has_a_positive_control_through_gluetuns_namespace() -> None:
    """`docker run --network container:gluetun` joining Gluetun's namespace
    is proven separately from the real check, against a target that must
    always answer regardless of tunnel state (Gluetun's own control server,
    on loopback, which is never subject to Gluetun's own firewall) - so a
    "couldn't connect" result below is trusted to mean the kill switch, not
    a broken namespace join.
    """
    step = _step_named(_stack_smoke_job(), "nothing gets out without the tunnel")
    run = step["run"]

    assert "127.0.0.1:8000/v1/vpn/status" in run
    assert "control_exit" in run
    assert "::error::the kill-switch check could not run" in run
    assert "::error::the kill-switch check's positive control got no HTTP response" in run


def test_stack_smoke_kill_switch_real_check_never_reads_a_docker_failure_as_blocked() -> None:
    """The bug this hardens: `if docker run ...; then error; fi` reads ANY
    nonzero exit - including `docker run` itself failing to join the
    namespace (Docker's own 125+ convention) - as "the kill switch held".
    The exit code is captured and judged instead of the command's
    truthiness, and a docker-level failure gets its own distinct message.
    """
    step = _step_named(_stack_smoke_job(), "nothing gets out without the tunnel")
    run = step["run"]

    assert "real_exit=$?" in run
    assert "-ge 125" in run
    assert 'if [ "$real_exit" -eq 0 ]' in run
    # The old vacuous pattern must be gone: a bare `if docker run ...
    # --network container:gluetun ... ; then` immediately followed by the
    # kill-switch error, with no captured exit code in between.
    vacuous_pattern = (
        "if docker run --rm --network container:gluetun alpine:3.20"
        " wget -q -T 5 -O /dev/null http://1.1.1.1; then"
    )
    assert vacuous_pattern not in run


def test_stack_smoke_vpn_login_never_leaks_into_compose_or_diagnostics() -> None:
    step = _step_named(_stack_smoke_job(), "vpn login stays out of compose")
    run = step["run"]

    assert "compose.yaml" in run
    assert "/api/deploy/diagnostics" in run
    assert "marrquee-ci-not-a-user" in run
    assert "MARRQUEE_CI_VPN_PASSWORD" in run
    assert "::error::" in run


def test_stack_smoke_vpn_login_leak_check_never_echoes_the_password() -> None:
    step = _step_named(_stack_smoke_job(), "vpn login stays out of compose")
    for line in step["run"].splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or "::error::" in stripped:
            assert "MARRQUEE_CI_VPN_PASSWORD" not in stripped, (
                f"the VPN password appears in an echoed line: {line!r}"
            )


def test_stack_smoke_asserts_the_vpn_secrets_folder_is_root_only() -> None:
    step = _step_named(_stack_smoke_job(), "vpn secrets folder is root-only")
    run = step["run"]

    assert "/marrquee/vpn" in run
    assert "stat -c '%a %u'" in run
    assert '"700 0"' in run
    assert '"600 0"' in run
    assert "::error::" in run


def test_stack_smoke_cancels_the_vpn_add() -> None:
    step = _step_named(_stack_smoke_job(), "cancel the vpn add")
    run = step["run"]

    assert "/hub/apps/gluetun/cancel" in run
    assert '"303"' in run
    assert "::error::" in run


def test_stack_smoke_polls_until_gluetun_leaves_hub_status_after_cancel() -> None:
    step = _step_named(_stack_smoke_job(), "poll", "gluetun is fully removed")
    run = step["run"]

    assert "/api/hub/status" in run
    assert 'app_id == "gluetun"' in run
    assert "seq 1 " in run
    assert "while true" not in run
    assert "::error::" in run


def test_stack_smoke_cancel_removes_the_container_and_clears_the_secrets_folder() -> None:
    step = _step_named(_stack_smoke_job(), "cancel removed gluetun's container")
    run = step["run"]

    assert "docker inspect gluetun" in run
    assert "ls -A" in run
    assert "::error::gluetun's container still exists after cancel" in run
    assert "::error::" in run


def test_stack_smoke_vpn_steps_run_in_order_add_then_control_then_kill_switch_then_cancel() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    login_index = names.index(_step_named(job, "throwaway", "vpn", "login")["name"])
    add_index = names.index(_step_named(job, "add the vpn", "throwaway login")["name"])
    poll_index = names.index(_step_named(job, "poll", "vpn's add", "plain failure")["name"])
    words_index = names.index(_step_named(job, "vpn failure is one of the two")["name"])
    control_index = names.index(_step_named(job, "control server wants marrquee's key")["name"])
    running_index = names.index(
        _step_named(job, "gluetun's container is running", "kill-switch")["name"]
    )
    kill_switch_index = names.index(_step_named(job, "nothing gets out without the tunnel")["name"])
    leak_index = names.index(_step_named(job, "vpn login stays out of compose")["name"])
    perms_index = names.index(_step_named(job, "vpn secrets folder is root-only")["name"])
    cancel_index = names.index(_step_named(job, "cancel the vpn add")["name"])
    removed_index = names.index(_step_named(job, "poll", "gluetun is fully removed")["name"])
    cleared_index = names.index(_step_named(job, "cancel removed gluetun's container")["name"])

    assert (
        login_index
        < add_index
        < poll_index
        < words_index
        < control_index
        < running_index
        < kill_switch_index
        < leak_index
        < perms_index
        < cancel_index
        < removed_index
        < cleared_index
    )


# --- stack-smoke: qBittorrent inside Gluetun's network (developer test) ----
# The fake VPN login above never connects, so this whole block runs with a
# real Sonarr and a real qBittorrent but no live tunnel. Every needle below
# pairs "qbittorrent" with another word so `_step_named`'s first-match rule
# can never land on an existing arr-app step of the same generic shape
# (e.g. "the one login").


def test_stack_smoke_reasserts_gluetun_running_before_qbittorrent_joins_its_network() -> None:
    """Mirrors the existing kill-switch wait: qBittorrent's `network_mode:
    service:gluetun` would fail for an unrelated reason (no such container)
    if Gluetun were ever missing here, not because the network really
    isn't shared.
    """
    job = _stack_smoke_job()
    step = _step_named(job, "gluetun is still running", "qbittorrent")
    run = step["run"]

    assert ".State.Running" in run
    assert "seq 1 " in run
    assert "while true" not in run
    assert "::error::" in run

    steps = _steps(job)
    perms_step = _step_named(job, "vpn secrets folder is root-only")
    bring_up_step = _step_named(job, "qbittorrent inside the vpn's network")
    assert steps.index(perms_step) < steps.index(step) < steps.index(bring_up_step)


def test_stack_smoke_brings_qbittorrent_up_inside_gluetuns_network() -> None:
    """The FIRST assertion this chunk owns: qBittorrent's compose branch
    really does render with no network of its own, in memory, without ever
    writing the grown state back to `install.json`.
    """
    step = _step_named(_stack_smoke_job(), "qbittorrent inside the vpn's network")
    run = step["run"]

    assert "with_app_added" in run
    assert "write_qbit_conf" in run
    assert "build_stack_plan" in run
    assert "render_compose" in run
    assert "/config/ci-qbit-compose.yaml" in run
    assert "docker cp" in run
    assert "docker compose -p marrquee-apps" in run
    assert "--no-recreate qbittorrent" in run
    assert "HostConfig.NetworkMode" in run
    assert "container:" in run
    assert "::error::qBittorrent is not inside the VPN's network" in run
    assert "::error::" in run

    # The key itself must never reach stdout - only its length.
    assert "key length" in run
    assert "print(api_key)" not in run
    assert "print(key)" not in run


def test_stack_smoke_qbittorrent_bring_up_waits_for_its_container_before_driving_it() -> None:
    step = _step_named(_stack_smoke_job(), "qbittorrent inside the vpn's network")
    run = step["run"]

    assert ".State.Running" in run
    assert "seq 1 " in run
    assert "while true" not in run


def test_stack_smoke_qbittorrent_takes_marrquees_key() -> None:
    step = _step_named(_stack_smoke_job(), "qbittorrent takes marrquee's key")
    run = step["run"]

    assert "HttpQbitClient" in run
    assert "gluetun:8080" in run
    assert "api/v2/app/version" in run
    assert "200" in run
    assert "401" in run or "403" in run
    assert "::error::" in run


def test_stack_smoke_qbittorrent_key_check_never_prints_the_key() -> None:
    step = _step_named(_stack_smoke_job(), "qbittorrent takes marrquee's key")
    run = step["run"]

    assert "print(api_key)" not in run
    assert "print(key)" not in run
    assert "print(wrong_key)" not in run


def test_stack_smoke_qbittorrent_takes_the_one_login() -> None:
    step = _step_named(_stack_smoke_job(), "qbittorrent", "one login")
    run = step["run"]

    assert "HttpLoginApplier" in run
    assert "SavedLogin" in run
    assert "MARRQUEE_CI_PASSWORD" in run
    assert "gluetun:8080/api/v2/auth/login" in run
    assert "status_code != 204" in run
    assert "::error::" in run


def test_stack_smoke_qbittorrent_login_check_never_echoes_the_password() -> None:
    step = _step_named(_stack_smoke_job(), "qbittorrent", "one login")
    for line in step["run"].splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or "::error::" in stripped:
            assert "MARRQUEE_CI_PASSWORD" not in stripped, (
                f"the CI password appears in an echoed line: {line!r}"
            )


def test_stack_smoke_sonarr_connects_to_qbittorrent_with_the_key_while_the_tunnel_is_down() -> None:
    step = _step_named(_stack_smoke_job(), "sonarr connects to qbittorrent")
    run = step["run"]

    assert "ensure_qbit_category" in run
    assert "ensure_download_client" in run
    assert 'present = ("gluetun", "qbittorrent")' in run
    assert "present=present" in run
    assert "/api/v3/downloadclient" in run
    assert "QBittorrent" in run
    assert "removeCompletedDownloads" in run
    assert "gluetun" in run
    assert "::error::" in run


def test_stack_smoke_qbittorrent_cannot_reach_the_internet() -> None:
    step = _step_named(_stack_smoke_job(), "the downloader can't reach the internet")
    run = step["run"]

    assert "docker exec qbittorrent" in run
    assert "1.1.1.1" in run
    assert "::error::qBittorrent reached the internet outside the VPN" in run


def test_stack_smoke_kill_switch_check_asserts_the_probe_tool_exists() -> None:
    """A missing `curl` (exit 127) must never read as "blocked" - the tool
    is checked before either the positive control or the real check ever
    runs, with `wget` as a fallback for whichever the image actually ships.
    """
    step = _step_named(_stack_smoke_job(), "the downloader can't reach the internet")
    run = step["run"]

    assert "command -v curl" in run
    assert "command -v wget" in run
    assert "::error::curl is not available in the qBittorrent container" in run
    assert "the kill-switch check cannot run" in run


def test_stack_smoke_kill_switch_check_has_a_positive_control() -> None:
    """Gluetun's own control server, reached on loopback through the SAME
    shared network namespace qBittorrent's real check below is judged in,
    is never subject to Gluetun's own firewall - so it must always answer,
    proving the probe tool and the namespace both work.
    """
    step = _step_named(_stack_smoke_job(), "the downloader can't reach the internet")
    run = step["run"]

    assert "127.0.0.1:8000/v1/vpn/status" in run
    assert "::error::the kill-switch check's positive control got no HTTP response" in run


def test_stack_smoke_kill_switch_check_whitelists_exit_codes_instead_of_truthiness() -> None:
    """The bug this hardens: `if docker exec qbittorrent curl ...; then
    error; fi` reads ANY nonzero exit - a missing tool, a stopped
    container, an unrelated curl error - as "the kill switch held". Only
    curl's own couldn't-connect (7) and timeout (28) codes count as
    blocked; 0 is reached (an error); anything else is reported as its own,
    distinct failure rather than silently passing.
    """
    step = _step_named(_stack_smoke_job(), "the downloader can't reach the internet")
    run = step["run"]

    assert "real_exit=$?" in run
    assert "7|28" in run
    assert "::error::the kill-switch check failed for an unrelated reason" in run
    # The old vacuous pattern must be gone.
    assert "if docker exec qbittorrent curl -s -m 5 -o /dev/null http://1.1.1.1; then" not in run


def test_stack_smoke_removes_qbittorrent_before_gluetuns_teardown() -> None:
    job = _stack_smoke_job()
    step = _step_named(job, "remove qbittorrent")
    run = step["run"]

    assert "docker rm -f qbittorrent" in run

    steps = _steps(job)
    cannot_reach_step = _step_named(job, "the downloader can't reach the internet")
    cancel_step = _step_named(job, "cancel the vpn add")
    assert steps.index(cannot_reach_step) < steps.index(step) < steps.index(cancel_step)


def test_stack_smoke_qbittorrent_steps_run_between_the_secrets_check_and_the_vpn_cancel() -> None:
    """Pins the whole block's placement: after the root-only secrets check
    and before the VPN cancel step, so the existing order test that checks
    `perms_index < cancel_index` stays meaningful rather than merely true.
    """
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    perms_index = names.index(_step_named(job, "vpn secrets folder is root-only")["name"])
    running_index = names.index(_step_named(job, "gluetun is still running", "qbittorrent")["name"])
    bring_up_index = names.index(_step_named(job, "qbittorrent inside the vpn's network")["name"])
    key_index = names.index(_step_named(job, "qbittorrent takes marrquee's key")["name"])
    login_index = names.index(_step_named(job, "qbittorrent", "one login")["name"])
    sonarr_index = names.index(_step_named(job, "sonarr connects to qbittorrent")["name"])
    kill_switch_index = names.index(
        _step_named(job, "the downloader can't reach the internet")["name"]
    )
    remove_index = names.index(_step_named(job, "remove qbittorrent")["name"])
    cancel_index = names.index(_step_named(job, "cancel the vpn add")["name"])

    assert (
        perms_index
        < running_index
        < bring_up_index
        < key_index
        < login_index
        < sonarr_index
        < kill_switch_index
        < remove_index
        < cancel_index
    )


def test_stack_smoke_every_new_qbittorrent_step_emits_error_on_failure() -> None:
    job = _stack_smoke_job()
    names = [
        ("gluetun is still running", "qbittorrent"),
        ("qbittorrent inside the vpn's network",),
        ("qbittorrent takes marrquee's key",),
        ("qbittorrent", "one login"),
        ("sonarr connects to qbittorrent",),
        ("the downloader can't reach the internet",),
    ]
    for needles in names:
        step = _step_named(job, *needles)
        assert "::error::" in step["run"], f"step {step['name']!r} never emits ::error::"


def test_stack_smoke_new_qbittorrent_step_names_avoid_forbidden_needles() -> None:
    """`_step_named` returns the FIRST match - a new step whose name
    containing one of these generic phrases would silently resolve to an
    unrelated, already-existing step instead of its own.
    """
    job = _stack_smoke_job()
    forbidden = ("throwaway login", "cancel", "nothing gets out")
    new_step_needles = [
        ("gluetun is still running", "qbittorrent"),
        ("qbittorrent inside the vpn's network",),
        ("qbittorrent takes marrquee's key",),
        ("qbittorrent", "one login"),
        ("sonarr connects to qbittorrent",),
        ("the downloader can't reach the internet",),
        ("remove qbittorrent",),
    ]
    for needles in new_step_needles:
        name = str(_step_named(job, *needles)["name"]).lower()
        for phrase in forbidden:
            assert phrase not in name, f"step {name!r} contains the forbidden phrase {phrase!r}"


# --- Real Docker proves no-VPN mode, the safe move, and the way back. These
# steps run after the VPN cancel above, against a stack that already has
# qBittorrent and Gluetun both absent, Gluetun's port free, and Sonarr
# already pointed at a "gluetun" download-client entry left by an earlier
# step in this job. ---------------------------------------------------------


def test_stack_smoke_restarts_radarr_before_the_no_vpn_move() -> None:
    """Every wiring run the no-VPN steps trigger waits on every installed
    app, Radarr included - restarting it here (once) instead of letting
    each later run eat its own ready timeout for a Radarr that never came
    back.
    """
    job = _stack_smoke_job()
    step = _step_named(job, "start radarr again")
    run = step["run"]

    assert "docker start radarr" in run
    assert "seq 1 " in run
    assert "while true" not in run
    assert "::error::" in run

    names = [str(s.get("name", "")) for s in _steps(job)]
    radarr_index = names.index(step["name"])
    stop_index = names.index(_step_named(job, "stop radarr")["name"])
    choose_index = names.index(_step_named(job, "choose", "no-vpn")["name"])
    assert stop_index < radarr_index < choose_index


def test_stack_smoke_no_vpn_wrong_sentence_is_tried_first_and_saves_nothing() -> None:
    step = _step_named(_stack_smoke_job(), "choose", "no-vpn")
    run = step["run"]

    wrong_pos = run.index("typed=wrong")
    file_check_pos = run.index("without_vpn.json")
    stage_one_pos = run.index("stage=${stage}")
    assert wrong_pos < file_check_pos < stage_one_pos

    assert "test -e /config/without_vpn.json" in run
    assert "WITHOUT_VPN_PHRASE" in run
    assert '"303"' in run
    assert "::error::" in run


def test_stack_smoke_no_vpn_move_wipes_qbittorrents_config_folder_first() -> None:
    step = _step_named(_stack_smoke_job(), "choose", "no-vpn")
    run = step["run"]

    assert "rm -rf" in run
    assert "/marrquee/apps/qbittorrent" in run
    rm_pos = run.index("rm -rf")
    wrong_pos = run.index("typed=wrong")
    assert rm_pos < wrong_pos


def test_stack_smoke_no_vpn_add_moves_sonarrs_client_host_to_qbittorrent() -> None:
    """An earlier step already left Sonarr's downloadclient entry pointed
    at "gluetun" - the real no-VPN add has to notice the host changed (by
    name, since the implementation alone never changes) and PUT it back to
    "qbittorrent".
    """
    step = _step_named(_stack_smoke_job(), "add qbittorrent without a vpn")
    assert '"202"' in step["run"]
    assert "::error::" in step["run"]

    poll_step = _step_named(_stack_smoke_job(), "poll", "qbittorrent finishes adding without a vpn")
    assert "add_state" in poll_step["run"]
    assert "::error::" in poll_step["run"]

    assert_step = _step_named(_stack_smoke_job(), "sonarr's client points at it")
    run = assert_step["run"]
    assert "running_without_vpn" in run
    assert "container:" in run
    assert "127.0.0.1:8080" in run
    assert 'value == "qbittorrent"' in run
    assert "::error::" in run


def test_stack_smoke_no_vpn_hand_over_reuses_the_masked_vpn_password() -> None:
    """No new credential is generated for this second VPN attempt - the
    same masked throwaway login from earlier in the job is reused, so a
    leak here would already have tripped `::add-mask::` long before this
    step ever ran.
    """
    step = _step_named(_stack_smoke_job(), "hands", "qbittorrent's port")
    run = step["run"]

    assert "${MARRQUEE_CI_VPN_PASSWORD}" in run
    assert "openssl rand" not in run
    assert "::add-mask::" not in run
    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") or "::error::" in stripped or "::notice::" in stripped:
            assert "MARRQUEE_CI_VPN_PASSWORD" not in stripped, (
                f"the VPN password appears in an echoed line: {line!r}"
            )


def test_stack_smoke_no_vpn_hand_over_step_checks_qbittorrent_is_gone_and_gluetun_holds_8080() -> (
    None
):
    step = _step_named(_stack_smoke_job(), "hands", "qbittorrent's port")
    run = step["run"]

    assert '"303"' in run
    assert "Add your VPN did not start" in run
    assert "docker inspect qbittorrent" in run
    assert "qBittorrent was still running when the VPN started" in run
    assert "HostConfig.PortBindings" in run
    assert '"8080/tcp"' in run
    assert "The VPN did not get qBittorrent's port" in run


def test_stack_smoke_no_vpn_hand_over_failure_is_one_of_the_two_plain_sentences() -> None:
    step = _step_named(_stack_smoke_job(), "no-vpn hand-over", "plain sentences")
    run = step["run"]

    assert "_split_failure_text" in run
    assert "failure_vpn_refused" in run
    assert "failure_vpn_not_connected" in run
    assert "NO_VPN_GLUETUN_LINE" in run
    assert 'select(.app_id == "qbittorrent") | .paused' in run
    assert '"true"' in run
    assert "::error::" in run


def test_stack_smoke_no_vpn_restore_brings_qbittorrent_back_on_its_own_network() -> None:
    step = _step_named(_stack_smoke_job(), "no-vpn restore")
    run = step["run"]

    assert "/hub/apps/gluetun/cancel" in run
    assert '"303"' in run
    assert "docker inspect gluetun" in run
    assert "container:" in run
    assert "running_without_vpn" in run
    assert "ls -A" in run
    assert "test -e /config/without_vpn.json" in run
    assert "::error::" in run


def test_stack_smoke_no_vpn_steps_run_after_cancel_in_order_choose_add_hand_over_keep() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    cancel_assert_index = names.index(
        _step_named(job, "cancel removed gluetun's container")["name"]
    )
    radarr_index = names.index(_step_named(job, "start radarr again")["name"])
    choose_index = names.index(_step_named(job, "choose", "no-vpn")["name"])
    add_index = names.index(_step_named(job, "add qbittorrent without a vpn")["name"])
    add_poll_index = names.index(
        _step_named(job, "poll", "qbittorrent finishes adding without a vpn")["name"]
    )
    add_assert_index = names.index(_step_named(job, "sonarr's client points at it")["name"])
    hand_over_index = names.index(_step_named(job, "hands", "qbittorrent's port")["name"])
    hand_over_poll_index = names.index(_step_named(job, "poll", "no-vpn hand-over")["name"])
    hand_over_assert_index = names.index(
        _step_named(job, "no-vpn hand-over", "plain sentences")["name"]
    )
    keep_index = names.index(_step_named(job, "no-vpn restore")["name"])
    drive_index = names.index(_step_named(job, "drive check agrees")["name"])
    syncs_index = names.index(_step_named(job, "recyclarr syncs quality")["name"])
    amber_index = names.index(_step_named(job, "sync turns amber")["name"])
    plex_host_index = names.index(_step_named(job, "plex runs on the host network")["name"])
    plex_secret_index = names.index(_step_named(job, "link code stays root-only")["name"])
    dump_index = names.index(_step_named(job, "dump diagnostics")["name"])

    assert (
        cancel_assert_index
        < radarr_index
        < choose_index
        < add_index
        < add_poll_index
        < add_assert_index
        < hand_over_index
        < hand_over_poll_index
        < hand_over_assert_index
        < keep_index
        < drive_index
        < syncs_index
        < amber_index
        < plex_host_index
        < plex_secret_index
        < dump_index
    )


def test_stack_smoke_every_no_vpn_step_emits_error_on_failure() -> None:
    job = _stack_smoke_job()
    needle_sets = [
        ("start radarr again",),
        ("choose", "no-vpn"),
        ("add qbittorrent without a vpn",),
        ("poll", "qbittorrent finishes adding without a vpn"),
        ("sonarr's client points at it",),
        ("hands", "qbittorrent's port"),
        ("poll", "no-vpn hand-over"),
        ("no-vpn hand-over", "plain sentences"),
        ("no-vpn restore",),
        ("drive check agrees",),
        ("recyclarr syncs quality",),
        ("sync turns amber",),
        ("plex runs on the host network",),
        ("link code stays root-only",),
    ]
    for needles in needle_sets:
        step = _step_named(job, *needles)
        assert "::error::" in step["run"], f"step {step['name']!r} never emits ::error::"


def test_stack_smoke_no_vpn_step_names_avoid_forbidden_needles() -> None:
    """`_step_named` returns the FIRST match - a new no-VPN step whose name
    contains one of these generic phrases (already used by an earlier step
    in this same job) would silently resolve to that earlier step instead
    of its own.
    """
    job = _stack_smoke_job()
    forbidden = ("developer test", "remove qbittorrent", "add the vpn", "cancel the vpn add")
    needle_sets = [
        ("start radarr again",),
        ("choose", "no-vpn"),
        ("add qbittorrent without a vpn",),
        ("poll", "qbittorrent finishes adding without a vpn"),
        ("sonarr's client points at it",),
        ("hands", "qbittorrent's port"),
        ("poll", "no-vpn hand-over"),
        ("no-vpn hand-over", "plain sentences"),
        ("no-vpn restore",),
        ("drive check agrees",),
    ]
    for needles in needle_sets:
        name = str(_step_named(job, *needles)["name"]).lower()
        for phrase in forbidden:
            assert phrase not in name, f"step {name!r} contains the forbidden phrase {phrase!r}"


def test_stack_smoke_drive_check_agrees_with_sonarrs_own_hard_link() -> None:
    """Marrquee's saved verdict, made from its own `/host` view of the
    drive, must agree with a hard link Sonarr makes for real inside its own
    container's `/data` view - only a real daemon and a real bind mount can
    prove the two views see the same filesystem.
    """
    step = _step_named(_stack_smoke_job(), "drive check agrees")
    run = step["run"]

    # Marrquee's own saved verdict, read from inside marrquee-stack-smoke.
    assert "load_hardlink_result" in run
    assert "Settings.from_env" in run
    assert "works/None" in run

    # Sonarr's own hard link, run as the install's own PUID/PGID, with a
    # tool-exists check first and a trap that always cleans up.
    assert "install['puid']" in run
    assert "install['pgid']" in run
    assert "command -v ln" in run
    assert "command -v stat" in run
    assert 'docker exec -u "$puid:$pgid" sonarr' in run
    assert "trap" in run
    assert ".ci-link-proof" in run
    assert "stat -c %i" in run

    # The leftover-file check: a positive control before the real check,
    # and an exit-code whitelist rather than a bare truthiness test.
    assert "test -d" in run
    assert ".marrquee-link-test" in run
    assert "leftover" in run

    # Diagnostics reads the same "works" words a person would see, compared
    # the way Jinja actually escapes an apostrophe.
    assert "DRIVE_WORKS_TITLE" in run
    assert "html.unescape" in run
    assert "html.escape" not in run

    assert run.count("::error::") >= 5


def test_stack_smoke_drive_check_never_aborts_silently_under_set_e() -> None:
    """Every `docker exec` in this step runs under `set -euo pipefail` - one
    that fails outright (the container isn't ready yet, a stray import
    error) must not abort the whole step with no `::error::` reason. The
    poll loop retries instead of dying on its first failed attempt, and the
    PUID/PGID read is checked before `read` ever sees its output.
    """
    step = _step_named(_stack_smoke_job(), "drive check agrees")
    run = step["run"]

    assert '2>/dev/null) || outcome=""' in run
    assert "ids=$(docker exec marrquee-stack-smoke python3 -c" in run
    assert "couldn't read PUID/PGID from /config/install.json" in run
    assert 'read -r puid pgid <<< "$ids"' in run


def test_stack_smoke_drive_check_leftover_case_arms_are_structurally_sound() -> None:
    """A plain substring match on `::error::` or `exit 1` anywhere in the
    step would still pass if the `case` arm that actually GATES that text
    were hollowed out to a no-op, or a no-op arm grew a stray `exit 1`
    itself. Each arm is checked on its own, by its own label.
    """
    step = _step_named(_stack_smoke_job(), "drive check agrees")
    run = step["run"]

    case_block = _text_between(run, 'case "$leftover" in', "esac")
    arms = _shell_case_arms(case_block)

    assert set(arms) == {"0", "1", "*"}

    # exit code 0 = the test file exists = litter was left behind: a real
    # failure, not silence.
    assert "::error::" in arms["0"]
    assert "exit 1" in arms["0"]

    # exit code 1 = the test file is gone = the check passed clean: nothing
    # to report, and nothing that exits the step.
    assert arms["1"] == ""

    # anything else = the check itself couldn't run: also a real failure.
    assert "::error::" in arms["*"]
    assert "exit 1" in arms["*"]


def test_stack_smoke_drive_check_sonarrs_link_proof_actually_compares_inodes() -> None:
    """The proof that Sonarr's own link worked is the LAST thing its shell
    script does, under `set -e` - a `true` (or any other placeholder)
    swapped in for the inode comparison would make the script "succeed"
    unconditionally, which a bare `in run` substring check for the two
    variable names could never catch.
    """
    step = _step_named(_stack_smoke_job(), "drive check agrees")
    run = step["run"]

    start_marker = 'docker exec -u "$puid:$pgid" sonarr sh -c \''
    script = _text_between(run, start_marker, "'")

    assert "set -e" in script
    assert "source_inode=$(stat -c %i /data/torrents/tv/.ci-link-proof)" in script
    assert "dest_inode=$(stat -c %i /data/media/tv/.ci-link-proof)" in script
    assert _last_nonblank_line(script) == '[ "$source_inode" = "$dest_inode" ]'


def test_stack_smoke_cleanup_list_already_covers_qbittorrent_and_gluetun() -> None:
    """The no-VPN steps leave qBittorrent installed and Gluetun absent - the
    final cleanup still has to remove qBittorrent unconditionally (it does;
    Gluetun's absence is already proven by the restore step) so a failed
    run still tears down every container this job made.
    """
    step = _step_named(_stack_smoke_job(), "clean up")
    run = step["run"]
    assert "qbittorrent" in run
    assert "gluetun" in run
    assert "recyclarr" in run
    assert "plex" in run


# --- stack-smoke: Recyclarr on a real daemon - the guide-backed profiles
# really land in Sonarr and Radarr, it runs as the drive owner with no key
# of its own, and a sync that cannot reach a stopped Radarr turns the
# poster amber with the exact reason `recyclarr_line_app_down` gives. -----


def test_stack_smoke_recyclarr_sync_step_installs_through_the_hub_endpoint_and_polls_states() -> (
    None
):
    step = _step_named(_stack_smoke_job(), "recyclarr syncs quality")
    run = step["run"]

    assert "docker start radarr" in run
    assert "/api/hub/apps/recyclarr/install" in run
    assert '"answers":{"tv_quality":"1080p","movie_quality":"4k"}' in run
    assert '"202"' in run
    assert "add_state" in run
    assert "sync_state" in run
    assert 'select(.app_id == "recyclarr")' in run
    assert "seq 1 " in run
    assert "while true" not in run
    assert "Recyclarr's first sync failed:" in run
    assert "Recyclarr never finished adding and syncing within 10 minutes" in run
    assert run.count("::error::") >= 5

    # The decisive comparisons, pinned by their exact text - a poll loop
    # whose break condition or final gate was hollowed out to something
    # always-true would still contain every substring above.
    assert 'if [ "$add_state" = "null" ] && [ "$sync_state" = "ok" ]; then' in run
    assert 'if [ "$add_state" != "null" ] || [ "$sync_state" != "ok" ]; then' in run


def test_stack_smoke_recyclarr_sync_step_retry_loops_never_abort_under_set_e() -> None:
    """Every polling loop's own `curl` is guarded so one transient failure
    retries instead of aborting the whole step under `set -euo pipefail`.
    """
    step = _step_named(_stack_smoke_job(), "recyclarr syncs quality")
    run = step["run"]

    assert "set -euo pipefail" in run
    assert run.count('status=$(curl -fsS http://127.0.0.1:7788/api/hub/status) || status=""') >= 2


def test_stack_smoke_recyclarr_sync_step_checks_the_log_marrquee_reads() -> None:
    step = _step_named(_stack_smoke_job(), "recyclarr syncs quality")
    run = step["run"]

    assert "/host${RUNNER_TEMP}/marrquee-smoke/media/marrquee/apps/recyclarr/logs/cli" in run
    assert r"^recyclarr_.*\.debug\.log$" in run
    assert "Recyclarr wrote no log where Marrquee reads it" in run


def test_stack_smoke_recyclarr_sync_step_reads_live_quality_profiles_with_httpx() -> None:
    """The one condition reading source can't settle: Recyclarr's own
    v8.7.2 config sync really creates the guide-backed profiles in a live
    Sonarr and Radarr, read back with the same keys Marrquee generated.
    """
    step = _step_named(_stack_smoke_job(), "recyclarr syncs quality")
    run = step["run"]

    assert "import httpx" in run
    assert "/config/install.json" in run
    assert "http://sonarr:8989/api/v3/qualityprofile" in run
    assert "http://radarr:7878/api/v3/qualityprofile" in run
    assert '"X-Api-Key": api_keys["sonarr"]' in run
    assert '"X-Api-Key": api_keys["radarr"]' in run
    assert '"WEB-1080p" not in sonarr_names' in run
    assert '"UHD Bluray + WEB" not in radarr_names' in run
    assert "Recyclarr's guide-backed profiles never landed:" in run


def test_stack_smoke_recyclarr_sync_step_checks_it_runs_as_the_drive_owner() -> None:
    """WHICH USER runs this: Recyclarr's own image ignores PUID/PGID, so
    `user:` landing it on the drive owner - not the image's own default -
    is a live-daemon proof, not something reading `compose.py` can settle.
    """
    step = _step_named(_stack_smoke_job(), "recyclarr syncs quality")
    run = step["run"]

    assert "install['puid']" in run
    assert "install['pgid']" in run
    assert "docker inspect -f '{{.Config.User}}' recyclarr" in run
    assert '"${puid}:${pgid}"' in run
    assert "command -v stat" in run
    assert "stat -c '%a %u'" in run
    assert '"600 ${puid}"' in run
    assert "recyclarr.yml" in run


def test_stack_smoke_recyclarr_sync_step_key_leak_check_is_structurally_sound() -> None:
    """A plain substring match on `::error::` or `exit 1` anywhere in this
    step would still pass if the `case` arm that actually gates a leaked
    key were hollowed out to a no-op, or a no-op arm grew a stray `exit 1`
    of its own - each arm is checked on its own, by its own label, the
    same discipline the drive check's leftover-file check already uses.
    """
    step = _step_named(_stack_smoke_job(), "recyclarr syncs quality")
    run = step["run"]

    assert "command -v grep" in run
    assert "the positive control failed" in run
    assert "sed -n '/^  sonarr:/,/^$/p'" in run
    assert "sed -n '/^  recyclarr:/,/^$/p'" in run
    assert 'grep -qF "$sonarr_key"' in run
    assert 'for key in "$sonarr_key" "$radarr_key"' in run

    case_block = _text_between(run, 'case "$leaked" in', "esac")
    arms = _shell_case_arms(case_block)

    assert set(arms) == {"0", "1", "*"}

    # exit code 1 = grep found nothing = no key leaked: nothing to report.
    assert arms["1"] == ""

    # exit code 0 = grep found the key = it leaked into compose.yaml: a
    # real failure, not silence.
    assert "::error::" in arms["0"]
    assert "exit 1" in arms["0"]

    # anything else = the check itself couldn't run: also a real failure.
    assert "::error::" in arms["*"]
    assert "exit 1" in arms["*"]


def test_stack_smoke_amber_step_stops_radarr_and_polls_for_the_app_down_reason() -> None:
    step = _step_named(_stack_smoke_job(), "sync turns amber")
    run = step["run"]

    assert "docker stop radarr" in run
    assert "/hub/apps/recyclarr/sync" in run
    assert '"303"' in run
    assert 'select(.app_id == "recyclarr")' in run
    assert '"failed"' in run
    assert "A failed sync was not reported (state" in run
    assert "seq 1 " in run
    assert "while true" not in run
    assert run.count("::error::") >= 4


def test_stack_smoke_amber_step_compares_the_reason_with_html_unescape_never_html_escape() -> None:
    """Apostrophes: Recyclarr's words contain one ("it's"), and the
    comparison must never accidentally re-encode a real apostrophe as its
    own opposite.
    """
    step = _step_named(_stack_smoke_job(), "sync turns amber")
    run = step["run"]

    assert "from marrquee.words import recyclarr_line_app_down" in run
    assert 'recyclarr_line_app_down("Radarr")' in run
    assert "html.unescape" in run
    assert "html.escape" not in run
    assert "the failed sync's reason did not match" in run

    # The decisive comparison itself, pinned by its exact text - a `true`
    # or an unconditional `SystemExit(0)` swapped in for the equality check
    # would still contain every substring above.
    assert "raise SystemExit(0 if actual == expected else 1)" in run


def test_stack_smoke_amber_step_restores_radarr_and_syncs_back_to_ok() -> None:
    """The amber step's own positive control is step 1's "ok" - this only
    proves the restore path, not a second independent control.
    """
    step = _step_named(_stack_smoke_job(), "sync turns amber")
    run = step["run"]

    assert run.count("docker start radarr") == 1
    assert run.count('"303"') == 2
    assert '"ok"' in run
    assert "Recyclarr never recovered to sync_state=ok" in run


def test_stack_smoke_recyclarr_and_amber_steps_pin_every_post_loop_verdict() -> None:
    """Every poll loop in both steps ends its own life-or-death branch:
    the exact guard, its own `::error::` line, and a bare `exit 1` right
    after it - not a `true`/`false` placeholder that would leave every
    other assertion in this file's other tests still sitting untouched in
    the step's text.
    """
    job = _stack_smoke_job()
    sync_run = _step_named(job, "recyclarr syncs quality")["run"]
    amber_run = _step_named(job, "sync turns amber")["run"]

    _assert_post_loop_verdict(
        sync_run,
        'if [ "$radarr_state" != "up" ]; then',
        "Radarr was not Up ahead of the Recyclarr sync proof",
    )
    _assert_post_loop_verdict(
        sync_run,
        'if [ "$add_state" != "null" ] || [ "$sync_state" != "ok" ]; then',
        "Recyclarr never finished adding and syncing within 10 minutes",
    )
    _assert_post_loop_verdict(
        amber_run,
        'if [ "$sync_state" != "failed" ]; then',
        "A failed sync was not reported",
    )
    _assert_post_loop_verdict(
        amber_run,
        'if [ "$radarr_state" != "up" ]; then',
        "Radarr never came back Up after being restarted",
    )
    _assert_post_loop_verdict(
        amber_run,
        'if [ "$sync_state" != "ok" ]; then',
        "Recyclarr never recovered to sync_state=ok",
    )


def test_stack_smoke_recyclarr_step_names_avoid_forbidden_needles() -> None:
    """`_step_named` returns the FIRST match - a step named with a generic
    phrase already used by an earlier step in this same job (or the
    "developer test" phrase this chunk was told never to use) would
    silently resolve to that earlier step instead of its own.
    """
    job = _stack_smoke_job()
    forbidden = (
        "developer test",
        "stop radarr",
        "start radarr again",
        "cancel",
        "clean up",
        "dump diagnostics",
    )
    for needles in [("recyclarr syncs quality",), ("sync turns amber",)]:
        name = str(_step_named(job, *needles)["name"]).lower()
        for phrase in forbidden:
            assert phrase not in name, f"step {name!r} contains the forbidden phrase {phrase!r}"


# --- stack-smoke: Plex on a real daemon - host networking, the gateway
# address, the FILE__ claim read and root-only secrets. The account link
# itself stays PENDING the owner's own NAS run. ----------------------------


def test_stack_smoke_plex_step_names_avoid_forbidden_needles() -> None:
    """Same reasoning as the Recyclarr steps: a name that collides with an
    earlier step's own name (or the "developer test" phrase this chunk was
    told never to use) would silently resolve `_step_named` to that earlier
    step instead of Plex's own.
    """
    job = _stack_smoke_job()
    forbidden = (
        "developer test",
        "recyclarr",
        "sync turns amber",
        "stop radarr",
        "start radarr again",
        "cancel",
        "clean up",
        "dump diagnostics",
    )
    for needles in [
        ("plex runs on the host network",),
        ("link code stays root-only",),
    ]:
        name = str(_step_named(job, *needles)["name"]).lower()
        for phrase in forbidden:
            assert phrase not in name, f"step {name!r} contains the forbidden phrase {phrase!r}"


def test_stack_smoke_plex_host_step_grows_the_plan_in_memory_and_writes_the_fake_claim() -> None:
    """Plex is added to the compose plan the same way `DeployManager._stack_plan`
    does it for real - `without_vpn_confirmed` included, since qBittorrent is
    already running without Gluetun by this point in the job - but entirely
    in memory: install.json is only ever read, never re-saved, and the
    rendered file lands beside the real install rather than on top of it.
    """
    step = _step_named(_stack_smoke_job(), "plex runs on the host network")
    run = step["run"]

    assert "set -euo pipefail" in run
    assert "from marrquee.install import with_app_added" in run
    assert 'with_app_added(state, "plex")' in run
    assert "from marrquee.without_vpn import without_vpn_confirmed" in run
    assert "without_vpn=without_vpn_confirmed(settings.config_dir)" in run
    assert 'write_plex_claim(settings, root, "claim-marrquee-ci-not-real")' in run
    assert "f\"/host{os.environ['RUNNER_TEMP']}/marrquee-smoke/ci-plex-compose.yaml\"" in run
    # The real install's own compose.yaml is never named as a write target
    # anywhere in this step - only ever read from, in the secrets step below.
    assert "write_text" in run
    assert "compose_path.write_text(render_compose(plan))" in run


def test_stack_smoke_plex_host_step_starts_it_through_socket_docker_engine_and_checks_ok() -> None:
    """The decisive comparison is pinned two ways, both scoped to THIS
    heredoc only (`_heredoc_body`), never a bare substring search over the
    whole step: line-adjacency (the guard and its `raise` sit immediately
    after the `compose_up` call, verbatim) and a real `ast.parse` of the
    body, so `if not result.ok:` swapped for `if False:` - or any other
    hollowed guard - is caught even though an identical-looking line
    already exists, for real, in an unrelated step's own heredoc elsewhere
    in this same file.
    """
    run = _step_named(_stack_smoke_job(), "plex runs on the host network")["run"]
    body = _heredoc_body(run, "ci_plex_bring_up.py")

    assert "from marrquee.docker_client import SocketDockerEngine" in run
    assert (
        "engine = SocketDockerEngine(\n"
        "        settings.docker_socket, compose_binary=settings.compose_binary\n"
        "    )" in body
    )

    lines = body.splitlines()
    compose_up_line = 'result = await engine.compose_up("marrquee-apps", compose_path, "plex")'
    compose_up_index = next(
        index for index, line in enumerate(lines) if line.strip() == compose_up_line
    )
    guard_line = lines[compose_up_index + 1].strip()
    raise_line = lines[compose_up_index + 2].strip()
    assert guard_line == "if not result.ok:"
    assert raise_line == 'raise SystemExit(f"compose up for plex failed: {result.output}")'

    # Belt and suspenders: real Python, not just adjacent lines - a stray
    # `pass` or a re-indented guard that broke the block would show up here
    # even if the line-by-line check above were somehow fooled.
    tree = ast.parse(body)

    def _is_not_result_ok(test: ast.expr) -> bool:
        return (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Attribute)
            and test.operand.attr == "ok"
            and isinstance(test.operand.value, ast.Name)
            and test.operand.value.id == "result"
        )

    def _raises_system_exit(stmt: ast.stmt) -> bool:
        return (
            isinstance(stmt, ast.Raise)
            and isinstance(stmt.exc, ast.Call)
            and isinstance(stmt.exc.func, ast.Name)
            and stmt.exc.func.id == "SystemExit"
        )

    guards = [
        node for node in ast.walk(tree) if isinstance(node, ast.If) and _is_not_result_ok(node.test)
    ]
    assert guards, "no `if not result.ok:` guard found in the bring-up script"
    assert guards[0].body and _raises_system_exit(guards[0].body[0]), (
        "the if-not-result.ok guard's body doesn't raise SystemExit"
    )

    assert (
        'if ! bring_up_output=$(docker exec -e RUNNER_TEMP="$RUNNER_TEMP" marrquee-stack-smoke '
        'python3 /tmp/ci_plex_bring_up.py 2>&1) || [ -z "$bring_up_output" ]; then' in run
    )
    assert "::error::Plex's in-memory compose bring-up failed" in run
    assert "::notice::" in run


def test_stack_smoke_plex_host_step_pins_the_network_mode_verdict() -> None:
    """A failed `docker inspect` must itself be caught with its own
    `::error::` (bare under `set -euo pipefail`, it would otherwise abort
    the step silently) before the real "is it host mode" verdict is even
    reached.
    """
    run = _step_named(_stack_smoke_job(), "plex runs on the host network")["run"]

    assert "docker inspect -f '{{.HostConfig.NetworkMode}}' plex" in run
    _assert_post_loop_verdict(
        run,
        "if ! network_mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' plex) "
        '|| [ -z "$network_mode" ]; then',
        "couldn't inspect the plex container's network mode",
    )
    _assert_post_loop_verdict(
        run,
        'if [ "$network_mode" != "host" ]; then',
        "Plex is not on the host network",
    )


def test_stack_smoke_plex_host_step_reaches_plex_through_its_own_gateway_address() -> None:
    """Marrquee resolves its own reachable address the same way production
    code does - `plex_host_address` off `self_container_id()` - never a
    hardcoded `127.0.0.1` or the bridge-only `http://plex:32400` a
    Plex-only stack could never create.
    """
    run = _step_named(_stack_smoke_job(), "plex runs on the host network")["run"]

    assert "from marrquee.plex import plex_host_address" in run
    assert "await engine.self_container_id()" in run
    assert "address = await plex_host_address(engine, self_id)" in run
    assert (
        "if ! address=$(docker exec marrquee-stack-smoke python3 /tmp/ci_plex_address.py) "
        '|| [ -z "$address" ]; then' in run
    )


def test_stack_smoke_plex_host_step_polls_identity_bounded_and_pins_both_verdicts() -> None:
    """The identity poll is a bounded loop (never `while true`), and both of
    its own post-loop verdicts are pinned verbatim: reachability first
    (a None/empty result), then the claimed check second - a fake claim
    code must never be reported as linked.
    """
    run = _step_named(_stack_smoke_job(), "plex runs on the host network")["run"]

    assert "from marrquee.plex import HttpPlexServer, plex_base_url" in run
    assert "for _ in $(seq 1 60); do" in run
    assert "while true" not in run
    assert (
        'identity_state=$(docker exec -e PLEX_ADDRESS="$address" marrquee-stack-smoke '
        'python3 /tmp/ci_plex_identity.py) || identity_state=""' in run
    )

    _assert_post_loop_verdict(
        run,
        'if [ "$identity_state" = "none" ] || [ -z "$identity_state" ]; then',
        "Marrquee couldn't reach Plex through the host address",
    )
    _assert_post_loop_verdict(
        run,
        'if [ "$identity_state" != "unclaimed" ]; then',
        "A fake claim code was reported as linked",
    )


def test_stack_smoke_plex_host_step_checks_the_logs_with_a_tool_check_and_whitelist() -> None:
    """Both linuxserver init lines are checked with `grep -F` (never a regex
    that could partially match something else), gated by a `command -v
    grep` tool check, and the grep's own exit code is whitelisted rather
    than trusted blindly - the same shape the Recyclarr key-leak check
    already uses.
    """
    run = _step_named(_stack_smoke_job(), "plex runs on the host network")["run"]

    assert "command -v grep" in run
    assert "plex_logs=$(docker logs plex 2>&1 || true)" in run
    assert '"PLEX_CLAIM set from FILE__PLEX_CLAIM"' in run
    assert '"Unable to claim Plex server"' in run
    assert 'grep -qF "$phrase"' in run

    case_block = _text_between(run, 'case "$found" in', "esac")
    arms = _shell_case_arms(case_block)
    assert arms == {
        "0": "",
        "1": 'echo "::error::Plex\'s own log never said: ${phrase}"; exit 1',
        "*": (
            "echo \"::error::couldn't check Plex's own logs for its claim code "
            '(grep exit ${found})"; exit 1'
        ),
    }


def test_stack_smoke_plex_host_step_polls_web_index_bounded_and_pins_the_verdict() -> None:
    """The web UI can lag `/identity`, so this polls rather than asserting a
    single request - and a connection refused (curl's own non-zero exit)
    must never abort the step silently under `set -euo pipefail`.
    """
    run = _step_named(_stack_smoke_job(), "plex runs on the host network")["run"]

    assert (
        "web_code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:32400/web/index.html) "
        '|| web_code=""' in run
    )
    assert "for _ in $(seq 1 12); do" in run
    assert "while true" not in run
    _assert_post_loop_verdict(
        run,
        'if [ "$web_code" != "200" ]; then',
        "Plex's /web/index.html did not answer 200",
    )


def test_stack_smoke_plex_secrets_step_checks_folder_and_file_permissions() -> None:
    """0700 for the folder, 0600 for the file, both owned by root (uid 0) -
    Marrquee's own container runs as root, and only it is ever meant to
    read this folder.
    """
    run = _step_named(_stack_smoke_job(), "link code stays root-only")["run"]

    assert "command -v stat" in run
    assert "stat -c '%a %u'" in run

    _assert_post_loop_verdict(
        run,
        'if [ "$dir_perms" != "700 0" ]; then',
        "Plex's secrets folder was not 0700 owned by root",
    )
    _assert_post_loop_verdict(
        run,
        'if [ "$file_perms" != "600 0" ]; then',
        "Plex's claim file was not 0600 owned by root",
    )


def test_stack_smoke_plex_secrets_step_never_echoes_the_claim_or_a_token() -> None:
    step = _step_named(_stack_smoke_job(), "link code stays root-only")
    for line in step["run"].splitlines():
        stripped = line.strip()
        if stripped.startswith("echo"):
            assert "claim-marrquee-ci-not-real" not in stripped, (
                f"the fake claim code appears in an echoed line: {line!r}"
            )


def test_stack_smoke_plex_secrets_step_proves_the_positive_control_before_the_real_check() -> None:
    """A grep that could never find anything must fail loudly (the positive
    control) rather than let the real negative check below it pass by
    accident - the same shape the Recyclarr key-leak check already uses.
    """
    run = _step_named(_stack_smoke_job(), "link code stays root-only")["run"]

    assert (
        'if ! docker exec marrquee-stack-smoke grep -qF "claim-marrquee-ci-not-real" '
        '"$claim_path"; then' in run
    )
    assert "the positive control failed" in run

    positive_control_index = run.index("the positive control failed")
    negative_check_index = run.index(
        'docker exec marrquee-stack-smoke grep -qF "claim-marrquee-ci-not-real" "$compose_path"'
    )
    assert positive_control_index < negative_check_index

    case_block = _text_between(run, 'case "$leaked" in', "esac")
    arms = _shell_case_arms(case_block)
    assert arms == {
        "1": "",
        "0": 'echo "::error::the fake Plex claim code leaked into the real compose.yaml"; exit 1',
        "*": (
            "echo \"::error::couldn't check compose.yaml for a leaked Plex claim code "
            '(grep exit ${leaked})"; exit 1'
        ),
    }


def test_stack_smoke_plex_secrets_step_clears_the_claim_and_confirms_the_folder_is_empty() -> None:
    run = _step_named(_stack_smoke_job(), "link code stays root-only")["run"]

    assert "from marrquee.plex import clear_plex_claim" in run
    assert "clear_plex_claim(settings, PurePosixPath(state.storage_root))" in run

    _assert_post_loop_verdict(
        run,
        "if ! docker exec marrquee-stack-smoke python3 /tmp/ci_plex_clear_claim.py; then",
        "clear_plex_claim failed to run",
    )
    assert (
        "if ! remaining=$(docker exec marrquee-stack-smoke sh -c \"ls -A '${secrets_dir}'\"); then"
        in run
    )
    _assert_post_loop_verdict(
        run,
        'if [ -n "$remaining" ]; then',
        "Plex's secrets folder was not empty after clear_plex_claim",
    )


# --- stack-smoke: Jellyfin on a real daemon - the graphics chip probe never
# starts a container or creates a host path, the Hub's own install endpoint
# finishes Jellyfin's first-time setup with the one login and its libraries,
# and changing that login changes Jellyfin's admin without restarting it.
# Real hardware transcoding stays PENDING the owner's own NAS. --------------


def test_stack_smoke_jellyfin_step_names_avoid_forbidden_needles() -> None:
    """Same reasoning as the Recyclarr and Plex steps above: a name that
    collides with an earlier step's own name (or the generic "developer
    test" phrase) would silently resolve `_step_named` to that earlier
    step instead of Jellyfin's own.
    """
    job = _stack_smoke_job()
    forbidden = (
        "developer test",
        "recyclarr",
        "sync turns amber",
        "stop radarr",
        "start radarr again",
        "cancel",
        "clean up",
        "dump diagnostics",
        "plex runs on the host network",
        "link code stays root-only",
        "throwaway",
    )
    for needles in [
        ("graphics chip check",),
        ("jellyfin is set up",),
        ("changes jellyfin's admin",),
    ]:
        name = str(_step_named(job, *needles)["name"]).lower()
        for phrase in forbidden:
            assert phrase not in name, f"step {name!r} contains the forbidden phrase {phrase!r}"


def test_stack_smoke_jellyfin_steps_run_after_plex_and_before_dump() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    plex_secret_index = names.index(_step_named(job, "link code stays root-only")["name"])
    chip_index = names.index(_step_named(job, "graphics chip check")["name"])
    jf_setup_index = names.index(_step_named(job, "jellyfin is set up")["name"])
    jf_login_index = names.index(_step_named(job, "changes jellyfin's admin")["name"])
    dump_index = names.index(_step_named(job, "dump diagnostics")["name"])

    assert plex_secret_index < chip_index < jf_setup_index < jf_login_index < dump_index


def test_stack_smoke_every_new_jellyfin_step_emits_error_on_failure() -> None:
    job = _stack_smoke_job()
    needle_groups = [
        ("graphics chip check",),
        ("jellyfin is set up",),
        ("changes jellyfin's admin",),
    ]
    for needles in needle_groups:
        step = _step_named(job, *needles)
        assert "::error::" in step["run"], f"step {step['name']!r} never emits ::error::"


def test_stack_smoke_dump_step_now_also_dumps_jellyfins_logs() -> None:
    step = _step_named(_stack_smoke_job(), "dump diagnostics")
    assert "docker logs jellyfin || true" in step["run"]


def test_stack_smoke_cleanup_removes_jellyfin_and_the_graphics_check_container() -> None:
    step = _step_named(_stack_smoke_job(), "clean up")
    run = step["run"]
    assert "jellyfin" in run
    assert "marrquee-graphics-check" in run


def test_stack_smoke_cleanup_removes_the_made_up_device_node_only_when_marked() -> None:
    """A device node this job mknod'd on the runner is its own to remove -
    one it found already there (a real NAS-hosted runner, say) is never
    touched, so the marker file gates the whole block.
    """
    step = _step_named(_stack_smoke_job(), "clean up")
    run = step["run"]

    assert 'if [ -f "$RUNNER_TEMP/marrquee-made-dri" ]; then' in run
    assert "sudo rm -f /dev/dri/renderD128 || true" in run
    assert "sudo rmdir /dev/dri 2>/dev/null || true" in run


# --- The graphics chip check: absent/present classification against the
# real Docker daemon, no leftover container, no created path, and the fake
# device node made (and marked) only when the runner doesn't already have
# one. -----------------------------------------------------------------------


def test_stack_smoke_graphics_chip_step_removes_leftover_plex_before_probing() -> None:
    """Plex was only ever added to the compose plan in memory, so its
    container has to go here before Jellyfin's own proof begins - the
    product's one-of rule means the two never really run together.
    """
    run = _step_named(_stack_smoke_job(), "graphics chip check")["run"]

    assert "docker rm -f plex > /dev/null 2>&1 || true" in run
    _assert_post_loop_verdict(
        run,
        'if [ -n "$plex_leftover" ]; then',
        "Plex's container was still present after docker rm -f plex",
    )

    rm_index = run.index("docker rm -f plex")
    probe_index = run.index('cat > "$RUNNER_TEMP/ci_graphics_probe.py"')
    assert rm_index < probe_index


def test_stack_smoke_graphics_chip_step_anchors_the_plex_filter_exactly() -> None:
    """`--filter name=plex` is a substring match - it would also match a
    hypothetical `plex-something` container. Anchored with `^...$`, the
    same shape the rest of this job already uses for an exact container
    name (`docker ps --filter "name=^${name}$" ...`).
    """
    run = _step_named(_stack_smoke_job(), "graphics chip check")["run"]

    assert "docker ps -aq --filter name=^plex$" in run
    assert "docker ps -aq --filter name=plex)" not in run


def test_stack_smoke_graphics_chip_step_probe_script_calls_the_real_engine() -> None:
    """Real, parseable Python, scoped to this one heredoc - `ast.parse`
    catches a mangled or hollowed-out probe script even though an
    identical-looking `probe_host_path` call could exist in some other
    step's own heredoc elsewhere in this file.
    """
    run = _step_named(_stack_smoke_job(), "graphics chip check")["run"]
    body = _heredoc_body(run, "ci_graphics_probe.py")

    assert "from marrquee.docker_client import SocketDockerEngine" in run
    assert 'await engine.probe_host_path(self_id, "/marrquee-ci-no-such-device")' in body
    assert 'await engine.probe_host_path(self_id, "/dev/null")' in body

    tree = ast.parse(body)
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "probe_host_path"
    ]
    assert len(calls) == 2, "the probe script must call probe_host_path exactly twice"


def test_stack_smoke_graphics_chip_step_pins_the_absent_and_present_verdicts_as_case_arms() -> None:
    """Each verdict is its own `case` arm - a `*)` catch-all swallowing the
    real label (or an arm hollowed to a no-op) shows up as a plain string
    diff here, not a substring match either shape would still satisfy.
    """
    run = _step_named(_stack_smoke_job(), "graphics chip check")["run"]

    absent_block = _text_between(run, 'case "$absent_result" in', "esac")
    absent_arms = _shell_case_arms(absent_block)
    assert absent_arms == {
        "absent": "",
        "*": (
            'echo "::error::The graphics chip check read ${absent_result} for '
            '/marrquee-ci-no-such-device"; exit 1'
        ),
    }

    present_block = _text_between(run, 'case "$present_result" in', "esac")
    present_arms = _shell_case_arms(present_block)
    assert present_arms == {
        "present": "",
        "*": (
            'echo "::error::The graphics chip check read ${present_result} for /dev/null"; exit 1'
        ),
    }


def test_stack_smoke_graphics_chip_step_leaves_no_container_and_creates_no_path() -> None:
    run = _step_named(_stack_smoke_job(), "graphics chip check")["run"]

    assert "docker ps -aq --filter name=marrquee-graphics-check" in run
    _assert_post_loop_verdict(
        run,
        'if [ -n "$leftover" ]; then',
        "The graphics chip check left a container behind",
    )
    _assert_post_loop_verdict(
        run,
        "if ! test ! -e /marrquee-ci-no-such-device; then",
        "The graphics chip check created the path it was only ever meant to probe",
    )


def test_stack_smoke_graphics_chip_step_mknods_the_device_only_when_missing_and_marks_it() -> None:
    run = _step_named(_stack_smoke_job(), "graphics chip check")["run"]

    if_block = _text_between(run, "if [ -e /dev/dri/renderD128 ]; then", "fi")
    assert "sudo mkdir -p /dev/dri" not in _text_between(
        run, "if [ -e /dev/dri/renderD128 ]; then", "else"
    )
    assert "sudo mknod -m 0666 /dev/dri/renderD128 c 226 128" in if_block
    assert 'touch "$RUNNER_TEMP/marrquee-made-dri"' in if_block


def test_stack_smoke_graphics_chip_step_restarts_and_waits_bounded_for_healthz() -> None:
    """`docker restart` (never a recreate) is the only way the in-memory
    `GraphicsChipCheck` cache - already settled on "absent" by every earlier
    Hub load in this job - gets asked again now that the node exists.
    """
    run = _step_named(_stack_smoke_job(), "graphics chip check")["run"]

    assert "docker restart marrquee-stack-smoke" in run
    assert "for _ in $(seq 1 30); do" in run
    assert "while true" not in run

    restart_index = run.index("docker restart marrquee-stack-smoke")
    wait_index = run.index("for _ in $(seq 1 30); do")
    assert restart_index < wait_index

    _assert_post_loop_verdict(
        run,
        'if [ "$healthz_ready" != "true" ]; then',
        "Marrquee never answered /healthz within 60s after restarting for the graphics chip",
    )


# --- Jellyfin is set up with the one login, its libraries and the graphics
# chip: the Hub's own install endpoint, real host networking and device
# pass-through, and Jellyfin's own API read back through the saved key. -----


def test_stack_smoke_jellyfin_setup_step_checks_the_chip_question_is_offered() -> None:
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]

    assert "http://127.0.0.1:7788/?panel=install" in run
    _assert_post_loop_verdict(
        run,
        'if ! echo "$install_html" | grep -qF \'data-question-step="jellyfin:graphics"\'; then',
        "The graphics question wasn't offered although /dev/dri/renderD128 exists",
    )


def test_stack_smoke_jellyfin_setup_step_installs_through_the_hub_endpoint_answering_yes() -> None:
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]

    assert "/api/hub/apps/jellyfin/install" in run
    assert '{"answers":{"graphics_chip":"yes"}}' in run
    assert (
        'if [ "$code" != "202" ]; then' in run
        and "POST /api/hub/apps/jellyfin/install returned ${code}, expected 202" in run
    )


def test_stack_smoke_jellyfin_setup_step_install_post_guards_a_dead_connection() -> None:
    """`curl -s` without `-f` exits non-zero on a connection refused, and
    under `set -euo pipefail` that would abort the step with no `::error::`
    at all - the assignment itself has to be guarded, not just the HTTP
    status read out of it afterwards.
    """
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]

    _assert_post_loop_verdict(
        run,
        "if ! response=$(curl -s -w '\\n%{http_code}' -X POST "
        "http://127.0.0.1:7788/api/hub/apps/jellyfin/install "
        '-H "Content-Type: application/json" '
        '-d \'{"answers":{"graphics_chip":"yes"}}\'); then',
        "couldn't reach Marrquee to install Jellyfin",
    )


def test_stack_smoke_jellyfin_setup_step_polls_bounded_and_pins_the_final_verdict() -> None:
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]

    assert "for _ in $(seq 1 120); do" in run
    assert 'select(.app_id == "jellyfin")' in run
    assert (
        'if [ "$state" = "up" ] && [ "$add_state" = "null" ] && [ -z "$note" ] '
        '&& [ "$busy" = "false" ]; then' in run
    )
    _assert_post_loop_verdict(
        run,
        'if [ "$state" != "up" ] || [ "$add_state" != "null" ] || [ -n "$note" ] '
        '|| [ "$busy" != "false" ]; then',
        "Jellyfin never finished adding within 10 minutes",
    )
    assert "::notice::Jellyfin's image pull and first-time setup took" in run


def test_stack_smoke_jellyfin_setup_step_pins_the_network_mode_and_device_verdicts() -> None:
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]

    assert "docker inspect -f '{{.HostConfig.NetworkMode}}' jellyfin" in run
    _assert_post_loop_verdict(
        run,
        "if ! network_mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' jellyfin) "
        '|| [ -z "$network_mode" ]; then',
        "couldn't inspect the jellyfin container's network mode",
    )
    _assert_post_loop_verdict(
        run,
        'if [ "$network_mode" != "host" ]; then',
        "Jellyfin is not on the host network",
    )

    assert "docker inspect -f '{{range .HostConfig.Devices}}{{.PathOnHost}}{{end}}' jellyfin" in run
    _assert_post_loop_verdict(
        run,
        'if [ "$device_path" != "/dev/dri" ]; then',
        "Jellyfin's graphics chip device was not passed through as /dev/dri",
    )
    _assert_post_loop_verdict(
        run,
        "if ! docker exec jellyfin test -c /dev/dri/renderD128; then",
        "/dev/dri/renderD128 is not a character device inside the jellyfin container",
    )


def test_stack_smoke_jellyfin_setup_step_checks_script_reaches_it_through_the_host_address() -> (
    None
):
    """Never a Docker-network name like `http://jellyfin:8096` - a
    Jellyfin-only install may never create that network at all, so this
    reaches it the same way Plex's own proof does, through its own gateway
    address.
    """
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]
    body = _heredoc_body(run, "ci_jellyfin_checks.py")

    assert "from marrquee.plex import plex_host_address" in run
    assert "from marrquee.jellyfin import (" in run
    assert "address = await plex_host_address(engine, self_id)" in body
    assert "base_url = jellyfin_base_url(address)" in body
    assert "http://jellyfin:8096" not in run


def test_stack_smoke_jellyfin_setup_step_checks_script_pins_every_decisive_guard() -> None:
    """Scoped to this one heredoc (`_heredoc_body`) - an identical-looking
    `is not True` or `!= "Marrquee"` comparison could exist in some other
    step's own script elsewhere in this file, and a mutation hollowing one
    of these guards must be caught even so.
    """
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]
    body = _heredoc_body(run, "ci_jellyfin_checks.py")
    tree = ast.parse(body)

    source_lines = body.splitlines()

    def _guard_line(text: str) -> str:
        matches = [line for line in source_lines if line.strip() == text]
        assert matches, f"guard {text!r} not found verbatim in the checks script"
        return matches[0]

    _guard_line('if public_info.payload.get("StartupWizardCompleted") is not True:')
    _guard_line('if public_info.payload.get("ServerName") != "Marrquee":')
    _guard_line('if ("movies", (movies_path,)) not in units:')
    _guard_line('if ("tvshows", (tv_path,)) not in units:')
    _guard_line('if accel not in ("vaapi", 5):')
    _guard_line('if encoding.payload.get("VaapiDevice") != GRAPHICS_DEVICE_NODE:')

    # Belt and suspenders: real Python, not just adjacent-looking lines.
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    assert len(raises) >= 6


def test_stack_smoke_jellyfin_setup_step_checks_libraries_use_the_shared_path_helper() -> None:
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]
    body = _heredoc_body(run, "ci_jellyfin_checks.py")

    assert "from marrquee.storage import container_media_path" in run
    assert 'movies_path = str(container_media_path("movies"))' in body
    assert 'tv_path = str(container_media_path("tv"))' in body
    assert "parse_virtual_folders" in run


def test_stack_smoke_jellyfin_setup_step_checks_the_link_target_and_file_permissions() -> None:
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]

    assert "http://127.0.0.1:8096/" in run
    _assert_post_loop_verdict(
        run,
        'if [ "$web_code" != "200" ]; then',
        "Jellyfin's own sign-in page did not answer 200",
    )

    assert "command -v stat" in run
    _assert_post_loop_verdict(
        run,
        'if [ "$jellyfin_json_perms" != "600 0" ]; then',
        "jellyfin.json was not 0600 owned by root",
    )


def test_stack_smoke_jellyfin_setup_step_key_check_runs_the_positive_control_first() -> None:
    """A positive control that could never find anything (the key not even
    in its own raw file) must fail loudly, before the real leak check below
    it ever gets a chance to pass by accident.
    """
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]
    body = _heredoc_body(run, "ci_jellyfin_key_check.py")

    assert "if key not in raw_jellyfin_json:" in body
    assert "the positive control failed" in body

    positive_control_index = body.index("the positive control failed")
    leak_check_index = body.index(
        'print("verdict=leak" if key in compose_text else "verdict=clean")'
    )
    assert positive_control_index < leak_check_index


def test_stack_smoke_jellyfin_setup_step_pins_the_key_check_case_arms() -> None:
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]

    case_block = _text_between(run, 'case "$verdict" in', "esac")
    arms = _shell_case_arms(case_block)
    assert arms == {
        "clean": "",
        "leak": 'echo "::error::the Jellyfin API key leaked into compose.yaml"; exit 1',
        "*": (
            'echo "::error::the Jellyfin key check produced an unexpected verdict '
            '(${verdict})"; exit 1'
        ),
    }


def test_stack_smoke_jellyfin_setup_step_key_check_never_echoes_the_key_itself() -> None:
    """Only a clean/leak verdict and a length ever leave the Python process -
    the key itself is read, compared and discarded entirely inside it.
    """
    run = _step_named(_stack_smoke_job(), "jellyfin is set up")["run"]
    body = _heredoc_body(run, "ci_jellyfin_key_check.py")

    # Pinned to the exact two prints this script may ever make - a third
    # print, or either one widened to interpolate `key` itself instead of a
    # verdict or a length, would fail this exact-set comparison.
    print_lines = [line.strip() for line in body.splitlines() if line.strip().startswith("print(")]
    assert print_lines == [
        'print("verdict=leak" if key in compose_text else "verdict=clean")',
        'print(f"key_length={len(key)}")',
    ]

    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo"):
            assert not re.search(r"\$\{?key\}?(?![_a-zA-Z])", stripped), (
                f"a bare $key reference appears in an echoed line: {line!r}"
            )


# --- Changing the one login changes Jellyfin's admin: a second throwaway
# password, the Hub's own change route, and a real sign-in proving the old
# login no longer works - all without ever restarting Jellyfin. ------------


def test_stack_smoke_jellyfin_login_step_generates_and_masks_a_second_password() -> None:
    run = _step_named(_stack_smoke_job(), "changes jellyfin's admin")["run"]

    assert "new_password=$(openssl rand -hex 16)" in run
    assert "::add-mask::$new_password" in run

    generate_index = run.index("openssl rand -hex 16")
    mask_index = run.index("::add-mask::$new_password")
    assert generate_index < mask_index


def test_stack_smoke_jellyfin_login_step_never_restarts_jellyfin_and_never_echoes_a_secret() -> (
    None
):
    step = _step_named(_stack_smoke_job(), "changes jellyfin's admin")
    run = step["run"]

    assert "docker restart jellyfin" not in run
    assert "docker rm -f jellyfin" not in run

    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo") and "::add-mask::" not in stripped:
            assert "$new_password" not in stripped
            assert "$MARRQUEE_CI_PASSWORD" not in stripped


def test_stack_smoke_jellyfin_login_step_posts_the_change_with_the_right_fields() -> None:
    run = _step_named(_stack_smoke_job(), "changes jellyfin's admin")["run"]

    assert "http://127.0.0.1:7788/hub/login/change" in run
    assert '--data-urlencode "current_password=${MARRQUEE_CI_PASSWORD}"' in run
    assert '--data-urlencode "username=marrquee-ci-two"' in run
    assert '--data-urlencode "password=${new_password}"' in run
    assert '--data-urlencode "password_again=${new_password}"' in run

    _assert_post_loop_verdict(
        run,
        'if [ "$change_code" != "303" ]; then',
        "POST /hub/login/change returned",
    )


def test_stack_smoke_jellyfin_login_step_change_post_guards_a_dead_connection() -> None:
    """Same reasoning as the install POST above: `curl -s` without `-f`
    exits non-zero on a connection refused, and under `set -euo pipefail`
    that would abort the step silently unless the assignment itself is
    guarded.
    """
    run = _step_named(_stack_smoke_job(), "changes jellyfin's admin")["run"]

    _assert_post_loop_verdict(
        run,
        "if ! change_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "
        "http://127.0.0.1:7788/hub/login/change "
        '--data-urlencode "current_password=${MARRQUEE_CI_PASSWORD}" '
        '--data-urlencode "username=marrquee-ci-two" '
        '--data-urlencode "password=${new_password}" '
        '--data-urlencode "password_again=${new_password}"); then',
        "couldn't reach Marrquee to change the login",
    )


def test_stack_smoke_jellyfin_login_step_polls_busy_and_banner_bounded() -> None:
    run = _step_named(_stack_smoke_job(), "changes jellyfin's admin")["run"]

    assert "for _ in $(seq 1 90); do" in run
    assert "while true" not in run
    assert 'if [ "$busy" = "false" ] && [ "$banner" = "none" ]; then' in run
    _assert_post_loop_verdict(
        run,
        'if [ "$busy" != "false" ] || [ "$banner" != "none" ]; then',
        "the login change never finished within 3 minutes",
    )


def test_stack_smoke_jellyfin_login_step_checks_new_login_works_and_old_login_fails() -> None:
    run = _step_named(_stack_smoke_job(), "changes jellyfin's admin")["run"]
    body = _heredoc_body(run, "ci_jellyfin_login_change.py")

    assert '"Username": "marrquee-ci-two", "Pw": new_password' in body
    assert '"Username": "marrquee-ci", "Pw": old_password' in body

    stripped_lines = [line.strip() for line in body.splitlines()]
    assert "if not new_auth.ok:" in stripped_lines
    assert "if old_auth.status != 401:" in stripped_lines

    tree = ast.parse(body)
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    assert len(raises) >= 2


def test_stack_smoke_jellyfin_login_step_passes_secrets_only_through_docker_exec_env() -> None:
    """The password only ever appears inside `docker exec -e`, the same
    shape qBittorrent's own key check already uses - never printed, and
    never passed as a bare argument a process listing could show.
    """
    run = _step_named(_stack_smoke_job(), "changes jellyfin's admin")["run"]

    assert (
        'docker exec -e JELLYFIN_NEW_PASSWORD="$new_password" '
        '-e JELLYFIN_OLD_PASSWORD="$MARRQUEE_CI_PASSWORD" marrquee-stack-smoke '
        "python3 /tmp/ci_jellyfin_login_change.py" in run
    )


# --- Chunk 6: connecting your own Plex is refused while Jellyfin holds the
# one media-server slot, and the folder-visibility probe leaves a real
# bind-mounted folder exactly as it found it. --------------------------------


def test_stack_smoke_existing_plex_step_names_avoid_forbidden_needles() -> None:
    """Same reasoning as every other story's own naming test: a name that
    collides with an earlier step's own name (or the generic "developer
    test" phrase) would silently resolve `_step_named` to that earlier
    step instead of this one's.
    """
    job = _stack_smoke_job()
    forbidden = (
        "developer test",
        "recyclarr",
        "sync turns amber",
        "stop radarr",
        "start radarr again",
        "cancel",
        "clean up",
        "dump diagnostics",
        "plex runs on the host network",
        "link code stays root-only",
        "throwaway",
        "graphics chip check",
        "jellyfin is set up",
        "changes jellyfin's admin",
    )
    for needles in [("connecting your own plex",), ("plex folder check",)]:
        name = str(_step_named(job, *needles)["name"]).lower()
        for phrase in forbidden:
            assert phrase not in name, f"step {name!r} contains the forbidden phrase {phrase!r}"


def test_stack_smoke_existing_plex_steps_run_after_jellyfin_login_and_before_dump() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    jf_login_index = names.index(_step_named(job, "changes jellyfin's admin")["name"])
    own_plex_index = names.index(_step_named(job, "connecting your own plex")["name"])
    folder_check_index = names.index(_step_named(job, "plex folder check")["name"])
    dump_index = names.index(_step_named(job, "dump diagnostics")["name"])

    assert jf_login_index < own_plex_index < folder_check_index < dump_index


def test_stack_smoke_every_new_existing_plex_step_emits_error_on_failure() -> None:
    job = _stack_smoke_job()
    for needles in [("connecting your own plex",), ("plex folder check",)]:
        step = _step_named(job, *needles)
        assert "::error::" in step["run"], f"step {step['name']!r} never emits ::error::"


_CURL_ASSIGNMENT_RE = re.compile(r"(\w+)=\$\(curl\b")


def _assert_every_curl_assignment_is_guarded(run: str) -> None:
    """Every `name=$(curl ...)` line is its own `if ! ...; then` guard - a
    bare assignment under `set -euo pipefail` still aborts the step on a
    real curl failure, but silently, with no `::error::` a reader (or
    GitHub's own public annotations API) could ever see.
    """
    for line in run.splitlines():
        stripped = line.strip()
        match = _CURL_ASSIGNMENT_RE.search(stripped)
        if match is None:
            continue
        assert stripped.startswith("if ! "), (
            f"a curl call assigned to {match.group(1)!r} is not wrapped in its own "
            f"`if ! ...` guard: {stripped!r}"
        )


def test_stack_smoke_existing_plex_step_guards_every_curl_assignment() -> None:
    run = _step_named(_stack_smoke_job(), "connecting your own plex")["run"]
    _assert_every_curl_assignment_is_guarded(run)
    assert "set -euo pipefail" in run
    assert "while true" not in run


def test_stack_smoke_plex_folder_check_step_guards_its_own_substitutions() -> None:
    run = _step_named(_stack_smoke_job(), "plex folder check")["run"]
    assert "set -euo pipefail" in run
    assert "while true" not in run
    assert 'if ! before=$(ls -A "$root/data/media/movies"); then' in run
    assert 'if ! after=$(ls -A "$root/data/media/movies"); then' in run
    assert (
        "if ! marker_output=$(docker exec marrquee-stack-smoke python3 "
        '/tmp/ci_plex_folder_marker.py) || [ -z "$marker_output" ]; then' in run
    )
    assert "command -v find > /dev/null" in run
    find_index = run.index("command -v find")
    leftover_index = run.index('leftover=$(find "$root/data/media/movies"')
    assert find_index < leftover_index


def test_stack_smoke_existing_plex_steps_never_echo_a_token() -> None:
    for step_needle in ("connecting your own plex", "plex folder check"):
        run = _step_named(_stack_smoke_job(), step_needle)["run"]
        for line in run.splitlines():
            stripped = line.strip()
            if stripped.startswith("echo"):
                assert "token" not in stripped.lower(), f"a token-shaped word was echoed: {line!r}"


def test_stack_smoke_existing_plex_pinned_lines_are_unique_anchors() -> None:
    """Every line pinned below names `existing-plex` or `plex-check`
    outright - Plex, Jellyfin and qBittorrent's own steps share plenty of
    identical-looking lines (`command -v grep`, a `case` arm's exact
    wording), so a pin that didn't name one of these two on its own line
    could silently match a copy sitting in a completely different step.
    """
    own_plex_run = _step_named(_stack_smoke_job(), "connecting your own plex")["run"]
    folder_check_run = _step_named(_stack_smoke_job(), "plex folder check")["run"]

    pinned = [
        (own_plex_run, "grep -F -q 'existing-plex' \"$compose_file\""),
        (
            own_plex_run,
            'echo "::error::existing-plex leaked into the real compose.yaml"; exit 1 ;;',
        ),
        (
            own_plex_run,
            'docker cp "$RUNNER_TEMP/ci_existing_plex_refused.py" '
            "marrquee-stack-smoke:/tmp/ci_existing_plex_refused.py",
        ),
        (
            own_plex_run,
            "if docker exec marrquee-stack-smoke test -e /config/existing_plex.json; then",
        ),
        (folder_check_run, "find \"$root/data/media/movies\" -name 'marrquee-plex-check-*'"),
    ]
    anchor_re = re.compile(r"existing[_-]plex|plex[_-]check")
    for run, line in pinned:
        assert anchor_re.search(line) is not None, line
        assert line in run, f"expected pinned line missing from its own step: {line!r}"


def test_stack_smoke_existing_plex_install_row_and_json_endpoint_refusals_are_pinned() -> None:
    run = _step_named(_stack_smoke_job(), "connecting your own plex")["run"]

    assert 'data-install-row="existing-plex".*?</li>' in run
    assert "PLEX_EXCLUDES_JELLYFIN, hub_install_unavailable" in run
    assert (
        "if ! json_response=$(curl -s -w '\\n%{http_code}' -X POST "
        "http://127.0.0.1:7788/api/hub/apps/existing-plex/install "
        "-H 'Content-Type: application/json' "
        '-d \'{"answers":{}}\') || [ -z "$json_response" ]; then' in run
    )
    _assert_post_loop_verdict(
        run,
        'if [ "$json_code" != "409" ]; then',
        "POST /api/hub/apps/existing-plex/install returned",
    )


def test_stack_smoke_existing_plex_sign_in_and_connect_posts_are_pinned() -> None:
    run = _step_named(_stack_smoke_job(), "connecting your own plex")["run"]

    assert (
        "if ! sign_in_response=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "
        '-X POST http://127.0.0.1:7788/plex/sign-in --data-urlencode "then=connect") '
        '|| [ -z "$sign_in_response" ]; then' in run
    )
    _assert_post_loop_verdict(
        run,
        'if [ "$sign_in_response" != "303 http://127.0.0.1:7788/" ]; then',
        "POST /plex/sign-in (then=connect) returned",
    )
    assert (
        "if ! connect_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "
        'http://127.0.0.1:7788/plex/connect --data-urlencode "machine_id=ci-none") '
        '|| [ -z "$connect_code" ]; then' in run
    )
    _assert_post_loop_verdict(
        run, 'if [ "$connect_code" != "303" ]; then', "POST /plex/connect returned"
    )


def test_stack_smoke_existing_plex_record_check_has_its_positive_control_first() -> None:
    run = _step_named(_stack_smoke_job(), "connecting your own plex")["run"]

    positive_index = run.index("docker exec marrquee-stack-smoke test -e /config/install.json")
    negative_index = run.index(
        "docker exec marrquee-stack-smoke test -e /config/existing_plex.json"
    )
    assert positive_index < negative_index
    assert "the positive control failed" in run


def test_stack_smoke_existing_plex_compose_check_runs_grep_then_positive_control_then_case() -> (
    None
):
    run = _step_named(_stack_smoke_job(), "connecting your own plex")["run"]

    grep_tool_index = run.index("command -v grep > /dev/null")
    positive_control_index = run.index("grep -F -q 'jellyfin:' \"$compose_file\"")
    case_index = run.index('case "$existing_plex_leaked" in')
    assert grep_tool_index < positive_control_index < case_index

    case_block = _text_between(run, 'case "$existing_plex_leaked" in', "esac")
    arms = _shell_case_arms(case_block)
    assert arms == {
        "1": "",
        "0": 'echo "::error::existing-plex leaked into the real compose.yaml"; exit 1',
        "*": (
            "echo \"::error::couldn't check compose.yaml for a leaked existing-plex service "
            '(grep exit ${existing_plex_leaked})"; exit 1'
        ),
    }


def test_stack_smoke_existing_plex_deploy_app_list_check_is_pinned() -> None:
    run = _step_named(_stack_smoke_job(), "connecting your own plex")["run"]

    assert (
        'if ! deploy_body=$(curl -fsS http://127.0.0.1:7788/api/deploy) || [ -z "$deploy_body" ]; '
        "then" in run
    )
    assert "jq -e 'all(.apps[]; .app_id != \"existing-plex\")'" in run


def test_stack_smoke_plex_folder_check_marker_verdicts_are_pinned() -> None:
    run = _step_named(_stack_smoke_job(), "plex folder check")["run"]
    body = _heredoc_body(run, "ci_plex_folder_marker.py")

    assert 'print(f"created={created}")' in body
    assert 'print(f"removed={removed}")' in body
    assert 'print(f"sentinel_survived={sentinel_survived}")' in body

    assert 'echo "$marker_output" | grep -qF "created=True"' in run
    assert 'echo "$marker_output" | grep -qF "removed=True"' in run
    assert 'echo "$marker_output" | grep -qF "sentinel_survived=True"' in run
    assert 'if [ "$after" != "$before" ]; then' in run


def test_stack_smoke_plex_folder_check_verdicts_are_computed_not_hardcoded() -> None:
    """Each of `created`/`removed`/`sentinel_survived` must come from a real
    filesystem check, never a bare `True` a mutation could plant in its
    place and still print a line the grep above happily matches - pinned
    both as an exact source line (the honest expression) and structurally
    (an `ast.parse` of the scoped heredoc body, so a literal boolean swapped
    in for any of the three assignments is caught even if its own line
    still LOOKED like an assignment).
    """
    run = _step_named(_stack_smoke_job(), "plex folder check")["run"]
    body = _heredoc_body(run, "ci_plex_folder_marker.py")

    assert "created = (movies / marker_name).is_dir()" in body
    assert "removed = not (movies / marker_name).exists()" in body
    assert "sentinel_survived = sentinel.exists()" in body

    tree = ast.parse(body)
    literal_bool_assignments = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in ("created", "removed", "sentinel_survived")
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, bool)
    }
    assert literal_bool_assignments == set(), (
        f"these verdicts are hardcoded booleans instead of real checks: {literal_bool_assignments}"
    )


def test_stack_smoke_dump_diagnostics_step_gained_no_existing_plex_container_log() -> None:
    """existing-plex has no container of its own - the failure dump's own
    list of `docker logs` targets, and the cleanup step's own container
    list, must both stay exactly what they were.
    """
    job = _stack_smoke_job()
    dump_run = _step_named(job, "dump diagnostics")["run"]
    cleanup_run = _step_named(job, "clean up every container")["run"]

    assert "existing-plex" not in dump_run
    assert "docker logs plex" in dump_run
    assert "docker logs jellyfin" in dump_run
    assert "existing-plex" not in cleanup_run


# --- Seerr: real Docker proves it set up, signed in, and a request reaching
# Radarr - the cycle's finale. Two steps only, "seerr is set up" and "request
# in seerr" - the Plex-reachability step the plan first sketched was dropped:
# CI's Plex is removed long before these run (`docker rm -f plex` above), and
# Plex and Jellyfin never run at once, so a Plex-backed Seerr stays PENDING
# the owner's NAS. ------------------------------------------------------------

_SEERR_STEP_NEEDLES = [("seerr is set up",), ("request in seerr",)]


def test_stack_smoke_seerr_step_names_avoid_forbidden_needles() -> None:
    """Same reasoning as every other story's own naming test: a name that
    collides with an earlier step's own name (or a generic phrase like
    "developer test") would silently resolve `_step_named` to that earlier
    step instead of this one's.
    """
    job = _stack_smoke_job()
    forbidden = (
        "developer test",
        "recyclarr",
        "sync turns amber",
        "stop radarr",
        "start radarr again",
        "cancel",
        "clean up",
        "dump diagnostics",
        "plex runs on the host network",
        "link code stays root-only",
        "throwaway",
        "graphics chip check",
        "jellyfin is set up",
        "changes jellyfin's admin",
        "connecting your own plex",
        "plex folder check",
    )
    for needles in _SEERR_STEP_NEEDLES:
        name = str(_step_named(job, *needles)["name"]).lower()
        for phrase in forbidden:
            assert phrase not in name, f"step {name!r} contains the forbidden phrase {phrase!r}"


def test_stack_smoke_seerr_steps_run_after_the_plex_folder_check_and_before_dump() -> None:
    job = _stack_smoke_job()
    names = [str(step.get("name", "")) for step in _steps(job)]

    folder_check_index = names.index(_step_named(job, "plex folder check")["name"])
    seerr_setup_index = names.index(_step_named(job, "seerr is set up")["name"])
    seerr_request_index = names.index(_step_named(job, "request in seerr")["name"])
    dump_index = names.index(_step_named(job, "dump diagnostics")["name"])

    assert folder_check_index < seerr_setup_index < seerr_request_index < dump_index


def test_stack_smoke_every_new_seerr_step_emits_error_on_failure() -> None:
    job = _stack_smoke_job()
    for needles in _SEERR_STEP_NEEDLES:
        step = _step_named(job, *needles)
        run = step["run"]
        assert "::error::" in run, f"step {step['name']!r} never emits ::error::"
        assert "set -euo pipefail" in run
        assert "while true" not in run


def test_stack_smoke_seerr_steps_guard_every_curl_assignment() -> None:
    """The strict `if ! ...` guard applies to every one-shot curl call in
    these two steps. The poll loop's own `status=$(curl ...) || status=""`
    is the same retry idiom every other polling step in this job already
    uses - it is not silent, because the loop's own post-loop verdict emits
    its own `::error::` when the state never settles - so, exactly as every
    other step's own guard test already does, it is excluded here rather
    than asserted against.
    """
    setup_run = _step_named(_stack_smoke_job(), "seerr is set up")["run"]
    request_run = _step_named(_stack_smoke_job(), "request in seerr")["run"]

    poll_line = 'status=$(curl -fsS http://127.0.0.1:7788/api/hub/status) || status=""'
    assert poll_line in setup_run
    _assert_every_curl_assignment_is_guarded(setup_run.replace(poll_line, ""))
    _assert_every_curl_assignment_is_guarded(request_run)


def test_stack_smoke_seerr_setup_step_guards_its_own_substitutions() -> None:
    """Every non-curl substitution this step relies on (the stat, the three
    docker inspect reads, the docker-exec'd setup script) is its own `if !
    ...` guard too, the same standard the curl calls already get.
    """
    run = _step_named(_stack_smoke_job(), "seerr is set up")["run"]

    assert 'if ! seerr_uid=$(stat -c \'%u\' "$seerr_dir") || [ -z "$seerr_uid" ]; then' in run
    assert (
        "if ! seerr_init=$(docker inspect -f '{{.HostConfig.Init}}' seerr) "
        '|| [ -z "$seerr_init" ]; then' in run
    )
    assert (
        "if ! seerr_port=$(docker inspect -f "
        "'{{index .HostConfig.PortBindings \"5055/tcp\"}}' seerr) "
        '|| [ -z "$seerr_port" ]; then' in run
    )
    assert (
        "if ! seerr_gateway=$(docker inspect -f "
        "'{{(index .NetworkSettings.Networks \"marrquee\").Gateway}}' seerr) "
        '|| [ -z "$seerr_gateway" ]; then' in run
    )
    assert (
        'if ! setup_output=$(docker exec -e SEERR_GATEWAY="$seerr_gateway" '
        "marrquee-stack-smoke python3 /tmp/ci_seerr_setup.py 2>&1) "
        '|| [ -z "$setup_output" ]; then' in run
    )


def test_stack_smoke_seerr_setup_step_pins_the_post_loop_and_uid_verdicts() -> None:
    run = _step_named(_stack_smoke_job(), "seerr is set up")["run"]

    _assert_post_loop_verdict(
        run,
        'if [ "$state" != "up" ] || [ "$add_state" != "null" ] || [ -n "$note" ] '
        '|| [ "$busy" != "false" ]; then',
        "Seerr never finished adding within 10 minutes",
    )
    _assert_post_loop_verdict(run, 'if [ "$seerr_uid" != "1000" ]; then', "not owned by uid 1000")
    assert 'if [ ! -e "$seerr_dir/settings.json" ]; then' in run
    assert "::error::Seerr couldn't write its settings folder" in run
    _assert_post_loop_verdict(
        run, 'if [ "$seerr_init" != "true" ]; then', "seerr's container did not run with an init"
    )


def test_stack_smoke_seerr_setup_step_compose_check_runs_grep_then_positive_control_then_case() -> (
    None
):
    run = _step_named(_stack_smoke_job(), "seerr is set up")["run"]

    grep_tool_index = run.index("command -v grep")
    positive_control_index = run.index("grep -F -q 'seerr:'")
    case_index = run.index('case "$seerr_init_rendered" in')
    assert grep_tool_index < positive_control_index < case_index

    case_block = _text_between(run, 'case "$seerr_init_rendered" in', "esac")
    arms = _shell_case_arms(case_block)
    assert arms["0"] == ""
    assert "seerr" in arms["1"].lower() and "init: true" in arms["1"]
    assert "seerr" in arms["*"].lower() and "grep exit" in arms["*"]


def test_stack_smoke_seerr_setup_heredoc_reads_the_login_and_key_inside_the_container() -> None:
    """The one login's password changed in an earlier step and was never
    exported anywhere bash could read it back - `load_login` inside the
    container is the only correct source, so the heredoc must use it
    instead of any leftover `$MARRQUEE_CI_PASSWORD`-shaped shortcut.
    """
    run = _step_named(_stack_smoke_job(), "seerr is set up")["run"]
    body = _heredoc_body(run, "ci_seerr_setup.py")

    assert "load_login(" in body
    assert "settings/jellyfin" in body
    assert "settings/jellyfin/library" not in body
    assert '"username": "marrquee-ci' not in body
    assert "MARRQUEE_CI_PASSWORD" not in run


def test_stack_smoke_seerr_request_heredoc_pins_the_movie_ids_and_status() -> None:
    run = _step_named(_stack_smoke_job(), "request in seerr")["run"]
    body = _heredoc_body(run, "ci_seerr_request.py")

    assert '"mediaId": 603' in body
    assert '"mediaId": 13' in body
    assert "status" in body
    assert "secrets.token_hex(16)" in body


def test_stack_smoke_seerr_request_never_puts_radarrs_key_in_a_url() -> None:
    """Every other key in this job travels in a header, never a query
    string - a query string is echoed back in access logs and shell
    history in a way a header never is. Scoped to this one heredoc so a
    legitimate `"apikey"`-shaped word anywhere else in the file (there is
    none, but a future step could add one) can never satisfy this pin.
    """
    body = _heredoc_body(
        _step_named(_stack_smoke_job(), "request in seerr")["run"], "ci_seerr_request.py"
    )

    assert "apikey" not in body.lower()
    assert '{"X-Api-Key": radarr_key}' in body
    assert "headers=radarr_headers" in body


def test_stack_smoke_seerr_request_verdict_resolves_the_real_radarr_profile_name() -> None:
    """`radarr_got_it` must mean "Radarr actually filed this under Recyclarr's
    own UHD Bluray + WEB profile", not merely "Radarr has some profile id
    for it" - the movie's own `qualityProfileId` is meaningless without the
    quality-profile list to resolve it against a name.
    """
    body = _heredoc_body(
        _step_named(_stack_smoke_job(), "request in seerr")["run"], "ci_seerr_request.py"
    )

    assert "api/v3/qualityprofile" in body
    assert '"UHD Bluray + WEB"' in body
    assert 'profile_names.get(profile_id) == "UHD Bluray + WEB"' in body


def test_stack_smoke_seerr_setup_heredoc_pins_the_real_verdict_conditions() -> None:
    """The two checks that decide whether Seerr's first-run setup actually
    finished right are pinned as their own exact source lines - a mutation
    that hollows either one out (to `if False:`, say) changes this literal
    text, even though the check's own printed verdict below is computed
    fresh and would otherwise look unaffected.
    """
    body = _heredoc_body(
        _step_named(_stack_smoke_job(), "seerr is set up")["run"], "ci_seerr_setup.py"
    )

    assert 'if public.payload.get("initialized") is not True:' in body
    assert 'if main_settings.payload.get("defaultPermissions") != 32:' in body


def test_stack_smoke_seerr_request_heredoc_pins_the_real_verdict_conditions() -> None:
    body = _heredoc_body(
        _step_named(_stack_smoke_job(), "request in seerr")["run"], "ci_seerr_request.py"
    )

    assert (
        "if not isinstance(owner_request.payload, dict) "
        'or owner_request.payload.get("status") != 2:' in body
    )
    assert 'friend_waits = friend_payload.get("status") == 1' in body


_SEERR_VERDICT_BOOLEAN_NAMES = frozenset(
    {
        "initialized_ok",
        "local_login_off",
        "default_permissions_ok",
        "sonarr_ok",
        "radarr_ok",
        "auth_ok",
        "radarr_got_it",
        "friend_waits",
    }
)


def _literal_true_assignments(body: str, names: frozenset[str]) -> set[str]:
    tree = ast.parse(body)
    return {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in names
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    }


def test_stack_smoke_seerr_heredocs_never_hardcode_a_verdict_boolean() -> None:
    """Every printed verdict boolean in both heredocs - `friend_waits`,
    `radarr_got_it` and the setup script's own `*_ok` values - must be a
    real derived expression, the same standard the plex folder marker's
    own `created`/`removed`/`sentinel_survived` verdicts already get. A
    literal `False` is a legitimate starting point before a real check runs
    (several of these do exactly that), but a bare literal `True` is never
    legitimate anywhere in this pair of scripts: every one of these names
    is only ever set from a real comparison.
    """
    setup_body = _heredoc_body(
        _step_named(_stack_smoke_job(), "seerr is set up")["run"], "ci_seerr_setup.py"
    )
    request_body = _heredoc_body(
        _step_named(_stack_smoke_job(), "request in seerr")["run"], "ci_seerr_request.py"
    )

    hardcoded = _literal_true_assignments(
        setup_body, _SEERR_VERDICT_BOOLEAN_NAMES
    ) | _literal_true_assignments(request_body, _SEERR_VERDICT_BOOLEAN_NAMES)
    assert hardcoded == set(), (
        f"these verdicts are hardcoded to True instead of derived: {hardcoded}"
    )


def test_stack_smoke_seerr_request_step_pins_the_radarr_and_friend_verdicts() -> None:
    run = _step_named(_stack_smoke_job(), "request in seerr")["run"]

    assert (
        "if ! request_output=$(docker exec marrquee-stack-smoke "
        "python3 /tmp/ci_seerr_request.py 2>&1) "
        '|| [ -z "$request_output" ]; then' in run
    )
    assert 'grep -qF "radarr_got_it=True"' in run
    assert (
        "::error::The owner's request didn't reach Radarr "
        "(Seerr needs TMDB - check the runner's internet)" in run
    )
    assert 'grep -qF "friend_waits=True"' in run
    assert "::error::A shared person's request didn't wait for approval" in run


def test_stack_smoke_seerr_pinned_lines_are_unique_anchors() -> None:
    setup_run = _step_named(_stack_smoke_job(), "seerr is set up")["run"]
    request_run = _step_named(_stack_smoke_job(), "request in seerr")["run"]

    pinned = [
        (
            setup_run,
            'docker cp "$RUNNER_TEMP/ci_seerr_setup.py" '
            "marrquee-stack-smoke:/tmp/ci_seerr_setup.py",
        ),
        (
            request_run,
            'docker cp "$RUNNER_TEMP/ci_seerr_request.py" '
            "marrquee-stack-smoke:/tmp/ci_seerr_request.py",
        ),
    ]
    for run, line in pinned:
        assert "seerr" in line.lower()
        assert line in run


def test_stack_smoke_seerr_steps_never_echo_a_secret() -> None:
    for needles in _SEERR_STEP_NEEDLES:
        run = _step_named(_stack_smoke_job(), *needles)["run"]
        for line in run.splitlines():
            stripped = line.strip()
            if stripped.startswith("echo"):
                lowered = stripped.lower()
                assert "password" not in lowered
                assert "token" not in lowered
                assert "api_key" not in lowered


def test_stack_smoke_dump_diagnostics_step_gained_seerr_logs() -> None:
    job = _stack_smoke_job()
    dump_run = _step_named(job, "dump diagnostics")["run"]
    cleanup_run = _step_named(job, "clean up every container")["run"]

    assert "docker logs seerr" in dump_run
    assert re.search(r"docker rm -f[^\n]*\bseerr\b", cleanup_run)


# ---------------------------------------------------------------------------
# Shell semantics, not just text: GitHub runs a step with no `shell:` as
# `bash -e {0}`, so a command that is EXPECTED to fail (a blocked tunnel, a
# keyed control server answering 401) aborts the step before a following
# `x_exit=$?` line can ever capture it. The first real CI run of the VPN
# checks died exactly that way, silently. These tests run the two
# kill-switch steps under that same shell with a stub `docker` on PATH.
# ---------------------------------------------------------------------------

_STUB_DOCKER = r"""#!/bin/sh
# A stand-in `docker` for the kill-switch steps. KS_LEAK=1 lets traffic
# out; KS_TOOL picks which probe tool the qBittorrent image "ships".
args="$*"
case "$args" in
  *"command -v curl"*) [ "$KS_TOOL" = curl ]; exit $? ;;
  *"command -v wget"*) [ "$KS_TOOL" = wget ]; exit $? ;;
esac
case "$args" in
  *"curl"*"127.0.0.1:8000"*) printf 401; exit 0 ;;
  *"wget"*"127.0.0.1:8000"*) echo "  HTTP/1.1 401 Unauthorized" >&2; exit 1 ;;
  *"container:gluetun"*"1.1.1.1"*|*"exec qbittorrent"*"1.1.1.1"*)
    if [ "$KS_LEAK" = 1 ]; then exit 0; fi
    case "$args" in
      *curl*) exit 28 ;;
      *) echo "wget: can't connect to remote host (1.1.1.1): Connection refused" >&2; exit 1 ;;
    esac ;;
  *"1.1.1.1"*) exit 0 ;;
esac
echo "unexpected docker call: $args" >&2
exit 99
"""


def _run_step_like_github(
    run: str, tmp_path: Path, *, leak: bool, tool: str = "wget"
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(_STUB_DOCKER)
    docker.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(run)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "KS_LEAK": "1" if leak else "0",
        "KS_TOOL": tool,
    }
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_gluetun_kill_switch_step_passes_under_bash_e_when_the_tunnel_blocks(
    tmp_path: Path,
) -> None:
    run = _step_named(_stack_smoke_job(), "nothing gets out without the tunnel")["run"]
    result = _run_step_like_github(run, tmp_path, leak=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_gluetun_kill_switch_step_fails_loudly_under_bash_e_when_traffic_leaks(
    tmp_path: Path,
) -> None:
    run = _step_named(_stack_smoke_job(), "nothing gets out without the tunnel")["run"]
    result = _run_step_like_github(run, tmp_path, leak=True)
    assert result.returncode != 0
    assert "::error::Traffic left through the kill switch" in result.stdout


def test_qbittorrent_kill_switch_step_passes_under_bash_e_when_the_tunnel_blocks(
    tmp_path: Path,
) -> None:
    run = _step_named(_stack_smoke_job(), "the downloader can't reach the internet")["run"]
    for tool in ("curl", "wget"):
        result = _run_step_like_github(run, tmp_path, leak=False, tool=tool)
        assert result.returncode == 0, f"{tool}: {result.stdout}{result.stderr}"
        assert "kill switch held" in result.stdout


def test_qbittorrent_kill_switch_step_fails_loudly_under_bash_e_when_traffic_leaks(
    tmp_path: Path,
) -> None:
    run = _step_named(_stack_smoke_job(), "the downloader can't reach the internet")["run"]
    for tool in ("curl", "wget"):
        result = _run_step_like_github(run, tmp_path, leak=True, tool=tool)
        assert result.returncode != 0, tool
        assert "::error::qBittorrent reached the internet outside the VPN" in result.stdout


def test_no_step_captures_an_exit_code_that_bash_e_would_never_reach() -> None:
    """A bare `x=$?` line only runs if the command before it succeeded, under
    GitHub's default `bash -e` - so it must sit inside a `set +e` region.
    The errexit-safe form is `cmd || x=$?` on the command's own line."""
    offenders = []
    for job_name, job in _jobs().items():
        for step in _steps(job):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            errexit_off = False
            for line in run.splitlines():
                stripped = line.strip()
                if stripped == "set +e":
                    errexit_off = True
                elif stripped.startswith("set -e"):
                    errexit_off = False
                elif re.fullmatch(r"\w+=\$\?", stripped) and not errexit_off:
                    offenders.append(f"{job_name} / {step.get('name')}: {stripped}")
    assert offenders == []


def test_qbittorrent_login_check_judges_the_status_code_not_the_old_body() -> None:
    """qBittorrent 5.x answers `auth/login` with 204 No Content on success
    and 401 on a wrong password - the old "Ok."/"Fails." bodies are
    gone. The first real CI run failed a login that had worked because the
    check compared the body to "Ok."."""
    run = _step_named(_stack_smoke_job(), "qbittorrent takes the one login")["run"]

    assert '"Ok."' not in run
    assert "good.status_code != 204" in run
    assert "bad.status_code != 401" in run
    # The wrong password must not ride the right one's session cookie.
    assert run.count("async with httpx.AsyncClient(") == 2
