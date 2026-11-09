#!/usr/bin/env python3
"""Install everything outside CloudStack (docs/installation.md).

    python3.12 install.py

**Standard library only, on purpose.** This script's first job is to
build the virtualenv the engine lives in, so it cannot import anything
from that virtualenv — including `aisvc` itself. Nothing here may grow
a third-party import.

What it owns:

- the virtualenv and the dependencies,
- the database schema, through Alembic rather than the models,
- the configuration file, written once with the right permissions,
- whether everything the engine talks to can actually be reached.

What it will not do, and says so at the end: anything inside
CloudStack. The plugin JAR, the UI section and the nine configuration
keys need a management server, a restart and decisions only an
operator can make. Pretending to have done them would be worse than
leaving them on a list.

Conventions it keeps, because the rest of the project keeps them:
every step is idempotent, a second run reports "unchanged" rather than
redoing work, and `--dry-run` writes nothing at all.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    #: How reachability is asked about. Injected in tests so the
    #: checks can be exercised without a network.
    Probe = Callable[[str, int, float], bool]
    #: How a question is put to an operator; `input` in a real run.
    Ask = Callable[[str], str]

__all__ = [
    "DATABASE_ENGINES",
    "DEFAULT_ENGINE",
    "MINIMUM_PYTHON",
    "REPO_ROOT",
    "SECRET_FIELDS",
    "AnswerError",
    "Check",
    "Probe",
    "Run",
    "Settings",
    "cloudstack_todo",
    "connectivity_checks",
    "database_note",
    "database_url",
    "describe",
    "engine_of",
    "extras_for",
    "load_answers",
    "python_check",
    "reachable",
    "read_env",
    "repo_check",
    "split_endpoint",
    "systemd_unit",
    "venv_ready",
    "write_env",
]

REPO_ROOT = Path(__file__).resolve().parent

MINIMUM_PYTHON = (3, 12)

#: Seconds to wait on a TCP connect. Short: this runs over every
#: dependency, and an unreachable one should not hold up the rest.
PROBE_TIMEOUT = 4.0

#: The two backends ADR-0004 supports, each with its driver and port.
#: The driver matters: the obvious MariaDB package name is LGPL, which
#: is Category X for an ASF project, so PyMySQL (MIT) is used instead.
DATABASE_ENGINES = {
    "postgres": {
        "scheme": "postgresql+pg8000",
        "port": 5432,
        "extra": "",
        "label": "PostgreSQL (recommended; its own server)",
    },
    "mariadb": {
        "scheme": "mysql+pymysql",
        "port": 3306,
        "extra": "mariadb",
        "label": "MariaDB or MySQL (can share the server CloudStack uses)",
    },
}

DEFAULT_ENGINE = "postgres"

#: Default ports, so an operator can type a hostname and be understood.
SCHEME_PORTS = {
    "http": 80,
    "https": 443,
    "postgresql": 5432,
    "postgres": 5432,
    "mysql": 3306,
    "mariadb": 3306,
}

#: Never printed, never echoed back, never put in a check's detail.
SECRET_FIELDS = frozenset(
    {
        "database_url",
        "cloudstack_api_key",
        "cloudstack_secret_key",
        "cks_join_token",
        "hf_token",
    }
)


class AnswerError(Exception):
    """An answers file could not be used."""


@dataclass(frozen=True, slots=True)
class Check:
    """One thing that was looked at, and what to do if it is wrong."""

    name: str
    ok: bool
    detail: str
    remedy: str = ""
    #: Not configured rather than broken. Not a failure: several of
    #: these are genuinely optional, and reporting them as failures
    #: teaches an operator to ignore the report.
    skipped: bool = False

    @property
    def mark(self) -> str:
        if self.skipped:
            return "--"
        return "ok" if self.ok else "!!"


@dataclass
class Settings:
    """Everything the engine reads from its environment.

    The names match the variables in `docs/installation.md`; the field
    names are their lower-case tails, which is also what an answers
    file uses.
    """

    database_url: str = ""
    cloudstack_api_url: str = ""
    cloudstack_api_key: str = ""
    cloudstack_secret_key: str = ""
    cks_join_endpoint: str = ""
    cks_join_token: str = ""
    cks_ca_cert_hash: str = ""
    registry_bucket: str = "models"
    registry_endpoint: str = ""
    gateway_url: str = ""
    hf_token: str = ""
    hf_endpoint: str = ""
    prometheus_url: str = ""
    namespace: str = "aisvc"
    tls_cert: str = ""
    tls_key: str = ""
    tls_client_ca: str = ""
    spec_path: str = "examples/platform.yaml"


#: Field name to environment variable. `CLOUDSTACK_*` are the
#: CloudStack client's own names; everything else is `AISVC_*`.
ENV_NAMES = {
    "database_url": "AISVC_DATABASE_URL",
    "cloudstack_api_url": "CLOUDSTACK_API_URL",
    "cloudstack_api_key": "CLOUDSTACK_API_KEY",
    "cloudstack_secret_key": "CLOUDSTACK_SECRET_KEY",
    "cks_join_endpoint": "AISVC_CKS_JOIN_ENDPOINT",
    "cks_join_token": "AISVC_CKS_JOIN_TOKEN",
    "cks_ca_cert_hash": "AISVC_CKS_CA_CERT_HASH",
    "registry_bucket": "AISVC_REGISTRY_BUCKET",
    "registry_endpoint": "AISVC_REGISTRY_ENDPOINT",
    "gateway_url": "AISVC_GATEWAY_URL",
    "hf_token": "AISVC_HF_TOKEN",
    "hf_endpoint": "AISVC_HF_ENDPOINT",
    "prometheus_url": "AISVC_PROMETHEUS_URL",
    "namespace": "AISVC_NAMESPACE",
    "tls_cert": "AISVC_TLS_CERT",
    "tls_key": "AISVC_TLS_KEY",
    "tls_client_ca": "AISVC_TLS_CLIENT_CA",
    "spec_path": "AISVC_SPEC",
}

#: What each one is, for the prompts and the generated file.
PROMPTS = {
    "database_url": "Database URL (postgresql+pg8000://… or mysql+pymysql://…)",
    "cloudstack_api_url": "CloudStack API URL",
    "cloudstack_api_key": "CloudStack API key",
    "cloudstack_secret_key": "CloudStack secret key",
    "cks_join_endpoint": "CKS join endpoint",
    "cks_join_token": "CKS join token",
    "cks_ca_cert_hash": "CKS CA certificate hash",
    "registry_bucket": "Object store bucket",
    "registry_endpoint": "Object store endpoint (blank for AWS S3)",
    "gateway_url": "Gateway base URL",
    "hf_token": "Hugging Face token (blank if none)",
    "hf_endpoint": "Hugging Face endpoint (blank for the public Hub)",
    "prometheus_url": "Prometheus URL (blank disables autoscaling)",
    "namespace": "Kubernetes namespace",
    "tls_cert": "Path to the engine's TLS certificate",
    "tls_key": "Path to its key",
    "tls_client_ca": "Path to the CA that signs client certificates",
    "spec_path": "Path to platform.yaml",
}

#: Asked for, but an install is still usable without them.
OPTIONAL_FIELDS = frozenset(
    {
        "registry_endpoint",
        "gateway_url",
        "hf_token",
        "hf_endpoint",
        "prometheus_url",
        "tls_cert",
        "tls_key",
        "tls_client_ca",
    }
)

# -- preflight -------------------------------------------------------------


def python_check(version: tuple[int, ...] | None = None) -> Check:
    """Whether this interpreter can run the engine."""
    found = version or sys.version_info[:3]
    spelled = ".".join(str(part) for part in found)
    if tuple(found[:2]) >= MINIMUM_PYTHON:
        return Check("python", True, f"{spelled}")
    return Check(
        "python",
        False,
        f"{spelled}",
        remedy="the engine needs Python 3.12 or newer; install it and run this script with it",
    )


def repo_check(root: Path) -> Check:
    """Whether this is the repository, which every path here assumes."""
    if (root / "pyproject.toml").is_file() and (root / "aisvc").is_dir():
        return Check("repository", True, str(root))
    return Check(
        "repository",
        False,
        str(root),
        remedy="run this from the repository root, where pyproject.toml and aisvc/ are",
    )


# -- reachability ----------------------------------------------------------


def split_endpoint(url: str) -> tuple[str, int] | None:
    """The host and port a URL points at, or None if it names no socket.

    Handles the three shapes an operator actually types: a full URL, a
    SQLAlchemy URL with credentials in it, and a bare `host:port`.
    """
    if not url:
        return None

    parsed = urlsplit(url if "//" in url else f"//{url}")
    scheme = parsed.scheme.split("+", 1)[0].lower()

    if scheme == "sqlite" or (not parsed.hostname and not parsed.port):
        return None

    host = parsed.hostname
    if not host:
        return None

    port = parsed.port or SCHEME_PORTS.get(scheme)
    if port is None:
        return None
    return host, int(port)


def reachable(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether a TCP connection opens. Never raises.

    A connect is all this can honestly check without the drivers the
    virtualenv has not installed yet. It catches the common install
    failure — a firewall, a typo, the wrong port — and says nothing
    about credentials, which the first real call will.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def connectivity_checks(config: Settings, probe: Probe | None = None) -> list[Check]:
    """Reach every dependency that was configured, skip the rest."""
    look = probe or reachable

    wanted = [
        ("database", config.database_url, "Postgres is not answering", True),
        (
            "cloudstack api",
            config.cloudstack_api_url,
            "the management server is not answering",
            True,
        ),
        ("object store", config.registry_endpoint, "the object store is not answering", False),
        ("kubernetes", config.cks_join_endpoint, "the cluster API is not answering", False),
        ("gateway", config.gateway_url, "the gateway is not answering", False),
        ("prometheus", config.prometheus_url, "Prometheus is not answering", False),
    ]

    checks: list[Check] = []
    for name, value, failure, required in wanted:
        if not value:
            checks.append(
                Check(name, False, "not configured", remedy=_skip_remedy(name), skipped=True)
            )
            continue

        endpoint = split_endpoint(value)
        if endpoint is None:
            checks.append(
                Check(name, True, "not a network address; nothing to probe", skipped=False)
            )
            continue

        host, port = endpoint
        where = f"{host}:{port}"
        if look(host, port, PROBE_TIMEOUT):
            checks.append(Check(name, True, where))
        else:
            checks.append(
                Check(
                    name,
                    False,
                    f"{failure} at {where}",
                    remedy=(
                        f"check the address, the firewall and that the service is up"
                        f"{'' if required else '; this one is optional'}"
                    ),
                )
            )
    return checks


def _skip_remedy(name: str) -> str:
    if name == "prometheus":
        return (
            "set AISVC_PROMETHEUS_URL to enable autoscaling; without it replicas hold at the floor"
        )
    if name == "object store":
        return "leave unset for AWS S3; set it for Ceph RGW or MinIO"
    return f"configure {name} before the engine will be able to use it"


# -- configuration ---------------------------------------------------------


def describe(config: Settings) -> dict[str, str]:
    """The configuration as it may be shown to a human.

    Secrets are replaced, not truncated. The database URL keeps its
    shape without its password, because an operator needs to recognise
    which database this is.
    """
    shown: dict[str, str] = {}
    for item in fields(config):
        value = getattr(config, item.name)
        if not value:
            continue
        if item.name == "database_url":
            shown["database"] = _without_password(value)
        elif item.name in SECRET_FIELDS:
            shown[item.name] = "set (not shown)"
        else:
            shown[item.name] = value
    return shown


def _without_password(url: str) -> str:
    """A URL with its password replaced, for a log line or a summary."""
    if "@" not in url:
        return url
    prefix, _, host = url.rpartition("@")
    scheme, sep, credentials = prefix.partition("://")
    if not sep:
        return f"***@{host}"
    user = credentials.partition(":")[0]
    return f"{scheme}://{user}:***@{host}"


def render_env(config: Settings) -> str:
    """The .env file's contents.

    A value that was never set is left out rather than written empty:
    the engine treats an unset variable and an empty one differently
    in places, and an empty line invites someone to "fix" it.
    """
    lines = [
        "# Written by install.py. Secrets live here, so it is mode 0600",
        "# and git-ignored. See docs/installation.md.",
    ]
    for item in fields(config):
        value = str(getattr(config, item.name))
        if not value:
            continue
        lines.append(f"{ENV_NAMES[item.name]}={value}")
    return "\n".join(lines) + "\n"


def write_env(path: Path, config: Settings) -> None:
    """Write the file, readable only by its owner."""
    path.write_text(render_env(config))
    path.chmod(0o600)


def read_env(path: Path) -> dict[str, str]:
    """Read a .env file back. Missing file reads as empty."""
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        found[name.strip()] = value.strip()
    return found


def load_answers(path: Path) -> dict[str, str]:
    """Settings from a JSON file, for a scripted install.

    An unknown key is an error rather than something to ignore: a typo
    would otherwise leave the real setting unset and the run looking
    like it worked.
    """
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AnswerError(f"could not read {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise AnswerError(f"{path} should hold a JSON object of settings")

    known = {item.name for item in fields(Settings)}
    unknown = sorted(set(loaded) - known)
    if unknown:
        raise AnswerError(
            f"{path} has settings that do not exist: {', '.join(unknown)}. "
            f"Known settings: {', '.join(sorted(known))}"
        )
    return {name: str(value) for name, value in loaded.items()}


def database_url(
    engine: str,
    *,
    host: str,
    name: str,
    user: str,
    password: str,
    port: int | None = None,
) -> str:
    """Build a SQLAlchemy URL for one of the supported backends.

    The user and password are percent-encoded: a password holding `@`
    or `/` would otherwise be parsed as part of the host, and the
    failure reads as "could not resolve <half your password>".
    """
    chosen = DATABASE_ENGINES.get(engine)
    if chosen is None:
        known = ", ".join(sorted(DATABASE_ENGINES))
        raise AnswerError(f"{engine!r} is not a supported database; choose one of {known}")

    where = f"{host}:{port}" if port else f"{host}:{chosen['port']}"
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}" if user else ""
    at = "@" if credentials else ""
    url = f"{chosen['scheme']}://{credentials}{at}{where}/{name}"

    if engine == "mariadb":
        # Without this MariaDB may default to latin1 and mangle the
        # first non-ASCII model name or domain path it is given.
        url = f"{url}?charset=utf8mb4"
    return url


def engine_of(url: str) -> str:
    """Which backend a URL names, or "" for neither.

    A rerun reads the URL back out of .env, so the driver follows from
    it without asking the operator the same question twice.
    """
    if not url or "://" not in url:
        return ""
    scheme = url.split("://", 1)[0].lower()
    base = scheme.split("+", 1)[0]
    for name, chosen in DATABASE_ENGINES.items():
        if scheme == chosen["scheme"] or base == str(chosen["scheme"]).split("+", 1)[0]:
            return name
    if base in {"mysql", "mariadb"}:
        return "mariadb"
    return ""


def extras_for(engine: str) -> list[str]:
    """Which pip extras this backend needs."""
    chosen = DATABASE_ENGINES.get(engine, {})
    extra = str(chosen.get("extra", ""))
    return [extra] if extra else []


def database_note(engine: str) -> str:
    """Anything worth saying about this choice, once it is made."""
    if engine != "mariadb":
        return ""
    return (
        "MariaDB selected. Installing the PyMySQL driver (MIT); the package named "
        "'mariadb' is LGPL and must not be used. Sharing CloudStack's own server is "
        "possible but see ADR-0004 (docs/adr/0004-state-store.md): the gateway writes "
        "a usage row per request, so a separate server is still recommended."
    )


def venv_ready(venv: Path) -> bool:
    """Whether a usable virtualenv is already there.

    An interrupted run can leave the directory without an interpreter,
    which is not ready and must not be mistaken for ready.
    """
    return (venv / "bin" / "python").exists()


# -- the handover ----------------------------------------------------------


def cloudstack_todo() -> list[str]:
    """What this script will not do, and nobody should assume it did."""
    return [
        "Build the CloudStack modules from source and then the plugin JAR "
        "(java-plugin/README.md); CloudStack publishes no Maven artifacts.",
        "Deploy the JAR into the management server's classpath and restart it.",
        "Set the nine configuration keys, starting with ai.services.enabled "
        "for the zone and the three engine TLS keys.",
        "Copy the UI section into CloudStack's ui/ tree and register it in "
        "src/config/router.js, then rebuild the UI.",
        "Mount gateway-hooks/ into the LiteLLM proxy and point the gateway at it.",
        "Then: docs/installation.md has the whole sequence, including what "
        "to run for the first zone.",
    ]


def systemd_unit(name: str, root: Path, command: str) -> str:
    """A unit file for one long-running engine command."""
    note = ""
    if command == "batch":
        note = (
            "# The batch worker has no leader election: run exactly one\n"
            "# instance of this across the whole platform.\n"
        )
    return (
        f"{note}[Unit]\n"
        f"Description=CloudStack AI Services ({command})\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={root}\n"
        f"EnvironmentFile={root / '.env'}\n"
        f"ExecStart={root / '.venv' / 'bin' / 'aisvc'} {command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


# -- the run ---------------------------------------------------------------


@dataclass
class Run:
    """One pass of the installer over one repository."""

    root: Path
    settings: Settings
    dry_run: bool = False
    dev: bool = False
    actions: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def venv(self) -> Path:
        return self.root / ".venv"

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    def say(self, line: str) -> None:
        self.actions.append(line)
        print(f"  {line}")

    def run_command(self, argv: Sequence[str], *, env: dict[str, str] | None = None) -> bool:
        """Run a command, or say what would have been run."""
        printable = " ".join(argv)
        if self.dry_run:
            self.say(f"would run: {printable}")
            return True

        self.say(f"running: {printable}")
        merged = {**os.environ, **(env or {})}
        completed = subprocess.run(argv, cwd=self.root, env=merged, check=False)
        if completed.returncode != 0:
            self.failures.append(f"{printable} exited {completed.returncode}")
            return False
        return True

    # -- steps --

    def create_venv(self) -> bool:
        if venv_ready(self.venv):
            self.say(f"{self.venv.name}/ is already there — unchanged")
            return True
        if self.dry_run:
            self.say(f"would create {self.venv}")
            return True
        return self.run_command([sys.executable, "-m", "venv", str(self.venv)])

    def install_target(self) -> str:
        """`.` plus whichever extras this install needs.

        The database extra follows the URL rather than a separate
        answer, so a rerun against an existing .env installs the right
        driver without being told again.
        """
        extras = (["dev"] if self.dev else []) + extras_for(engine_of(self.settings.database_url))
        return f".[{','.join(extras)}]" if extras else "."

    def install_package(self, *, dev: bool | None = None) -> bool:
        if dev is not None:
            self.dev = dev
        if self.dry_run and not venv_ready(self.venv):
            self.say("would install the engine into the virtualenv")
            return True
        return self.run_command(
            [
                str(self.venv / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "-q",
                "-e",
                self.install_target(),
            ]
        )

    def write_configuration(self) -> bool:
        wanted = render_env(self.settings)
        if self.env_path.is_file() and self.env_path.read_text() == wanted:
            self.say(f"{self.env_path.name} is unchanged")
            return True
        if self.dry_run:
            self.say(f"would write {self.env_path} (mode 0600)")
            return True
        write_env(self.env_path, self.settings)
        self.say(f"wrote {self.env_path} (mode 0600)")
        return True

    def migrate(self) -> bool:
        """Bring the schema up, through Alembic.

        Before anything else touches the database — `aisvc serve` runs
        as several replicas and deliberately does not migrate, and a
        database built any other way cannot be managed afterwards.
        """
        if not self.settings.database_url:
            self.say("no database configured — skipping the migration")
            return True
        return self.run_command(
            [str(self.venv / "bin" / "alembic"), "upgrade", "head"],
            env={"AISVC_DATABASE_URL": self.settings.database_url},
        )

    def validate_spec(self) -> bool:
        spec = self.root / self.settings.spec_path
        if not self.dry_run and not spec.is_file():
            self.say(f"no spec at {spec} — skipping validation")
            return True
        return self.run_command([str(self.venv / "bin" / "aisvc"), "validate", "--spec", str(spec)])

    def write_units(self, directory: Path) -> bool:
        for name, command in (("aisvc-api", "serve"), ("aisvc-batch", "batch")):
            path = directory / f"{name}.service"
            body = systemd_unit(name, self.root, command)
            if path.is_file() and path.read_text() == body:
                self.say(f"{path.name} is unchanged")
                continue
            if self.dry_run:
                self.say(f"would write {path}")
                continue
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            self.say(f"wrote {path}")
        return True


# -- prompting -------------------------------------------------------------


def ask_for_settings(
    defaults: Settings,
    ask: Ask | None = None,
    *,
    only_missing: bool = False,
) -> Settings:
    """Walk an operator through the settings.

    A blank answer keeps the default, so a second run is a matter of
    pressing return through what is already right.
    """
    prompt = ask or input
    answers: dict[str, str] = {}

    for item in fields(defaults):
        current = str(getattr(defaults, item.name))
        if only_missing and current:
            answers[item.name] = current
            continue

        shown = (
            "(set, press return to keep)" if (current and item.name in SECRET_FIELDS) else current
        )
        suffix = f" [{shown}]" if shown else ""
        optional = " (optional)" if item.name in OPTIONAL_FIELDS else ""
        given = prompt(f"{PROMPTS[item.name]}{optional}{suffix}: ").strip()
        answers[item.name] = given or current

    return Settings(**answers)


def settings_from_environment(defaults: Settings) -> Settings:
    """Pick up anything already exported, so a rerun keeps it."""
    answers = {
        item.name: os.environ.get(ENV_NAMES[item.name], "") or str(getattr(defaults, item.name))
        for item in fields(defaults)
    }
    return Settings(**answers)


def settings_from_env_file(path: Path, defaults: Settings) -> Settings:
    """Pick up a previous run's answers."""
    stored = read_env(path)
    answers = {
        item.name: stored.get(ENV_NAMES[item.name], "") or str(getattr(defaults, item.name))
        for item in fields(defaults)
    }
    return Settings(**answers)


