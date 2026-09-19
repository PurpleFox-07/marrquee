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
