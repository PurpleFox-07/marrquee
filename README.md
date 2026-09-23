# Marrquee

[![CI](https://github.com/PurpleFox-07/marrquee/actions/workflows/ci.yml/badge.svg)](https://github.com/PurpleFox-07/marrquee/actions/workflows/ci.yml)

The arr apps have always been for command-line people. Marrquee is
the friendly way in: one install, a few plain questions, and a page
where everything you set up is one click away.

Open it and you land on the setup wizard: pick your apps, point it at
your big drive, and it takes it from there. Then the Deploy screen
starts your apps for you, one at a time, and connects them together -
no typing, no command line. A separate diagnostics page proves
Marrquee is running and can talk to Docker.

## What works today

Right now Marrquee sets up three apps - Prowlarr, Sonarr and Radarr -
on a NAS or Linux machine that already runs Docker. There is no VPN
and no downloader yet; those are next. Nothing here will ever touch a
media library you already have.

## What you need

- A NAS or Linux machine that already runs Docker (this was built and
  tested against a Ugreen NAS running UGOS Pro).
- Port `7788` free on that machine. If something else is already using
  it, you can change the number - see the notes in each install method
  below.

## First-time setup: publish the image

This only needs doing once, the first time this project is set up on
GitHub. If Marrquee has already been published and you just want to
install it, skip ahead to **Install it** below.

1. **Make the home for the code.** Go to github.com, sign in as
   `PurpleFox-07`, and click **New repository**. Name it exactly
   `marrquee`, choose **Public**, and do **not** tick "Add a README" -
   this repository already has one. Click **Create repository**.
2. **Push this folder to it.** In Terminal, in this project's folder,
   paste the two lines GitHub shows under "…or push an existing
   repository":
   ```bash
   git remote add origin https://github.com/PurpleFox-07/marrquee.git
   git push -u origin main
   ```
3. **Watch it check itself.** On the repository's GitHub page, open the
   **Actions** tab. A run starts automatically. A green tick means the
   image built, started, and talked to a real Docker daemon on both
   Intel/AMD and ARM chips before anything was published. A red X means
   something needs fixing - nothing is published on a red run.
4. **Make the published image public.** GitHub keeps a freshly
   published image **private** by default, even though this repository
   is public - if you skip this step, your NAS will be turned away when
   it tries to download Marrquee. On the repository's GitHub page, find
   **Packages** on the right-hand side and click `marrquee`. Then open
   **Package settings**, scroll down to **Danger Zone**, choose
   **Change visibility**, select **Public**, and type `marrquee` to
   confirm.

## Install it

Pick whichever of these two feels more comfortable. Both install the
exact same thing, and both pull the published image,
`ghcr.io/purplefox-07/marrquee:latest`, from GitHub Container Registry
(once it has been published - see **First-time setup** above).

Settings now live in a `config` folder next to wherever you installed
from, instead of inside Docker itself, so you can see them in the NAS's
Files app.

**Already installed Marrquee before?** Ugreen's Docker app was updated
in September 2026 and now refuses the old `-v /:/host` line. If you
installed before then, paste the current [`compose.install.yaml`](./compose.install.yaml)
over your old one (see **Option B: paste a file** below, or step 2
under **Update Marrquee and deploy from your browser** further down),
or reinstall using Option A below with today's mounts.
If you installed using the old `marrquee-config` Docker volume, nothing
carries over from it automatically - but there's nothing to lose yet,
since this early version of Marrquee didn't save anything worth keeping
there.

### Option A: one command

If your NAS lets you run a single Docker command (for example over
SSH, or through a "run a command" box in its Docker app), paste this:

```bash
docker run -d \
  --name marrquee \
  --restart unless-stopped \
  -p 7788:7788 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd)/config:/config" \
  -v /volume1:/host/volume1 \
  ghcr.io/purplefox-07/marrquee:latest
```

What each line does:

- `-p 7788:7788` - makes Marrquee reachable at port 7788 on your NAS. If
  something else already uses 7788, change only the **first** number -
  the second one is fixed inside the container.
- `-v /var/run/docker.sock:/var/run/docker.sock` - lets Marrquee see
  whether Docker is running, and lets it start the apps you choose -
  through the compose file it writes for you - once you deploy.
- `-v "$(pwd)/config:/config"` - a folder next to wherever you run this
  command, for Marrquee to keep the choices you make. It survives
  restarts and reinstalls. Run the command from a folder you'll
  remember (or replace `$(pwd)/config` with a full path of your own,
  such as `/volume1/docker/marrquee/config`).
- `-v /volume1:/host/volume1` - this lets Marrquee see the shared
  folders under `/volume1`, so it can check the folder you type and
  build the media folders inside it. Marrquee only ever writes inside
  the one folder you choose. If your NAS also has a `/volume2`, add
  `-v /volume2:/host/volume2` too.

### Option B: paste a file

Many NAS Docker apps (including the Ugreen UGOS Pro one, under
**Docker > Project > Create**) let you paste a whole compose file
instead of typing a command. Use the contents of
[`compose.install.yaml`](./compose.install.yaml) from this repository -
it installs the same image, on the same port, with the same mounts as
Option A.

If your NAS's Docker app refuses one of the mount lines, fall back to
Option A instead.

## After it's running

Open a browser and go to `http://<your NAS's address>:7788`.

- **The first time**, you land on the setup wizard's first screen -
  "What do you want on your media server?" - with three apps already
  ticked. Follow it through: pick your apps, point it at your big
  drive, check the time zone, then press **Deploy your media server**
  on the Deploy screen and watch your apps come up.
- **Once your apps are deployed**, opening
  `http://<your NAS's address>:7788` takes you straight to your Hub:
  one poster per app, each showing Status: Up or Status: Down. Click
  a poster to open that app.

Want to check Marrquee's own health instead - whether it can talk to
Docker and save its settings? Go to
`http://<your NAS's address>:7788/diagnostics`. A green "Talking to
Docker" line means it's ready. If a line there is red, the page itself
explains what to do in plain language - fix what it describes, then
select **Check again**.

## If something goes wrong

Open `http://<your NAS's address>:7788/diagnostics`. If a deploy went
wrong, the **Last problem** section there has a **Copy** button -
press it and send what it copies.

## Update Marrquee and deploy from your browser

Everything below is done in a web browser - nothing to paste into a
Terminal or an SSH session.

1. **Wait for the green tick.** In your browser, open the project on
   github.com and click the **Actions** tab. Wait until the newest run
   shows a green tick. That means the new Marrquee was built, tested
   and published. A red X means stop and tell me.
2. **Update Marrquee on the NAS.** Open the Ugreen **Docker** app and
   go to **Project**. Next to `marrquee` you should see a mark saying
   an update is available. Choose to update it. The NAS downloads the
   newest Marrquee and restarts it, which takes a minute. **If you
   don't see an update option, stop here and tell me what that screen
   shows.** Do not use a command line.
3. **Check it's healthy.** Go to
   `http://<your NAS's address>:7788/diagnostics`. You should see a
   green "Talking to Docker" line. Further down there is a new **Last
   problem** section. It may show an old problem from earlier testing,
   or say nothing has gone wrong. Either is fine.
4. **Go through the wizard.** Go to
   `http://<your NAS's address>:7788/setup/apps`. Leave Prowlarr,
   Sonarr and Radarr ticked and press **Continue**. Type your big
   drive's folder, check the time zone, and press **Continue to
   Deploy**.
5. **Read the ticket.** The Deploy screen lists your three apps and
   the folders it will build. The paths should be on your drive
   (starting with your folder), never `/data`. If something is wrong,
   press **Back**.
6. **Press "Deploy your media server"** and watch. Each poster lights
   up gold, then turns green, one at a time. The first time can take a
   few minutes while the apps download. A note saying an app "is
   taking a little longer than usual" is normal. Then the wiring steps
   play, with a gold outline on the apps being connected, and finally
   "Now showing: your media server".
7. **Open Prowlarr from the finale.** Press **Open Prowlarr**. In
   Prowlarr, open **Settings -> Apps**. Sonarr and Radarr should
   already be listed. Nobody typed a key or an address.
8. **If anything turns red**, press **See the technical details**,
   then **Copy** in the Last problem section, and paste it to me. Also
   tell me whether Copy worked or whether it asked you to press
   Ctrl+C.
9. **Visit your Hub.**

   - 9a. Press **Go to your Hub**. You should see three purple
     posters, each saying **Status: Up** with a green dot.
   - 9b. On your **phone**, on the same Wi-Fi, open
     `http://<your NAS's address>:7788`. You should land straight on
     the Hub. Tap **Sonarr** and it should open in a new tab. *If it
     doesn't, write down exactly what the address bar says.*
   - 9c. Restart Marrquee from the Ugreen **Docker** app, then open
     the address again. You should still land on the Hub.
   - 9d. In the Ugreen **Docker** app, **stop** the Radarr container.
     Within about 15 seconds the Radarr poster should grey out and
     say **Status: Down** with a red dot and "Radarr stopped - last
     seen just now." Start it again, and it should go back to
     **Status: Up** by itself.

## Developing Marrquee

Marrquee is a Python project managed by
[uv](https://docs.astral.sh/uv/), which also installs the right
Python version for you.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # once, if you don't have uv yet
uv sync --dev                                     # installs Python 3.12 and every dependency
uv run pytest                                     # runs the test suite
```

Before committing, the same checks the automated pipeline runs are:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

### Watch the Deploy screen and the Hub on your Mac (developer preview)

This is a developer tool, not something an owner needs - it's for
checking the Deploy screen's four beats (ready, running, wiring,
finale) and the Hub without a NAS or a real Docker daemon.
[`tools/dev_fake_server.py`](./tools/dev_fake_server.py) runs the real
deploy engine, view model, templates and scripts against a scripted
Docker engine, readiness probe and wiring runner, in a temporary
folder it deletes on its own when you stop it. It never ships: it
lives under `tools/`, which `.dockerignore` excludes from the image,
and there is no setting anywhere that would turn it on by accident.

```bash
uv run python tools/dev_fake_server.py --scene happy
uv run python tools/dev_fake_server.py --scene wiring-problem
uv run python tools/dev_fake_server.py --scene failure
```

Then open <http://127.0.0.1:7788/deploy> and press **Deploy your media
server**. `happy` plays a clean run (with one app pausing just long
enough to show the reassurance note); `wiring-problem` reaches a green
finale with a wiring note and something to see on `/diagnostics#last-problem`;
`failure` shows the error frame with a stuck app.

```bash
uv run python tools/dev_fake_server.py --scene hub
uv run python tools/dev_fake_server.py --scene hub-stopped
```

Then open <http://127.0.0.1:7788> - `hub` shows all three apps
**Status: Up**; `hub-stopped` shows Radarr **Status: Down** with a
"last seen" line. The Hub's posters won't open anything on your Mac -
there are no real apps there. These screens are just for looking at.
