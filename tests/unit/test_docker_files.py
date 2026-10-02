"""Phase 23: checks on the Docker files that need no Docker.

Nothing here builds or starts a container (that was done by hand, see docs/DOCKER.md), so
the tests run everywhere. They read the files and check the promises they make:
valid syntax, ports only on 127.0.0.1, no secrets, an unprivileged user, names and ports
that agree between files, and code that can be imported on Linux.
"""

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

from app.config.settings import Settings
from app.files.workspace import _system_roots
from app.security.network import host_allowed

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
MONITORING = ROOT / "deploy" / "docker-compose.observability.yml"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
COMPOSE_FILES = [COMPOSE, MONITORING]


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def interpolate(value: object) -> str:
    """What Compose makes of `${NAME:-default}` when NAME is not set."""
    return re.sub(r"\$\{\w+(?::?-([^}]*))?\}", lambda m: m.group(1) or "", str(value))


def services(path: Path) -> dict[str, dict]:
    return load(path)["services"]


def all_services() -> list[tuple[str, str, dict]]:
    return [(p.name, name, svc) for p in COMPOSE_FILES for name, svc in services(p).items()]


def dockerfile_instructions() -> list[tuple[str, str]]:
    """(INSTRUCTION, rest) pairs, with `\\` line continuations joined and comments dropped."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    pairs = []
    for line in joined.splitlines():
        if line.strip():
            word, _, rest = line.strip().partition(" ")
            pairs.append((word.upper(), rest.strip()))
    return pairs


# ---------- Compose: syntax and safety ----------


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_files_are_valid_yaml_with_services(path):
    data = load(path)
    assert data["services"]
    for name, service in data["services"].items():
        assert "image" in service or "build" in service, name


def test_every_published_port_is_on_127_0_0_1_only():
    published = [
        (file, name, port) for file, name, svc in all_services() for port in svc.get("ports", [])
    ]
    assert published  # the test must not pass because it found nothing
    for file, name, port in published:
        assert str(port).startswith("127.0.0.1:"), f"{file}: {name} publishes {port}"


def test_no_service_gets_special_powers_over_the_pc():
    for file, name, svc in all_services():
        where = f"{file}: {name}"
        assert not svc.get("privileged"), where
        assert svc.get("network_mode") != "host", where
        assert "pid" not in svc and "cap_add" not in svc, where
        for volume in svc.get("volumes", []):
            assert "docker.sock" not in str(volume), where


def test_arthur_runs_without_extra_linux_privileges():
    arthur = services(COMPOSE)["arthur"]
    assert arthur["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in arthur["security_opt"]


def test_no_secret_is_written_into_a_compose_file():
    secret_like = re.compile(r"KEY|SECRET|PASSWORD|TOKEN", re.IGNORECASE)
    for file, name, svc in all_services():
        assert "env_file" not in svc, f"{file}: {name} would receive the whole .env"
        for key, value in (svc.get("environment") or {}).items():
            if secret_like.search(key):
                # Only "take it from the owner's .env" is allowed, never a value or a default.
                assert re.fullmatch(r"\$\{\w+(:-)?\}", str(value)), f"{file}: {name}.{key}"


def test_images_are_pinned_to_a_version():
    for file, name, svc in all_services():
        if "build" in svc:
            continue
        image, _, tag = svc["image"].partition(":")
        assert tag and tag != "latest", f"{file}: {name} uses {image} without a fixed version"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_mounted_config_files_exist(path):
    for name, svc in services(path).items():
        for volume in svc.get("volumes", []):
            source = interpolate(volume).split(":")[0]
            if source.startswith("./") and not source.startswith("./data"):
                assert (path.parent / source).exists(), f"{name}: {source} is missing"


def test_named_volumes_are_declared():
    for path in COMPOSE_FILES:
        declared = set(load(path).get("volumes") or {})
        for name, svc in services(path).items():
            for volume in svc.get("volumes", []):
                source = interpolate(volume).split(":")[0]
                if not source.startswith((".", "/")):
                    assert source in declared, f"{path.name}: {name} uses undeclared {source}"


# ---------- Compose: ARTHUR's settings inside the container ----------


def container_settings() -> Settings:
    env = services(COMPOSE)["arthur"]["environment"]
    values = {key.lower(): interpolate(value) for key, value in env.items() if key != "TZ"}
    unknown = set(values) - set(Settings.model_fields)
    assert not unknown, f"not ARTHUR settings (typo?): {sorted(unknown)}"
    return Settings(_env_file=None, **values)


def test_container_environment_is_made_of_real_settings_and_validates():
    settings = container_settings()
    assert settings.ollama_base_url == "http://host.docker.internal:11434"
    assert settings.computer_use_enabled is False  # a container cannot see the desktop
    assert settings.browser_enabled is False  # only with the optional Chromium build
    assert settings.search_provider == "duckduckgo"


def test_container_file_tools_only_see_the_mounted_folder():
    settings = container_settings()
    assert settings.file_roots == [Path("/files")]
    assert settings.files_save_dir == Path("/files/reports")
    targets = [interpolate(v).split(":")[1] for v in services(COMPOSE)["arthur"]["volumes"]]
    for needed in ("/files", "/app/data", str(settings.models_path.as_posix()), "/voices"):
        assert needed in targets


def test_container_has_a_time_zone_setting():
    assert "TZ" in services(COMPOSE)["arthur"]["environment"]  # reminders use local time


def test_private_data_lives_in_a_docker_volume_not_in_the_image():
    volumes = [interpolate(v) for v in services(COMPOSE)["arthur"]["volumes"]]
    assert "arthur-data:/app/data" in volumes


# ---------- Prometheus and Grafana agree with Compose ----------


def scrape_targets(path: Path) -> list[str]:
    groups = [s for job in load(path)["scrape_configs"] for s in job["static_configs"]]
    return [target for group in groups for target in group["targets"]]


def test_prometheus_scrapes_the_arthur_service_by_a_name_arthur_accepts():
    (target,) = scrape_targets(ROOT / "deploy" / "prometheus" / "prometheus.yml")
    host, _, port = target.partition(":")
    compose = services(COMPOSE)
    assert host in compose  # a service name is the host name inside Docker's network
    assert compose[host]["ports"] == [f"127.0.0.1:{port}:{port}"]
    assert host_allowed(target, container_settings().extra_hosts)
    assert not host_allowed(target)  # ...and only because ALLOWED_HOSTS says so


def test_host_mode_prometheus_scrapes_the_pc():
    assert scrape_targets(ROOT / "deploy" / "prometheus" / "prometheus.host.yml") == [
        "host.docker.internal:8000"
    ]
    mounts = [str(v) for v in services(MONITORING)["prometheus"]["volumes"]]
    assert any("prometheus.host.yml:/etc/prometheus/prometheus.yml" in m for m in mounts)


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_grafana_finds_prometheus_and_the_dashboard(path):
    grafana_dir = ROOT / "deploy" / "grafana"
    (source,) = load(grafana_dir / "provisioning" / "datasources" / "prometheus.yml")["datasources"]
    host, _, port = source["url"].removeprefix("http://").partition(":")
    compose = services(path)
    assert host in compose
    assert any(str(p).endswith(f":{port}") for p in compose[host]["ports"])

    (provider,) = load(grafana_dir / "provisioning" / "dashboards" / "arthur.yml")["providers"]
    targets = [str(v).split(":")[1] for v in compose["grafana"]["volumes"]]
    assert provider["options"]["path"] in targets

    dashboard = (grafana_dir / "dashboards" / "arthur.json").read_text(encoding="utf-8")
    assert json.loads(dashboard)["title"]
    used = set(re.findall(r'"datasource":\s*\{[^{}]*?"uid":\s*"([^"]+)"', dashboard))
    assert used == {source["uid"]}


def test_searxng_is_optional_private_and_answers_json():
    searxng = services(COMPOSE)["searxng"]
    assert searxng["profiles"] == ["search"]  # not started by a plain `up`
    assert "ports" not in searxng
    settings = load(ROOT / "docker" / "searxng" / "settings.yml")
    assert "json" in settings["search"]["formats"]
    assert "secret_key" not in settings.get("server", {})
    env = services(COMPOSE)["arthur"]["environment"]
    assert env["SEARXNG_URL"] == "http://searxng:8080"


# ---------- Dockerfile ----------


def test_dockerfile_uses_the_project_python_and_an_unprivileged_user():
    instructions = dockerfile_instructions()
    (base,) = [rest for word, rest in instructions if word == "FROM"]
    assert base.startswith("python:3.12-")
    users = [rest for word, rest in instructions if word == "USER"]
    assert users and users[-1] not in ("root", "0")
    # USER comes after the last RUN: nothing is installed or changed as that user.
    words = [word for word, _ in instructions]
    assert words.index("USER") > max(i for i, word in enumerate(words) if word == "RUN")


def test_dockerfile_copies_only_code_never_the_whole_folder():
    instructions = dockerfile_instructions()
    assert not [rest for word, rest in instructions if word == "ADD"]
    sources = {rest.split()[0] for word, rest in instructions if word == "COPY"}
    assert sources == {"requirements.txt", "app/", "frontend/"}


def test_dockerfile_starts_the_server_on_the_port_compose_publishes():
    instructions = dict(dockerfile_instructions())
    command = json.loads(instructions["CMD"])
    assert command[-4:] == ["--host", "0.0.0.0", "--port", "8000"]
    assert "--reload" not in command
    assert instructions["EXPOSE"] == "8000"
    assert "/health" in instructions["HEALTHCHECK"]
    build = services(COMPOSE)["arthur"]["build"]
    assert (ROOT / build["context"] / build["dockerfile"]) == ROOT / "." / "docker" / "Dockerfile"
    assert DOCKERFILE.exists()


def test_dockerignore_keeps_secrets_and_data_out_of_the_build():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for needed in (".env", ".env.*", "data/", ".git/", ".venv/", "**/*.pem", "**/*.key"):
        assert needed in lines, needed


# ---------- the code itself must start on Linux ----------

WINDOWS_ONLY = {"pywinauto", "pythoncom", "pywintypes", "comtypes", "winreg", "msvcrt", "_winapi"}
WINDOWS_ATTRS = {"windll", "WinDLL", "oledll", "ProactorEventLoop", "startfile"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def import_time_statements(body: list[ast.stmt]):
    """Statements that run when the module is imported (not the insides of functions)."""
    for node in body:
        if isinstance(node, FUNCTIONS):
            continue
        if isinstance(node, ast.If) and "platform" in ast.unparse(node.test):
            continue  # guarded on purpose
        if isinstance(node, ast.ClassDef | ast.If | ast.Try | ast.With):
            blocks = [node.body, getattr(node, "orelse", []), getattr(node, "finalbody", [])]
            blocks += [handler.body for handler in getattr(node, "handlers", [])]
            for block in blocks:
                yield from import_time_statements(block)
        else:
            yield node


def is_windows_only(module: str) -> bool:
    top = module.split(".")[0]
    return top in WINDOWS_ONLY or top.startswith("win32")


def windows_only_at_import(source: str) -> list[str]:
    found = []
    for statement in import_time_statements(ast.parse(source).body):
        if isinstance(statement, ast.Import):
            found += [a.name for a in statement.names if is_windows_only(a.name)]
        elif isinstance(statement, ast.ImportFrom):
            found += [statement.module] if is_windows_only(statement.module or "") else []
        else:
            found += [
                n.attr
                for n in ast.walk(statement)
                if isinstance(n, ast.Attribute) and n.attr in WINDOWS_ATTRS
            ]
    return found


def test_the_checker_itself_sees_windows_only_code():
    assert windows_only_at_import("import win32gui") == ["win32gui"]
    assert windows_only_at_import("from pywinauto import Desktop") == ["pywinauto"]
    assert windows_only_at_import("class A:\n    x = ctypes.windll.user32") == ["windll"]
    assert windows_only_at_import("try:\n    import pythoncom\nexcept ImportError:\n    pass")
    assert windows_only_at_import("def f():\n    import win32gui") == []
    assert windows_only_at_import("if sys.platform == 'win32':\n    import winreg") == []


def test_no_module_needs_windows_just_to_be_imported():
    problems = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        found = windows_only_at_import(path.read_text(encoding="utf-8"))
        if found:
            problems[path.relative_to(ROOT).as_posix()] = found
    assert not problems


def test_windows_only_packages_are_marked_in_requirements():
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name = re.split(r"[<>=!~;\[ ]", line.strip(), maxsplit=1)[0].lower()
        if name in ("pywinauto", "pywin32", "comtypes"):
            assert 'sys_platform == "win32"' in line, line


def test_web_page_file_names_match_exactly():
    """Windows finds Dashboard.css when the page asks for dashboard.css; Linux does not."""
    frontend = ROOT / "frontend"
    real = {p.relative_to(frontend).as_posix() for p in frontend.rglob("*") if p.is_file()}
    asked = set()
    for page in frontend.rglob("*.html"):
        asked |= set(re.findall(r'(?:src|href)="/?([^":#?]+\.\w+)"', page.read_text("utf-8")))
    for script in frontend.rglob("*.js"):
        text = script.read_text("utf-8")
        asked |= set(re.findall(r'["\']/?([\w./-]+\.(?:js|css|html))["\']', text))
    assert asked  # found something to check
    assert not {name for name in asked if name not in real}


def test_linux_system_folders_are_off_limits_in_a_container():
    roots = _system_roots(windows=False)
    for folder in ("/etc", "/proc", "/root", "/app"):
        assert Path(folder).resolve() in roots
    assert Path("/files").resolve() not in roots
