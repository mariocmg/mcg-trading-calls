from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    underlying TEXT NOT NULL,
    expiry TEXT NOT NULL,
    contract TEXT NOT NULL,
    call_put TEXT NOT NULL,
    strike REAL NOT NULL,
    last REAL,
    bid REAL,
    ask REAL,
    volume INTEGER,
    open_interest INTEGER,
    implied_volatility REAL,
    dte INTEGER,
    UNIQUE(scan_id, contract)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_scan ON snapshots(scan_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_contract ON snapshots(contract);
CREATE INDEX IF NOT EXISTS idx_snapshots_underlying ON snapshots(underlying);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    created_at TEXT NOT NULL,
    event_at TEXT,
    underlying TEXT NOT NULL,
    contract TEXT NOT NULL,
    call_put TEXT NOT NULL,
    strike REAL NOT NULL,
    expiry TEXT NOT NULL,
    dte INTEGER,
    price REAL,
    volume INTEGER,
    open_interest INTEGER,
    delta_volume INTEGER,
    premium_usd REAL,
    vol_oi_ratio REAL,
    reason TEXT NOT NULL,
    side_bias TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_underlying ON alerts(underlying);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_event_at ON alerts(event_at);
CREATE INDEX IF NOT EXISTS idx_alerts_scan ON alerts(scan_id);
"""

_INITIALIZED_DBS: set[str] = set()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    
    path_key = str(db_path.resolve())
    if path_key not in _INITIALIZED_DBS:
        init_db(conn)
        _INITIALIZED_DBS.add(path_key)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    _ensure_indexes(conn)


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_alerts_underlying ON alerts(underlying);
        CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);
        CREATE INDEX IF NOT EXISTS idx_alerts_event_at ON alerts(event_at);
        CREATE INDEX IF NOT EXISTS idx_alerts_scan ON alerts(scan_id);
        CREATE INDEX IF NOT EXISTS idx_alerts_outcome ON alerts(outcome_hit);
        CREATE INDEX IF NOT EXISTS idx_alerts_inst_label ON alerts(inst_label);
        """
    )
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "spot" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN spot REAL")
    if "otm_pct" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN otm_pct REAL")
    if "event_at" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN event_at TEXT")
    if "inst_score" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN inst_score REAL")
    if "inst_label" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN inst_label TEXT")
    if "inst_reason" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN inst_reason TEXT")
    if "cluster_n" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN cluster_n INTEGER")
    if "outcome_hit" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN outcome_hit INTEGER")
    if "outcome_return" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN outcome_return REAL")
    if "outcome_at" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN outcome_at TEXT")
    if "opening" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN opening INTEGER")
    if "inst_subtype" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN inst_subtype TEXT")
    if "inst_ml_prob" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN inst_ml_prob REAL")
    if "inst_pct" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN inst_pct REAL")
    if "is_block" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN is_block INTEGER")
    if "combo_type" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN combo_type TEXT")
    if "outcome_oi_growth" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN outcome_oi_growth REAL")
    if "outcome_adj_return" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN outcome_adj_return REAL")
    for col in ("iv_change", "rel_vol", "oi_change_norm", "tod_score"):
        if col not in cols:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} REAL")
    if "side_conf" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN side_conf REAL")
    if "outcome_horizon_days" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN outcome_horizon_days INTEGER")
    if "campaign_id" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN campaign_id INTEGER")
    # scans: data-quality gate result
    scan_cols = {r[1] for r in conn.execute("PRAGMA table_info(scans)").fetchall()}
    if "quality_ok" not in scan_cols:
        conn.execute("ALTER TABLE scans ADD COLUMN quality_ok INTEGER")
    if "quality_note" not in scan_cols:
        conn.execute("ALTER TABLE scans ADD COLUMN quality_note TEXT")
    # snapshots: gamma for GEX levels
    snap_cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
    if "gamma" not in snap_cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN gamma REAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inst_memory (
            bucket TEXT PRIMARY KEY,
            hits INTEGER NOT NULL DEFAULT 0,
            n INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS strike_levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL REFERENCES scans(id),
            computed_at TEXT NOT NULL,
            underlying TEXT NOT NULL,
            expiry TEXT NOT NULL,
            strike REAL NOT NULL,
            call_oi INTEGER,
            put_oi INTEGER,
            call_gex REAL,
            put_gex REAL,
            net_gex REAL,
            call_premium REAL,
            put_premium REAL,
            UNIQUE(scan_id, underlying, expiry, strike)
        );
        CREATE INDEX IF NOT EXISTS idx_levels_lookup
            ON strike_levels(underlying, expiry, scan_id);
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            underlying TEXT NOT NULL,
            contract TEXT NOT NULL,
            call_put TEXT NOT NULL,
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            direction TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_scan_id INTEGER,
            n_prints INTEGER NOT NULL DEFAULT 0,
            total_premium REAL NOT NULL DEFAULT 0,
            total_delta_volume INTEGER NOT NULL DEFAULT 0,
            avg_score REAL,
            max_score REAL
        );
        CREATE INDEX IF NOT EXISTS idx_campaigns_contract
            ON campaigns(contract, status);
        CREATE INDEX IF NOT EXISTS idx_campaigns_status
            ON campaigns(status, updated_at);
        CREATE TABLE IF NOT EXISTS positioning (
            underlying TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL,
            call_premium REAL,
            put_premium REAL,
            net_premium REAL,
            gex_ok INTEGER,
            gex_strike REAL,
            gex_value REAL,
            gex_side TEXT,
            call_avg_strike REAL,
            put_avg_strike REAL,
            avg_strike REAL
        );
        """
    )
    pos_cols = {r[1] for r in conn.execute("PRAGMA table_info(positioning)").fetchall()}
    if pos_cols:
        if "call_avg_strike" not in pos_cols:
            conn.execute("ALTER TABLE positioning ADD COLUMN call_avg_strike REAL")
        if "put_avg_strike" not in pos_cols:
            conn.execute("ALTER TABLE positioning ADD COLUMN put_avg_strike REAL")
        if "avg_strike" not in pos_cols:
            conn.execute("ALTER TABLE positioning ADD COLUMN avg_strike REAL")
    conn.commit()


def cleanup_old_snapshots(conn: sqlite3.Connection, keep_days: int = 14) -> int:
    """Prune snapshots older than keep_days to control database growth."""
    cur = conn.execute(
        """
        DELETE FROM snapshots
        WHERE scan_id IN (
            SELECT id FROM scans WHERE scanned_at < datetime('now', ?)
        )
        """,
        (f"-{int(keep_days)} days",),
    )
    conn.commit()
    # Prune unreferenced scans
    conn.execute(
        """
        DELETE FROM scans
        WHERE scanned_at < datetime('now', ?)
          AND id NOT IN (SELECT DISTINCT scan_id FROM alerts)
        """,
        (f"-{int(keep_days)} days",),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def save_positioning(conn: sqlite3.Connection, summary: dict[str, Any]) -> None:
    gex = summary.get("gex") or {}
    conn.execute(
        """
        INSERT INTO positioning (
            underlying, updated_at, call_premium, put_premium, net_premium,
            gex_ok, gex_strike, gex_value, gex_side,
            call_avg_strike, put_avg_strike, avg_strike
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(underlying) DO UPDATE SET
            updated_at = excluded.updated_at,
            call_premium = excluded.call_premium,
            put_premium = excluded.put_premium,
            net_premium = excluded.net_premium,
            gex_ok = excluded.gex_ok,
            gex_strike = excluded.gex_strike,
            gex_value = excluded.gex_value,
            gex_side = excluded.gex_side,
            call_avg_strike = excluded.call_avg_strike,
            put_avg_strike = excluded.put_avg_strike,
            avg_strike = excluded.avg_strike
        """,
        (
            summary["underlying"],
            datetime.now(timezone.utc).isoformat(),
            summary.get("call_premium"),
            summary.get("put_premium"),
            summary.get("net_premium"),
            1 if gex.get("ok") else 0,
            gex.get("strike"),
            gex.get("gex"),
            gex.get("side"),
            summary.get("call_avg_strike"),
            summary.get("put_avg_strike"),
            summary.get("avg_strike"),
        ),
    )
    conn.commit()


def load_positioning(conn: sqlite3.Connection, underlying: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM positioning WHERE underlying = ?",
        (underlying.upper(),),
    ).fetchone()
    return dict(row) if row else None


def start_scan(conn: sqlite3.Connection) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("INSERT INTO scans (scanned_at) VALUES (?)", (now,))
    conn.commit()
    return int(cur.lastrowid)


def insert_snapshots(
    conn: sqlite3.Connection, scan_id: int, rows: Iterable[dict[str, Any]]
) -> int:
    payload = [
        (
            scan_id,
            r["underlying"],
            r["expiry"],
            r["contract"],
            r["call_put"],
            r["strike"],
            r.get("last"),
            r.get("bid"),
            r.get("ask"),
            r.get("volume"),
            r.get("open_interest"),
            r.get("implied_volatility"),
            r.get("dte"),
            r.get("gamma"),
        )
        for r in rows
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO snapshots (
            scan_id, underlying, expiry, contract, call_put, strike,
            last, bid, ask, volume, open_interest, implied_volatility, dte, gamma
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def insert_alerts(conn: sqlite3.Connection, alerts: Iterable[dict[str, Any]]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    payload = [
        (
            a["scan_id"],
            now,
            a.get("event_at") or now,
            a["underlying"],
            a["contract"],
            a["call_put"],
            a["strike"],
            a["expiry"],
            a.get("dte"),
            a.get("price"),
            a.get("volume"),
            a.get("open_interest"),
            a.get("delta_volume"),
            a.get("premium_usd"),
            a.get("vol_oi_ratio"),
            a["reason"],
            a.get("side_bias"),
            a.get("spot"),
            a.get("otm_pct"),
            a.get("inst_score"),
            a.get("inst_label"),
            a.get("inst_reason"),
            a.get("inst_subtype"),
            a.get("inst_ml_prob"),
            a.get("inst_pct"),
            a.get("cluster_n"),
            a.get("opening"),
            a.get("is_block"),
            a.get("combo_type"),
            a.get("iv_change"),
            a.get("rel_vol"),
            a.get("oi_change_norm"),
            a.get("tod_score"),
            a.get("side_conf"),
            a.get("campaign_id"),
        )
        for a in alerts
    ]
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT INTO alerts (
            scan_id, created_at, event_at, underlying, contract, call_put, strike, expiry,
            dte, price, volume, open_interest, delta_volume, premium_usd,
            vol_oi_ratio, reason, side_bias, spot, otm_pct,
            inst_score, inst_label, inst_reason, inst_subtype, inst_ml_prob, inst_pct,
            cluster_n, opening, is_block, combo_type,
            iv_change, rel_vol, oi_change_norm, tod_score, side_conf, campaign_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def previous_scan_id(conn: sqlite3.Connection, current_scan_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM scans WHERE id < ? ORDER BY id DESC LIMIT 1",
        (current_scan_id,),
    ).fetchone()
    return int(row["id"]) if row else None


def load_snapshot_map(conn: sqlite3.Connection, scan_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE scan_id = ?", (scan_id,)
    ).fetchall()
    return {r["contract"]: r for r in rows}


# Labels that pass the institutional gate. Used by every read path so the
# CLI, the web UI and the summary always count the same thing.
INSTITUTIONAL_LABELS = ("INSTITUTIONAL", "HEDGE")

DEFAULT_MIN_SCORE = 0.5


def is_institutional_row(r: sqlite3.Row, min_score: float = DEFAULT_MIN_SCORE) -> bool:
    """Shared predicate: RTH stamp + institutional label + score threshold."""
    from flow.market_hours import is_rth_timestamp

    stamp = r["event_at"] or r["created_at"]
    if not is_rth_timestamp(stamp):
        return False
    keys = r.keys()
    label = r["inst_label"] if "inst_label" in keys else None
    if label not in INSTITUTIONAL_LABELS:
        return False
    score = r["inst_score"] if "inst_score" in keys else None
    if score is None or float(score) < min_score:
        return False
    return True


def recent_alerts(
    conn: sqlite3.Connection,
    limit: int = 50,
    underlying: str | None = None,
    call_put: str | None = None,
    days: int | None = None,
    after_id: int | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if underlying:
        clauses.append("underlying = ?")
        params.append(underlying.upper())
    if call_put:
        clauses.append("call_put = ?")
        params.append(call_put.upper())
    if days is not None:
        clauses.append("COALESCE(event_at, created_at) >= datetime('now', ?)")
        params.append(f"-{int(days)} days")
    if after_id is not None:
        clauses.append("id > ?")
        params.append(int(after_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Over-fetch then apply the shared institutional predicate in Python
    # (the RTH check can't be expressed in SQL).
    fetch_n = max(limit * 5, 250)
    params.append(fetch_n)
    rows = conn.execute(
        f"SELECT * FROM alerts {where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    out: list[sqlite3.Row] = []
    for r in rows:
        if not is_institutional_row(r, min_score=min_score):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def weekly_summary(
    conn: sqlite3.Connection,
    days: int = 7,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Call/put premium totals over the last N days, using the SAME
    institutional predicate as recent_alerts so the numbers agree."""
    rows = conn.execute(
        """
        SELECT underlying, call_put, premium_usd, delta_volume,
               event_at, created_at, inst_label, inst_score
        FROM alerts
        WHERE created_at >= datetime('now', ?)
          AND inst_label IN ('INSTITUTIONAL', 'HEDGE')
          AND inst_score IS NOT NULL
          AND inst_score >= ?
        """,
        (f"-{days} days", min_score),
    ).fetchall()
    agg: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        if not is_institutional_row(r, min_score=min_score):
            continue
        key = (r["underlying"], r["call_put"])
        b = agg.setdefault(key, {"alert_count": 0, "total_premium": 0.0, "total_delta_volume": 0})
        b["alert_count"] += 1
        b["total_premium"] += float(r["premium_usd"] or 0)
        b["total_delta_volume"] += float(r["delta_volume"] or 0)
    return [
        {
            "underlying": u,
            "call_put": cp,
            "alert_count": int(b["alert_count"]),
            "total_premium": b["total_premium"],
            "total_delta_volume": int(b["total_delta_volume"]),
        }
        for (u, cp), b in sorted(agg.items())
    ]


def latest_scan_info(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, scanned_at FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()


def scan_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM scans").fetchone()
    return int(row["n"]) if row else 0


def alert_count(conn: sqlite3.Connection, days: int | None = None) -> int:
    if days is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE created_at >= datetime('now', ?)",
            (f"-{int(days)} days",),
        ).fetchone()
    return int(row["n"]) if row else 0


def list_underlyings(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT underlying FROM alerts ORDER BY underlying"
    ).fetchall()
    return [r["underlying"] for r in rows]


def save_strike_levels(
    conn: sqlite3.Connection, scan_id: int, underlying: str, levels: list[dict[str, Any]]
) -> int:
    """Persist per-strike GEX/OI/premium levels for one scan."""
    now = datetime.now(timezone.utc).isoformat()
    payload = [
        (
            scan_id,
            now,
            underlying.upper(),
            lv["expiry"],
            lv["strike"],
            lv.get("call_oi"),
            lv.get("put_oi"),
            lv.get("call_gex"),
            lv.get("put_gex"),
            lv.get("net_gex"),
            lv.get("call_premium"),
            lv.get("put_premium"),
        )
        for lv in levels
    ]
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO strike_levels (
            scan_id, computed_at, underlying, expiry, strike,
            call_oi, put_oi, call_gex, put_gex, net_gex, call_premium, put_premium
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def load_strike_levels(
    conn: sqlite3.Connection, underlying: str, scan_id: int | None = None
) -> list[dict[str, Any]]:
    """Latest strike levels for an underlying (defaults to the newest scan)."""
    if scan_id is None:
        row = conn.execute(
            "SELECT MAX(scan_id) AS sid FROM strike_levels WHERE underlying = ?",
            (underlying.upper(),),
        ).fetchone()
        if not row or row["sid"] is None:
            return []
        scan_id = int(row["sid"])
    rows = conn.execute(
        "SELECT * FROM strike_levels WHERE underlying = ? AND scan_id = ? ORDER BY strike",
        (underlying.upper(), scan_id),
    ).fetchall()
    return [dict(r) for r in rows]


def contract_delta_history(
    conn: sqlite3.Connection, contract: str, n: int = 5
) -> list[int]:
    """Recent per-scan delta_volume values for a contract (for block detection)."""
    rows = conn.execute(
        "SELECT delta_volume FROM alerts WHERE contract = ? ORDER BY id DESC LIMIT ?",
        (contract, n),
    ).fetchall()
    return [int(r["delta_volume"] or 0) for r in rows]


def contract_volume_history(
    conn: sqlite3.Connection, contract: str, n: int = 10
) -> list[int]:
    """Recent per-scan volumes for a contract (for relative-volume features)."""
    rows = conn.execute(
        "SELECT volume FROM snapshots WHERE contract = ? ORDER BY scan_id DESC LIMIT ?",
        (contract, n),
    ).fetchall()
    return [int(r["volume"] or 0) for r in rows]


def latest_snapshots(
    conn: sqlite3.Connection, underlying: str
) -> list[dict[str, Any]]:
    """Chain rows from the most recent scan for one underlying."""
    row = conn.execute("SELECT MAX(id) AS sid FROM scans").fetchone()
    if not row or row["sid"] is None:
        return []
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE scan_id = ? AND underlying = ?",
        (int(row["sid"]), underlying.upper()),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_oi_map(conn: sqlite3.Connection) -> dict[str, int]:
    """Open interest per contract from the most recent scan."""
    row = conn.execute("SELECT MAX(id) AS sid FROM scans").fetchone()
    if not row or row["sid"] is None:
        return {}
    rows = conn.execute(
        "SELECT contract, open_interest FROM snapshots WHERE scan_id = ?",
        (int(row["sid"]),),
    ).fetchall()
    return {r["contract"]: int(r["open_interest"] or 0) for r in rows}


def calibration_lookup(
    conn: sqlite3.Connection, direction: str, n_checks: int
) -> tuple[int, int]:
    """(hits, n) of resolved outcomes for setups like this one.

    Used to empirically calibrate vector confidence: how often did past
    BULLISH/BEARISH calls with this many evidence checks actually work?
    """
    rows = conn.execute(
        """
        SELECT outcome_hit FROM alerts
        WHERE outcome_hit IS NOT NULL
          AND (
            (call_put = 'C' AND ? = 'BULLISH')
            OR (call_put = 'P' AND ? = 'BEARISH')
            OR (inst_label = 'HEDGE' AND ? = 'BEARISH')
          )
        """,
        (direction, direction, direction),
    ).fetchall()
    # NOTE: n_checks bucketing is approximated by score tercile until we
    # persist the check count per alert (see vector output `checks_ok`).
    hits = sum(int(r["outcome_hit"]) for r in rows)
    return hits, len(rows)


def find_open_campaign(
    conn: sqlite3.Connection, contract: str, max_age_hours: float = 6.0
) -> dict[str, Any] | None:
    """The ACTIVE campaign for this contract, if touched recently."""
    row = conn.execute(
        """
        SELECT * FROM campaigns
        WHERE contract = ? AND status = 'ACTIVE'
          AND updated_at >= datetime('now', ?)
        ORDER BY updated_at DESC LIMIT 1
        """,
        (contract, f"-{max_age_hours} hours"),
    ).fetchone()
    return dict(row) if row else None


def create_campaign(conn: sqlite3.Connection, alert: dict[str, Any]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    direction = (
        "BULLISH"
        if alert.get("call_put") == "C"
        else ("BEARISH" if alert.get("call_put") == "P" else "NEUTRAL")
    )
    cur = conn.execute(
        """
        INSERT INTO campaigns (
            underlying, contract, call_put, strike, expiry, direction,
            status, started_at, updated_at, last_scan_id,
            n_prints, total_premium, total_delta_volume, avg_score, max_score
        ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            alert.get("underlying"), alert.get("contract"), alert.get("call_put"),
            alert.get("strike"), alert.get("expiry"), direction,
            now, now, alert.get("scan_id"),
            float(alert.get("premium_usd") or 0), int(alert.get("delta_volume") or 0),
            alert.get("inst_score"), alert.get("inst_score"),
        ),
    )
    return int(cur.lastrowid)


def touch_campaign(conn: sqlite3.Connection, campaign_id: int, alert: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    score = float(alert.get("inst_score") or 0)
    conn.execute(
        """
        UPDATE campaigns
        SET updated_at = ?, last_scan_id = ?,
            n_prints = n_prints + 1,
            total_premium = total_premium + ?,
            total_delta_volume = total_delta_volume + ?,
            avg_score = (avg_score * n_prints + ?) / (n_prints + 1),
            max_score = MAX(max_score, ?)
        WHERE id = ?
        """,
        (now, alert.get("scan_id"), float(alert.get("premium_usd") or 0),
         int(alert.get("delta_volume") or 0), score, score, campaign_id),
    )


def close_stale_campaigns(conn: sqlite3.Connection, max_age_hours: float = 6.0) -> int:
    cur = conn.execute(
        """
        UPDATE campaigns SET status = 'CLOSED'
        WHERE status = 'ACTIVE' AND updated_at < datetime('now', ?)
        """,
        (f"-{max_age_hours} hours",),
    )
    conn.commit()
    return cur.rowcount


def list_campaigns(
    conn: sqlite3.Connection,
    underlying: str | None = None,
    status: str = "ACTIVE",
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = ["status = ?"]
    params: list[Any] = [status]
    if underlying:
        clauses.append("underlying = ?")
        params.append(underlying.upper())
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT * FROM campaigns WHERE {' AND '.join(clauses)}
        ORDER BY total_premium DESC LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def mark_scan_quality(
    conn: sqlite3.Connection, scan_id: int, ok: bool, note: str = ""
) -> None:
    conn.execute(
        "UPDATE scans SET quality_ok = ?, quality_note = ? WHERE id = ?",
        (1 if ok else 0, note, scan_id),
    )
    conn.commit()
