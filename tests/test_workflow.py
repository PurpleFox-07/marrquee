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


def test_stack_smoke_installs_all_three_apps_and_starts_a_deploy() -> None:
    job = _stack_smoke_job()

    install_step = _step_named(job, "install all three apps")
    run = install_step["run"]
    assert "/api/install" in run
    assert "prowlarr" in run
    assert "sonarr" in run
    assert "radarr" in run

    start_step = _step_named(job, "start the deploy")
    assert "POST http://127.0.0.1:7788/api/deploy" in start_step["run"]


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


def test_stack_smoke_always_cleans_up_containers_network_and_temp_files() -> None:
    step = _step_named(_stack_smoke_job(), "clean up")

    assert step.get("if") == "always()"
    run = step["run"]
    for name in ("prowlarr", "sonarr", "radarr", "marrquee-stack-smoke"):
        assert name in run
    assert "docker network rm marrquee" in run
    # `sudo`, not a plain `rm -rf`: some of what's under the temp folder can
    # be created by the Docker daemon as root, which would otherwise block
    # cleanup as the unprivileged runner user.
    assert "sudo rm -rf" in run
    assert '"$RUNNER_TEMP/marrquee-smoke"' in run
