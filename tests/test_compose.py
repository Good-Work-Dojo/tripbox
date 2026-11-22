# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The shipped compose file, read as text and as YAML (nothing runs): a page, a worker and an
optional bot; what each receives and mounts; the page's one published port; the hardening every
service shares. The page container is the browser-reachable one, so what it may receive is
compared with the settings registry itself, never with a copy of its values."""

import re
import stat
import subprocess
from pathlib import Path

import yaml

from tripbox.config import (
    COMPOSE_ONLY,
    CONTAINER_PORT,
    IMAGE_ENV,
    KEY_BY_NAME,
    KEYS,
    PAGE_SECRETS,
    Settings,
)

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose.yaml"

LICENCE_HEADER = "# SPDX-License-Identifier: AGPL-3.0-only\n# Additional terms: see NOTICE\n"
HEAD = """\
# tripbox: forwarded travel confirmation emails become a trips page.
# One image, three services: web (the page), worker (reads mail and what you hand in) and,
# with the "telegram" profile, bot.
#
# Quick start, in an empty directory (in .env a $ is written $$):
#   curl -fsSL https://github.com/Good-Work-Dojo/tripbox/releases/latest/download/tripbox.tar.gz \\
#     | tar -xz && cd tripbox
#   (umask 077 && echo 'TRIPBOX_PASSWORD=<choose a password of 10 characters or more>' > .env)
#   docker compose up -d
# then open http://localhost:8110 and sign in. On a server, add two lines to .env first:
#   TRIPBOX_LISTEN=0.0.0.0:8110
#   TRIPBOX_BASE_URL=http://<its name or address>:8110
# The guides are in the source at https://github.com/Good-Work-Dojo/tripbox: docs/install.md
# (backup, restore, upgrade), docs/configuration.md (every setting), docs/mail.md, docs/models.md,
# docs/privacy.md, docs/security.md, docs/airtrail.md and docs/reverse-proxy.md.
# "docker compose run --rm worker tripbox doctor" says what is set up and what is wrong.
# The folders data, archive, logs, config and backups beside this file are yours: the services
# run as user 1000 unless TRIPBOX_UID and TRIPBOX_GID say otherwise. Back up data and archive.
"""

COMPOSE_NAMES = {name: default for name, default, _ in COMPOSE_ONLY}
FOLDER_KEY = {
    "data": "TRIPBOX_DATA_DIR",
    "archive": "TRIPBOX_ARCHIVE_DIR",
    "logs": "TRIPBOX_LOGS_DIR",
    "config": "TRIPBOX_CONFIG_DIR",
    "backups": "TRIPBOX_BACKUPS_DIR",
}
# What each service mounts, in this order; config alone read-only
FOLDERS = {
    "web": ("data", "logs", "config"),
    "worker": ("data", "archive", "logs", "backups", "config"),
    "bot": ("data", "archive", "logs", "config"),
}
HARDENING = {
    "image": "${TRIPBOX_IMAGE:-ghcr.io/good-work-dojo/tripbox:1}",
    "user": "${TRIPBOX_UID:-1000}:${TRIPBOX_GID:-1000}",
    "restart": "unless-stopped",
    "init": True,
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
}
# Every key each service has, the shared block's included: a key added anywhere fails the pin
# until it is named here
SERVICE_KEYS = {
    "web": {*HARDENING, "command", "environment", "volumes", "ports", "tmpfs", "mem_limit",
            "depends_on", "healthcheck"},
    "worker": {*HARDENING, "command", "env_file", "environment", "volumes", "tmpfs", "mem_limit",
               "healthcheck"},
    "bot": {*HARDENING, "profiles", "command", "env_file", "environment", "volumes", "tmpfs",
            "mem_limit", "depends_on", "healthcheck"},
}
LIMITS = {
    "web": ("512m", ["/tmp:size=64m"]),
    "worker": ("1g", ["/tmp:size=256m"]),
    "bot": ("512m", ["/tmp:size=256m"]),
}
LISTEN = KEY_BY_NAME["TRIPBOX_LISTEN"]
# compose's interpolation: $NAME, ${NAME}, ${NAME:-x}, ${NAME-x}, ${NAME:?x}, ${NAME?x},
# ${NAME:+x}; `$$` is a literal dollar sign, read left to right
_REFERENCE = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)|\$([A-Za-z_][A-Za-z0-9_]*)")
# the one form the shipped file uses: a name and its default, which holds no `$` or brace
_DEFAULTED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^${}]*)\}")


def text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def model(source: str | None = None) -> dict:
    return yaml.safe_load(text() if source is None else source)


def references(value: str) -> list[str]:
    """The names a compose value interpolates (`$$` is no reference)."""
    found = []
    for match in _REFERENCE.finditer(value):
        name = match.group(1) or match.group(2)
        if name:
            found.append(name)
    return found


def strings(node, path: str = ""):
    """Every string in a parsed YAML node, keys included, with where it is."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, str(key)
            yield from strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for value in node:
            yield from strings(value, path)
    elif isinstance(node, str):
        yield path, node


