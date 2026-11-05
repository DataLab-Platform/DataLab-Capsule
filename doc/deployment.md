# Deployment notes

DataLab-Capsule needs no server: capsules, reviews and reports are files. This page covers the two things that are deployed: the optional registry and the profile pages. For DataLab-Web hosting, see its own documentation; it only needs DataLab-Capsule available to its Pyodide runtime.

## Registry (optional)

The registry stores manifests, the attachments they declare and reviews; it never stores workspaces. Scope of this version, accepted for the demonstrator: no accounts, quotas, rate limits or delete API; one bearer token protects publishing; reading is open.

### Install and run

```bash
python -m venv /opt/capsule-registry
/opt/capsule-registry/bin/pip install "datalab-capsule[registry]"
export REGISTRY_TOKEN="$(openssl rand -hex 32)"   # keep it in a secret store, not in shell history
/opt/capsule-registry/bin/workspace-capsule-registry /var/lib/capsule-registry \
    --host 127.0.0.1 --port 8765 --token-env REGISTRY_TOKEN \
    --origin https://web.example
```

- `ROOT` (here `/var/lib/capsule-registry`) holds `registry.sqlite3` and the `manifests/`, `reviews/` and `attachments/` folders. Back up the whole folder together; files are named by digest and written atomically.
- The token is read from the environment variable named by `--token-env`, never from the command line. Without a token, publishing is open: local use only.
- `--origin` lists the browser origins allowed to call the registry (DataLab-Web deployments), without credentials. Repeat it for several origins; the default (any origin) is for local use.
- Run it as an unprivileged user, as a service (systemd or equivalent), listening on `127.0.0.1` only.
- Run one server process per registry folder: the command starts a single process, and two processes sharing the same folder are not supported.

### Behind a reverse proxy

Serve it only through HTTPS, on its own host name, with a reverse proxy that limits request rates and body sizes. The registry itself refuses bodies over 8 MiB (manifest), 16 MiB (attachment) and 1 MiB (review) while reading them; set the proxy limit slightly above 16 MiB. Example (nginx; the rate-limit zone is declared once in the `http` block, the location in the `server` block of `registry.example`):

```nginx
# http block: 10 requests per second and per client address, 10 MiB of state
limit_req_zone $binary_remote_addr zone=registry:10m rate=10r/s;

server {
    server_name registry.example;
    # listen 443 ssl; ssl_certificate ...; ssl_certificate_key ...;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 17m;
        limit_req zone=registry burst=20 nodelay;
        limit_req_status 429;
    }
}
```

DataLab-Web users then enter `https://registry.example` as the registry address; the command line takes the same address (`workspace-capsule registry list https://registry.example`). Under a path prefix the API works, but the `/docs` page does not find `/openapi.json`.

### Restricting access

The registry has no user accounts: publishing needs the token, and anyone who can reach the service can read it. Restrict reading by restricting who reaches the service, with the organisation's own network controls: deploy it on an isolated network (below), or let the reverse proxy accept only the organisation's addresses or VPN, for example in the `location` block above:

```nginx
allow 10.0.0.0/8;   # the organisation's networks
deny all;
```

The clients of this version (DataLab-Web, DataLab Desktop and the command line) send no credentials when reading, and only the token when publishing. An authentication that the proxy requires on every request (HTTP Basic, single sign-on cookies, client certificates) therefore blocks them. User authentication and identity management are outside the scope of this version.

### On an isolated network

Neither the registry nor the command line needs Internet access. On a connected machine with the same platform and Python version as the target host, download the wheels, copy the folder to the isolated network, then install without a package index and run the registry as above, with a certificate of the organisation for HTTPS:

```bash
pip download "datalab-capsule[registry]" --dest wheels             # connected machine
pip install --no-index --find-links wheels "datalab-capsule[registry]"   # isolated host
```

DataLab Desktop also publishes to and reads from such a registry. DataLab-Web downloads its runtime from the Internet when it starts (no offline mode) and cannot be used on a network without Internet access in this version.

### Checks after deployment

```bash
curl -s https://registry.example/api/v1/status          # {"registry": "datalab-capsule", ..., "token_required": true}
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://registry.example/api/v1/revisions   # 401
workspace-capsule registry publish https://registry.example example.dlcapsule --token-env REGISTRY_TOKEN
workspace-capsule registry list https://registry.example
```

The API is described at `/docs` and `/openapi.json`. Residual risks are listed in the [security analysis](security.md).

## Profile pages

The profile, terms, schemas and context are served from the `DataLab-Platform/capsule` repository by GitHub Pages under `https://datalab-platform.com/capsule/`. Publishing is a push to `main`. After each publication, check that every page and resource answers 200, that the served files are byte-identical to the commit, and that the SHA-256 of the schemas and context are unchanged unless the change is intended (they are part of the published format).
