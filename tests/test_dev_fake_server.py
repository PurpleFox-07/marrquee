"""Tests for the Mac preview: a Docker-less demo server for the Deploy screen.

Nothing here waits a real second (every scene is driven end to end with an
injected clock and sleep, the same shape `tests/test_deploy.py` and
`tests/test_wiring_engine.py` already use) and nothing here starts a real
uvicorn server - `build_app` alone is enough to drive `app.state.deploy`
directly, the same way `tests/test_deploy_page.py` drives it through a
saved snapshot instead of a stub.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from marrquee.deploy import DeploySnapshot, read_last_failure
from tools.dev_fake_server import SCENES, build_app

_REPO_ROOT = Path(__file__).resolve().parents[1]
_README_PATH = _REPO_ROOT / "README.md"
_DOCKERIGNORE_PATH = _REPO_ROOT / ".dockerignore"


class _FakeClock:
    """A clock and a sleep function that agree with each other and never
    actually wait - mirrors `tests/test_deploy.py`'s own fixture, so every
    scene's waits (a poll interval, a reassurance threshold, a wiring
    pause) cost nothing real.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)  # let the background deploy task take its turn


async def _play_scene(
    scene: str, root: Path, *, budget: int = 200_000
) -> tuple[list[DeploySnapshot], Path]:
    """Build the demo app, start its one deploy, and poll to a terminal phase.

    Returns every distinct snapshot seen (so a test can check something
    that was only ever true mid-run, like a reassurance note) and the
    config folder the demo used, for reading back `last-failure.txt`.
    """
    clock = _FakeClock()
    app = build_app(scene=scene, root=root, clock=clock.time, sleep=clock.sleep)
    manager = app.state.deploy
    manager.start()

    seen: list[DeploySnapshot] = []
    for _ in range(budget):
        current = manager.snapshot()
        if not seen or current != seen[-1]:
            seen.append(current)
        if current.phase in ("finale", "error"):
            # The task's own final `await` still has to be resumed and
            # unwound before it's truly done - a few more turns settles it.
            for _ in range(5):
                await asyncio.sleep(0)
            return seen, root / "config"
        await asyncio.sleep(0)
    raise AssertionError(f"scene {scene!r} never reached a terminal phase")


@pytest.mark.parametrize("scene", SCENES)
async def test_each_scene_reaches_its_ending_with_an_injected_clock(
    scene: str, tmp_path: Path
) -> None:
    history, _config_dir = await _play_scene(scene, tmp_path / scene)
    final = history[-1]
    if scene == "failure":
        assert final.phase == "error"
    else:
        assert final.phase == "finale"


async def test_the_happy_scene_shows_the_reassurance_note_for_exactly_one_app(
    tmp_path: Path,
) -> None:
    history, _config_dir = await _play_scene("happy", tmp_path)
    noted_app_ids = {
        app.app_id for snapshot in history for app in snapshot.apps if app.note is not None
    }
    assert noted_app_ids == {"radarr"}


async def test_the_wiring_problem_scene_leaves_text_for_last_problem_to_show(
    tmp_path: Path,
) -> None:
    _history, config_dir = await _play_scene("wiring-problem", tmp_path)
    problem = read_last_failure(config_dir)
    assert problem is not None
    assert problem.strip() != ""


async def test_the_dev_server_writes_nothing_outside_its_temporary_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    siblings_before = set(tmp_path.iterdir())

    _history, config_dir = await _play_scene("happy", root)

    assert (config_dir / "install.json").exists()  # the demo really did write inside `root`
    assert set(tmp_path.iterdir()) == siblings_before | {root}


def test_tools_is_excluded_by_dockerignore_so_the_demo_cannot_ship() -> None:
    lines = {line.strip() for line in _DOCKERIGNORE_PATH.read_text().splitlines()}
    assert "tools" in lines


def test_the_readme_user_path_contains_no_curl_or_docker_command() -> None:
    readme = _README_PATH.read_text()
    marker = "## Developing Marrquee"
    assert marker in readme
    user_path, _developer_section = readme.split(marker, 1)
    assert "curl " not in user_path
    assert "docker logs" not in user_path
