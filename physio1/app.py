import hmac, os, datetime, threading, json, uuid, secrets
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   send_from_directory, abort, session, jsonify)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

PORT = 5002

app = Flask(__name__)

_secret = os.environ.get("PHYSIO_SECRET_KEY")
if not _secret:
    raise RuntimeError("PHYSIO_SECRET_KEY is not set. Run setup_env.ps1 first.")
app.secret_key = _secret

IS_PRODUCTION = bool(os.environ.get("PHYSIO_PRODUCTION"))
app.config.update(
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8),
    WTF_CSRF_TIME_LIMIT=3600,
)

csrf    = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[])

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONTENT_DIR   = os.path.join(BASE_DIR, 'content')
IMAGES_DIR    = os.path.join(CONTENT_DIR, 'images')
CONTENT_FILE  = os.path.join(CONTENT_DIR, 'content.json')
BOOKINGS_FILE = os.path.join(CONTENT_DIR, 'bookings.json')

os.makedirs(IMAGES_DIR, exist_ok=True)

ADMIN_USER = os.environ.get("PHYSIO_ADMIN_USER", "")
ADMIN_PASS = os.environ.get("PHYSIO_ADMIN_PASS", "")
if not ADMIN_USER or not ADMIN_PASS:
    raise RuntimeError("PHYSIO_ADMIN_USER and PHYSIO_ADMIN_PASS must be set. Run setup_env.ps1 first.")

