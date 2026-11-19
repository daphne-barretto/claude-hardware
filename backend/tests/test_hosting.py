"""render.yaml, the launch scripts and the deploy docs (README "Deploy", docs/architecture.md
"Hosting") match the hosting setup."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def services() -> dict[str, dict]:
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    return {service["name"]: service for service in blueprint["services"]}


def test_staging_auto_deploys_from_main_with_the_single_image() -> None:
    staging = services()["claude-hardware-staging"]
    assert staging["branch"] == "main"
    assert staging["autoDeployTrigger"] == "commit"
    # No rootDir: the whole repo is the build root, so backend changes redeploy too.
    assert "rootDir" not in staging
    assert staging["dockerfilePath"] == "./Dockerfile"
    assert (ROOT / "Dockerfile").is_file()


def test_demo_deploys_only_through_its_hook() -> None:
    demo = services()["claude-hardware-demo"]
    assert demo["autoDeployTrigger"] == "off"


def test_both_services_share_image_plan_env_group_and_health_check() -> None:
    all_services = list(services().values())
    assert len(all_services) == 2
    for service in all_services:
        assert service["type"] == "web"
        assert service["runtime"] == "docker"
        assert service["plan"] == "free"
        assert service["dockerContext"] == "."
        assert service["dockerfilePath"] == "./Dockerfile"
        assert service["healthCheckPath"] == "/health"
        assert {"fromGroup": "claude-hardware"} in service["envVars"]
        # Secrets come only from the env group.
        assert not any(var.get("key") == "ANTHROPIC_API_KEY" for var in service["envVars"])


def test_health_alias_is_proxied_to_the_backend() -> None:
    config = (ROOT / "frontend" / "next.config.ts").read_text()
    assert 'source: "/health"' in config
    assert "/api/health`" in config


def test_the_readme_covers_the_deploy_hook_and_storage() -> None:
    readme = (ROOT / "README.md").read_text()
    deploy = readme.split("## Deploy", 1)[1].split("\n## ", 1)[0]
    assert "render.yaml" in deploy
    assert "deploy hook" in deploy.lower()
    assert "DATA_DIR" in deploy
    assert "free plan" in deploy
    assert "disk" in deploy


def test_the_image_serves_the_real_api_and_the_mock_stays_opt_in() -> None:
    # Staging runs the image, so its default is the real session API.
    start = (ROOT / "scripts" / "start.sh").read_text()
    assert "uvicorn app.main:app" in start
    assert "mock_api" not in start
    # `make dev` runs the real backend unless MOCK_API is set, which runs the mock.
    makefile = (ROOT / "Makefile").read_text()
    assert "BACKEND_APP := $(if $(MOCK_API),app.mock_api:app,app.main:app)" in makefile
    assert (ROOT / "backend" / "app" / "mock_api.py").is_file()


# Node's default HTTP agent, which the Next.js proxy uses, closes idle keep-alive sockets after
# 5 s; uvicorn's default is 5 s too.
NODE_AGENT_IDLE_S = 5


def test_every_backend_behind_the_next_proxy_keeps_idle_sockets_longer_than_the_proxy() -> None:
    """With both at 5 s the proxy reused sockets uvicorn had just closed: ECONNRESET, a 500 in
    the browser (16 in one 40-minute QA run; brief 6). The backend must keep
    an idle socket open longer than the proxy does, so the proxy is always the one to close it."""
    launches = {
        "scripts/start.sh (the Docker image)": (ROOT / "scripts" / "start.sh").read_text(),
        "make dev": (ROOT / "Makefile").read_text().split("\ndev:", 1)[1].split("\n\n", 1)[0],
        "make e2e": (ROOT / "frontend" / "playwright.config.ts").read_text(),
        "make qa": (ROOT / "frontend" / "e2e" / "qa" / "qa.config.ts").read_text(),
    }
    for where, text in launches.items():
        lines = [line for line in text.splitlines() if re.search(r"\buvicorn\b", line)]
        lines = [line for line in lines if not line.lstrip().startswith(("#", "//"))]
        assert lines, where
        for line in lines:
            found = re.search(r"--timeout-keep-alive[ =](\d+)", line)
            assert found, f"{where}: {line.strip()}"
            assert int(found.group(1)) > NODE_AGENT_IDLE_S, f"{where}: {line.strip()}"


def test_hosting_lists_the_memory_of_each_process_and_the_minimum_instance() -> None:
    from geometry import slots

    doc = (ROOT / "docs" / "architecture.md").read_text()
    memory = doc.split("### Memory", 1)[1].split("\n### ", 1)[0]
    for process in ("FastAPI server", "Next.js", "Agent CLI", "Sandboxed build", "Geometry worker"):
        assert process in memory
    assert "Minimum instance size" in memory
    assert f"`{slots.MEMORY_ENV}` (default {slots.DEFAULT_MEMORY_MB})" in memory
    # The build slots have no fixed default: they are sized from the machine.
    assert f"`{slots.CONCURRENCY_ENV}`: build slots" in memory
