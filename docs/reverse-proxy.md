# tripbox behind a reverse proxy

The configurations in `docs/proxy/` put a reverse proxy in front of tripbox's page, at a name of
its own or under a path of a site the proxy already serves. Each file's first line names the
`TRIPBOX_BASE_URL` it goes with; set that in `.env`.

| Layout | `TRIPBOX_BASE_URL` | Caddy | nginx | Traefik |
|---|---|---|---|---|
| A name of its own | `https://trips.example.com` | `docs/proxy/caddy-hostname.Caddyfile` | `docs/proxy/nginx-hostname.conf` | `docs/proxy/traefik-hostname.yml` |
| A path of a site | `https://example.com/tripbox` | `docs/proxy/caddy-subpath.Caddyfile` | `docs/proxy/nginx-subpath.conf` | `docs/proxy/traefik-subpath.yml` |
| AirTrail and tripbox at one hostname | `https://flights.example.com/tripbox` | `docs/proxy/caddy-airtrail.Caddyfile` | [below](#airtrail-and-tripbox-at-one-hostname) (not run) | — |

A proxy is a way to put tripbox on the internet with https, which its own port does not serve.
Docker's published ports bypass a host firewall such as `ufw`: publish tripbox's port on
`127.0.0.1` (the default) and let the proxy be the only thing that is open. [Security](security.md)
says what else to know.

## Putting the proxy on tripbox's network

None of this has been run. Each block goes in `compose.override.yaml` beside `compose.yaml`, which
`docker compose` reads by itself; the recipes in `docs/proxy/` send requests to `web:8110`.

A proxy of your own as a service of tripbox's project, publishing 80 and 443 (Caddy here, its
files in `./Caddyfile`). The fixed address is for `TRIPBOX_AUTH=proxy`: list it in
`TRIPBOX_TRUSTED_PROXIES` (`198.51.100.10` here). Without that mode the subnet and the address can
go.

```yaml
services:
  proxy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
    networks:
      default:
        ipv4_address: 198.51.100.10
networks:
  default:
    ipam:
      config:
        - subnet: 198.51.100.0/24
```

A proxy that already runs in another compose project: join the page to its network, and send
requests to `web:8110` from it. `docker network ls` lists the network's name.

```yaml
services:
  web:
    networks: [default, proxy]
networks:
  proxy:
    external: true
    name: proxy_default
```

## Facts every proxy has to respect

- **Where the page is.** The recipes send requests to `web:8110`: the proxy runs as a service in
  the same compose project as tripbox, on its network, and reaches the page's own port. A proxy
  installed on the host reaches the port compose publishes instead, `127.0.0.1:8110` by default
  (`TRIPBOX_LISTEN`).
- **Links come from the setting alone.** The page believes no `X-Forwarded-*` or `Forwarded`
  header and does not build a link from the request's `Host`: every link, redirect, share link and
  calendar address is built from `TRIPBOX_BASE_URL`, so it must be the address people open. A form
  post whose `Origin` is not that address's origin is refused.
- **A path.** Under a sub-path the page answers at `/tripbox/...`; the bare `/tripbox` is sent on
  to `/tripbox/` by the page itself. The recipes hand the path on as it came. A proxy that takes the
  path off works too: the page answers both.
- **The body limit.** The page takes uploads of up to 10 MB. nginx refuses a request body over
  1 MB unless `client_max_body_size` says otherwise; the nginx recipes set `11m`. Caddy and Traefik
  set no limit by default.
- **Nothing else.** The page uses no WebSocket and streams nothing, so a proxy's WebSocket and
  buffering settings can stay as they are.
- **TLS.** The base URLs above are `https`. Caddy gets the certificate for the name by itself.
  The nginx recipes listen on port 80 as shipped, and each carries a comment that says which
  `listen 443 ssl;` and certificate lines to add for an `https` base URL; the Traefik recipes say
  which entry point and certificate resolver of yours to name. Saved copies for use with no network
  (`TRIPBOX_OFFLINE`) work only over `https` or on `localhost`.
- **A login proxy** (Authelia, Authentik, oauth2-proxy and the like) must let three paths through
  without its own login: `<base>/s/` (share links), `<base>/feed/` (calendar apps) and
  `<base>/static/` (the page's files).
- **`TRIPBOX_AUTH=proxy`.** The page believes the user header only from the addresses in
  `TRIPBOX_TRUSTED_PROXIES`. The proxy must set this header itself on every request it passes to
  tripbox, replacing any header of that name a browser sent — on the paths it lets through
  without a sign-in as well. A proxy that only adds the header when its sign-in answers lets a
  browser choose who it is. The header's name and its rules are the
  `TRIPBOX_PROXY_USER_HEADER` row of `docs/configuration.md`. Run the proxy as a service on
  tripbox's compose network with a fixed address (a network whose subnet you set, the proxy's
  `ipv4_address` in it) and list that address. Never list Docker's gateway address or its
  network: when a port is published, every outside client may appear to come from it. The
  shipped `compose.yaml` publishes tripbox's port on this computer alone by default
  (`TRIPBOX_LISTEN`), which is harmless: a request through it comes from that gateway address and
  is never signed in.

## AirTrail and tripbox at one hostname

