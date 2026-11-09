"""The installer for everything outside CloudStack (docs/installation.md).

`install.py` sits at the repository root and imports nothing but the
standard library, because its first job is to build the virtualenv the
rest of the engine lives in. It cannot depend on what it installs.

What it owns is the engine side: the virtualenv, the dependencies, the
database schema, the configuration file, and the reachability of
everything the engine talks to. What it deliberately does not own is
anything inside CloudStack — the plugin JAR, the UI section and the
configuration keys are an operator's to do, and the run ends by saying
so rather than pretending otherwise.
"""

from __future__ import annotations

import socket
import stat
from typing import TYPE_CHECKING

import pytest

import install

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def listening() -> Iterator[tuple[str, int]]:
    """A real socket to probe, so reachability is not faked."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    yield "127.0.0.1", int(server.getsockname()[1])
    server.close()


def settings(**kw: str) -> install.Settings:
    base = {
        "database_url": "postgresql+pg8000://aisvc:pw@db.example:5432/aisvc",
        "cloudstack_api_url": "https://cloud.example/client/api",
        "cloudstack_api_key": "key",
        "cloudstack_secret_key": "secret",
    }
    base.update(kw)
    return install.Settings(**base)


class TestPreflight:
    def test_this_python_passes(self) -> None:
        assert install.python_check().ok

    def test_an_older_python_is_refused_with_the_version_it_needs(self) -> None:
        check = install.python_check((3, 11, 9))

        assert not check.ok
        assert "3.12" in check.remedy

    def test_the_repository_root_is_recognised(self) -> None:
        assert install.repo_check(install.REPO_ROOT).ok

    def test_somewhere_else_is_not(self, tmp_path: Path) -> None:
        check = install.repo_check(tmp_path)

        assert not check.ok
        assert "repository root" in check.remedy


class TestEndpointParsing:
    """Reachability needs a host and a port out of whatever was typed."""

    def test_a_postgres_url_with_credentials(self) -> None:
        # The password must not be mistaken for part of the host.
        assert install.split_endpoint("postgresql+pg8000://u:p@db.example:5432/x") == (
            "db.example",
            5432,
        )

    def test_postgres_defaults_to_5432(self) -> None:
        assert install.split_endpoint("postgresql+pg8000://u:p@db.example/x") == (
            "db.example",
            5432,
        )

    def test_https_defaults_to_443(self) -> None:
        assert install.split_endpoint("https://cloud.example/client/api") == ("cloud.example", 443)

    def test_http_defaults_to_80(self) -> None:
        assert install.split_endpoint("http://minio.example/") == ("minio.example", 80)

    def test_an_explicit_port_wins(self) -> None:
        assert install.split_endpoint("http://minio.example:9000") == ("minio.example", 9000)

    def test_a_bare_host_and_port(self) -> None:
        assert install.split_endpoint("db.example:5432") == ("db.example", 5432)

    def test_sqlite_has_no_endpoint(self) -> None:
        # Probing a file URL as a socket would report a failure that
        # means nothing.
        assert install.split_endpoint("sqlite+pysqlite:////tmp/x.db") is None

    def test_nonsense_is_none_rather_than_an_exception(self) -> None:
        assert install.split_endpoint("") is None


class TestReachability:
    def test_an_open_port_is_reachable(self, listening: tuple[str, int]) -> None:
        host, port = listening

        assert install.reachable(host, port, timeout=2.0)

    def test_a_closed_port_is_not(self) -> None:
        # Port 1 on loopback: refused immediately, no waiting.
        assert not install.reachable("127.0.0.1", 1, timeout=2.0)

    def test_an_unresolvable_host_does_not_raise(self) -> None:
        # One unreachable dependency must not stop the other checks.
        assert not install.reachable("nx.invalid", 443, timeout=2.0)


class TestConnectivityChecks:
    def probe(self, *reachable_hosts: str) -> install.Probe:
        def answer(host: str, port: int, timeout: float) -> bool:
            return host in reachable_hosts

        return answer

    def test_a_reachable_dependency_passes(self) -> None:
        checks = install.connectivity_checks(settings(), self.probe("db.example", "cloud.example"))
        by_name = {check.name: check for check in checks}

        assert by_name["database"].ok
        assert by_name["cloudstack api"].ok

    def test_an_unreachable_one_fails_with_a_remedy(self) -> None:
        checks = install.connectivity_checks(settings(), self.probe())
        database = next(check for check in checks if check.name == "database")

        assert not database.ok
        assert database.remedy

    def test_what_was_not_configured_is_skipped_not_failed(self) -> None:
        # An operator who has not set up Prometheus yet has not failed
        # the install; autoscaling is opt-in.
        checks = install.connectivity_checks(settings(), self.probe())
        prometheus = next(check for check in checks if check.name == "prometheus")

        assert prometheus.skipped
        assert not prometheus.ok
        assert "autoscal" in prometheus.remedy.lower()

    def test_a_check_never_prints_a_password(self) -> None:
        checks = install.connectivity_checks(settings(), self.probe())

        for check in checks:
            assert "pw" not in check.detail.split("db.example")[0]
            assert "secret" not in check.detail


class TestTheEnvironmentFile:
    def test_it_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        install.write_env(path, settings())

        assert install.read_env(path)["AISVC_DATABASE_URL"] == (
            "postgresql+pg8000://aisvc:pw@db.example:5432/aisvc"
        )

    def test_an_unset_value_is_left_out_entirely(self, tmp_path: Path) -> None:
        # An empty variable is not the same as an unset one: the engine
        # treats "" as "not configured" in some places and as a value
        # in others, so it should simply not be there.
        path = tmp_path / ".env"
        install.write_env(path, settings())

        assert "AISVC_HF_TOKEN" not in path.read_text()

    def test_it_is_written_readable_only_by_its_owner(self, tmp_path: Path) -> None:
        # It holds the database password, the CloudStack secret key and
        # the Hugging Face token.
        path = tmp_path / ".env"
        install.write_env(path, settings(hf_token="hf_xxx"))

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_value_with_spaces_survives(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        install.write_env(path, settings(registry_bucket="two words"))

        assert install.read_env(path)["AISVC_REGISTRY_BUCKET"] == "two words"

    def test_reading_a_file_that_is_not_there_is_empty(self, tmp_path: Path) -> None:
        assert install.read_env(tmp_path / "nothing") == {}

    def test_comments_and_blank_lines_are_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("# a comment\n\nAISVC_NAMESPACE=aisvc\n")

        assert install.read_env(path) == {"AISVC_NAMESPACE": "aisvc"}


class TestSecretsNeverGetPrinted:
    def test_the_summary_redacts_every_secret(self) -> None:
        shown = install.describe(settings(hf_token="hf_xxx"))

        # The values only: a field is allowed to be called
        # cloudstack_secret_key, it just may not carry the key.
        printed = " ".join(value for name, value in shown.items() if name != "database")
        for secret in ("pw", "secret", "hf_xxx"):
            assert secret not in printed

    def test_the_database_url_keeps_its_shape_without_the_password(self) -> None:
        # An operator needs to recognise which database this is.
        shown = install.describe(settings())

        assert "db.example" in shown["database"]
        assert "pw" not in shown["database"]

    def test_every_secret_field_is_listed_as_one(self) -> None:
        # A field added to Settings and forgotten here would be printed
        # in full, which is how a token reaches a terminal recording.
        for field in ("cloudstack_secret_key", "cks_join_token", "hf_token", "database_url"):
            assert field in install.SECRET_FIELDS


class TestWhatIsAlreadyDone:
    """Every step is idempotent; a second run should report so."""

    def test_a_missing_virtualenv_is_work_to_do(self, tmp_path: Path) -> None:
        assert not install.venv_ready(tmp_path / ".venv")

    def test_an_existing_one_is_not(self, tmp_path: Path) -> None:
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")

        assert install.venv_ready(venv)

    def test_a_virtualenv_without_an_interpreter_is_not_ready(self, tmp_path: Path) -> None:
        # A half-built environment from an interrupted run.
        (tmp_path / ".venv" / "bin").mkdir(parents=True)

        assert not install.venv_ready(tmp_path / ".venv")


class TestTheCloudStackHandover:
    """What the installer will not do, said plainly at the end."""

    def test_it_names_the_plugin_the_ui_and_the_keys(self) -> None:
        todo = " ".join(install.cloudstack_todo()).lower()

        assert "plugin" in todo
        assert "ui" in todo
        assert "ai.services.enabled" in todo

    def test_it_says_where_the_written_guide_is(self) -> None:
        assert any("docs/installation.md" in line for line in install.cloudstack_todo())

    def test_it_does_not_claim_to_have_done_them(self) -> None:
        for line in install.cloudstack_todo():
            assert not line.lower().startswith("installed")


class TestTheRun:
    def plan(self, root: Path, **kw: object) -> install.Run:
        return install.Run(root=root, settings=settings(), dry_run=True, **kw)  # type: ignore[arg-type]

    def test_a_dry_run_creates_nothing(self, tmp_path: Path) -> None:
        run = self.plan(tmp_path)

        run.write_configuration()

        assert not (tmp_path / ".env").exists()

    def test_a_dry_run_still_says_what_it_would_do(self, tmp_path: Path) -> None:
        run = self.plan(tmp_path)

        run.write_configuration()

        assert any(".env" in line for line in run.actions)

    def test_a_real_run_writes_the_file(self, tmp_path: Path) -> None:
        run = install.Run(root=tmp_path, settings=settings(), dry_run=False)

        run.write_configuration()

        assert (tmp_path / ".env").exists()

    def test_writing_the_same_configuration_twice_is_reported_as_unchanged(
        self, tmp_path: Path
    ) -> None:
        run = install.Run(root=tmp_path, settings=settings(), dry_run=False)
        run.write_configuration()

        again = install.Run(root=tmp_path, settings=settings(), dry_run=False)
        again.write_configuration()

        assert any("unchanged" in line for line in again.actions)


class TestAnswersFromAFile:
    def test_an_answers_file_fills_the_settings(self, tmp_path: Path) -> None:
        path = tmp_path / "answers.json"
        path.write_text('{"database_url": "sqlite+pysqlite:////tmp/a.db", "namespace": "ai"}')

        loaded = install.load_answers(path)

        assert loaded["database_url"] == "sqlite+pysqlite:////tmp/a.db"
        assert loaded["namespace"] == "ai"

    def test_an_unknown_key_is_refused_rather_than_ignored(self, tmp_path: Path) -> None:
        # A typo in an automation file would otherwise leave the real
        # setting unset and the run looking successful.
        path = tmp_path / "answers.json"
        path.write_text('{"databse_url": "x"}')

        with pytest.raises(install.AnswerError) as caught:
            install.load_answers(path)

        assert "databse_url" in str(caught.value)

    def test_a_malformed_file_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "answers.json"
        path.write_text("{not json")

        with pytest.raises(install.AnswerError):
            install.load_answers(path)


class TestTheSystemdUnits:
    def test_the_api_unit_runs_serve(self) -> None:
        unit = install.systemd_unit("aisvc-api", install.REPO_ROOT, "serve")

        assert "aisvc serve" in unit
        assert str(install.REPO_ROOT) in unit

    def test_it_loads_the_environment_file(self) -> None:
        unit = install.systemd_unit("aisvc-api", install.REPO_ROOT, "serve")

        assert "EnvironmentFile=" in unit

    def test_the_batch_unit_warns_that_only_one_may_run(self) -> None:
        # There is no leader election in the batch worker.
        unit = install.systemd_unit("aisvc-batch", install.REPO_ROOT, "batch")

        assert "one" in unit.lower()
