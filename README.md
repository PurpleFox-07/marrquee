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

### Option A: one command

If your NAS lets you run a single Docker command (for example over
SSH, or through a "run a command" box in its Docker app), paste this:

```bash
docker run -d \
  --name marrquee \
  --restart unless-stopped \
  -p 7788:7788 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v marrquee-config:/config \
  -v /:/host \
  ghcr.io/purplefox-07/marrquee:latest
```

What each line does:

- `-p 7788:7788` - makes Marrquee reachable at port 7788 on your NAS. If
  something else already uses 7788, change only the **first** number -
  the second one is fixed inside the container.
- `-v /var/run/docker.sock:/var/run/docker.sock` - lets Marrquee see
  whether Docker is running and read its version. In this version
  Marrquee only looks; nothing here can start, stop or change a
  container.
- `-v marrquee-config:/config` - a place for Marrquee to keep the
  choices you make. It survives restarts and reinstalls.
- `-v /:/host` - this lets Marrquee see your drives, so it can check
  the folder you type and build the media folders inside it. Marrquee
  only ever writes inside the one folder you choose.

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
