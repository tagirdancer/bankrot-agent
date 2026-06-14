"""
SQLite-хранилище: сохранённые лоты, история дайджеста, напоминания.
Надёжнее json на Railway (один файл, атомарные записи).
"""
import os, sqlite3, json
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "bankrot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS saved_lots (
        lot_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        title TEXT,
        url TEXT,
        price REAL,
        deadline TEXT,
        lot_json TEXT,
        saved_at TEXT,
        reminded INTEGER DEFAULT 0,
        PRIMARY KEY (lot_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS digest_history (
        lot_id TEXT PRIMARY KEY,
        first_seen TEXT,
        last_seen TEXT,
        last_price REAL,
        show_count INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS agent_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        run_type TEXT,
        categories TEXT,
        all_lots_count INTEGER DEFAULT 0,
        result_count INTEGER DEFAULT 0,
        alerts INTEGER DEFAULT 0,
        skipped INTEGER DEFAULT 0,
        partial INTEGER DEFAULT 0,
        stats_json TEXT,
        results_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_agent_runs_finished ON agent_runs(finished_at DESC);
    """)
    conn.commit()
    conn.close()


def record_digest_lot(lot_id: str, price: float) -> dict:
    """Дедупликация дайджеста: пометка «показывали» / «цена изменилась»."""
    conn = get_conn()
    now = datetime.now().isoformat()
    row = conn.execute("SELECT * FROM digest_history WHERE lot_id=?", (lot_id,)).fetchone()
    note = ""
    if row:
        old_price = row["last_price"] or 0
        conn.execute(
            "UPDATE digest_history SET last_seen=?, last_price=?, show_count=show_count+1 WHERE lot_id=?",
            (now, price, lot_id),
        )
        if price and old_price and abs(price - old_price) > 1000:
            note = f"цена изменилась: было {fmt_price(old_price)} → стало {fmt_price(price)}"
        else:
            note = "показывали ранее"
    else:
        conn.execute(
            "INSERT INTO digest_history (lot_id, first_seen, last_seen, last_price, show_count) VALUES (?,?,?,?,1)",
            (lot_id, now, now, price),
        )
    conn.commit()
    conn.close()
    return {"note": note}


def fmt_price(p):
    try:
        p = float(p)
        if p >= 1_000_000:
            return f"{p/1_000_000:.1f} млн ₽"
        return f"{int(p):,} ₽".replace(",", " ")
    except Exception:
        return "—"


def save_lot_for_user(chat_id: str, lot: dict, an: dict):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO saved_lots (lot_id, chat_id, title, url, price, deadline, lot_json, saved_at, reminded)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(lot_id, chat_id) DO UPDATE SET
            title=excluded.title, url=excluded.url, price=excluded.price,
            deadline=excluded.deadline, lot_json=excluded.lot_json, saved_at=excluded.saved_at
    """, (
        lot.get("id"), str(chat_id), lot.get("title", ""), lot.get("url", ""),
        float(an.get("lot_price_raw", 0) or 0),
        lot.get("application_deadline", ""),
        json.dumps({"lot": lot, "an": {k: an[k] for k in an if k != "extra_checks"}}, ensure_ascii=False),
        now,
    ))
    conn.commit()
    conn.close()


def get_saved_lots(chat_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM saved_lots WHERE chat_id=? ORDER BY saved_at DESC", (str(chat_id),)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_due_reminders() -> list:
    """Лоты с дедлайном через 1-2 дня, ещё не напоминали."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM saved_lots WHERE reminded=0 AND deadline IS NOT NULL AND deadline != ''"
    ).fetchall()
    conn.close()
    due = []
    now = datetime.now()
    for r in rows:
        dl = r["deadline"]
        for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                d = datetime.strptime(dl[:10], fmt)
                days = (d - now).days
                if 1 <= days <= 2:
                    due.append(dict(r))
                break
            except ValueError:
                continue
    return due


def mark_reminded(lot_id: str, chat_id: str):
    conn = get_conn()
    conn.execute(
        "UPDATE saved_lots SET reminded=1 WHERE lot_id=? AND chat_id=?",
        (lot_id, str(chat_id)),
    )
    conn.commit()
    conn.close()


def _an_for_store(an: dict) -> dict:
    skip = {"extra_checks", "verdict_card"}
    return {k: v for k, v in an.items() if k not in skip and isinstance(v, (str, int, float, bool, type(None), list, dict))}


def save_agent_run(started_at: str, run_type: str, categories, results: dict,
                   all_lots_count: int, alerts: int, skipped: int,
                   partial: bool, stats: dict) -> int:
    """Сохраняет снимок прогона для /latest."""
    flat = []
    for cat, items in (results or {}).items():
        for lot, an in items:
            flat.append({
                "cat": cat,
                "lot": lot,
                "an": _an_for_store(an),
                "score": float(an.get("total_score", 0) or 0),
            })
    flat.sort(key=lambda x: x["score"], reverse=True)
    flat = flat[:60]
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO agent_runs (
            started_at, finished_at, run_type, categories,
            all_lots_count, result_count, alerts, skipped, partial,
            stats_json, results_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        started_at, datetime.now().isoformat(), run_type,
        ",".join(sorted(categories)) if categories else "",
        all_lots_count, len(flat), alerts, skipped, 1 if partial else 0,
        json.dumps(stats or {}, ensure_ascii=False),
        json.dumps(flat, ensure_ascii=False),
    ))
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def get_latest_run() -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM agent_runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    data["stats"] = json.loads(data.get("stats_json") or "{}")
    data["results"] = json.loads(data.get("results_json") or "[]")
    return data


def format_latest_run_messages(run: dict, top_n: int = 12) -> list:
    """Текст для /latest — готовый снимок без нового прогона."""
    from analyzer import format_short_lot_message

    try:
        finished = datetime.fromisoformat(run["finished_at"]).strftime("%d.%m.%Y %H:%M")
    except Exception:
        finished = run.get("finished_at", "—")
    run_type = run.get("run_type", "scheduled")
    type_label = {"scheduled": "по расписанию", "manual": "ручной", "gha": "GitHub Actions"}.get(run_type, run_type)
    partial = " ⚠️ _частичный_" if run.get("partial") else ""
    stats = run.get("stats") or {}
    header = (
        f"📋 *Последний прогон* — {finished}{partial}\n"
        f"Тип: {type_label} | изучено: *{run.get('all_lots_count', 0)}* лотов\n"
        f"В снимке: *{run.get('result_count', 0)}* | 🔔 горячих: *{run.get('alerts', 0)}*\n"
    )
    if stats:
        header += (
            f"⏱ сбор {stats.get('collect_sec', 0):.0f}с | "
            f"лёгкий {stats.get('light_sec', 0):.0f}с | "
            f"тяжёлый {stats.get('heavy_sec', 0):.0f}с\n"
        )
    header += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    items = run.get("results") or []
    if not items:
        return [header + "_Лотов в снимке нет — дождитесь следующего прогона._"]
    parts, current = [], header
    for i, item in enumerate(items[:top_n]):
        lot, an = item.get("lot", {}), item.get("an", {})
        block = format_short_lot_message(lot, an, f"#{i + 1} · {item.get('score', '?')}/10") + "\n\n"
        if len(current) + len(block) > 3800:
            parts.append(current)
            current = block
        else:
            current += block
    parts.append(current)
    return parts