def page_setting(name: str) -> bool:
    """A setting the page container may receive: a web key, and if a secret, its login pair's."""
    key = KEY_BY_NAME.get(name)
    return key is not None and key.web and (not key.secret or name in PAGE_SECRETS)


def web_service_leaks(compose_text: str) -> list[str]:
    """What in this compose text would hand the page container more than its settings: an
    `env_file` (in any form, an anchor merge included), an environment name that is no page
    setting, an environment value that interpolates any name but its own, or a value anywhere
    else in the service (command, labels, a volume, a port, the image, the user) that
    interpolates a secret or a name that is neither a non-secret page setting nor one of the
    compose file's own names."""
    web = yaml.safe_load(compose_text)["services"]["web"]
    environment = web.get("environment")
    leaks = [] if isinstance(environment, dict) and environment else ["environment: not a mapping"]
    if "env_file" in web:
        leaks.append("env_file")
    for name, value in (environment or {}).items():
        if not page_setting(name):
            leaks.append(f"environment: {name}")
        for ref in references(str(value)):
            if ref != name:
                leaks.append(f"{name}: ${ref}")
    rest = {k: v for k, v in web.items() if k != "environment"}
    for path, value in strings(rest):
        for ref in references(value):
            key = KEY_BY_NAME.get(ref)
            if ref not in COMPOSE_NAMES and not (key and key.web and not key.secret):
                leaks.append(f"{path.split('.')[0]}: ${ref}")
    return leaks


def shape_problems(compose_text: str) -> list[str]:
    """Each key a service has beyond its own (`SERVICE_KEYS`) or lacks, and each value of the
    shared block a service holds otherwise, as `<service>: <key>`."""
    found = []
    for name, svc in yaml.safe_load(compose_text)["services"].items():
        keys = set(svc)
        found += [f"{name}: {key}" for key in sorted(keys ^ SERVICE_KEYS.get(name, set()))]
        found += [f"{name}: {key}" for key, value in HARDENING.items()
                  if key in svc and svc[key] != value]
    return found


def test_the_stack_is_a_page_a_worker_and_an_optional_bot():
    source = text()
    assert source.startswith(LICENCE_HEADER + HEAD)
    m = model()
    assert set(m) == {"name", "x-hardening", "services"}  # no networks, no volumes
    assert m["name"] == "tripbox"
    services = m["services"]
    assert list(services) == ["web", "worker", "bot"]
    assert services["bot"]["profiles"] == ["telegram"]
    assert [n for n, s in services.items() if "profiles" in s] == ["bot"]
    run = ["python", "-m", "tripbox"]
    assert services["worker"]["command"] == [*run, "work"]
    assert services["bot"]["command"] == [*run, "bot"]
    # inside its container the page listens on every interface, on the image's own port: a
    # published port reaches nothing on the container's loopback
    assert services["web"]["command"] == [
        *run, "web", "--host", "0.0.0.0", "--port", str(CONTAINER_PORT)
    ]  # fmt: skip
    assert services["web"]["depends_on"] == ["worker"]
    assert services["bot"]["depends_on"] == ["worker"]
    assert "depends_on" not in services["worker"]
    # the worker and the bot read the whole settings file; the page never does
    assert services["worker"]["env_file"] == ".env" and services["bot"]["env_file"] == ".env"
    assert source.count("<<: *hardening") == 3
    assert "TRIPBOX_PORT" not in source
    assert not re.search(r"schedul|supercronic|cron", source, re.IGNORECASE)


def test_the_page_receives_every_page_setting_and_nothing_else():
    """Every key the registry lets the page have, by name, in registry order: none for
    development, and not the image's own marker (the image sets it; a value from compose would
    replace it). The folders are written out; every other value is the setting from .env, with
    the registry's default where compose itself needs one (the zone, and where the port is
    published)."""
    environment = model()["services"]["web"]["environment"]
    wanted = [k for k in KEYS if k.web and k.feature != "dev" and k.name != IMAGE_ENV]
    assert list(environment) == [k.name for k in wanted]
    folders = {FOLDER_KEY[f] for f in FOLDERS["web"]}
    for key in wanted:
        if key.name in folders:
            assert environment[key.name] == key.default, key.name
        elif key.name in ("TZ", "TRIPBOX_LISTEN"):
            assert environment[key.name] == f"${{{key.name}:-{key.default}}}", key.name
        else:
            assert environment[key.name] == f"${{{key.name}:-}}", key.name
    assert web_service_leaks(text()) == []


