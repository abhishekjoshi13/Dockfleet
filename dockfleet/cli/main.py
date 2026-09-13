import sys
import subprocess
import json
from pathlib import Path
import logging
import time
from datetime import datetime

import typer
from pydantic import ValidationError
from sqlmodel import Session, select

from dockfleet.cli.config import load_config
from dockfleet.core.orchestrator import Orchestrator, get_logs
from dockfleet.health.seed import bootstrap_from_path
from dockfleet.health.scheduler import HealthScheduler
from dockfleet.health.status import update_service_health
from dockfleet.health.models import engine
from dockfleet.health.logs import LogEvent  # make sure this exists

app = typer.Typer(help="DockFleet CLI - Manage Docker services from YAML configuration")
validate_app = typer.Typer()
app.add_typer(validate_app, name="validate")

# ------------------------------------------------
# Logging setup for health scheduler
# ------------------------------------------------

HEALTH_LOG_PATH = Path("dockfleet-health.log")


def setup_health_logging() -> None:
    """
    Configure logging so health scheduler logs go only to dockfleet-health.log.
    Call this before starting HealthScheduler.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(HEALTH_LOG_PATH, encoding="utf-8"),
        ],
    )


# ------------------------------------------------
# validate
# ------------------------------------------------

@validate_app.callback(invoke_without_command=True)
def validate(path: Path = typer.Argument("examples/dockfleet.yaml")):
    """Validate a DockFleet YAML configuration file before running services."""
    try:
        load_config(path)
        typer.echo("✓ Config valid")
    except ValidationError as e:
        typer.echo("✗ Config validation failed\n")

        for err in e.errors():
            location = " -> ".join(str(x) for x in err["loc"])
            msg = err["msg"]

            if "resources" in location and "memory" in location:
                typer.echo(f"[ERROR] Invalid memory limit → {msg}")
            elif "resources" in location and "cpu" in location:
                typer.echo(f"[ERROR] Invalid CPU value → {msg}")
            elif "depends_on" in location:
                typer.echo(f"[ERROR] Dependency issue → {msg}")
            elif "environment" in location:
                typer.echo(f"[ERROR] Environment format issue → {msg}")
            else:
                typer.echo(f"[ERROR] {location}: {msg}")

        raise typer.Exit(code=1)
    except Exception as e:
        typer.echo(f"Unexpected error: {e}")
        raise typer.Exit(code=1)


# ------------------------------------------------
# seed
# ------------------------------------------------

@app.command()
def seed(path: Path = typer.Argument("examples/dockfleet.yaml")):
    """Initialize the service database and register services from the configuration."""
    try:
        typer.echo(f"Seeding services from {path}...")
        bootstrap_from_path(str(path))
        typer.echo("✓ Seeding complete")
    except Exception as e:
        typer.echo(f"Seeding failed: {e}")
        raise typer.Exit(code=1)


# ------------------------------------------------
# up
# ------------------------------------------------

@app.command()
def up(path: Path = typer.Argument("examples/dockfleet.yaml")):
    """
    Start all services and the health engine (self-healing mode).

    - Bootstraps health DB from YAML.
    - Starts HealthScheduler in background (logs to dockfleet-health.log).
    - Starts services via Orchestrator.
    - Returns immediately (no blocking loop, no log spam on stdout).
    """
    try:
        # Configure scheduler logging once
        setup_health_logging()

        # Load config
        config = load_config(path)

        typer.echo(f"Starting services from {path}...\n")

        # Ensure DB has Service rows for this config
        typer.echo(f"Bootstrapping health DB from {path} ...")
        bootstrap_from_path(str(path))

        # Start health scheduler in background (self-healing)
        scheduler = HealthScheduler(config)
        scheduler.start()
        typer.echo(
            f"Health scheduler started in background; logs -> {HEALTH_LOG_PATH}\n"
        )

        # Start orchestrator (non-blocking orchestration only)
        orch = Orchestrator(config)
        orch.up()

        typer.echo("Services started.")
        typer.echo("Use `dockfleet health-logs` to inspect health engine output.")
    except Exception as e:
        typer.echo(f"Error starting services: {e}")
        raise typer.Exit(code=1)


# ------------------------------------------------
# down
# ------------------------------------------------

@app.command()
def down(path: Path = typer.Argument("examples/dockfleet.yaml")):
    """Stop and remove all containers managed by DockFleet."""
    try:
        config = load_config(path)

        typer.echo(f"Stopping services from {path}...\n")

        orch = Orchestrator(config)
        orch.down()

        typer.echo("\n✓ Services stopped")
    except Exception as e:
        typer.echo(f"Error stopping services: {e}")
        raise typer.Exit(code=1)


# ------------------------------------------------
# ps
# ------------------------------------------------

@app.command()
def ps(path: Path = typer.Argument("examples/dockfleet.yaml"),
       output_json: bool = typer.Option(False, "--json", help="Output sevice status in JSON format"),
):
    """Show currently running DockFleet containers."""
    try:
        

        config = load_config(path)
        orch = Orchestrator(config)
        if output_json:
            stats = orch.get_service_stats()
            serialized_stats = [stat.model_dump() for stat in stats]
            typer.echo(json.dumps(serialized_stats, indent=2))
            return
        typer.echo("Listing running containers...\n")
        orch.ps()
    except Exception as e:
        typer.echo(f"Error listing containers: {e}")
        raise typer.Exit(code=1)


# ------------------------------------------------
# logs (docker logs)
# ------------------------------------------------

@app.command()
def logs(
    service: str = typer.Argument(..., help="Service name"),
    lines: int = typer.Option(100, "--lines", help="Number of log lines"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
):
    """
    Show raw Docker logs for a DockFleet service container.
    """
    container_name = f"dockfleet_{service}"

    try:
        if follow:
            typer.echo(f"Streaming logs for {service} (Ctrl+C to stop)\n")
            subprocess.run(
                ["docker", "logs", "-f", "--tail", str(lines), container_name]
            )
        else:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(lines), container_name],
                capture_output=True,
                text=True,
            )
            typer.echo(result.stdout)
    except Exception:
        typer.echo(f"Service '{service}' not found or container not running.")
        raise typer.Exit(code=1)


# ------------------------------------------------
# show-logs (DB logs)
# ------------------------------------------------

@app.command("show-logs")
def show_logs(
    service: str = typer.Option(None, "--service", help="Filter by service name"),
    limit: int = typer.Option(50, "--limit", help="Number of logs to show"),
):
    """
    Show aggregated logs stored in DockFleet database.
    """
    try:
        with Session(engine) as session:
            query = select(LogEvent).limit(limit)

            if service:
                query = query.where(LogEvent.service_name == service)

            logs = session.exec(query).all()

            if not logs:
                typer.echo("No logs found.")
                return

            for log in logs:
                ts = getattr(log, "timestamp", None) or getattr(
                    log, "created_at", None
                )
                if ts:
                    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    ts_str = "no-time"

                typer.echo(f"[{ts_str}] [{log.service_name}] {log.message}")
    except Exception as e:
        typer.echo(f"Failed to fetch logs: {e}")
        raise typer.Exit(code=1)


# ------------------------------------------------
# doctor
# ------------------------------------------------

@app.command()
def doctor():
    """Check system environment (Python version and Docker availability)."""
    typer.echo("Running DockFleet doctor...\n")

    # Python version check
    version = sys.version.split()[0]
    typer.echo(f"Python version: {version}")

    # Docker check
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        typer.echo(f"Docker detected: {result.stdout.strip()}")
        typer.echo("✓ Environment looks good")
    except Exception:
        typer.echo("✗ Docker not found or not running")
        raise typer.Exit(code=1)


# ------------------------------------------------
# health-dev (unchanged behavior, for dev)
# ------------------------------------------------

@app.command("health-dev")
def health_dev(
    path: Path = typer.Argument("examples/dockfleet.yaml"),
    once: bool = typer.Option(
        False,
        "--once",
        help="Run a single health pass and exit (useful for tests).",
    ),
    no_restart: bool = typer.Option(
        False,
        "--no-restart",
        help="Run health checks without triggering container restarts.",
    ),
):
    """
    Developer command to run the health check scheduler backed by SQLite DB.
    """
    try:
        typer.echo("Starting DockFleet health check scheduler (DEV MODE)")
        typer.echo(
            "Press Ctrl+C to stop\n" if not once else "Running a single health pass\n"
        )

        config = load_config(path)
        if no_restart:
            config.self_healing = False
            for svc in config.services.values():
                svc.self_healing = False

        # Ensure DB and Service rows are present
        typer.echo(f"Bootstrapping health DB from {path} ...")
        bootstrap_from_path(str(path))

        typer.echo("Health engine using default SQLite DB\n")

        # check if any service has healthcheck defined
        services_with_health = [
            name
            for name, svc in config.services.items()
            if svc.healthcheck is not None
        ]

        if not services_with_health:
            typer.echo("No services with healthcheck defined in config.")
            raise typer.Exit(code=1)

        scheduler = HealthScheduler(config)

        if once:
            scheduler._logger = logging.getLogger(__name__)
            for name, svc_cfg in config.services.items():
                hc = svc_cfg.healthcheck
                if hc is None:
                    continue
                ok = scheduler._run_single_check(name, hc)
                status_str = "HEALTHY" if ok else "UNHEALTHY"
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                typer.echo(f"[{timestamp}] {name}: {status_str.lower()}")
                update_service_health(
                    name,
                    ok,
                    reason=None if ok else "health check failed",
                )
                scheduler._handle_post_health(name)
            typer.echo("Single health pass complete.")
            return

        # Normal long-running mode
        typer.echo("Health monitoring started...")

        scheduler.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            typer.echo("\nStopping health scheduler...")
            scheduler.stop()
    except Exception as e:
        typer.echo(f"Health scheduler failed: {e}")
        raise typer.Exit(code=1)


# ------------------------------------------------
# self-heal (unchanged; continuous health loop only)
# ------------------------------------------------

@app.command("self-heal")
def self_heal(
    path: Path = typer.Argument("examples/dockfleet.yaml"),
):
    """
    Run DockFleet in continuous self-healing mode (health checks only).
    """
    try:
        typer.echo("Starting DockFleet self-healing loop...\n")

        config = load_config(path)

        typer.echo(f"Bootstrapping health DB from {path} ...")
        bootstrap_from_path(str(path))

        scheduler = HealthScheduler(config)

        typer.echo("Self-healing active. Press Ctrl+C to stop.\n")

        scheduler.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            typer.echo("\nStopping self-healing loop...")
            scheduler.stop()
    except Exception as e:
        typer.echo(f"Self-heal command failed: {e}")
        raise typer.Exit(code=1)


# ------------------------------------------------
# health-logs (NEW)
# ------------------------------------------------

@app.command("health-logs")
def health_logs(
    follow: bool = typer.Option(True, "--follow", "-f", help="Follow log output"),
    lines: int = typer.Option(50, "--lines", "-n", help="Number of last lines to show"),
):
    """
    Show health scheduler logs from dockfleet-health.log.
    """
    log_path = HEALTH_LOG_PATH

    if not log_path.exists():
        typer.echo("No health log file found yet.")
        raise typer.Exit(code=1)

    content = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    tail = content[-lines:]
    for line in tail:
        typer.echo(line)

    if not follow:
        return

    typer.echo("\n-- following dockfleet-health.log (Ctrl+C to stop) --")
    last_size = log_path.stat().st_size
    try:
        while True:
            time.sleep(1)
            new_size = log_path.stat().st_size
            if new_size > last_size:
                with log_path.open("r", encoding="utf-8", errors="ignore") as f:
                    f.seek(last_size)
                    for line in f:
                        typer.echo(line.rstrip("\n"))
                last_size = new_size
    except KeyboardInterrupt:
        typer.echo("\nStopped following health logs.")


if __name__ == "__main__":
    app()