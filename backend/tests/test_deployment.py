"""Deployment concerns (L41), asserted rather than described.

The ones that matter:

  * `test_the_production_overlay_publishes_only_nginx` -- the defect this level
    found. `ports: []` does NOT remove a base file's publish.
  * `test_the_nginx_api_block_forwards_the_websocket_upgrade` -- the other one.
    Without it the realtime feed is dead behind the proxy.
  * `test_an_unstamped_build_says_so` -- a release you cannot identify is one
    you cannot roll back to.
  * `test_no_committed_deployment_file_contains_a_literal_secret`.
  * `test_the_production_overlay_does_not_enable_live_trading`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from app.core import release
from app.core.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
OVERRIDE = ROOT / "docker-compose.override.yml"
PROD = ROOT / "docker-compose.prod.yml"
NGINX_DEV = ROOT / "nginx" / "nginx.conf"
NGINX_PROD = ROOT / "nginx" / "nginx.prod.conf"
DOCKERFILE = ROOT / "backend" / "Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ============================================================ 1. the port defect


def test_the_base_compose_publishes_nothing() -> None:
    """The whole reason the override file exists.

    Compose merges `ports` by APPENDING, so an overlay cannot remove a publish
    the base file declares -- `ports: []` in the production file left
    PostgreSQL on 5440, Redis on 6390 and the API on 8000, while the file's own
    comment said otherwise. Safe-by-default in the base is the only arrangement
    an overlay can actually build on.
    """
    for name, service in _yaml(COMPOSE)["services"].items():
        assert not service.get("ports"), f"{name} publishes a port in the base file"


def test_the_development_override_restores_every_port() -> None:
    """Moving them out must not change what a developer gets."""
    published = {
        name: {str(p).split(":")[-1] for p in service.get("ports", [])}
        for name, service in _yaml(OVERRIDE)["services"].items()
    }
    assert published["postgres"] == {"5432"}
    assert published["redis"] == {"6379"}
    assert published["api"] == {"8000"}
    assert published["nginx"] == {"80"}
    # All on loopback. None of them is reachable from the network even in
    # development.
    for service in _yaml(OVERRIDE)["services"].values():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), port


def test_the_production_overlay_publishes_only_nginx() -> None:
    """Read from the file rather than the merged config, which needs Docker.

    `test_the_base_compose_publishes_nothing` is the other half: together they
    mean the merged production configuration can only contain nginx's ports,
    because there is nothing else to merge.
    """
    for name, service in _yaml(PROD)["services"].items():
        ports = service.get("ports") or []
        if name == "nginx":
            assert {str(p).split(":")[-1] for p in ports} == {"80", "443"}
        else:
            assert not ports, f"{name} publishes {ports} in production"


# ====================================================== 2. the websocket defect


def _location_block(config: str, path: str) -> str:
    """The body of one `location` block, by brace matching.

    A regex over the whole file would match an `Upgrade` header in a different
    block and report the proxy as correct when it is not -- which is exactly
    the shape of the defect being tested for, since `location /` had the
    headers all along.
    """
    # The brace matters: `location /api/` is a prefix of
    # `location /api/v1/webhooks/`, so a bare `index` finds the webhook
    # block instead -- which has no upgrade headers and should not.
    start = config.index(f"location {path} {{")
    depth = 0
    for i in range(start, len(config)):
        if config[i] == "{":
            depth += 1
        elif config[i] == "}":
            depth -= 1
            if depth == 0:
                return config[start : i + 1]
    raise AssertionError(f"unterminated location {path}")


@pytest.mark.parametrize("path", [NGINX_DEV, NGINX_PROD])
def test_the_nginx_api_block_forwards_the_websocket_upgrade(path: Path) -> None:
    """The realtime socket is served at /v1/realtime/ws, behind /api/.

    Before L41 these three lines were on `location /` (the frontend) and not on
    `location /api/`, so nginx answered the handshake itself instead of
    forwarding it. Reproduced at L41 against a running stack: HTTP 404 with the
    old config, HTTP 101 with this one. Every live update in the platform --
    orders, positions, notifications, health -- was affected.
    """
    block = _location_block(path.read_text(encoding="utf-8"), "/api/")
    assert "proxy_http_version 1.1" in block
    assert "proxy_set_header Upgrade $http_upgrade" in block
    assert "proxy_set_header Connection $connection_upgrade" in block


@pytest.mark.parametrize("path", [NGINX_DEV, NGINX_PROD])
def test_the_proxy_caps_the_body_at_the_gateways_own_limit(path: Path) -> None:
    """64 KB matches the webhook gateway. Rejecting it here means an oversized
    body never occupies an application worker."""
    config = path.read_text(encoding="utf-8")
    assert "client_max_body_size 64k;" in config
    assert "server_tokens off;" in config


@pytest.mark.parametrize("path", [NGINX_DEV, NGINX_PROD])
def test_the_webhook_is_rate_limited_more_tightly_than_the_api(path: Path) -> None:
    """TradingView sends one alert per signal; a burst is a retry storm."""
    config = path.read_text(encoding="utf-8")
    api = re.search(r"zone=api:\S+\s+rate=(\d+)r/s", config)
    webhook = re.search(r"zone=webhook:\S+\s+rate=(\d+)r/s", config)
    assert api and webhook
    assert int(webhook.group(1)) < int(api.group(1))


def test_production_terminates_tls_and_never_proxies_plain_http() -> None:
    """Port 80 exists only to redirect and to answer the ACME challenge.

    A plain-HTTP path to the API is a plain-HTTP path to a session cookie,
    whatever the redirect says.
    """
    config = NGINX_PROD.read_text(encoding="utf-8")
    assert "listen 443 ssl;" in config
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
    # No TLS 1.0/1.1 anywhere.
    assert "TLSv1.1" not in config and "TLSv1 " not in config
    # The :80 server block proxies nothing to the api.
    plain = config[config.index("listen 80;") : config.index("listen 443")]
    assert "proxy_pass http://api" not in plain
    assert "return 301 https://" in plain


# ============================================================ 3. release identity


def test_an_unstamped_build_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """`"unknown"`, never a guess and never a stale inherited value."""
    for name in (release.ENV_COMMIT, release.ENV_BUILD_TIME, release.ENV_RELEASE):
        monkeypatch.delenv(name, raising=False)
    release.current.cache_clear()
    built = release.current("0.2.0")
    assert built.commit == release.UNKNOWN
    assert built.built_at == release.UNKNOWN
    assert built.stamped is False
    release.current.cache_clear()


def test_a_stamped_build_reports_its_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(release.ENV_COMMIT, "c961abfc1667a53797238fad2f99be6e5779f681")
    monkeypatch.setenv(release.ENV_BUILD_TIME, "2026-09-05T14:56:35Z")
    monkeypatch.setenv(release.ENV_RELEASE, "0.41.0")
    release.current.cache_clear()
    built = release.current("0.2.0")
    assert built.stamped is True
    assert built.short_commit == "c961abfc1667"
    assert built.version == "0.41.0"
    release.current.cache_clear()


def test_release_never_shells_out_to_git() -> None:
    """A production container has no .git and no git binary.

    A version function that tries and fails is one that raises inside a health
    check.
    """
    source = (ROOT / "backend" / "app" / "core" / "release.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "rev-parse", "os.system", "popen"):
        assert forbidden not in source.lower()


def test_the_schema_head_is_read_from_alembic_not_hard_coded() -> None:
    """A version that claims a migration state it does not have is worse than
    one that says it does not know."""
    head = release.schema_head()
    assert head != release.UNKNOWN
    versions = sorted((ROOT / "backend" / "alembic" / "versions").glob("[0-9]*.py"))
    assert head.startswith(versions[-1].stem.split("_")[0])


def test_the_release_payload_is_a_fixed_set_of_fields() -> None:
    """No passthrough of arbitrary environment variables -- that is how a
    version endpoint ends up reporting a connection string."""
    described = release.describe(Settings(_env_file=None))
    assert set(described) == {
        "service",
        "version",
        "commit",
        "built_at",
        "stamped",
        "schema_head",
    }


def test_the_dockerfile_stamps_the_release() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    for name in (release.ENV_COMMIT, release.ENV_BUILD_TIME, release.ENV_RELEASE):
        assert f"ARG {name}" in text
        assert f"ENV {name}=" in text


# ====================================================== 4. workers and safety


def test_the_api_and_the_worker_split_the_background_work() -> None:
    """Step 9. A second API replica must not be a second monitoring worker."""
    services = _yaml(PROD)["services"]
    assert services["api"]["environment"]["WORKERS_ENABLED"] == "false"
    assert services["worker"]["environment"]["WORKERS_ENABLED"] == "true"
    # And the recovery sequence runs the other way round: it belongs to the
    # process that gates order submission.
    assert services["api"]["environment"]["RECOVERY_STARTUP_CHECKS"] == "true"
    assert services["worker"]["environment"]["RECOVERY_STARTUP_CHECKS"] == "false"


def test_the_worker_container_declares_no_replicas() -> None:
    """There is no leader election. Two workers are two monitoring workers."""
    worker = _yaml(PROD)["services"]["worker"]
    assert "replicas" not in worker.get("deploy", {})
    assert worker.get("container_name") == "tr-worker"


def test_the_production_overlay_does_not_enable_live_trading() -> None:
    """Stated explicitly rather than inherited, so a reader of the production
    file can see that production does not trade with real money."""
    for name in ("api", "worker"):
        env = _yaml(PROD)["services"][name]["environment"]
        assert env["TRADING_MODE"] == "paper"
        assert env["LIVE_TRADING"] == "false"


def test_workers_enabled_defaults_to_true() -> None:
    """A single-process deployment must behave exactly as it did before L41."""
    assert Settings(_env_file=None).workers_enabled is True


def test_every_container_has_a_memory_limit() -> None:
    """An unbounded container on a shared host takes the host down with it."""
    for name, service in _yaml(PROD)["services"].items():
        limits = service.get("deploy", {}).get("resources", {}).get("limits", {})
        assert limits.get("memory"), f"{name} has no memory limit"
        assert limits.get("cpus"), f"{name} has no cpu limit"


def test_redis_refuses_writes_rather_than_evicting() -> None:
    """The default `allkeys-lru` silently DROPS data under memory pressure.

    A rate limiter that silently forgets is one that stops limiting exactly
    when the traffic is heaviest. Refusing is loud, and loud is recoverable.
    """
    command = _yaml(PROD)["services"]["redis"]["command"]
    assert "noeviction" in command
    assert "--maxmemory" in command
    assert "yes" in command  # appendonly


def test_the_production_healthcheck_probes_readiness_not_liveness() -> None:
    """/health touches no dependency, so it reports a container with a dead
    database as healthy -- and a rolling deploy would replace a working
    container with a broken one.

    NOT /health/trading: that is 503 whenever the platform should not trade,
    which is normal and is not a reason to restart anything.
    """
    check = " ".join(str(x) for x in _yaml(PROD)["services"]["api"]["healthcheck"]["test"])
    assert "/health/ready" in check
    assert "/health/trading" not in check


# ================================================================= 5. secrets


DEPLOYMENT_FILES = [COMPOSE, OVERRIDE, PROD, NGINX_DEV, NGINX_PROD, DOCKERFILE, WORKFLOW]


def test_no_committed_deployment_file_contains_a_literal_secret() -> None:
    """Every secret must arrive as a variable, never a value."""
    pattern = re.compile(
        r"(password|secret|token|api[_-]?key|webhook_url)\s*[:=]\s*['\"]?([A-Za-z0-9_/+.-]{12,})",
        re.IGNORECASE,
    )
    for path in DEPLOYMENT_FILES:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = pattern.search(line)
            if not match:
                continue
            value = match.group(2)
            # A variable reference, a placeholder or a documented default is
            # not a secret.
            assert "${" in line or value in {"must", "unknown"} or line.lstrip().startswith("#"), (
                f"{path.name}:{lineno} may contain a literal secret"
            )


def test_production_refuses_to_start_without_its_secrets() -> None:
    """`${VAR:?...}` fails the deploy rather than falling back to a
    development value."""
    text = PROD.read_text(encoding="utf-8")
    for name in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "DATABASE_URL", "CORS_ORIGINS"):
        assert f"${{{name}:?" in text, f"{name} has a silent fallback in production"


def test_the_frontend_receives_only_public_configuration() -> None:
    """Only NEXT_PUBLIC_* reaches the browser."""
    for path in (COMPOSE, PROD):
        frontend = _yaml(path)["services"].get("frontend", {})
        for key in frontend.get("environment", {}):
            assert key.startswith("NEXT_PUBLIC_"), f"{key} would reach the browser"


# ================================================================ 6. the image


def test_the_image_runs_as_a_non_root_user() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "USER app" in text
    assert "--uid 10001" in text
    assert text.strip().splitlines()[-1].startswith("CMD")


def test_the_image_does_not_fork_uvicorn_workers() -> None:
    """The process holds the safe-mode latch, the step-up grants and possibly
    the rate limiter; `--workers N` forks N copies that cannot see each other."""
    # The CMD line only. The file's comments explain WHY there is no
    # `--workers`, and a naive substring search over the whole file finds the
    # explanation and fails.
    cmd = [ln for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines() if ln.startswith("CMD")]
    assert len(cmd) == 1, cmd
    assert "--workers" not in cmd[0]
    assert "--reload" not in cmd[0]


def test_ci_gates_the_image_build_on_the_tests() -> None:
    """ "Do not deploy if critical tests fail", as a dependency in the graph
    rather than a step somebody can reorder."""
    workflow = _yaml(WORKFLOW)
    images = workflow["jobs"]["images"]
    assert set(images["needs"]) == {"backend", "frontend"}


def test_ci_never_tags_an_image_latest() -> None:
    """`latest` overwrites the thing you would roll back to."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert ":latest" not in text