def test_the_page_settings_pin_catches_each_way_of_handing_it_more():
    """The pin above, run on altered copies of the file: each way of handing the page more
    than its settings is caught."""
    source = text()
    command = (
        '    command: ["python", "-m", "tripbox", "web", "--host", "0.0.0.0", "--port", "8110"]\n'
    )
    origin = '      TRIPBOX_BASE_URL: "${TRIPBOX_BASE_URL:-}"\n'
    shared = "x-hardening: &hardening\n"
    volume = "      - type: bind\n        source: ./logs\n"
    for anchor in (command, origin, shared):
        assert source.count(anchor) == 1, anchor
    assert source.count(volume) == 3  # the page's, the worker's and the bot's

    def altered(old: str, new: str) -> list[str]:
        return web_service_leaks(source.replace(old, new))

    assert altered(command, command + "    env_file: .env\n") == ["env_file"]
    assert altered(command, command + "    env_file: [.env]\n") == ["env_file"]
    assert altered(command, command + "    env_file:\n      - path: .env\n") == ["env_file"]
    # the shared block merged into the page carries it too
    assert altered(shared, shared + "  env_file: .env\n") == ["env_file"]
    for value, name in (
        ("${TRIPBOX_TELEGRAM_BOT_TOKEN}", "TRIPBOX_TELEGRAM_BOT_TOKEN"),
        ("${TRIPBOX_MAIL_PASSWORD:-}", "TRIPBOX_MAIL_PASSWORD"),
        ("${TRIPBOX_LLM_API_KEY-x}", "TRIPBOX_LLM_API_KEY"),
        ("${TRIPBOX_AIRTRAIL_API_KEY:?missing}", "TRIPBOX_AIRTRAIL_API_KEY"),
        ("https://${TRIPBOX_AEROAPI_KEY:+x}", "TRIPBOX_AEROAPI_KEY"),
        ("$TRIPBOX_AIRTRAIL_URL", "TRIPBOX_AIRTRAIL_URL"),
        ("${HOSTNAME}", "HOSTNAME"),
        ("${TRIPBOX_BASE_URL:-${TRIPBOX_MAIL_USER}}", "TRIPBOX_MAIL_USER"),
        ("${TRIPBOX_PASSWORD}", "TRIPBOX_PASSWORD"),
    ):
        leak = altered(origin, f'      TRIPBOX_BASE_URL: "{value}"\n')
        assert leak == [f"TRIPBOX_BASE_URL: ${name}"], value
    assert altered(origin, '      TRIPBOX_BASE_URL: "$${TRIPBOX_MAIL_PASSWORD}"\n') == []
    assert altered(origin, '      TRIPBOX_BASE_URL: "$$$TRIPBOX_MAIL_USER"\n') == [
        "TRIPBOX_BASE_URL: $TRIPBOX_MAIL_USER"
    ]
    assert altered(origin, origin + '      TRIPBOX_MAIL_PASSWORD: "x"\n') == [
        "environment: TRIPBOX_MAIL_PASSWORD"
    ]
    # a page setting, so no leak; the list the test above pins leaves it out all the same
    assert altered(origin, origin + '      TRIPBOX_IN_IMAGE: "1"\n') == []
    # a secret interpolated anywhere else in the service
    secret = "TRIPBOX_MAIL_PASSWORD"
    assert altered(
        command, command.replace('"8110"]', f'"8110", "${{{secret}}}"]')
    ) == [f"command: ${secret}"]
    assert altered(command, command + f'    labels:\n      note: "${{{secret}:-}}"\n') == [
        f"labels: ${secret}"
    ]
    assert altered(volume, volume.replace("./logs", f"./${{{secret}}}")) == [
        f"volumes: ${secret}"
    ]
    # the login pair is the page's own, but only as its own settings
    assert altered(command, command + '    labels:\n      note: "${TRIPBOX_PASSWORD}"\n') == [
        "labels: $TRIPBOX_PASSWORD"
    ]


