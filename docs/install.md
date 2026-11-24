# Installing, backing up and upgrading

The [README](../README.md) has the quick start. This page is what comes after it: what you need
first, where things live, backups, restoring, upgrading and the corners of Docker.

## What you need

- **Docker with the Compose v2 plugin.** `docker compose version` must print a version. The older
  `docker-compose` (a separate program, version 1) does not read this file.
- **An amd64 or an arm64 computer.** The arm64 image reads emails with an older version of the
  extractor, so a few emails that carry structured data there may need a model.
- **A user who may run `docker`**, and a folder of their own to unpack into.
- A computer that stays on if you want mail read by itself: the worker polls the mailbox.

## What the bundle holds

`tripbox.tar.gz` unpacks to a folder `tripbox/` with `compose.yaml`, `.env.example`,
`config/travellers.example.yaml` and four empty folders the services write to: `data` (the
ledger), `archive` (saved emails), `logs` and `backups`. Your own settings go in a `.env` beside
`compose.yaml`; `.env.example` lists every setting with a short note, and
[configuration](configuration.md) explains each.

The services run as user 1000 unless `TRIPBOX_UID` and `TRIPBOX_GID` say otherwise, and the four
folders must be writable by that user. `tripbox doctor` says which folder is not and the command
that fixes it (`docker compose run --rm worker tripbox doctor`).

- **Your user is not 1000.** Set `TRIPBOX_UID` and `TRIPBOX_GID` in `.env` to your own ids
  (`id -u`, `id -g`) before the first `docker compose up -d`, or `chown` the four folders to 1000.