# -- reporting -------------------------------------------------------------


def print_checks(title: str, checks: Iterable[Check]) -> bool:
    """Print a block of checks; return whether any hard one failed."""
    print(f"\n{title}")
    failed = False
    for check in checks:
        print(f"  [{check.mark}] {check.name}: {check.detail}")
        if check.remedy and not check.ok:
            print(f"       {check.remedy}")
        if not check.ok and not check.skipped:
            failed = True
    return failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install the CloudStack AI Services engine and its dependencies. "
        "Everything inside CloudStack is left to the operator and listed at the end.",
    )
    parser.add_argument("--answers", type=Path, help="JSON file of settings; implies --yes")
    parser.add_argument("--yes", action="store_true", help="do not prompt")
    parser.add_argument("--dry-run", action="store_true", help="write nothing; say what would run")
    parser.add_argument("--dev", action="store_true", help="install the test dependencies too")
    parser.add_argument(
        "--database",
        choices=sorted(DATABASE_ENGINES),
        help="which backend the engine's own state lives in (default: taken from the "
        "URL you give, else postgres). See docs/adr/0004-state-store.md.",
    )
    parser.add_argument("--units", type=Path, help="write systemd unit files into this directory")
    parser.add_argument(
        "--skip-migration", action="store_true", help="do not run the database migration"
    )
    args = parser.parse_args(argv)

    print("CloudStack AI Services — installing the parts outside CloudStack\n")

    preflight = [python_check(), repo_check(REPO_ROOT)]
    if print_checks("Preflight", preflight):
        print("\nStopped: the preflight checks above have to pass first.")
        return 1

    # Earlier answers win over the built-in defaults, and an exported
    # variable wins over both: a rerun should not undo a change.
    config = settings_from_env_file(REPO_ROOT / ".env", Settings())
    config = settings_from_environment(config)

    if args.answers:
        try:
            config = Settings(
                **{
                    **{f.name: getattr(config, f.name) for f in fields(config)},
                    **load_answers(args.answers),
                }
            )
        except AnswerError as exc:
            print(f"\n{exc}")
            return 1
    elif not args.yes:
        if not config.database_url and args.database is None:
            print("\nWhere should the engine keep its own state?")
            for name in sorted(DATABASE_ENGINES):
                marker = " (default)" if name == DEFAULT_ENGINE else ""
                print(f"  {name}: {DATABASE_ENGINES[name]['label']}{marker}")
            chosen = input(f"Database [{DEFAULT_ENGINE}]: ").strip() or DEFAULT_ENGINE
            if chosen not in DATABASE_ENGINES:
                print(f"\n{chosen!r} is not one of {', '.join(sorted(DATABASE_ENGINES))}")
                return 1
            args.database = chosen
        config = ask_for_settings(config)

    # An explicit --database with no URL yet gets a template to edit.
    chosen_engine = args.database or engine_of(config.database_url) or DEFAULT_ENGINE
    note = database_note(chosen_engine)
    if note:
        print(f"\n{note}")

    print("\nConfiguration")
    for name, value in describe(config).items():
        print(f"  {name}: {value}")

    run = Run(root=REPO_ROOT, settings=config, dry_run=args.dry_run, dev=args.dev)

    print("\nInstalling")
    steps: list[Callable[[], bool]] = [
        run.create_venv,
        run.install_package,
        run.write_configuration,
    ]
    if not args.skip_migration:
        steps.append(run.migrate)
    steps.append(run.validate_spec)
    if args.units:
        steps.append(lambda: run.write_units(args.units))

    for step in steps:
        if not step():
            break

    print_checks("Reachability", connectivity_checks(config))

    if run.failures:
        print("\nFailed:")
        for failure in run.failures:
            print(f"  - {failure}")
        return 1

    print("\nDone." if not args.dry_run else "\nDry run — nothing was written.")
    print("\nStill yours to do, inside CloudStack:")
    for line in cloudstack_todo():
        print(f"  - {line}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