def test_the_printed_password_line_writes_a_private_file_the_page_refuses(tmp_path):
    """The quick start's settings line, run as printed, writes .env for its owner alone, and the
    password it holds is the example: the page refuses it until the user chooses their own."""
    (line,) = [ln for ln in HEAD.splitlines() if "TRIPBOX_PASSWORD=" in ln]
    command = line.removeprefix("#").strip()
    assert command == (
        "(umask 077 && echo 'TRIPBOX_PASSWORD=<choose a password of 10 characters or more>'"
        " > .env)"
    )
    subprocess.run(["sh", "-c", command], cwd=tmp_path, check=True, timeout=30)
    written = tmp_path / ".env"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    name, _, value = written.read_text().rstrip("\n").partition("=")
    assert name == "TRIPBOX_PASSWORD" and value.startswith("<") and value.endswith(">")
    s = Settings.from_env({name: value})
    assert s.feature("web").state == "invalid"
    assert [p.key for p in s.problems] == ["TRIPBOX_PASSWORD"]
    assert "still the example" in str(s.problems[0])


def test_the_pages_only_secrets_are_its_login_pair():
    """The page container's only secrets are its own login pair: it opens the page and nothing
    else (no mailbox, model, flight log or notifier reads it), and compose hands the page that
    pair by name, as it hands it every other page setting."""
    pair = [k for k in KEYS if k.web and k.secret]
    assert [k.name for k in pair] == list(PAGE_SECRETS)
    assert PAGE_SECRETS == ("TRIPBOX_PASSWORD", "TRIPBOX_PASSWORD_HASH")
    assert {k.feature for k in pair} == {"web"}
    assert [k.field for k in pair] == ["password", "password_hash"]
    web = model()["services"]["web"]
    secrets = [n for n in web["environment"] if KEY_BY_NAME.get(n) and KEY_BY_NAME[n].secret]
    assert secrets == list(PAGE_SECRETS)
    for name in PAGE_SECRETS:
        assert web["environment"][name] == f"${{{name}:-}}", name
    # only the page's own code and the settings read the pair
    src = ROOT / "src" / "tripbox"
    readers = sorted(
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if re.search(r"\.password(_hash)?\b", path.read_text(encoding="utf-8"))
    )
    assert readers == ["web/login.py"], readers
    # nor does the settings object show either value
    s = Settings.from_env({"TRIPBOX_PASSWORD": "S3CRET-PASSWORD-MARK"})
    assert s.password == "S3CRET-PASSWORD-MARK" and "S3CRET" not in repr(s)


def test_each_service_mounts_exactly_its_folders():
    """Bind mounts of the folders beside the file, each at the place the registry gives it,
    never created by compose (it would make a missing one root-owned): no named volume, no
    socket, no path outside the directory, and the raw email (`archive`) never in the page."""
    services = model()["services"]
    for name, folders in FOLDERS.items():
        expected = []
        for folder in folders:
            mount = {
                "type": "bind",
                "source": f"./{folder}",
                "target": KEY_BY_NAME[FOLDER_KEY[folder]].default,
                "bind": {"create_host_path": False},
            }
            if folder == "config":
                mount["read_only"] = True
            expected.append(mount)
        assert services[name]["volumes"] == expected, name
        assert all(m["target"] == f"/app/{f}" for m, f in zip(expected, folders, strict=True))
    assert "archive" not in FOLDERS["web"] and "backups" not in FOLDERS["web"]
    assert "backups" not in FOLDERS["bot"]
    assert "docker.sock" not in text() and ".." not in text()


def test_each_service_has_exactly_its_keys():
    """No service holds a key the pin does not name: what would reach past its container (another
    service's mounts, the Docker socket, the host's namespaces, devices or names, more
    capabilities or privileges) or hand the page its settings file is refused, as is a change of
    the shared block's security options."""
    source = text()
    assert shape_problems(source) == []
    command = (
        '    command: ["python", "-m", "tripbox", "web", "--host", "0.0.0.0", "--port", "8110"]\n'
    )
    assert source.count(command) == 1
    for line, key in (
        ("volumes_from: [worker]", "volumes_from"),
        ("use_api_socket: true", "use_api_socket"),
        ("privileged: true", "privileged"),
        ("cap_add: [SYS_ADMIN]", "cap_add"),
        ('devices: ["/dev/fuse"]', "devices"),
        ("pid: host", "pid"),
        ("ipc: host", "ipc"),
        ("network_mode: host", "network_mode"),
        ('extra_hosts: ["flightlog.example:192.0.2.10"]', "extra_hosts"),
        ('security_opt: ["seccomp:unconfined"]', "security_opt"),
        ("env_file: .env", "env_file"),
    ):
        altered = source.replace(command, f"{command}    {line}\n")
        assert shape_problems(altered) == [f"web: {key}"], line


