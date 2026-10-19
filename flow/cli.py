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
        from flow.institutional import model_info

        info = model_info()
        if info.get("synthetic"):
            console.print(
                "[dim]Nota: el score es heurístico — el modelo aún se entrena con datos "
                "sintéticos. Ejecuta `python -m flow evaluate` para ver hit-rates reales.[/dim]"
            )
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


def _inst_min_score(cfg) -> float:
    inst = (cfg.get("heuristics") or {}).get("institutional") or {}
    try:
        return float(inst.get("min_score", 0.5))
    except (TypeError, ValueError):
        return 0.5


def _print_alerts(alerts: list[dict]) -> None:
    table = Table(title="Unusual / institutional flow alerts")
    left = {"Underlying", "Why", "Bias", "CP", "Expiry", "Subtype", "Flags"}
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
        "Score",
        "Subtype",
        "Flags",
        "Why",
    ):
        table.add_column(col, justify="left" if col in left else "right")

    for a in alerts:
        dte = a.get("dte")
        score = a.get("inst_score")
        score_str = f"{score:.2f}" if score is not None else "—"
        subtype = str(a.get("inst_subtype") or a.get("inst_label") or "—")
        flags = []
        if a.get("is_block"):
            flags.append("BLOCK")
        if a.get("combo_type"):
            flags.append(str(a["combo_type"]))
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
            " ".join(flags) or "—",
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
    rows = db.recent_alerts(conn, limit=limit, min_score=_inst_min_score(cfg))
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
    rows = db.weekly_summary(conn, days=days, min_score=_inst_min_score(cfg))
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


@app.command()
def evaluate(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Hit-rate real del filtro institucional contra outcomes 1-5d resueltos."""
    from flow.evaluate import format_report, full_report

    cfg = _cfg(config)
    conn = db.connect(cfg["database"])
    try:
        rep = full_report(conn, min_score=0.0)
        console.print(format_report(rep))
        from flow.institutional import model_info

        info = model_info()
        console.print(
            f"\n[dim]Modelo: {'sintético (heurístico)' if info.get('synthetic') else info.get('source', '?')} "
            f"· n_train={info.get('n_train')}[/dim]"
        )
    finally:
        conn.close()


@app.command()
def levels(
    underlying: str = typer.Option("MU", "--underlying", "-u"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Muros de OI, gamma flip, max pain y buckets de DTE."""
    from flow.levels import summarize_persisted

    cfg = _cfg(config)
    conn = db.connect(cfg["database"])
    try:
        rows = db.load_strike_levels(conn, underlying)
        if not rows:
            console.print("[dim]Sin niveles todavía. Ejecuta un scan primero.[/]")
            raise typer.Exit(0)
        s = summarize_persisted([dict(r) for r in rows], underlying)
        console.print(f"[bold]{s['underlying']}[/] · {s['n_strikes']} strikes")
        console.print(f"GEX neta: ${s['net_gex_millions']}M")
        if s["call_wall"]:
            console.print(f"Muro call: {s['call_wall']['strike']:g} (OI {s['call_wall']['oi']:,})")
        if s["put_wall"]:
            console.print(f"Muro put:  {s['put_wall']['strike']:g} (OI {s['put_wall']['oi']:,})")
        if s["gamma_flip"]:
            console.print(f"Gamma flip: {s['gamma_flip']['strike']:g}")
        if s["max_pain_nearest"]:
            mp = s["max_pain_nearest"]
            console.print(f"Max pain ({mp['expiry']}): {mp['strike']:g}")
        # DTE buckets from latest snapshot
        from flow.levels import dte_buckets

        snaps = db.latest_snapshots(conn, underlying)
        buckets = dte_buckets([dict(x) for x in snaps], underlying)
        table = Table(title="Posicionamiento por DTE")
        table.add_column("Bucket")
        table.add_column("Call $", justify="right")
        table.add_column("Put $", justify="right")
        table.add_column("Call share", justify="right")
        for name, b in buckets.items():
            share = f"{b['call_share']:.0%}" if b["call_share"] is not None else "—"
            table.add_row(name, f"{b['call_premium']:,.0f}", f"{b['put_premium']:,.0f}", share)
        console.print(table)
    finally:
        conn.close()


@app.command()
def backtest(
    underlying: Optional[str] = typer.Option(None, "--underlying", "-u"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Walk-forward: ¿funcionaron las alertas de alto score? (outcomes resueltos)."""
    from flow.backtest import format_report, temporal_split_report, walk_forward

    cfg = _cfg(config)
    conn = db.connect(cfg["database"])
    try:
        rep = walk_forward(conn, underlying=underlying, min_score=0.5)
        console.print(format_report(rep))
        ts = temporal_split_report(conn)
        console.print("")
        if ts.get("ok"):
            console.print(
                f"[dim]Split temporal: entrenado en {ts['n_train']} antiguas → "
                f"test hit-rate {ts['test_hit_rate']:.1%} (base {ts['train_hit_rate']:.1%})[/dim]"
            )
            for k, v in ts["precision_at"].items():
                console.print(f"[dim]  test precision {k}: {'—' if v is None else f'{v:.1%}'}[/dim]")
        else:
            console.print(f"[dim]Split temporal: {ts.get('reason')}[/dim]")
    finally:
        conn.close()


@app.command()
def prune(
    keep_days: int = typer.Option(14, "--keep-days", help="Keep snapshots newer than N days"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Delete snapshots (and orphan scans) older than --keep-days. Asks first."""
    cfg = _cfg(config)
    conn = db.connect(cfg["database"])
    try:
        old = conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE scan_id IN "
            "(SELECT id FROM scans WHERE scanned_at < datetime('now', ?))",
            (f"-{int(keep_days)} days",),
        ).fetchone()["n"]
        if not old:
            console.print("[dim]Nothing to prune.[/dim]")
            return
        if not yes and not typer.confirm(f"Delete {old} snapshots older than {keep_days}d?"):
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)
        n = db.cleanup_old_snapshots(conn, keep_days=keep_days)
        console.print(f"[green]Pruned {n} old snapshots.[/green]")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
