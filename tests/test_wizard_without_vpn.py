"""Tests for the wizard's own break-glass screen - "Can't use a VPN right
now?" from Gluetun's own VPN question, walked as three real page loads that
share `partials/without_vpn_confirm.html` with the Hub's own panel.
"""

from __future__ import annotations

import dataclasses
import html
from pathlib import Path

from fastapi.testclient import TestClient

from marrquee import words
from marrquee.config import Settings
from marrquee.deploy import AppProgress, DeploySnapshot
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.state import write_json_atomic
from marrquee.without_vpn import without_vpn_confirmed


def _settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", host_mount=tmp_path / "host")


def _client(settings: Settings) -> TestClient:
    status = DockerStatus(connected=True, version="27.3.1")
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


def _write_finale_deploy(settings: Settings) -> None:
    snapshot = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(
            AppProgress(
                app_id="prowlarr",
                name="Prowlarr",
                state="done",
                chip="Ready",
                line="Prowlarr is ready",
                note=None,
                port=9696,
            ),
        ),
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(snapshot))


_APPS = "prowlarr,sonarr,gluetun,qbittorrent"


def test_the_wizard_walks_three_steps_and_continues_at_seeding_without_the_vpn(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    step1 = client.get("/setup/without-vpn", params={"apps": _APPS})
    assert step1.status_code == 200
    assert 'data-stage="1"' in step1.text
    assert (
        f'step-pill--current" aria-current="step">3 &middot; {words.VPN_STEP_TITLE}' in step1.text
    )

    step2 = client.post("/setup/without-vpn", data={"apps": _APPS, "stage": "1"})
    assert step2.status_code == 200
    assert 'data-stage="2"' in step2.text

    step3 = client.post("/setup/without-vpn", data={"apps": _APPS, "stage": "2"})
    assert step3.status_code == 200
    assert 'data-stage="3"' in step3.text

    confirmed = client.post(
        "/setup/without-vpn",
        data={"apps": _APPS, "stage": "3", "typed": words.WITHOUT_VPN_PHRASE},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == (
        "/setup/questions/qbittorrent/seeding?apps=prowlarr,sonarr,qbittorrent"
    )
    assert without_vpn_confirmed(settings.config_dir) is True


def test_a_wrong_sentence_re_renders_step_3_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    response = client.post(
        "/setup/without-vpn", data={"apps": _APPS, "stage": "3", "typed": "nope, not that"}
    )

    assert response.status_code == 200
    assert 'data-stage="3"' in response.text
    assert words.WITHOUT_VPN_PROBLEM_MISMATCH in html.unescape(response.text)
    assert without_vpn_confirmed(settings.config_dir) is False


def test_without_vpn_needs_qbittorrent_and_gluetun_in_apps(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    missing_gluetun = client.get(
        "/setup/without-vpn", params={"apps": "prowlarr,qbittorrent"}, follow_redirects=False
    )
    missing_qbittorrent = client.get(
        "/setup/without-vpn", params={"apps": "prowlarr,gluetun"}, follow_redirects=False
    )
    posted_without_apps = client.post(
        "/setup/without-vpn", data={"stage": "1"}, follow_redirects=False
    )

    for response in (missing_gluetun, missing_qbittorrent, posted_without_apps):
        assert response.status_code == 303
        assert response.headers["location"] == "/setup/apps"


def test_after_setup_is_done_every_without_vpn_visit_goes_home(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_finale_deploy(settings)
    client = _client(settings)

    get_response = client.get("/setup/without-vpn", params={"apps": _APPS}, follow_redirects=False)
    post_response = client.post(
        "/setup/without-vpn", data={"apps": _APPS, "stage": "1"}, follow_redirects=False
    )

    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/"
    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/"


def test_pills_after_the_escape_no_longer_show_your_vpn(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    client.post("/setup/without-vpn", data={"apps": _APPS, "stage": "1"})
    client.post("/setup/without-vpn", data={"apps": _APPS, "stage": "2"})
    confirmed = client.post(
        "/setup/without-vpn",
        data={"apps": _APPS, "stage": "3", "typed": words.WITHOUT_VPN_PHRASE},
        follow_redirects=False,
    )

    seeding_page = client.get(confirmed.headers["location"])

    assert seeding_page.status_code == 200
    assert words.VPN_STEP_TITLE not in seeding_page.text
    assert words.SEEDING_STEP_TITLE in seeding_page.text
    assert (
        f'step-pill--current" aria-current="step">3 &middot; {words.SEEDING_STEP_TITLE}'
        in seeding_page.text
    )