When you keep a flight log in AirTrail, both can share one name: AirTrail at its root and tripbox
under `/tripbox`, with `TRIPBOX_BASE_URL` set to `https://flights.example.com/tripbox`. AirTrail
must be the one at the root: it is served under no path of its own. Its `ORIGIN` setting is the
hostname, `https://flights.example.com`; tripbox's `TRIPBOX_AIRTRAIL_PUBLIC_URL` is the same
address, and its worker still reaches AirTrail directly
(`TRIPBOX_AIRTRAIL_URL=http://airtrail:3000`, with the worker and the bot joined to AirTrail's
network as [AirTrail](airtrail.md#switching-it-on) shows), not through the proxy. AirTrail's
Trips button is built with `/tripbox` (`integrations/airtrail/README.md`).

- **Caddy:** `docs/proxy/caddy-airtrail.Caddyfile`, the sub-path recipe with AirTrail in its last
  `handle`.
- **nginx** (not run): the two locations of `docs/proxy/nginx-subpath.conf`, then AirTrail's,
  after them:

  ```nginx
  server {
      listen 80;
      server_name flights.example.com;

      location = /tripbox {
          proxy_pass http://web:8110;
          proxy_set_header Host $host;
          client_max_body_size 11m;
      }

      location ^~ /tripbox/ {
          proxy_pass http://web:8110;
          proxy_set_header Host $host;
          client_max_body_size 11m;
      }

      # AirTrail takes uploads of up to 20 MB (its BODY_SIZE_LIMIT)
      location / {
          proxy_pass http://airtrail:3000;
          proxy_set_header Host $host;
          client_max_body_size 21m;
      }
  }
  ```

- **Traefik** (not run): the sub-path recipe with a second router for AirTrail at the hostname
  alone, and the router for `/tripbox` given the higher priority.
- **Cloudflare Tunnel** (not run): two public hostname rules for `flights.example.com`, in this
  order, since the first that matches wins: the path rule `^/tripbox(/.*)?$` with the service
  `http://web:8110`, then the hostname alone with the service `http://airtrail:3000`.

What sharing a name means:

- **AirTrail's cookie covers the whole hostname**, `/tripbox` included, and so does AirTrail's
  service worker, which v3.14.0 registers at `/`. It answers none of tripbox's requests: it
  serves no navigation (`navigateFallback: null`), its precache holds AirTrail's own files, and its
  runtime caching is for map hosts alone. tripbox reads only its own cookie, and its own service
  worker covers `/tripbox/` alone, where, as the narrower scope, it is the one in charge. Signing
  out of tripbox clears the browser's cache for the hostname, AirTrail's cached files included,
  and nothing else: not the site's storage, and not AirTrail's sign-in.
- **One hostname is one origin:** each app's scripts can reach the other's pages and, through
  them, its session, so a flaw in either is a flaw in both. The cookie's path isolates nothing.
  Put two apps at one hostname only when you trust both as one.
- **A login proxy** in front of the hostname must let tripbox's three paths through without its
  own login: `/tripbox/s/` (share links), `/tripbox/feed/` (calendar apps) and
  `/tripbox/static/` (the page's files).

## Cloudflare Tunnel and Nginx Proxy Manager

- **Cloudflare Tunnel:** a public hostname `trips.example.com` whose service is `http://web:8110`
  (cloudflared in the same compose project) or `http://127.0.0.1:8110` (cloudflared on the host),
  with `TRIPBOX_BASE_URL=https://trips.example.com`. For a path of a site, the hostname rule's path
  is `^/tripbox(/.*)?$`. Cloudflare Access in front of it is `TRIPBOX_AUTH=cloudflare`
  (`docs/configuration.md`); its Access application must let `<base>/s/` (share links),
  `<base>/feed/` (calendar apps) and `<base>/static/` (the page's files) through without its
  login, with a Bypass policy on those paths.
- **Nginx Proxy Manager:** a proxy host for `trips.example.com`, scheme `http`, forward hostname
  `web`, port `8110`, with `client_max_body_size 11m;` in its Advanced tab.

## What has been run

- **Run in containers:** Caddy and nginx, each at a name of its own and under a path, in front of
  a fresh tripbox in one compose project, over plain HTTP: the Caddy files with `http://` put
  before the site address (which turns Caddy's certificates off), the nginx files as written,
  and in each sub-path file one rule of the site's own put where it says the site's own go,
  which the recipe's must win over (for Caddy `rewrite * /index.html`, for nginx
  `location ~* \.(css|js)$ { return 404; }`), the images pinned by digest. Through each, a
  client addressed to the base URL signs in, pastes a booking and opens the trip it is filed
  into, makes a share link and opens it without a cookie, opens the calendar feed, uploads a
  file of 2 MiB, finds the service worker and the manifest scoped to the base path, signs out,
  and checks that every redirect and cookie stays under the base path and that addresses
  outside it are not the page's.
- **Run in containers:** AirTrail and tripbox at one hostname behind
  `docs/proxy/caddy-airtrail.Caddyfile` (with `http://` before the site address, and AirTrail's
  service under another name), AirTrail v3.14.0 built with the Trips button for `/tripbox`, over
  plain HTTP at a name that is not `localhost`, where a browser registers no service worker:
  neither app's worker was in play.
  Through Caddy, the hostname's root and `/api/v1` are AirTrail's, `/tripbox` is tripbox's and
  `/tripboxx` AirTrail's; signing in to tripbox keeps its cookie under `/tripbox`; the trips list
  links the flight log; a pasted booking reaches AirTrail with its four travellers; the built
  AirTrail serves the button's link, and a browser signed in to AirTrail's demo clicks it and
  lands on tripbox's sign-in page. The same run with AirTrail as upstream ships it finds no link.
- **Not run:** TLS, Traefik, a proxy that takes the path off, the fixed-address layout of
  `TRIPBOX_AUTH=proxy`, Cloudflare Tunnel, Nginx Proxy Manager, and nginx and Cloudflare Tunnel
  at one hostname.
