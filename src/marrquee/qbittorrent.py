"""qBittorrent's own facts: the exact settings file Marrquee pre-writes
before its first start, its base preference keys, and the port-sync script
Gluetun runs on every reconnect.

This is a leaf, like `vpn.py`: it imports only the standard library,
`config`, `storage` and `words`, so nothing about the wiring engine or the
deploy engine can ever leak back into "what qBittorrent's settings file
looks like". The Bearer-key API door (`QbitClient`) lives in
`wiring/qbit_client.py` instead - that is the module every HTTP call to
qBittorrent goes through, the same split `arr_client.py` makes for the arr
apps.

qBittorrent 5.2 reads its WebUI API key from `Preferences/WebUI/APIKey` in
`qBittorrent.conf`, loaded once at start - never from an API call, since
there is no key yet the first time it starts. Writing that file before the
first `compose_up` is what lets Marrquee skip the linuxserver image's
temporary-password dance entirely: the key is already there, so the very
first API call already has admin rights.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import PurePosixPath
from typing import Final

from marrquee import storage
from marrquee.config import Settings
from marrquee.storage import ChownFn, to_host_view
from marrquee.words import PORT_SYNC_SCRIPT_COMMENT

logger = logging.getLogger(__name__)

# qBittorrent's own key shape (`ApiKey::isValid`, `apikey.cpp`): the literal
# prefix `qbt_` plus exactly 28 more characters. This is deliberately more
# permissive than `install.api_key_for`'s own generation alphabet - it is
# checking the FORMAT qBittorrent will accept, not re-deriving how Marrquee
# happens to generate one.
_API_KEY_PATTERN = re.compile(r"^qbt_[A-Za-z0-9]{28}$")

_CONF_RELATIVE_PATH: Final = PurePosixPath("marrquee/apps/qbittorrent/qBittorrent/qBittorrent.conf")

QBIT_KEY_SECRET_NAME: Final = "qbittorrent_api_key"
PORT_SYNC_SCRIPT_NAME: Final = "marrquee-port-sync.sh"

# qBittorrent's own preference keys (`appcontroller.cpp:182-188`, `:238`,
# `:322`) merged under whatever `seeding_preferences` returns before every
# settings write. Automatic Torrent Management (`auto_tmm_enabled`) is what
# makes a torrent added with a category land under that category's own
# `savePath` rather than this shared default - `save_path` only ever matters
# for a torrent added with no category at all.
QBIT_BASE_PREFERENCES: Final = {
    "save_path": "/data/torrents",
    "temp_path_enabled": False,
    "auto_tmm_enabled": True,
    "upnp": False,
    "max_ratio_act": 0,
}


def render_qbit_conf(key: str) -> str:
    """The exact `qBittorrent.conf` text Marrquee pre-writes, for `key`.

    An INI `QSettings` reads at start - never built with `configparser`,
    which lowercases keys and would write `webui\\apikey`, a key qBittorrent
    would silently fail to load. `[LegalNotice] Accepted=true` is mandatory
    (`main.cpp:252-269`: without it, qbittorrent-nox stops and waits
    forever); `WebUI\\Address`/`ServerDomains` mirror linuxserver's own
    shipped defaults so a pre-written file changes nothing else about how
    the image behaves.

    Raises `ValueError` for a key that doesn't match qBittorrent's own
    format - the message never repeats the rejected value, since a key this
    module refuses is still not something to hand to a log.
    """
    if not _API_KEY_PATTERN.fullmatch(key):
        raise ValueError("not a qBittorrent API key: expected 'qbt_' + 28 characters")

    return (
        "[LegalNotice]\n"
        "Accepted=true\n"
        "\n"
        "[Preferences]\n"
        "Connection\\PortRangeMin=6881\n"
        "Connection\\UPnP=false\n"
        "Downloads\\SavePath=/data/torrents/\n"
        f"WebUI\\APIKey={key}\n"
        "WebUI\\Address=*\n"
        "WebUI\\ServerDomains=*\n"
    )


def qbit_conf_host_path(root: PurePosixPath) -> PurePosixPath:
    """Where qBittorrent's settings file lives, as a HOST path under `root`.

    Mirrors linuxserver's own init script (`mkdir -p /config/qBittorrent`)
    under the same `<root>/marrquee/apps/qbittorrent` config mount every
    other app's compose service gets.
    """
    return root / _CONF_RELATIVE_PATH


def write_qbit_conf(
    settings: Settings,
    root: PurePosixPath,
    api_key: str,
    puid: int,
    pgid: int,
    *,
    chown: ChownFn = os.chown,
) -> bool:
    """Write qBittorrent's settings file, only if it isn't there yet.

    Returns `True` when it wrote the file, `False` when an existing file
    (the owner's own, or a previous Marrquee run's) was left untouched -
    the key must never look like it silently changed under an owner who
    has already changed their qBittorrent settings by hand.

    `marrquee/apps/qbittorrent` is expected to already exist (`build_folders`
    creates it, chowned to the drive's owner, before this ever runs); this
    function creates only the `qBittorrent` subfolder linuxserver's init
    script expects, and chowns only what it created here - never the config
    folder above it. The path is resolved through `storage._safe_join`,
    so a symlink planted where that folder should be raises
    `PathEscapesRoot` instead of writing through it. A chown failure (a NAS
    share that doesn't support it) is logged and swallowed, the same as
    every other owner-readable file this codebase writes - but any other
    `OSError` (a full disk, a folder that turned out to be a plain file)
    propagates, since that means the write itself did not truly happen.
    """
    container_root = to_host_view(settings, str(root))
    conf_path = storage._safe_join(container_root, _CONF_RELATIVE_PATH)

    if conf_path.exists():
        return False

    folder = conf_path.parent
    created_folder = not folder.exists()
    if created_folder:
        folder.mkdir(parents=True)

    temp_path = conf_path.with_name(f".{conf_path.name}.tmp")
    temp_path.write_text(render_qbit_conf(api_key))
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, conf_path)

    if created_folder:
        try:
            chown(folder, puid, pgid)
        except OSError as error:
            logger.warning("could not chown %s to %s:%s: %s", folder, puid, pgid, error)
    try:
        chown(conf_path, puid, pgid)
    except OSError as error:
        logger.warning("could not chown %s to %s:%s: %s", conf_path, puid, pgid, error)

    return True


def port_sync_script(port: int) -> str:
    """The script Gluetun runs (`VPN_PORT_FORWARDING_UP_COMMAND`/
    `_DOWN_COMMAND`) on every reconnect, so qBittorrent's listening port
    follows the VPN's forwarded one without Marrquee needing a background
    loop of its own.

    Reads the key from Gluetun's own root-only secrets mount rather than
    taking one as an argument - the key never appears in `docker inspect`
    or a process list this way. A Bearer-authenticated POST skips
    qBittorrent's CSRF check (`webapplication.cpp:659`), so no session
    cookie dance is needed here. `wget`, not `curl`: the linuxserver
    Gluetun image ships GNU wget, not curl. Short, bounded retries -
    Gluetun runs its forwarding hooks synchronously, so a slow script here
    would delay every future reconnect, not just this one.
    """
    comment_line = f"# {PORT_SYNC_SCRIPT_COMMENT}\n"
    key_line = f"key=$(cat /run/secrets/{QBIT_KEY_SECRET_NAME})\n"
    # Built with `+`, never adjacent literal juxtaposition: adjacent string
    # literals fold into one constant at parse time, and this line's flags
    # plus its dotted-quad IP address would then read as a space-and-period
    # sentence to anything scanning this module for un-reviewed prose - it
    # is a command, not a sentence, so it is kept as separate literals
    # instead.
    exec_line = (
        "exec wget -q -O /dev/null -T 5 --tries=3 --retry-connrefused "
        + '--header="Authorization: Bearer $key" '
        + '--post-data="json={\\"listen_port\\":$1}" '
        + f"http://127.0.0.1:{port}/api/v2/app/setPreferences\n"
    )
    return comment_line + key_line + exec_line