_content_lock  = threading.Lock()
_bookings_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_content() -> dict:
    try:
        with open(CONTENT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_content(data: dict) -> None:
    with open(CONTENT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_content() -> dict:
    with _content_lock:
        return _load_content()


def _load_bookings() -> list:
    try:
        with open(BOOKINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_bookings(bookings: list) -> None:
    with open(BOOKINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(bookings, f, indent=2, ensure_ascii=False)


def get_bookings() -> list:
    with _bookings_lock:
        return _load_bookings()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Static content — images only (bookings.json / content.json are NOT served)
# ---------------------------------------------------------------------------

@app.route('/content/images/<path:filename>')
def content_images(filename: str):
    return send_from_directory(IMAGES_DIR, filename)


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html', c=get_content(), active='home')


@app.route('/about')
def about():
    return render_template('about.html', c=get_content(), active='about')


@app.route('/services')
def services():
    return render_template('services.html', c=get_content(), active='services')


@app.route('/pricing')
def pricing():
    return render_template('pricing.html', c=get_content(), active='pricing')


@app.route('/booking', methods=['GET'])
def booking():
    c = get_content()
    if not c.get('settings', {}).get('booking_enabled', True):
        return render_template('booking_closed.html', c=c, active='booking')
    return render_template('booking.html', c=c, active='booking')


# ---------------------------------------------------------------------------
# API — available time slots (GET, no CSRF needed)
# ---------------------------------------------------------------------------

@app.route('/api/slots')
@csrf.exempt
def api_slots():
    date_str = request.args.get('date', '')
    if not date_str:
        return jsonify([])

    c = get_content()
    avail   = c.get('availability', {})
    start_h = avail.get('start_hour', 8)
    end_h   = avail.get('end_hour', 18)
    slot_m  = avail.get('slot_duration', 60)
    allowed = avail.get('days', [0, 1, 2, 3, 4, 5])

    try:
        date_obj = datetime.date.fromisoformat(date_str)
    except ValueError:
        return jsonify([])

    if date_obj.weekday() not in allowed or date_obj < datetime.date.today():
        return jsonify([])

    all_slots = []
    current = start_h * 60
    while current < end_h * 60:
        h, m = divmod(current, 60)
        all_slots.append(f"{h:02d}:{m:02d}")
        current += slot_m

    booked = {
        b['time'] for b in get_bookings()
        if b['date'] == date_str and b['status'] != 'cancelled'
    }

    def fmt(t: str) -> str:
        h, m = map(int, t.split(':'))
        h12 = h % 12 or 12
        return f"{h12}:{m:02d}{'am' if h < 12 else 'pm'}"

    return jsonify([{'value': s, 'label': fmt(s)} for s in all_slots if s not in booked])


# ---------------------------------------------------------------------------
# Booking form POST
# ---------------------------------------------------------------------------

@app.route('/booking', methods=['POST'])
def booking_post():
    c = get_content()

    name      = request.form.get('name',      '')[:100].strip()
    email     = request.form.get('email',     '')[:200].strip()
    phone     = request.form.get('phone',     '')[:50].strip()
    service   = request.form.get('service',   '')[:100].strip()
    date_str  = request.form.get('date',      '')[:20].strip()
    time_slot = request.form.get('time_slot', '')[:10].strip()
    notes     = request.form.get('notes',     '')[:1000].strip()

    # Required field check
    if not all([name, email, service, date_str, time_slot]):
        return redirect('/booking?error=missing')

    # Validate service exists in current pricing
    valid_services = [p['name'] for p in c.get('pricing', [])]
    if service not in valid_services:
        return redirect('/booking?error=invalid')

    # Validate date
    try:
        date_obj = datetime.date.fromisoformat(date_str)
        if date_obj < datetime.date.today():
            return redirect('/booking?error=invalid')
    except ValueError:
        return redirect('/booking?error=invalid')

    price = next(
        (p['price'] for p in c.get('pricing', []) if p['name'] == service), 0
    )

    payment_enabled = c.get('settings', {}).get('payment_enabled', True)
    booking_id = str(uuid.uuid4())  # full UUID — not truncated

    record = {
        'id':             booking_id,
        'name':           name,
        'email':          email,
        'phone':          phone,
        'service':        service,
        'date':           date_str,
        'time':           time_slot,
        'notes':          notes,
        'price':          price,
        'payment_status': 'paid' if payment_enabled else 'pending',
        'status':         'confirmed',
        'created_at':     datetime.datetime.now().isoformat(),
    }

    with _bookings_lock:
        bookings = _load_bookings()
        bookings.append(record)
        _save_bookings(bookings)

    # Session-gate: only the browser that submitted can view the confirm page
    session['last_booking_id'] = booking_id
    return redirect(f'/booking/confirm/{booking_id}')


@app.route('/booking/confirm/<booking_id>')
def booking_confirm(booking_id: str):
    if session.get('last_booking_id') != booking_id:
        abort(404)
    session.pop('last_booking_id', None)
    booking = next((b for b in get_bookings() if b['id'] == booking_id), None)
    if not booking:
        abort(404)
    return render_template('booking_confirm.html', c=get_content(), booking=booking, active='booking')


# ---------------------------------------------------------------------------
# Admin — auth
# ---------------------------------------------------------------------------

@app.route('/admin/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def admin_login():
    error = None
    if request.method == 'POST':
        u = request.form.get('username', '')
        p = request.form.get('password', '')
        # Constant-time comparison prevents timing oracle attacks
        user_ok = hmac.compare_digest(u, ADMIN_USER)
        pass_ok = hmac.compare_digest(p, ADMIN_PASS)
        if user_ok and pass_ok:
            session.clear()
            session['admin'] = True
            return redirect('/admin')
        error = 'Incorrect username or password.'
    return render_template('admin_login.html', error=error)


@app.route('/admin/logout', methods=['POST'])
@login_required
def admin_logout():
    session.clear()
    return redirect('/admin/login')


# ---------------------------------------------------------------------------
# Admin — dashboard
# ---------------------------------------------------------------------------

@app.route('/admin')
@login_required
def admin():
    return render_template('admin.html', c=get_content(), bookings=get_bookings())


# ---------------------------------------------------------------------------
# Admin — banner
# ---------------------------------------------------------------------------

@app.route('/admin/banner', methods=['POST'])
@login_required
def admin_banner():
    enabled = request.form.get('enabled') == 'on'
    message = request.form.get('message', '')[:500].strip()
    btype   = request.form.get('type', 'info')
    if btype not in ('info', 'warning', 'promo'):
        btype = 'info'
    with _content_lock:
        data = _load_content()
        data['banner'] = {'enabled': enabled, 'message': message, 'type': btype}
        _save_content(data)
    return redirect('/admin#banner')


# ---------------------------------------------------------------------------
# Admin — content (hero / about / contact)
# ---------------------------------------------------------------------------

@app.route('/admin/content', methods=['POST'])
@login_required
def admin_content():
    with _content_lock:
        data = _load_content()

        h = data.setdefault('hero', {})
        h['title']        = request.form.get('hero_title',        '')[:200].strip()
        h['subtitle']     = request.form.get('hero_subtitle',     '')[:500].strip()
        h['tagline']      = request.form.get('hero_tagline',      '')[:200].strip()
        h['cta_book']     = request.form.get('hero_cta_book',     'Book a Session')[:50].strip()
        h['cta_services'] = request.form.get('hero_cta_services', 'Our Services')[:50].strip()

        a = data.setdefault('about', {})
        a['heading']        = request.form.get('about_heading',   '')[:200].strip()
        a['intro']          = request.form.get('about_intro',     '')[:500].strip()
        p1 = request.form.get('about_p1', '')[:1000].strip()
        p2 = request.form.get('about_p2', '')[:1000].strip()
        a['paragraphs']     = [p for p in [p1, p2] if p]
        a['therapist_name']  = request.form.get('therapist_name',  '')[:100].strip()
        a['therapist_title'] = request.form.get('therapist_title', '')[:100].strip()
        a['therapist_bio']   = request.form.get('therapist_bio',   '')[:1000].strip()

        ct = data.setdefault('contact', {})
        ct['phone']        = request.form.get('contact_phone', '')[:50].strip()
        ct['email']        = request.form.get('contact_email', '')[:200].strip()
        ct['service_area'] = request.form.get('contact_area',  '')[:200].strip()
        ct['hours']        = request.form.get('contact_hours', '')[:200].strip()

        _save_content(data)
    return redirect('/admin#content')


# ---------------------------------------------------------------------------
# Admin — services
# ---------------------------------------------------------------------------

@app.route('/admin/services', methods=['POST'])
@login_required
def admin_services():
    with _content_lock:
        data = _load_content()
        services = []
        for i in range(10):
            name = request.form.get(f'svc_{i}_name', '').strip()
            if not name:
                continue
            services.append({
                'id':       f'svc_{i}',
                'name':     name,
                'icon':     request.form.get(f'svc_{i}_icon',     '🏥').strip() or '🏥',
                'short':    request.form.get(f'svc_{i}_short',    '')[:500].strip(),
                'duration': request.form.get(f'svc_{i}_duration', '45 min').strip(),
            })
        data['services'] = services
        _save_content(data)
    return redirect('/admin#services')


# ---------------------------------------------------------------------------
# Admin — pricing
# ---------------------------------------------------------------------------

@app.route('/admin/pricing', methods=['POST'])
@login_required
def admin_pricing():
    with _content_lock:
        data = _load_content()
        pricing = []
        for i in range(10):
            name = request.form.get(f'price_{i}_name', '').strip()
            if not name:
                continue
            try:
                price = float(request.form.get(f'price_{i}_price', '0'))
            except ValueError:
                price = 0.0
            pricing.append({
                'name':        name,
                'price':       price,
                'duration':    request.form.get(f'price_{i}_duration',    '').strip(),
                'description': request.form.get(f'price_{i}_description', '')[:500].strip(),
            })
        data['pricing'] = pricing
        _save_content(data)
    return redirect('/admin#pricing')


# ---------------------------------------------------------------------------
# Admin — settings
# ---------------------------------------------------------------------------

@app.route('/admin/settings', methods=['POST'])
@login_required
def admin_settings():
    with _content_lock:
        data = _load_content()
        s = data.setdefault('settings', {})
        s['payment_enabled'] = request.form.get('payment_enabled') == 'on'
        s['booking_enabled'] = request.form.get('booking_enabled') == 'on'
        _save_content(data)
    return redirect('/admin#settings')


# ---------------------------------------------------------------------------
# Admin — booking status
# ---------------------------------------------------------------------------

@app.route('/admin/bookings/<booking_id>/status', methods=['POST'])
@login_required
def admin_booking_status(booking_id: str):
    status = request.form.get('status', 'confirmed')
    if status not in ('confirmed', 'completed', 'cancelled'):
        status = 'confirmed'
    with _bookings_lock:
        bookings = _load_bookings()
        for b in bookings:
            if b['id'] == booking_id:
                b['status'] = status
                break
        _save_bookings(bookings)
    return redirect('/admin#bookings')


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

@app.after_request
def set_security_headers(response):
    response.headers['X-Frame-Options']        = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options']  = 'nosniff'
    response.headers['Referrer-Policy']         = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
    return response


@app.context_processor
def inject_globals():
    return {'current_year': datetime.datetime.now().year}


if __name__ == '__main__':
    from waitress import serve
    serve(app, host='127.0.0.1', port=PORT)
