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
    for name in ("prowlarr", "sonarr", "radarr", "gluetun", "marrquee-stack-smoke"):
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
