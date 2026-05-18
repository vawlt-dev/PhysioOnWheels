import sqlite3
import os
import uuid
from datetime import datetime, timedelta, date as date_type
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'content', 'bookings.db')

DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS services (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                duration_mins INTEGER NOT NULL DEFAULT 60,
                price         REAL NOT NULL DEFAULT 0,
                active        INTEGER NOT NULL DEFAULT 1,
                display_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS working_hours (
                day_of_week INTEGER PRIMARY KEY,
                start_time  TEXT NOT NULL DEFAULT '08:00',
                end_time    TEXT NOT NULL DEFAULT '18:00',
                enabled     INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS blocked_events (
                id              TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                date            TEXT,
                start_time      TEXT NOT NULL,
                end_time        TEXT NOT NULL,
                recurrence      TEXT NOT NULL DEFAULT 'none',
                recurrence_days TEXT NOT NULL DEFAULT '',
                recurrence_end  TEXT,
                color           TEXT NOT NULL DEFAULT '#f97316'
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id               TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                email            TEXT NOT NULL,
                phone            TEXT NOT NULL DEFAULT '',
                service_id       TEXT,
                service_label    TEXT NOT NULL,
                duration_mins    INTEGER NOT NULL,
                price            REAL NOT NULL DEFAULT 0,
                date             TEXT NOT NULL,
                start_time       TEXT NOT NULL,
                end_time         TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',
                payment_status   TEXT NOT NULL DEFAULT 'unpaid',
                notes            TEXT NOT NULL DEFAULT '',
                cancel_token     TEXT UNIQUE,
                reschedule_token TEXT UNIQUE,
                created_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        # Seed working hours (Mon=0 to Sun=6)
        if conn.execute("SELECT COUNT(*) FROM working_hours").fetchone()[0] == 0:
            rows = [
                (0, '08:00', '18:00', 1),
                (1, '08:00', '18:00', 1),
                (2, '08:00', '18:00', 1),
                (3, '08:00', '18:00', 1),
                (4, '08:00', '18:00', 1),
                (5, '08:00', '13:00', 1),
                (6, '08:00', '18:00', 0),
            ]
            conn.executemany("INSERT INTO working_hours VALUES (?,?,?,?)", rows)

        # Seed default settings
        defaults = {
            'buffer_mins':                '30',
            'booking_horizon_weeks':      '6',
            'booking_enabled':            'true',
            'payment_enabled':            'false',
            'client_cancel_enabled':      'false',
            'client_reschedule_enabled':  'false',
            'email_enabled':              'false',
            'gmail_address':              '',
            'gmail_app_password':         '',
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

        # Seed services from content.json pricing if services table is empty
        if conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0:
            _seed_services_from_json(conn)

        conn.commit()


def _seed_services_from_json(conn: sqlite3.Connection) -> None:
    content_path = os.path.join(BASE_DIR, 'content', 'content.json')
    try:
        import json
        with open(content_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return

    duration_map = {
        '30 min': 30, '45 min': 45, '60 min': 60, '90 min': 90,
        '45-60 min': 60, '45–60 min': 60,
    }

    for i, p in enumerate(data.get('pricing', [])):
        raw_dur = p.get('duration', '60 min').strip()
        dur = duration_map.get(raw_dur, 60)
        conn.execute(
            "INSERT INTO services (id, name, description, duration_mins, price, active, display_order) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (str(uuid.uuid4()), p['name'], p.get('description', ''), dur, p.get('price', 0), i),
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = '') -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else default


def get_all_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r['key']: r['value'] for r in rows}


def save_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()


def save_settings(data: dict) -> None:
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            data.items(),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def get_services(active_only: bool = True) -> list[dict]:
    with get_conn() as conn:
        q = "SELECT * FROM services"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY display_order, name"
        return [dict(r) for r in conn.execute(q).fetchall()]


def get_service(service_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        return dict(row) if row else None


def upsert_service(data: dict) -> str:
    sid = data.get('id') or str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO services "
            "(id, name, description, duration_mins, price, active, display_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                data['name'],
                data.get('description', ''),
                int(data.get('duration_mins', 60)),
                float(data.get('price', 0)),
                1 if data.get('active', True) else 0,
                int(data.get('display_order', 0)),
            ),
        )
        conn.commit()
    return sid


def delete_service(service_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Working hours
# ---------------------------------------------------------------------------

def get_working_hours() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM working_hours ORDER BY day_of_week"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['day_name'] = DAY_NAMES[d['day_of_week']]
            result.append(d)
        return result


def save_working_hours(hours: list[dict]) -> None:
    with get_conn() as conn:
        for h in hours:
            conn.execute(
                "INSERT OR REPLACE INTO working_hours (day_of_week, start_time, end_time, enabled) "
                "VALUES (?, ?, ?, ?)",
                (
                    int(h['day_of_week']),
                    h['start_time'],
                    h['end_time'],
                    1 if h.get('enabled') else 0,
                ),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Blocked events
# ---------------------------------------------------------------------------

def get_all_blocked_events() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM blocked_events ORDER BY date, start_time"
        ).fetchall()]


def add_blocked_event(data: dict) -> str:
    eid = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO blocked_events "
            "(id, title, date, start_time, end_time, recurrence, recurrence_days, recurrence_end, color) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                eid,
                data['title'],
                data.get('date'),
                data['start_time'],
                data['end_time'],
                data.get('recurrence', 'none'),
                data.get('recurrence_days', ''),
                data.get('recurrence_end'),
                data.get('color', '#f97316'),
            ),
        )
        conn.commit()
    return eid


def update_blocked_event(event_id: str, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE blocked_events SET title=?, date=?, start_time=?, end_time=?, "
            "recurrence=?, recurrence_days=?, recurrence_end=?, color=? WHERE id=?",
            (
                data['title'],
                data.get('date'),
                data['start_time'],
                data['end_time'],
                data.get('recurrence', 'none'),
                data.get('recurrence_days', ''),
                data.get('recurrence_end'),
                data.get('color', '#f97316'),
                event_id,
            ),
        )
        conn.commit()


def delete_blocked_event(event_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM blocked_events WHERE id = ?", (event_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def _time_to_mins(t: str) -> int:
    h, m = map(int, t.split(':'))
    return h * 60 + m


def _mins_to_time(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _blocked_on_date(target: date_type) -> list[tuple[int, int]]:
    """Return list of (start_mins, end_mins) blocked intervals for a given date."""
    events = get_all_blocked_events()
    intervals: list[tuple[int, int]] = []
    dow = target.weekday()  # 0=Mon

    for e in events:
        s = _time_to_mins(e['start_time'])
        en = _time_to_mins(e['end_time'])

        if e['recurrence'] == 'none':
            if e['date'] == target.isoformat():
                intervals.append((s, en))

        elif e['recurrence'] == 'daily':
            end_d = e['recurrence_end']
            if end_d and target > date_type.fromisoformat(end_d):
                continue
            if e['date'] and target < date_type.fromisoformat(e['date']):
                continue
            intervals.append((s, en))

        elif e['recurrence'] == 'weekly':
            end_d = e['recurrence_end']
            if end_d and target > date_type.fromisoformat(end_d):
                continue
            if e['date'] and target < date_type.fromisoformat(e['date']):
                continue
            days = [int(d) for d in e['recurrence_days'].split(',') if d.strip()]
            if dow in days:
                intervals.append((s, en))

    return intervals


def is_slot_available(
    target: date_type,
    start_time: str,
    duration_mins: int,
    exclude_id: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Returns (available: bool, reason: str).
    Checks working hours, booking horizon, blocked events, and existing bookings.
    """
    today = date_type.today()
    horizon_weeks = int(get_setting('booking_horizon_weeks', '6'))
    buffer_mins   = int(get_setting('buffer_mins', '30'))

    if target < today:
        return False, 'Date is in the past'

    if (target - today).days > horizon_weeks * 7:
        return False, f'Date is beyond the {horizon_weeks}-week booking horizon'

    # Working hours check
    hours = get_working_hours()
    wh = next((h for h in hours if h['day_of_week'] == target.weekday()), None)
    if not wh or not wh['enabled']:
        return False, 'Not a working day'

    slot_s = _time_to_mins(start_time)
    slot_e = slot_s + duration_mins
    wh_s   = _time_to_mins(wh['start_time'])
    wh_e   = _time_to_mins(wh['end_time'])

    if slot_s < wh_s or slot_e > wh_e:
        return False, 'Outside working hours'

    # Blocked events check
    for (bs, be) in _blocked_on_date(target):
        if slot_s < be and slot_e > bs:
            return False, 'Time is blocked'

    # Existing bookings check (with buffer)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT start_time, end_time FROM bookings "
            "WHERE date = ? AND status NOT IN ('cancelled') AND id != ?",
            (target.isoformat(), exclude_id or ''),
        ).fetchall()

    for row in rows:
        existing_s = _time_to_mins(row['start_time'])
        existing_e = _time_to_mins(row['end_time']) + buffer_mins
        check_e    = slot_e + buffer_mins
        if slot_s < existing_e and check_e > existing_s:
            return False, 'Time slot is already booked'

    return True, ''


def get_available_slots(target: date_type, duration_mins: int) -> list[dict]:
    """Return all available start times for a given date and service duration."""
    hours = get_working_hours()
    wh = next((h for h in hours if h['day_of_week'] == target.weekday()), None)
    if not wh or not wh['enabled']:
        return []

    buffer_mins = int(get_setting('buffer_mins', '30'))
    wh_s = _time_to_mins(wh['start_time'])
    wh_e = _time_to_mins(wh['end_time'])
    slots = []
    current = wh_s

    while current + duration_mins <= wh_e:
        t = _mins_to_time(current)
        available, _ = is_slot_available(target, t, duration_mins)
        if available:
            h = current // 60
            m = current % 60
            h12 = h % 12 or 12
            label = f"{h12}:{m:02d}{'am' if h < 12 else 'pm'}"
            slots.append({'value': t, 'label': label})
        current += buffer_mins if buffer_mins > 0 else 30

    return slots


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

def get_all_bookings(status: Optional[str] = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE status = ? ORDER BY date, start_time",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bookings ORDER BY date DESC, start_time"
            ).fetchall()
        return [dict(r) for r in rows]


def get_booking(booking_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()
        return dict(row) if row else None


def get_booking_by_token(token: str, token_type: str = 'cancel') -> Optional[dict]:
    col = 'cancel_token' if token_type == 'cancel' else 'reschedule_token'
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM bookings WHERE {col} = ?", (token,)
        ).fetchone()
        return dict(row) if row else None


def get_bookings_by_email(email: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE LOWER(email) = LOWER(?) "
            "AND status != 'cancelled' ORDER BY date, start_time",
            (email,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_booking(data: dict) -> str:
    bid    = str(uuid.uuid4())
    c_tok  = secrets_token()
    r_tok  = secrets_token()
    now    = datetime.now().isoformat()

    start_mins = _time_to_mins(data['start_time'])
    end_mins   = start_mins + int(data['duration_mins'])
    end_time   = _mins_to_time(end_mins)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bookings "
            "(id, name, email, phone, service_id, service_label, duration_mins, price, "
            " date, start_time, end_time, status, payment_status, notes, "
            " cancel_token, reschedule_token, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bid,
                data['name'],
                data['email'],
                data.get('phone', ''),
                data.get('service_id'),
                data['service_label'],
                int(data['duration_mins']),
                float(data.get('price', 0)),
                data['date'],
                data['start_time'],
                end_time,
                data.get('status', 'pending'),
                data.get('payment_status', 'unpaid'),
                data.get('notes', ''),
                c_tok,
                r_tok,
                now,
            ),
        )
        conn.commit()
    return bid


def update_booking_status(booking_id: str, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id)
        )
        conn.commit()


def update_booking_payment(booking_id: str, payment_status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE bookings SET payment_status = ? WHERE id = ?",
            (payment_status, booking_id),
        )
        conn.commit()


def reschedule_booking(booking_id: str, new_date: str, new_start: str, duration_mins: int) -> None:
    new_end = _mins_to_time(_time_to_mins(new_start) + duration_mins)
    new_reschedule_token = secrets_token()
    with get_conn() as conn:
        conn.execute(
            "UPDATE bookings SET date=?, start_time=?, end_time=?, reschedule_token=? WHERE id=?",
            (new_date, new_start, new_end, new_reschedule_token, booking_id),
        )
        conn.commit()


def delete_booking(booking_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Token helper (avoids importing secrets in db module callers)
# ---------------------------------------------------------------------------

def secrets_token() -> str:
    import secrets as _secrets
    return _secrets.token_urlsafe(32)
