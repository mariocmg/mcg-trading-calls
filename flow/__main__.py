"""Punto de entrada: UN comando para todo.

  python -m flow           -> servidor web + escaneo en bucle (lo normal)
  python -m flow scan      -> una pasada y veredicto por consola, sin servidor
"""

from __future__ import annotations

import argparse
import sys


def cmd_scan() -> int:
    from flow.server import run_scan

    state = run_scan()
    v = state["verdict"]
    print(f"MU  spot ${state['spot']}" if state["spot"] else "MU  spot ?")
    print(f"{v['direction']} — {v['headline']}")
    print(v["evidence"])
    print(f"Neto con sesgo: ${v['net']:,}")
    for p in state["prints"]:
        print(
            f"  {p['call_put']} {p['strike']:g} {p['expiry']} {p['side']:7} "
            f"prima ${p['premium']:,}  Δvol {p['delta_vol']}  vol/OI {p['volume']}/{p['open_interest']}"
        )
    if state["levels"]:
        lv = state["levels"]
        print(
            f"Niveles ({lv['expiry']}): muro call {lv['call_wall']['strike']:g} | "
            f"muro put {lv['put_wall']['strike']:g} | max pain {lv['max_pain']['strike']:g}"
        )
    if state["error"]:
        print(f"AVISO: {state['error']}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flow", description="FLOW: posicionamiento institucional en MU, directo.")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "scan"])
    args = parser.parse_args(argv)
    if args.command == "scan":
        return cmd_scan()
    from flow.server import serve

    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
