# Music radio

Self-hosted AAC internet radio with a static web UI, Liquidsoap playout/HLS, Icecast, and a small API plus downloader worker. The web bundle and fonts are local; there are no Google Fonts runtime requests. The main player uses HLS.js and timed ID3 metadata. The control page reads the same timed metadata from a muted HLS reader and uses the API only for library and playback controls.

## Layout

- `web/` — static-web-server image and web assets.
- `radio/` — Icecast, Liquidsoap, API and downloader images/configuration.
- `deploy/nginx/music.conf.example` — generic reverse-proxy locations; provide TLS/domain configuration in your existing edge proxy.
- `data/` and `secrets/` — local runtime state; never commit these.

## Configure and start

Copy `.env.example` to `.env`, set a UUIDv4 for `RADIO_API_UUID` (for example, generate one with `uuidgen`), and choose the public URL prefix in your reverse-proxy configuration. The browser derives API, HLS, and font URLs from the page path, so the same image can be mounted at `/music/` or another prefix without rebuilding. Create these secret files with strong, distinct passwords:

- `secrets/icecast_source_password`
- `secrets/icecast_admin_password`

The Icecast entrypoint reads these files and then drops to the unprivileged `icecast` account; the host-side file owner does not need to match a container UID.

Create the data directories `data/{music,queue,incoming,state/jobs,logs/icecast,hls}`. Put AAC-compatible music source files in `data/music`. The worker runs as UID:GID `10001:10000` by default; make its writable bind-mounted directories owned by that ID before first start (adjust IDs in `.env` if needed):

```sh
sudo chown -R 10001:10000 data/queue data/incoming data/music data/state
```

The external Docker network named `edge` must exist and must be shared with the reverse proxy. The downloader container also requires the external network connectivity needed to reach YouTube.

Run from this directory:

```sh
docker compose build
docker compose up -d
docker compose ps
```

The web service binds `127.0.0.1:8088` by default for local checks and joins `edge` so the reverse proxy can address `music-web:8080`. API, Icecast and web are also attached to `edge`; keep that network private to trusted infrastructure. TLS certificates and domain names remain the responsibility of the outer proxy.

The bundled DotGothic16 font is licensed under SIL Open Font License 1.1; its license is included at `web/public/fonts/OFL.txt` ([upstream project](https://github.com/fontworks-fonts/DotGothic16)). Check the redistribution terms for the separately bundled FixederSys font before publishing the repository.

Add the locations from `deploy/nginx/music.conf.example` to the appropriate virtual host, replacing `/music` with your public prefix. The web proxy strips the prefix as shown. The sample assumes the proxy shares the `edge` network. For the default example prefix, the AAC endpoint is `/music/radio.aac`, HLS is `/music/live/index.m3u8`, and the control page is `/music/play/`.

To check configuration before starting:

```sh
docker compose --env-file .env config -q
```

## Data and secrets

Do not commit `.env`, `secrets/`, `data/`, downloaded music, HLS segments, or logs. Back up music, queue/state as needed, and secrets separately. The static web image is pinned to the upstream image digest in `web/Dockerfile`.
