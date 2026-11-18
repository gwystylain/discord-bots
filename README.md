# discord-bots

A home for our Discord bots. Every bot lives in its own package under `bots/`, they all share one
Docker image, and each one runs as its own container (its own compose service, token and settings).

Every push to `main` builds the image and publishes it to
`ghcr.io/gwystylain/discord-bots:latest`, which the TrueNAS app pulls.

## Bots

| Bot | Package | What it does |
| --- | --- | --- |
| Red Heather report | `bots/ski_brief` | `/skibrief` plus a weekly post: is skiing at Red Heather / Paul Ridge good this weekend? Combines mountain-forecast.com (1646 m and the 1000 m parking lot), the Snower "Paul Ridge" snow station and Avalanche Canada; Claude writes the brief. |

## Layout

```
bots/
  common/            shared helpers (env loading)
  ski_brief/         one package per bot; run with `python -m bots.ski_brief`
env/
  ski-brief.env.example   settings template per bot (real *.env files are git-ignored)
deploy/truenas.yaml  compose file for TrueNAS (pulls the prebuilt image)
compose.yaml         compose file for running locally (builds the image)
Dockerfile           one image for all bots
```

## Run locally

```bash
pip install -r requirements.txt
cp env/ski-brief.env.example env/ski-brief.env   # fill it in
python -m bots.ski_brief --print                  # print a brief, no Discord needed
python -m bots.ski_brief                          # run the bot
```

Or in Docker: `docker compose up -d --build`.

## Deploy on TrueNAS SCALE

1. **Settings files.** Make a dataset folder, e.g. `/mnt/SSD/ApplicationsDataset/discord-bots`, and put one
   `<bot>.env` file in it per bot (copy the template from `env/`, fill in the token and keys).
   The container runs as UID 10001, but the env file is read by Docker on the host, so it can be
   `root`-owned with mode `600`.
2. **Install.** Apps → Discover Apps → ⋮ → **Install via YAML**. Name it `discord-bots` and paste
   [`deploy/truenas.yaml`](deploy/truenas.yaml) (its paths point at that folder).
3. **Logs.** Apps → discord-bots → the container's logs show `Logged in as ...` and the bot's
   invite link.
4. **Update after a push.** Wait for the GitHub Action to finish, then in TrueNAS stop and start
   the app (or Edit → Save). `pull_policy: always` makes it pull the new `latest` image.

## Add a new bot

1. Create `bots/<name>/` with `__init__.py`, `__main__.py` and your code. Call
   `bots.common.load_env("<name-with-dashes>")` at startup, and support `--help`: CI runs
   `python -m bots.<name> --help` in the built image as a smoke test.
2. Add its packages to `requirements.txt`.
3. Add `env/<name>.env.example`.
4. Add a service to `compose.yaml` and `deploy/truenas.yaml`:
   ```yaml
   my-new-bot:
     <<: *bot
     command: ["python", "-m", "bots.my_new_bot"]
     env_file: /mnt/SSD/ApplicationsDataset/discord-bots/my-new-bot.env
   ```
5. Push to `main`, then put the new `.env` file on TrueNAS and update the app's YAML.
