"""
База данных — история лотов, портфель, статистика
"""
import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "bankrot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Создаёт таблицы если не существуют"""
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS lots (
        id TEXT PRIMARY KEY,
        title TEXT,
        url TEXT,
        category TEXT,
        region TEXT,
        first_seen TEXT,
        last_seen TEXT
    );

    CREATE TABLE IF NOT EXISTS lot_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lot_id TEXT,
        price REAL,
        step_current INTEGER,
        step_total INTEGER,
        score REAL,
        action TEXT,
        recorded_at TEXT,
        FOREIGN KEY(lot_id) REFERENCES lots(id)
    );

    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lot_id TEXT,
        sent_at TEXT,
        score REAL,
        action TEXT
    );

    CREATE TABLE IF NOT EXISTS portfolio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lot_id TEXT,
        title TEXT,
        url TEXT,
        category TEXT,
        buy_price REAL,
        market_price REAL,
        status TEXT DEFAULT 'watching',
        added_at TEXT,
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        lots_analyzed INTEGER,
        lots_recommended INTEGER,
        lots_go INTEGER,
        lots_wait INTEGER,
        categories TEXT
    );
    """)
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")


def save_lot(lot: dict, analysis: dict):
    """Сохраняет лот и его текущую цену"""
    conn = get_conn()
    now = datetime.now().isoformat()

    # Сохраняем/обновляем лот
    conn.execute("""
        INSERT INTO lots (id, title, url, category, region, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            category=excluded.category,
            last_seen=excluded.last_seen
    """, (
        lot.get("id"), lot.get("title"), lot.get("url"),
        lot.get("category"), lot.get("region"), now, now
    ))

    # Сохраняем цену
    price_str = analysis.get("price", "0")
    price_num = 0
    try:
        price_num = float(
            price_str.replace("млн ₽", "000000")
                     .replace(" ₽", "")
                     .replace(" ", "")
                     .replace(",", ".")
        )
        if "млн" in str(analysis.get("price", "")):
            price_num = float(price_str.replace(" млн ₽", "").replace(",", ".")) * 1_000_000
    except:
        pass

    conn.execute("""
        INSERT INTO lot_prices (lot_id, price, step_current, step_total, score, action, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        lot.get("id"), price_num,
        lot.get("step_current", 0), lot.get("step_total", 0),
        analysis.get("total_score", 0), analysis.get("action", ""),
        now
    ))

    conn.commit()
    conn.close()


def get_price_history(lot_id: str) -> list:
    """Возвращает историю цен лота"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT price, step_current, step_total, score, action, recorded_at
        FROM lot_prices
        WHERE lot_id = ?
        ORDER BY recorded_at ASC
    """, (lot_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_price_trend(lot_id: str) -> dict:
    """Анализирует тренд цены"""
    history = get_price_history(lot_id)
    if len(history) < 2:
        return {"trend": "новый", "drop_pct": 0, "days_tracked": 0, "history": history}

    first_price = history[0]["price"]
    last_price  = history[-1]["price"]
    drop_pct = round((first_price - last_price) / first_price * 100) if first_price > 0 else 0

    first_dt = datetime.fromisoformat(history[0]["recorded_at"])
    last_dt  = datetime.fromisoformat(history[-1]["recorded_at"])
    days = (last_dt - first_dt).days

    return {
        "trend":         "снижается" if drop_pct > 0 else "стабильна",
        "first_price":   first_price,
        "last_price":    last_price,
        "drop_pct":      drop_pct,
        "days_tracked":  days,
        "checks_count":  len(history),
        "history":       history
    }


def was_notified_recently(lot_id: str, hours: int = 24) -> bool:
    """Проверяет не уведомляли ли мы об этом лоте недавно"""
    conn = get_conn()
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    row = conn.execute("""
        SELECT id FROM notifications
        WHERE lot_id = ? AND sent_at > ?
    """, (lot_id, cutoff)).fetchone()
    conn.close()
    return row is not None


def mark_notified(lot_id: str, score: float, action: str):
    """Отмечает что уведомление отправлено"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO notifications (lot_id, sent_at, score, action)
        VALUES (?, ?, ?, ?)
    """, (lot_id, datetime.now().isoformat(), score, action))
    conn.commit()
    conn.close()


def save_stats(stats: dict):
    """Сохраняет статистику запуска"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO stats (date, lots_analyzed, lots_recommended, lots_go, lots_wait, categories)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        stats.get("analyzed", 0),
        stats.get("recommended", 0),
        stats.get("go", 0),
        stats.get("wait", 0),
        json.dumps(stats.get("categories", {}), ensure_ascii=False)
    ))
    conn.commit()
    conn.close()


def get_portfolio() -> list:
    """Возвращает портфель наблюдаемых лотов"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM portfolio ORDER BY added_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_to_portfolio(lot: dict, analysis: dict, notes: str = ""):
    """Добавляет лот в портфель наблюдения"""
    conn = get_conn()
    price_str = analysis.get("price", "0")
    market_str = analysis.get("market_price", "0")

    def parse_price(s):
        try:
            if "млн" in str(s):
                return float(str(s).replace(" млн ₽","").replace(",",".")) * 1_000_000
            return float(str(s).replace(" ₽","").replace(" ",""))
        except:
            return 0

    conn.execute("""
        INSERT INTO portfolio (lot_id, title, url, category, buy_price, market_price, added_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        lot.get("id"), lot.get("title"), lot.get("url"),
        lot.get("category"), parse_price(price_str), parse_price(market_str),
        datetime.now().isoformat(), notes
    ))
    conn.commit()
    conn.close()


def get_global_stats() -> dict:
    """Общая статистика работы агента"""
    conn = get_conn()
    total_lots   = conn.execute("SELECT COUNT(DISTINCT lot_id) FROM lot_prices").fetchone()[0]
    total_runs   = conn.execute("SELECT COUNT(*) FROM stats").fetchone()[0]
    top_cats     = conn.execute("""
        SELECT category, COUNT(*) as cnt FROM lots
        GROUP BY category ORDER BY cnt DESC
    """).fetchall()
    recent_go    = conn.execute("""
        SELECT COUNT(*) FROM lot_prices WHERE action='ВХОДИТЬ СЕЙЧАС'
        AND recorded_at > datetime('now', '-7 days')
    """).fetchone()[0]
    conn.close()
    return {
        "total_lots":  total_lots,
        "total_runs":  total_runs,
        "recent_go":   recent_go,
        "top_cats":    [(r["category"], r["cnt"]) for r in top_cats],
    }
