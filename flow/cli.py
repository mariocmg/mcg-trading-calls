from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from flow import db
from flow.config import load_config
from flow.fetcher import fetch_watchlist
from flow.heuristics import positioning_bias, score_contracts

app = typer.Typer(help="Delayed options-flow scanner (Yahoo) for weekly positioning.")
console = Console(force_terminal=True, legacy_windows=False)


def _cfg(path: Optional[str]):
    return load_config(Path(path) if path else None)


def _run_scan(cfg_path: Optional[str] = None) -> None:
    cfg = _cfg(cfg_path)
    console.print(
        f"[bold]Scanning[/] {', '.join(cfg['watchlist'])} "
        f"(provider: [cyan]{cfg.get('data_provider', 'cboe')}[/], expiries<={cfg['max_expiries']})..."
    )
    from flow.scan_runner import run_scan

    res = run_scan(cfg_path)
    if not res.get("ok"):
        console.print(f"[red]Scan failed: {res.get('error')}[/]")
        raise typer.Exit(1)

    scan_id = res["scan_id"]
    prev_id = res.get("previous_scan_id")
    n = res.get("contracts", 0)
    alerts = res.get("top_alerts", [])
    alert_count = res.get("alerts", 0)
    src = res.get("source", "")
    fb = res.get("fallback_reason")
    src_info = f"via {src}" + (f" (fallback: {fb})" if fb else "")

    console.print(
        f"Scan #{scan_id} [{src_info}]: [cyan]{n}[/] contracts stored, "
        f"[yellow]{alert_count}[/] alerts"
        + (f" (vs scan #{prev_id})" if prev_id else " (baseline — no prior scan)")
    )

    if alerts:
        _print_alerts(alerts[:25])
        if res.get("bias"):
            _print_bias(res["bias"])
    else:
        if res.get("baseline_only"):
            console.print("[dim]Baseline scan stored. Next scan will detect Δvolume alerts.[/]")
        else:
            console.print(
                "[dim]No contracts crossed thresholds. "
                "Try adjusting heuristics in config.yaml.[/]"
            )


def _print_alerts(alerts: list[dict]) -> None:
    table = Table(title="Unusual / institutional AI flow alerts")
    left = {"Underlying", "Why", "Bias", "CP", "Expiry", "AI Subtype"}
    for col in (
        "Underlying",
        "CP",
        "Strike",
        "Expiry",
        "DTE",
        "Px",
        "Vol",
        "dVol",
        "Premium $",
        "AI Score",
        "AI Subtype",
        "Why",
    ):
        table.add_column(col, justify="left" if col in left else "right")

    for a in alerts:
        dte = a.get("dte")
        score = a.get("inst_score")
        score_str = f"{score:.2f}" if score is not None else "—"
        subtype = str(a.get("inst_subtype") or a.get("inst_label") or "—")
        table.add_row(
            a["underlying"],
            a["call_put"],
            f"{a['strike']:.2f}",
            a["expiry"],
            "" if dte is None else str(dte),
            f"{a['price']:.2f}",
            str(a.get("volume") or 0),
            str(a.get("delta_volume") or 0),
            f"{a['premium_usd']:,.0f}",
            score_str,
            subtype,
            a["reason"],
        )
    console.print(table)


def _print_bias(bias: dict[str, dict[str, float]]) -> None:
    table = Table(title="Scan positioning bias (alert premium $)")
    table.add_column("Underlying")
    table.add_column("Call $", justify="right")
    table.add_column("Put $", justify="right")
    table.add_column("Net (C-P)", justify="right")
    table.add_column("Lean")
    for u, b in sorted(bias.items()):
        c = b.get("C", 0.0)
        p = b.get("P", 0.0)
        net = c - p
        lean = "CALLS" if net > 0 else ("PUTS" if net < 0 else "FLAT")
        table.add_row(u, f"{c:,.0f}", f"{p:,.0f}", f"{net:,.0f}", lean)
    console.print(table)


@app.command()
def scan(
    config: Optional[str] = typer.Option(
        None, "--config", "-c", help="Path to config.yaml"
    ),
) -> None:
    """Fetch delayed chains, store snapshot, emit alerts vs previous scan."""
    _run_scan(config)


@app.command()
def watch(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    once: bool = typer.Option(False, "--once", help="Single scan then exit"),
) -> None:
    """Poll on an interval (default 15 min). Ctrl+C to stop."""
    cfg = _cfg(config)
    interval = int(cfg.get("poll_interval_seconds", 900))
    while True:
        _run_scan(config)
        if once:
            break
        console.print(f"[dim]Sleeping {interval}s...[/]")
        time.sleep(interval)


@app.command("alerts")
def show_alerts(
    limit: int = typer.Option(40, "--limit", "-n"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Show recent stored alerts."""
    cfg = _cfg(config)
    conn = db.connect(cfg["database"])
    rows = db.recent_alerts(conn, limit=limit)
    if not rows:
        console.print("[dim]No alerts yet. Run: python -m flow scan[/]")
        raise typer.Exit(0)
    _print_alerts([dict(r) for r in rows])
    conn.close()


@app.command()
def summary(
    days: int = typer.Option(7, "--days", "-d"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Call/put alert premium totals over the last N days."""
    cfg = _cfg(config)
    conn = db.connect(cfg["database"])
    rows = db.weekly_summary(conn, days=days)
    info = db.latest_scan_info(conn)
    if info:
        console.print(f"Latest scan #{info['id']} at {info['scanned_at']}")
    if not rows:
        console.print("[dim]No alert history yet.[/]")
        raise typer.Exit(0)

    table = Table(title=f"Alert summary — last {days} days")
    table.add_column("Underlying")
    table.add_column("CP")
    table.add_column("Alerts", justify="right")
    table.add_column("Premium $", justify="right")
    table.add_column("dVol", justify="right")
    by_u: dict[str, dict[str, float]] = {}
    for r in rows:
        table.add_row(
            r["underlying"],
            r["call_put"],
            str(r["alert_count"]),
            f"{r['total_premium']:,.0f}",
            str(r["total_delta_volume"]),
        )
        by_u.setdefault(r["underlying"], {"C": 0.0, "P": 0.0})
        by_u[r["underlying"]][r["call_put"]] = float(r["total_premium"])
    console.print(table)
    _print_bias(by_u)
    conn.close()


if __name__ == "__main__":
    app()
