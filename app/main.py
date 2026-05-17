"""
Autonomous Lab Agent — CLI Entry Point.
Usage:
  python app/main.py run methodology.docx
  python app/main.py run methodology.pdf --output report.docx --session-id abc123
  python app/main.py status --session-id abc123
  python app/main.py inspect --file path/to/file.docx
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Optional

import typer
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config
from core.orchestrator import Orchestrator
from agents.report_agent import ReportMeta

console = Console()
app = typer.Typer(
    name="lab-agent",
    help="Autonomous desktop AI agent for completing Word/Excel lab assignments",
    add_completion=False,
)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure loguru with structured logging."""
    cfg = get_config()
    logger.remove()  # Remove default handler

    # Console output
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
               "<cyan>{name}</cyan> | {message}",
        colorize=True,
    )

    # File output with full detail
    log_file = cfg.log_dir / "agent_{time:YYYYMMDD_HHmmss}.log"
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_file),
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation=cfg.log_rotation,
        retention=cfg.log_retention,
        encoding="utf-8",
    )

    # Failure screenshots log
    fail_log = cfg.log_dir / "failures_{time:YYYYMMDD}.log"
    logger.add(
        str(fail_log),
        level="ERROR",
        format="{time} | {level} | {message}",
        filter=lambda r: r["level"].name in ("ERROR", "CRITICAL"),
    )


@app.command()
def run(
    methodology: Path = typer.Argument(..., help="Path to methodology .docx or .pdf file"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output report path"),
    session_id: Optional[str] = typer.Option(None, "--session-id", "-s", help="Resume session ID"),
    no_resume: bool = typer.Option(False, "--no-resume", help="Start fresh, ignore checkpoints"),
    student: Optional[str] = typer.Option(None, "--student", help="Student name for title page"),
    group: Optional[str] = typer.Option(None, "--group", help="Student group"),
    teacher: Optional[str] = typer.Option(None, "--teacher", help="Teacher name"),
    university: Optional[str] = typer.Option(None, "--university", help="University name"),
    lab_number: Optional[str] = typer.Option(None, "--lab-number", help="Lab number"),
    log_level: str = typer.Option("INFO", "--log-level", help="Logging level"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse only, don't execute GUI"),
) -> None:
    """Run the autonomous lab agent on a methodology file."""
    setup_logging(log_level)
    cfg = get_config()
    cfg.ensure_dirs()

    if not methodology.exists():
        console.print(f"[red]Error: File not found: {methodology}[/red]")
        raise typer.Exit(1)

    sid = session_id or str(uuid.uuid4())
    console.print(Panel(
        f"[bold cyan]Autonomous Lab Agent[/bold cyan]\n"
        f"Methodology: [yellow]{methodology}[/yellow]\n"
        f"Session: [green]{sid}[/green]\n"
        f"Resume: [blue]{not no_resume}[/blue]",
        title="Starting",
    ))

    # Build report metadata
    meta = ReportMeta(
        title=methodology.stem,
        student=student or "",
        group=group or "",
        teacher=teacher or "",
        university=university or cfg.report.university,
        department=cfg.report.department,
        lab_number=lab_number or "",
    )

    async def run_async() -> Path:
        orchestrator = Orchestrator(session_id=sid)
        return await orchestrator.run(
            methodology_path=methodology,
            output_path=output,
            resume=not no_resume,
            report_meta=meta,
        )

    if dry_run:
        console.print("[yellow]DRY RUN: Parsing methodology only[/yellow]")
        from agents.parser_agent import get_parser_agent
        parser = get_parser_agent()
        result = parser.parse(methodology)
        _print_parsed_tasks(result)
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_prog = progress.add_task("Running autonomous agent...", total=None)
        try:
            report_path = asyncio.run(run_async())
            progress.update(task_prog, description="[green]Completed!")
            console.print(Panel(
                f"[bold green]Report generated:[/bold green]\n{report_path}",
                title="Success",
            ))
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted. Session checkpointed — use --session-id to resume.[/yellow]")
            console.print(f"Session ID: [cyan]{sid}[/cyan]")
            raise typer.Exit(130)
        except Exception as exc:
            logger.exception("Agent failed")
            console.print(f"[bold red]Agent failed: {exc}[/bold red]")
            console.print(f"Session ID for resume: [cyan]{sid}[/cyan]")
            raise typer.Exit(1)


@app.command()
def inspect(
    file: Path = typer.Argument(..., help="Methodology file to inspect"),
    log_level: str = typer.Option("WARNING", "--log-level"),
) -> None:
    """Parse and display methodology structure without executing."""
    setup_logging(log_level)
    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    from agents.parser_agent import get_parser_agent
    parser = get_parser_agent()
    console.print(f"Parsing: [yellow]{file}[/yellow]")
    result = parser.parse(file)
    _print_parsed_tasks(result)


@app.command()
def status(
    session_id: str = typer.Argument(..., help="Session ID to inspect"),
) -> None:
    """Show status of an existing session."""
    setup_logging("WARNING")
    cfg = get_config()
    cfg.ensure_dirs()

    async def get_status() -> None:
        from storage.database import get_db
        db = await get_db()
        session = await db.get_session(session_id)
        if not session:
            console.print(f"[red]Session not found: {session_id}[/red]")
            return
        tasks = await db.get_pending_tasks(session_id)

        table = Table(title=f"Session: {session_id}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("Status", session.status)
        table.add_row("Created", str(session.created_at))
        table.add_row("Methodology", session.methodology_path)
        table.add_row("Pending tasks", str(len(tasks)))
        console.print(table)

        cp = get_checkpoint_manager().get_resume_point(session_id)
        if cp:
            console.print(f"[green]Checkpoint available: task={cp.task_id} step={cp.step_index}[/green]")

    from storage.checkpoints import get_checkpoint_manager
    asyncio.run(get_status())


@app.command()
def list_sessions() -> None:
    """List all known sessions."""
    setup_logging("WARNING")
    cfg = get_config()
    cp_dir = cfg.storage.checkpoint_dir
    if not cp_dir.exists():
        console.print("No sessions found")
        return

    files = list(cp_dir.glob("*.json"))
    if not files:
        console.print("No checkpoints found")
        return

    table = Table(title="Sessions with Checkpoints")
    table.add_column("Session ID")
    table.add_column("Task")
    table.add_column("Step")
    table.add_column("Checkpoint file")

    for f in sorted(files):
        import json
        try:
            data = json.loads(f.read_text())
            session = data.get("session_id", "?")
            task = data.get("task_id", "?")
            step = str(data.get("step_index", "?"))
            table.add_row(session[:8] + "...", task, step, f.name)
        except Exception:
            table.add_row("?", "?", "?", f.name)

    console.print(table)


def _print_parsed_tasks(methodology) -> None:
    from agents.parser_agent import ParsedMethodology
    console.print(Panel(
        f"[bold]{methodology.title}[/bold]\n"
        f"Language: {methodology.language}\n"
        f"Sections: {methodology.total_sections}\n"
        f"Tasks: {len(methodology.tasks)}\n\n"
        f"{methodology.global_context[:200]}",
        title="Methodology"
    ))

    table = Table(title="Tasks")
    table.add_column("#", width=4)
    table.add_column("ID")
    table.add_column("App", width=6)
    table.add_column("Title")
    table.add_column("Steps", width=6)

    for i, task in enumerate(methodology.tasks):
        table.add_row(
            str(i + 1),
            task.task_id,
            task.application,
            task.title[:50],
            str(len(task.steps)),
        )
    console.print(table)


if __name__ == "__main__":
    app()
