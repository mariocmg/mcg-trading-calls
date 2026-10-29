"""Punto de entrada: UN comando para todo.

  python -m flow              -> servidor web + escaneo en bucle (lo normal)
  python -m flow scan [SYM]   -> una pasada y veredicto por consola, sin servidor
"""

from __future__ import annotations

import argparse
import sys


def cmd_scan(ticker: str | None = None) -> int:
    from flow.server import run_scan

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    state = run_scan(ticker)
    sym = state.get("ticker", "MU")
    v = state["verdict"]
    pos = state.get("positioning") or {}
    print(f"{sym}  spot ${state['spot']}" if state["spot"] else f"{sym}  spot ?")
    print(f"{v['direction']} — {pos.get('thesis_es') or v['headline']}")
    print(v["evidence"])
    print(
        f"Neto ponderado por convicción: ${v['net']:,} "
        f"(IA ponderado ${v.get('net_ai', v['net']):,} · sin ponderar ${v['net_unweighted']:,})"
    )
    print(
        f"Confianza {v['confidence']} · etapa {v['stage_label']} · streak {v['streak']} scans · "
        f"Filtro IA: {v.get('n_ai_kept', 0)}/{v.get('n_prints', 0)} jugadas limpias"
    )
    for p in state["prints"]:
        flags = [
            s
            for s, on in (
                (p.get("ai_label", ""), True),
                ("NUEVA", p.get("is_new_position")),
                ("BARRIDO", p.get("sweep")),
                (p.get("ai_structure", ""), bool(p.get("ai_structure"))),
            )
            if on and s
        ]
        print(
            f"  {p['call_put']} {p['strike']:g} {p['expiry']} {p['side']:7} "
            f"prima ${p['premium']:,}  Δvol {p['delta_vol']}  vol/OI {p['volume']}/{p['open_interest']}  "
            f"dOI {p.get('d_oi')}  conv {p.get('conviction')}  {' · '.join(flags)}"
        )
    if state["levels"]:
        lv = state["levels"]
        gex_str = f" | {lv.get('gamma_regime_label')}" if lv.get("net_gex") is not None else ""
        print(
            f"Niveles ({lv['expiry']}): muro call {lv['call_wall']['strike']:g} | "
            f"muro put {lv['put_wall']['strike']:g} | max pain {lv['max_pain']['strike']:g}{gex_str}"
        )
    if state["error"]:
        print(f"AVISO: {state['error']}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flow",
        description="FLOW: posicionamiento institucional con Filtro IA al estilo BladeMap.",
    )
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "scan"])
    parser.add_argument("ticker", nargs="?", default=None, help="Ticker opcional (ej. MU, NVDA, TSLA, SPY)")
    args = parser.parse_args(argv)
    if args.command == "scan":
        return cmd_scan(args.ticker)
    from flow.server import serve

    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
