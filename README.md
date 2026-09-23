# Marrquee

[![CI](https://github.com/PurpleFox-07/marrquee/actions/workflows/ci.yml/badge.svg)](https://github.com/PurpleFox-07/marrquee/actions/workflows/ci.yml)

Marrquee is a self-hosted web app that sets up and wires together the
"arr" media apps (Sonarr, Radarr, Prowlarr, and friends) on your own
NAS or Linux box. This is its first piece: an installable page that
proves Marrquee is running and can talk to Docker. Later versions add
the setup wizard that picks your apps and builds your folders.

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
over your old one (see step 1 under **Try the deploy engine** below),
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

Open a browser and go to `http://<your NAS's address>:7788`. You
should see a purple page that says "Marrquee is running", followed by
a green "Talking to Docker" line with a version number underneath it.

If a line on that page is red instead of green, the page itself
explains what to do in plain language - fix what it describes, then
select **Check again**.

## Try the deploy engine (before there's a wizard screen)

The setup wizard - the screen where you pick your apps and point at your
big drive - is a later piece of this project. The part underneath it,
the deploy engine that actually builds your folders and starts your
apps, already works today, and you can drive it yourself with a
handful of pasted commands. Nothing below needs any programming
knowledge - it's the same "paste this, read what it says" pattern as
the install step above.

1. **Update Marrquee on the NAS, if you installed it before this
   feature existed.** Open the **Docker** app -> **Project**, select
   `marrquee`, and paste the current contents of
   [`compose.install.yaml`](./compose.install.yaml) over the old ones.
   The new lines mount a `config` folder next to the project, and your
   NAS's shared folders (`/volume1`, and `/volume2` too if you have
   one) at `/host/volume1` and `/host/volume2` - together, that's what
   lets Marrquee see your files. Click **Deploy** again. If you
   installed fresh using the instructions above, this step is already
   done - skip ahead.
2. **Check it came back.** In a browser go to
   `http://<your NAS's address>:7788`. You should still see the purple
   page with a green "Talking to Docker" line.
3. **Tell it what to build.** SSH into the NAS and paste this,
   replacing the path with your big drive's folder (on most NAS boxes
   it starts with `/volume1`):

   ```bash
   curl -X POST http://<your NAS's address>:7788/api/install \
     -H "Content-Type: application/json" \
     -d '{"path":"/volume1/media","app_ids":["prowlarr","sonarr","radarr"]}'
   ```

   A good answer looks like `{"saved":true}`. If it answers with a
   refusal instead, read it - it is written in plain words and tells
   you exactly what to change. Nothing has been created on your drive
   yet either way.
4. **Press the button.** Paste:

   ```bash
   curl -X POST http://<your NAS's address>:7788/api/deploy
   ```

   A good answer is a block of JSON with `"phase":"running"` in it.
5. **Watch it.** Paste this a few times over the next couple of
   minutes:

   ```bash
   curl http://<your NAS's address>:7788/api/deploy
   ```

   You'll see each app move from `"waiting"` to `"starting"` to
   `"done"`, in the `apps` list. The first run downloads three apps, so
   it can take a few minutes. If one app's `note` says it's taking
   longer than usual, that's normal and it isn't broken - only
   `"phase":"error"` means something needs your attention. A finished
   deploy answers with `"phase":"finale"` and every app `"done"`.
6. **Look at what it built.** In the NAS's **Files** app, open the
   folder you typed. You should see a `data` folder (with `media` and
   `torrents` inside) and a `marrquee` folder containing
   `compose.yaml`. Open `compose.yaml` - it's meant to be readable.
   That file is the actual description of your media server, and it's
   the file that created every container. It contains your apps'
   secret keys, so it's only readable by you (the owner of this
   folder) - don't paste its contents anywhere public.
7. **Open an app.** In a browser go to `http://<your NAS's
   address>:8989`. Sonarr should open straight up with no login. Same
   for `:7878` (Radarr) and `:9696` (Prowlarr).
8. **If something goes wrong.** Paste these two commands and send both
   of their outputs to whoever is helping you:

   ```bash
   curl http://<your NAS's address>:7788/api/deploy/diagnostics
   docker logs marrquee --tail 200
   ```

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