def test_the_shared_block_carries_no_setting_mount_or_port():
    shared = model()["x-hardening"]
    assert shared == HARDENING
    assert list(shared) == list(HARDENING)


def test_every_service_is_hardened_restarts_and_is_capped():
    for name, svc in model()["services"].items():
        for key, value in HARDENING.items():
            assert svc[key] == value, (name, key)
        memory, tmpfs = LIMITS[name]
        assert (svc["mem_limit"], svc["tmpfs"]) == (memory, tmpfs), name


def test_a_settings_line_cannot_move_a_mount():
    """Each service is told where its folders are with the same literal paths it mounts them
    at, after the settings file, so a folder line in .env changes nothing; the mounts themselves
    interpolate nothing."""
    services = model()["services"]
    for name, folders in FOLDERS.items():
        environment = services[name]["environment"]
        told = {k: v for k, v in environment.items() if k in FOLDER_KEY.values()}
        assert told == {FOLDER_KEY[f]: f"/app/{f}" for f in folders}, name
        for mount in services[name]["volumes"]:
            assert "$" not in mount["source"] + mount["target"], name
    # the worker and the bot receive nothing else by name: their settings come from .env
    for name in ("worker", "bot"):
        assert set(services[name]["environment"]) == {FOLDER_KEY[f] for f in FOLDERS[name]}


def test_only_the_page_publishes_a_port_and_on_loopback_by_default():
    services = model()["services"]
    web = services["web"]
    published = f"${{TRIPBOX_LISTEN:-{LISTEN.default}}}"
    assert LISTEN.default == f"127.0.0.1:{CONTAINER_PORT}"
    assert web["ports"] == [f"{published}:{CONTAINER_PORT}"]
    assert web["environment"]["TRIPBOX_LISTEN"] == published
    assert web["command"][-4:] == ["--host", "0.0.0.0", "--port", str(CONTAINER_PORT)]
    for name, svc in services.items():
        assert "network_mode" not in svc and "expose" not in svc, name
        if name != "web":
            assert "ports" not in svc, name


def test_the_health_checks_are_these():
    def check(mode, interval, timeout, start):
        return {
            "test": ["CMD", "python", "/app/scripts/healthcheck.py", mode],
            "interval": interval,
            "timeout": timeout,
            "start_period": start,
            "retries": 3,
        }

    services = model()["services"]
    assert services["web"]["healthcheck"] == check("web", "30s", "10s", "30s")
    assert services["worker"]["healthcheck"] == check("worker", "30s", "10s", "30s")
    assert services["bot"]["healthcheck"] == check("bot", "2m", "20s", "1m")


def test_every_name_the_file_reads_is_a_setting_or_a_compose_name():
    """Every `$` in the file outside a comment (the header says how a `$` is written in .env)
    starts `${NAME:-default}`, NAME a setting or one of the compose file's own names, and the
    default either empty or the registry's."""
    source = "\n".join(line for line in text().splitlines() if not line.lstrip().startswith("#"))
    seen = set()
    for at in (m.start() for m in re.finditer(r"\$", source)):
        found = _DEFAULTED.match(source, at)
        assert found, source[at : at + 40]
        name, default = found.groups()
        if name in COMPOSE_NAMES:
            assert default == COMPOSE_NAMES[name], name
        else:
            assert name in KEY_BY_NAME, name
            assert default in ("", KEY_BY_NAME[name].default), name
        seen.add(name)
    assert set(COMPOSE_NAMES) <= seen


def test_the_file_uses_nothing_resolved_before_it_can_be_read():
    """No include, extends or label_file anywhere in the file (rendering consumes them, so a
    check of the rendered model never sees what they brought in), and no container_name, which
    would fix a name on the host; nor does any service reach past its own container."""
    source = text()
    for word in ("include", "extends", "label_file", "container_name"):
        assert not re.search(rf"\b{word}\b", source, re.ASCII), word
    m = model()
    for key in ("secrets", "configs", "networks", "volumes"):
        assert key not in m, key
    for name, svc in m["services"].items():
        for key in ("secrets", "configs", "privileged", "network_mode", "pid", "devices",
                    "cap_add", "build", "container_name", "ipc", "userns_mode", "volumes_from",
                    "use_api_socket", "extra_hosts"):
            assert key not in svc, (name, key)