- **The bundle unpacked by root** leaves the folders owned by 1000, as the services expect. Your
  `.env` is then yours to protect: it should be readable by you alone (`umask 077`, as the quick
  start's command does).
- **Your own additions to the compose file** go in a file named `compose.override.yaml` beside
  it, which Compose reads by itself. Unpacking the bundle again, to pick up a new
  `compose.yaml`, replaces `compose.yaml`, `.env.example` and `config/travellers.example.yaml`
  and nothing else, so an override survives it.

### Travellers and the owner

Without `config/travellers.yaml` nobody is the owner: bookings are still filed into trips, and no
flight can reach AirTrail. Copy `config/travellers.example.yaml` to `config/travellers.yaml` and
put your own names in it, then run `docker compose restart`: the services read the file once, when
they start. [Configuration](configuration.md#travellersyaml-optional) says how to write it.

## On a server

Add the two lines of the README's "On a server" to `.env`, with your server's name or address.
Plain http on every interface suits a home network only. For a server on the internet, keep the
default `TRIPBOX_LISTEN` (this computer alone) and put a proxy that serves https in front: see
[the proxy recipes](reverse-proxy.md). Docker's published ports bypass a host firewall such as
`ufw`, so a port published on every interface is open whatever the firewall says
([security](security.md)).

A bare `docker run` of the image, in place of Compose, is for a try-out only: publish the port on
`127.0.0.1` (`-p 127.0.0.1:8110:8110`), and give it a login, as the compose file does.

## Backup

```sh
docker compose run --rm worker tripbox backup
```

writes one archive into `backups/`, named `tripbox-backup-<time>.tar.gz`, a new file of mode 600,
and says what it holds. It is made while tripbox runs: the ledger is taken as one consistent
state. An archive holds the ledger and the files in `config/`. It does not hold `.env` (keep your
settings and secrets yourself) and, unless you add `--with-mail`, not the saved emails. With
`--with-mail` it holds every file under `archive/`; that is personal data, so keep the archive
private. `--out FILE` writes the archive somewhere else, to a new file.

Nothing backs up on a schedule: you run the command, by hand or from a job of your own (`cron`, a
systemd timer). Nothing removes old archives, and nothing removes the copies of the ledger that an
upgrade makes in `data/`: prune them yourself. Copy the archives to another computer: a backup on
the same disk is not one.

## Restore

`restore` reads only an archive `backup` made. Stop tripbox first: a restore refuses while a
worker or anything else holds the ledger, and a process that has put a `-wal` file beside the
restored ledger can be adopted by mistake if it runs.

```sh
docker compose down
docker compose run --rm worker tripbox restore /app/backups/tripbox-backup-<time>.tar.gz
docker compose run --rm worker tripbox restore /app/backups/tripbox-backup-<time>.tar.gz --apply
docker compose up -d
```

The first `restore` changes nothing: it says what the archive holds, what is here now and how
`config/` differs. With `--apply` it copies the ledger it replaces beside itself
(`ledger-before-restore-….db` in `data/`), puts the archive's ledger back and adds the saved emails
that are missing. It never writes `config/`: copy those files back yourself if you need to. An
older ledger is upgraded the next time tripbox starts. An archive from a newer tripbox is refused:
upgrade tripbox first. If you extract `config/` from an archive as root, the files are root's with
mode 600, and `doctor` names the one the services cannot read and the `chmod` that fixes it.

To move to a new computer: unpack the bundle there and write its `.env` again (the archive does not
hold it), without starting anything; copy the archive into `backups/` there, where user 1000 (or
your `TRIPBOX_UID`) can read it; run the two `restore` commands above, then `docker compose up -d`;
last, take `config/` out of the archive by hand (`tar -xzf backups/<archive> config`, run beside
`compose.yaml`), as `restore` never writes it, and run `docker compose restart`.

## Upgrade

```sh
docker compose run --rm worker tripbox backup
docker compose pull
docker compose up -d
```

The image is `ghcr.io/good-work-dojo/tripbox:1`, which follows the latest release of version 1.
Set `TRIPBOX_IMAGE` in `.env` to a full version such as `ghcr.io/good-work-dojo/tripbox:1.2.3` to
stay on one release. A release can change `compose.yaml`, and unpacking the bundle again over the
folder is safe at any upgrade, as the user who unpacked it the first time (run as root, `tar`
gives the folders that already exist its own owner and mode): from the folder that holds
`tripbox/`, run the quick start's `curl` and `tar` (the first of its three commands) without the
`cd`. For a pinned version, take the
bundle of that release from its page on GitHub in place of the latest.

When a release changes how the ledger is stored, the first start copies the ledger beside itself,
as `data/ledger-before-v<n>-<time>.db`, before it upgrades it, and refuses to upgrade when it
cannot write the copy. That copy is not an archive: `restore` does not read it. To go back by hand
after an upgrade, stop tripbox, set the image to the version you came from, move `data/ledger.db`
(and any `ledger.db-wal` and `ledger.db-shm` beside it) out of the way, copy the `ledger-before-…`
file to `data/ledger.db`, and start (this route has not been run). A ledger from a newer tripbox
is refused, untouched, with a message that says so. Release 1.1.0 is such a release (schema 2 adds
cover photos; trips and bookings are not changed).

## Other ways to run Docker

These have not been run: Docker Desktop (macOS and Windows), rootless Docker, Podman, an SELinux
host, and the compose stack on arm64 hardware (CI starts it on GitHub's arm64 runner, never on an
arm64 computer of anyone's).

- **SELinux** (Fedora, Red Hat): a bind mount needs the `:z` label. `compose.yaml` writes each
  mount in the long form, where the label is `selinux: z` under `bind`. Compose replaces a base
  mount with an override mount of the same `target` whole, it does not merge the two field by
  field, so each entry below repeats everything the base mount has: `read_only: true` on `config`
  (the page must never write its settings folder) and `create_host_path: false` on every mount.
  Put this in your `compose.override.yaml`:

  ```yaml
  services:
    web:
      volumes:
        - type: bind
          source: ./data
          target: /app/data
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./logs
          target: /app/logs
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./config
          target: /app/config
          read_only: true
          bind: {selinux: z, create_host_path: false}
    worker:
      volumes:
        - type: bind
          source: ./data
          target: /app/data
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./archive
          target: /app/archive
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./logs
          target: /app/logs
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./backups
          target: /app/backups
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./config
          target: /app/config
          read_only: true
          bind: {selinux: z, create_host_path: false}
    bot:
      volumes:
        - type: bind
          source: ./data
          target: /app/data
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./archive
          target: /app/archive
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./logs
          target: /app/logs
          bind: {selinux: z, create_host_path: false}
        - type: bind
          source: ./config
          target: /app/config
          read_only: true
          bind: {selinux: z, create_host_path: false}
  ```

  Or label the folders yourself once, with
  `chcon -R -t container_file_t data archive logs backups config`. Neither has been run.
- **Rootless Docker and Podman**: the user the services run as is mapped by the engine. Set
  `TRIPBOX_UID` and `TRIPBOX_GID` so that the folders are writable by them, and let `doctor`
  say what is wrong.

## Checking it on a phone

The page is laid out for a phone's screen and was tested at phone widths, but no real phone was
used. If you try one, this takes five minutes, and an issue that says what differed helps: (1) open
the https address in Safari and sign in; (2) Share, Add to Home Screen: the icon is named `Trips`,
opens with no browser bars and asks for the password once; (3) open the list, then a trip, switch on
airplane mode, close and reopen the app: the list and the trip open under `Saved copy`, their
buttons off but the copy buttons; (4) switch airplane mode off and pull down: the banner goes; (5)
make a share link and open it in a private tab: no sign-in, only what was ticked; (6) Settings, Sign
out, airplane mode on: no trip opens.
