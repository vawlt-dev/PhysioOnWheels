import hmac, os, datetime, threading, json, uuid, secrets, smtplib
from email.mime.text import MIMEText
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   send_from_directory, abort, session, jsonify)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
import db

try:
    import stripe as _stripe
    _stripe_available = True
except ImportError:
    _stripe_available = False

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

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
CONTENT_DIR  = os.path.join(BASE_DIR, 'content')
IMAGES_DIR   = os.path.join(CONTENT_DIR, 'images')
CONTENT_FILE = os.path.join(CONTENT_DIR, 'content.json')

os.makedirs(IMAGES_DIR, exist_ok=True)
db.init_db()

ADMIN_USER         = os.environ.get("PHYSIO_ADMIN_USER", "")
ADMIN_PASS         = os.environ.get("PHYSIO_ADMIN_PASS", "")
STRIPE_SECRET_KEY  = os.environ.get("PHYSIO_STRIPE_SECRET_KEY", "")
STRIPE_PUBLIC_KEY  = os.environ.get("PHYSIO_STRIPE_PUBLIC_KEY", "")

if not ADMIN_USER or not ADMIN_PASS:
    raise RuntimeError("PHYSIO_ADMIN_USER and PHYSIO_ADMIN_PASS must be set. Run setup_env.ps1 first.")

STRIPE_ENABLED = bool(_stripe_available and STRIPE_SECRET_KEY and STRIPE_PUBLIC_KEY)

_content_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Content helpers
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


# ---------------------------------------------------------------------------
# Email helper
# ---------------------------------------------------------------------------

def send_email(to: str, subject: str, body: str) -> bool:
    s = db.get_all_settings()
    if s.get('email_enabled') != 'true':
        return False
    gmail  = s.get('gmail_address', '')
    app_pw = s.get('gmail_app_password', '')
    if not gmail or not app_pw:
        return False
    try:
        msg = MIMEText(body, 'html')
        msg['Subject'] = subject
        msg['From']    = gmail
        msg['To']      = to
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as srv:
            srv.login(gmail, app_pw)
            srv.sendmail(gmail, to, msg.as_string())
        return True
    except Exception:
        return False


def booking_email_body(booking: dict, extra: str = '') -> str:
    return f"""
    <p>Hi {booking['name']},</p>
    <p>Here are your appointment details:</p>
    <table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px;">
      <tr><td style="padding:4px 12px 4px 0;color:#64748b;">Service</td>
          <td><strong>{booking['service_label']}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#64748b;">Date</td>
          <td><strong>{booking['date']}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#64748b;">Time</td>
          <td><strong>{booking['start_time']}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#64748b;">Duration</td>
          <td><strong>{booking['duration_mins']} min</strong></td></tr>
    </table>
    {extra}
    <p style="color:#64748b;font-size:12px;margin-top:24px;">PhysioOnWheels</p>
    """


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
# Static — images only
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
    return render_template('pricing.html', c=get_content(), active='pricing',
                           db_services=db.get_services())


@app.route('/booking', methods=['GET'])
def booking():
    c = get_content()
    s = db.get_all_settings()
    if s.get('booking_enabled') != 'true':
        return render_template('booking_closed.html', c=c, active='booking')
    return render_template('booking.html', c=c, active='booking',
                           services=db.get_services(),
                           settings=s,
                           stripe_public_key=STRIPE_PUBLIC_KEY,
                           stripe_enabled=STRIPE_ENABLED)


# ---------------------------------------------------------------------------
# API — available slots for a service on a date
# ---------------------------------------------------------------------------

@app.route('/api/slots')
@csrf.exempt
def api_slots():
    date_str    = request.args.get('date', '')
    service_id  = request.args.get('service_id', '')

    if not date_str:
        return jsonify([])

    # Resolve duration: from service or fallback param
    if service_id:
        svc = db.get_service(service_id)
        duration = svc['duration_mins'] if svc else 60
    else:
        try:
            duration = int(request.args.get('duration', 60))
        except ValueError:
            duration = 60

    try:
        date_obj = datetime.date.fromisoformat(date_str)
    except ValueError:
        return jsonify([])

    slots = db.get_available_slots(date_obj, duration)
    return jsonify(slots)


# ---------------------------------------------------------------------------
# Booking form POST
# ---------------------------------------------------------------------------

@app.route('/booking', methods=['POST'])
def booking_post():
    s = db.get_all_settings()
    if s.get('booking_enabled') != 'true':
        abort(403)

    name       = request.form.get('name',       '')[:100].strip()
    email      = request.form.get('email',      '')[:200].strip()
    phone      = request.form.get('phone',      '')[:50].strip()
    service_id = request.form.get('service_id', '')
    date_str   = request.form.get('date',       '')[:20].strip()
    start_time = request.form.get('start_time', '')[:10].strip()
    notes      = request.form.get('notes',      '')[:1000].strip()

    if not all([name, email, date_str, start_time, service_id]):
        return redirect('/booking?error=missing')

    svc = db.get_service(service_id)
    if not svc:
        return redirect('/booking?error=invalid')

    try:
        date_obj = datetime.date.fromisoformat(date_str)
    except ValueError:
        return redirect('/booking?error=invalid')

    available, reason = db.is_slot_available(date_obj, start_time, svc['duration_mins'])
    if not available:
        return redirect(f'/booking?error={reason}')

    payment_enabled = s.get('payment_enabled') == 'true'

    booking_id = db.create_booking({
        'name':          name,
        'email':         email,
        'phone':         phone,
        'service_id':    service_id,
        'service_label': svc['name'],
        'duration_mins': svc['duration_mins'],
        'price':         svc['price'],
        'date':          date_str,
        'start_time':    start_time,
        'status':        'confirmed',
        'payment_status': 'unpaid',
        'notes':         notes,
    })

    booking = db.get_booking(booking_id)

    # Send confirmation email
    send_email(
        email,
        'Booking Confirmed — PhysioOnWheels',
        booking_email_body(booking, f"""
        <p>We look forward to seeing you.</p>
        <p><a href="{request.host_url}booking/manage/{booking['cancel_token']}">View, reschedule or cancel your appointment</a></p>
        """),
    )
    # Notify Thandeka
    owner_email = s.get('gmail_address', '')
    if owner_email:
        send_email(
            owner_email,
            f'New Booking — {name}',
            booking_email_body(booking, f'<p>Phone: {phone}</p><p>Notes: {notes}</p>'),
        )

    session['last_booking_id'] = booking_id
    return redirect(f'/booking/confirm/{booking_id}')


@app.route('/booking/confirm/<booking_id>')
def booking_confirm(booking_id: str):
    if session.get('last_booking_id') != booking_id:
        abort(404)
    session.pop('last_booking_id', None)
    booking = db.get_booking(booking_id)
    if not booking:
        abort(404)
    return render_template('booking_confirm.html', c=get_content(),
                           booking=booking, active='booking')


# ---------------------------------------------------------------------------
# Session lookup
# ---------------------------------------------------------------------------

@app.route('/my-bookings', methods=['GET', 'POST'])
def my_bookings():
    c = get_content()
    if request.method == 'GET':
        return render_template('my_bookings.html', c=c, active='')

    email = request.form.get('email', '').strip()[:200]
    if not email:
        return render_template('my_bookings.html', c=c, active='', error='Please enter your email.')

    bookings = db.get_bookings_by_email(email)
    if bookings:
        s = db.get_all_settings()
        lines = []
        for b in bookings:
            url = f"{request.host_url}booking/view/{b['id']}"
            lines.append(
                f"<b>{b['service_label']}</b> on {b['date']} at {b['start_time']}<br>"
                f'<a href="{url}">{url}</a>'
            )
        body = (
            '<p>Here are your upcoming appointments:</p>'
            + '<br><br>'.join(lines)
            + '<p style="color:#64748b;font-size:12px;margin-top:24px;">PhysioOnWheels</p>'
        )
        send_email(email, 'Your PhysioOnWheels Appointments', body)

    # Always show the same message (don't leak whether email exists)
    return render_template('my_bookings.html', c=c, active='',
                           sent=True, email=email)


@app.route('/booking/view/<booking_id>')
def booking_view(booking_id: str):
    booking = db.get_booking(booking_id)
    if not booking:
        abort(404)
    return redirect(f'/booking/manage/{booking["cancel_token"]}')


# ---------------------------------------------------------------------------
# Manage appointment (unified pay / reschedule / cancel page)
# ---------------------------------------------------------------------------

@app.route('/booking/manage/<token>', methods=['GET', 'POST'])
def booking_manage(token: str):
    booking = db.get_booking_by_token(token, 'cancel')
    if not booking:
        booking = db.get_booking_by_token(token, 'reschedule')
    if not booking:
        abort(404)

    s = db.get_all_settings()
    c = get_content()
    flash_success = flash_error = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'cancel':
            if s.get('client_cancel_enabled') != 'true':
                flash_error = 'Cancellations are not currently available online. Please contact us.'
            elif booking['status'] == 'cancelled':
                flash_error = 'This appointment is already cancelled.'
            else:
                db.update_booking_status(booking['id'], 'cancelled')
                booking = db.get_booking(booking['id'])
                send_email(
                    booking['email'],
                    'Appointment Cancelled — PhysioOnWheels',
                    booking_email_body(booking, '<p>Your appointment has been cancelled as requested.</p>'),
                )
                flash_success = 'Your appointment has been cancelled.'

        elif action == 'reschedule':
            if s.get('client_reschedule_enabled') != 'true':
                flash_error = 'Online rescheduling is not currently available. Please contact us.'
            else:
                new_date  = request.form.get('date',       '')[:20].strip()
                new_start = request.form.get('start_time', '')[:10].strip()
                if not new_date or not new_start:
                    return redirect(f'/booking/manage/{token}?reschedule_error=Please+select+a+date+and+time.')
                try:
                    date_obj = datetime.date.fromisoformat(new_date)
                except ValueError:
                    return redirect(f'/booking/manage/{token}?reschedule_error=Invalid+date.')
                available, reason = db.is_slot_available(
                    date_obj, new_start, booking['duration_mins'],
                    exclude_id=booking['id'],
                )
                if not available:
                    return redirect(f'/booking/manage/{token}?reschedule_error={reason}')
                db.reschedule_booking(booking['id'], new_date, new_start, booking['duration_mins'])
                booking = db.get_booking(booking['id'])
                send_email(
                    booking['email'],
                    'Appointment Rescheduled — PhysioOnWheels',
                    booking_email_body(booking, '<p>Your appointment has been moved to the new time above.</p>'),
                )
                flash_success = 'Your appointment has been rescheduled.'

        booking = db.get_booking(booking['id'])

    return render_template('booking_manage.html', c=c,
                           booking=booking, settings=s,
                           stripe_public_key=STRIPE_PUBLIC_KEY,
                           stripe_enabled=STRIPE_ENABLED,
                           flash_success=flash_success,
                           flash_error=flash_error,
                           active='')


# Legacy URL redirects — keep old email links working
@app.route('/booking/cancel/<token>', methods=['GET'])
def booking_cancel_redirect(token: str):
    return redirect(f'/booking/manage/{token}')

@app.route('/booking/reschedule/<token>', methods=['GET'])
def booking_reschedule_redirect(token: str):
    booking = db.get_booking_by_token(token, 'reschedule')
    if not booking:
        abort(404)
    return redirect(f'/booking/manage/{booking["cancel_token"]}')

@app.route('/pay/<booking_id>')
def custom_pay(booking_id: str):
    booking = db.get_booking(booking_id)
    if not booking:
        abort(404)
    return redirect(f'/booking/manage/{booking["cancel_token"]}')


# ---------------------------------------------------------------------------
# Stripe payment endpoints
# ---------------------------------------------------------------------------

@app.route('/api/payment/intent', methods=['POST'])
@csrf.exempt
def payment_intent():
    if not STRIPE_ENABLED:
        return jsonify({'error': 'Stripe not configured'}), 503

    data       = request.get_json(silent=True) or {}
    booking_id = data.get('booking_id', '')

    if booking_id:
        # custom_pay.html path — booking already exists
        booking = db.get_booking(booking_id)
        if not booking:
            return jsonify({'error': 'Booking not found'}), 404
    else:
        # booking.html path — create booking now, before payment
        service_id = data.get('service_id', '').strip()
        date       = data.get('date', '').strip()
        start_time = data.get('start_time', '').strip()
        name       = data.get('name', '').strip()
        email      = data.get('email', '').strip()
        if not all([service_id, date, start_time, name, email]):
            return jsonify({'error': 'Missing required fields'}), 400
        svc = db.get_service(service_id)
        if not svc:
            return jsonify({'error': 'Unknown service'}), 400
        ok, msg = db.is_slot_available(date, start_time, svc['duration_mins'])
        if not ok:
            return jsonify({'error': msg}), 409
        booking_id = db.create_booking({
            'service_id':    service_id,
            'service_label': svc['name'],
            'duration_mins': svc['duration_mins'],
            'price':         svc['price'],
            'date':          date,
            'start_time':    start_time,
            'name':          name,
            'email':         email,
            'phone':         data.get('phone', ''),
            'notes':         data.get('notes', ''),
            'payment_status': 'unpaid',
        })
        booking = db.get_booking(booking_id)
        if not booking:
            return jsonify({'error': 'Could not create booking'}), 500

    if booking['payment_status'] == 'paid':
        return jsonify({'error': 'Already paid'}), 400
    if booking['price'] <= 0:
        return jsonify({'error': 'No charge required'}), 400

    _stripe.api_key = STRIPE_SECRET_KEY
    intent = _stripe.PaymentIntent.create(
        amount=int(round(booking['price'] * 100)),
        currency='nzd',
        metadata={'booking_id': booking_id},
    )
    return jsonify({'client_secret': intent['client_secret'], 'booking_id': booking_id})


@app.route('/api/payment/complete', methods=['POST'])
@csrf.exempt
def payment_complete():
    data       = request.get_json(silent=True) or {}
    booking_id = data.get('booking_id', '')
    booking    = db.get_booking(booking_id)

    if not booking:
        return jsonify({'error': 'Booking not found'}), 404

    db.update_booking_payment(booking_id, 'paid')
    db.update_booking_status(booking_id, 'confirmed')

    s = db.get_all_settings()
    owner_email = s.get('gmail_address', '')
    send_email(
        booking['email'],
        'Payment Confirmed — PhysioOnWheels',
        booking_email_body(booking, f'<p>Your payment has been received and your appointment is confirmed.</p><p><a href="{request.host_url}booking/manage/{booking["cancel_token"]}">View your appointment</a></p>'),
    )
    if owner_email:
        send_email(owner_email, f'Payment Received — {booking["name"]}',
                   booking_email_body(booking))

    return jsonify({'ok': True})


@app.route('/api/payment/demo', methods=['POST'])
@csrf.exempt
def payment_demo():
    if STRIPE_ENABLED:
        return jsonify({'error': 'Demo payment not available when Stripe is configured'}), 400

    data       = request.get_json(silent=True) or {}
    booking_id = data.get('booking_id', '')
    booking    = db.get_booking(booking_id)

    if not booking:
        return jsonify({'error': 'Booking not found'}), 404
    if booking['payment_status'] == 'paid':
        return jsonify({'error': 'Already paid'}), 400

    db.update_booking_payment(booking_id, 'paid')
    db.update_booking_status(booking_id, 'confirmed')

    s = db.get_all_settings()
    owner_email = s.get('gmail_address', '')
    booking = db.get_booking(booking_id)
    send_email(
        booking['email'],
        'Payment Confirmed — PhysioOnWheels',
        booking_email_body(booking, f'<p>Your payment has been received and your appointment is confirmed.</p><p><a href="{request.host_url}booking/manage/{booking["cancel_token"]}">View your appointment</a></p>'),
    )
    if owner_email:
        send_email(owner_email, f'Payment Received — {booking["name"]}',
                   booking_email_body(booking))

    return jsonify({'ok': True})


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
    return render_template(
        'admin.html',
        c=get_content(),
        bookings=db.get_all_bookings(),
        services=db.get_services(active_only=False),
        working_hours=db.get_working_hours(),
        blocked_events=db.get_all_blocked_events(),
        settings=db.get_all_settings(),
    )


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
        a['heading']         = request.form.get('about_heading',   '')[:200].strip()
        a['intro']           = request.form.get('about_intro',     '')[:500].strip()
        p1 = request.form.get('about_p1', '')[:1000].strip()
        p2 = request.form.get('about_p2', '')[:1000].strip()
        a['paragraphs']      = [p for p in [p1, p2] if p]
        a['therapist_name']  = request.form.get('therapist_name',  '')[:100].strip()
        a['therapist_title'] = request.form.get('therapist_title', '')[:100].strip()
        a['therapist_bio']   = request.form.get('therapist_bio',   '')[:1000].strip()

        ct = data.setdefault('contact', {})
        ct['phone']        = request.form.get('contact_phone', '')[:50].strip()
        ct['email']        = request.form.get('contact_email', '')[:200].strip()
        ct['service_area'] = request.form.get('contact_area',  '')[:200].strip()
        # hours are derived from working_hours table — not stored here
        _save_content(data)
    return redirect('/admin#content')


# ---------------------------------------------------------------------------
# Admin — services (display text, separate from bookable services in DB)
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
                'icon':     request.form.get(f'svc_{i}_icon',     '').strip() or '',
                'short':    request.form.get(f'svc_{i}_short',    '')[:500].strip(),
                'duration': request.form.get(f'svc_{i}_duration', '').strip(),
            })
        data['services'] = services
        _save_content(data)
    return redirect('/admin#services')


# ---------------------------------------------------------------------------
# Admin — bookable services (DB)
# ---------------------------------------------------------------------------

@app.route('/admin/bookable-services', methods=['POST'])
@login_required
def admin_bookable_services():
    action = request.form.get('action', '')

    if action == 'delete':
        sid = request.form.get('service_id', '')
        if sid:
            db.delete_service(sid)
        return redirect('/admin#bookable-services')

    # upsert
    for i in range(20):
        name = request.form.get(f'bs_{i}_name', '').strip()
        if not name:
            continue
        db.upsert_service({
            'id':            request.form.get(f'bs_{i}_id', '').strip() or None,
            'name':          name,
            'description':   request.form.get(f'bs_{i}_desc',     '')[:500].strip(),
            'duration_mins': int(request.form.get(f'bs_{i}_dur',  60) or 60),
            'price':         float(request.form.get(f'bs_{i}_price', 0) or 0),
            'active':        request.form.get(f'bs_{i}_active') == 'on',
            'display_order': i,
        })
    return redirect('/admin#bookable-services')


# ---------------------------------------------------------------------------
# Admin — working hours
# ---------------------------------------------------------------------------

@app.route('/admin/working-hours', methods=['POST'])
@login_required
def admin_working_hours():
    hours = []
    for day in range(7):
        hours.append({
            'day_of_week': day,
            'start_time':  request.form.get(f'wh_{day}_start', '08:00'),
            'end_time':    request.form.get(f'wh_{day}_end',   '18:00'),
            'enabled':     request.form.get(f'wh_{day}_enabled') == 'on',
        })
    db.save_working_hours(hours)
    return redirect('/admin#working-hours')


# ---------------------------------------------------------------------------
# Admin — blocked events
# ---------------------------------------------------------------------------

@app.route('/admin/blocked-events', methods=['POST'])
@login_required
def admin_blocked_events():
    action = request.form.get('action', 'add')

    if action == 'delete':
        eid = request.form.get('event_id', '')
        if eid:
            db.delete_blocked_event(eid)
        return redirect('/admin#blocked-events')

    if action == 'update':
        eid = request.form.get('event_id', '')
        if eid:
            db.update_blocked_event(eid, _blocked_event_from_form())
        return redirect('/admin#blocked-events')

    db.add_blocked_event(_blocked_event_from_form())
    return redirect('/admin#blocked-events')


def _blocked_event_from_form() -> dict:
    recurrence = request.form.get('recurrence', 'none')
    days_raw   = request.form.getlist('recurrence_days')
    return {
        'title':           request.form.get('title',          '')[:200].strip(),
        'date':            request.form.get('date',           '') or None,
        'start_time':      request.form.get('start_time',     ''),
        'end_time':        request.form.get('end_time',       ''),
        'recurrence':      recurrence,
        'recurrence_days': ','.join(days_raw),
        'recurrence_end':  request.form.get('recurrence_end', '') or None,
        'color':           request.form.get('color',          '#f97316'),
    }


# ---------------------------------------------------------------------------
# Admin — settings
# ---------------------------------------------------------------------------

@app.route('/admin/settings', methods=['POST'])
@login_required
def admin_settings():
    db.save_settings({
        'booking_enabled':           'true' if request.form.get('booking_enabled')          == 'on' else 'false',
        'payment_enabled':           'true' if request.form.get('payment_enabled')           == 'on' else 'false',
        'client_cancel_enabled':     'true' if request.form.get('client_cancel_enabled')     == 'on' else 'false',
        'client_reschedule_enabled': 'true' if request.form.get('client_reschedule_enabled') == 'on' else 'false',
        'email_enabled':             'true' if request.form.get('email_enabled')             == 'on' else 'false',
        'buffer_mins':               str(int(request.form.get('buffer_mins', 30) or 30)),
        'booking_horizon_weeks':     str(int(request.form.get('booking_horizon_weeks', 6) or 6)),
        'gmail_address':             request.form.get('gmail_address',     '')[:200].strip(),
        'gmail_app_password':        request.form.get('gmail_app_password','')[:200].strip(),
    })
    return redirect('/admin#settings')


# ---------------------------------------------------------------------------
# Admin — booking status
# ---------------------------------------------------------------------------

@app.route('/admin/bookings/<booking_id>/status', methods=['POST'])
@login_required
def admin_booking_status(booking_id: str):
    status = request.form.get('status', 'confirmed')
    if status not in ('confirmed', 'completed', 'cancelled', 'pending'):
        status = 'confirmed'
    db.update_booking_status(booking_id, status)
    return redirect('/admin#bookings')


# ---------------------------------------------------------------------------
# Admin — create booking manually
# ---------------------------------------------------------------------------

@app.route('/admin/bookings/create', methods=['POST'])
@login_required
def admin_create_booking():
    name       = request.form.get('name',       '')[:100].strip()
    email      = request.form.get('email',      '')[:200].strip()
    phone      = request.form.get('phone',      '')[:50].strip()
    date_str   = request.form.get('date',       '')[:20].strip()
    start_time = request.form.get('start_time', '')[:10].strip()
    notes      = request.form.get('notes',      '')[:1000].strip()
    is_custom  = request.form.get('is_custom') == 'on'

    if not all([name, email, date_str, start_time]):
        return redirect('/admin#bookings?error=missing')

    try:
        date_obj = datetime.date.fromisoformat(date_str)
    except ValueError:
        return redirect('/admin#bookings?error=invalid')

    if is_custom:
        label    = request.form.get('custom_label', '')[:200].strip() or 'Custom Appointment'
        duration = int(request.form.get('custom_duration', 60) or 60)
        price    = float(request.form.get('custom_price', 0) or 0)
        sid      = None
    else:
        sid = request.form.get('service_id', '')
        svc = db.get_service(sid) if sid else None
        if not svc:
            return redirect('/admin#bookings?error=invalid')
        label    = svc['name']
        duration = svc['duration_mins']
        price    = svc['price']

    # Admin can override conflict checking — skip it for manual bookings
    booking_id = db.create_booking({
        'name':          name,
        'email':         email,
        'phone':         phone,
        'service_id':    sid,
        'service_label': label,
        'duration_mins': duration,
        'price':         price,
        'date':          date_str,
        'start_time':    start_time,
        'status':        'confirmed',
        'payment_status': 'unpaid',
        'notes':         notes,
    })

    # If custom booking, send payment link email
    if is_custom:
        booking = db.get_booking(booking_id)
        send_email(
            email,
            f'Payment Required — PhysioOnWheels',
            booking_email_body(booking, f"""
            <p>Please complete your booking by making payment:</p>
            <p><a href="{request.host_url}pay/{booking_id}" style="background:#0d9488;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px;">Pay ${price:.2f} NZD</a></p>
            """),
        )

    return redirect('/admin#bookings')


# ---------------------------------------------------------------------------
# Admin — send payment link for existing booking
# ---------------------------------------------------------------------------

@app.route('/admin/bookings/<booking_id>/send-payment-link', methods=['POST'])
@login_required
def admin_send_payment_link(booking_id: str):
    booking = db.get_booking(booking_id)
    if not booking:
        abort(404)

    manage_url = f"{request.host_url}booking/manage/{booking['cancel_token']}"
    btn = lambda label, url: f'<p><a href="{url}" style="background:#0d9488;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px;">{label}</a></p>'

    if booking['payment_status'] == 'paid':
        subject = 'Appointment Confirmation — PhysioOnWheels'
        extra   = f'<p>Your appointment is confirmed and payment has been received. We look forward to seeing you.</p>{btn("View Appointment", manage_url)}'
    else:
        price_label = f"Pay ${booking['price']:.2f} NZD"
        subject = 'Complete Your Booking — PhysioOnWheels'
        extra   = f'<p>Please complete your booking by making payment:</p>{btn(price_label, manage_url)}'

    send_email(booking['email'], subject, booking_email_body(booking, extra))
    return redirect('/admin#bookings')


# ---------------------------------------------------------------------------
# Admin — calendar API (FullCalendar feed)
# ---------------------------------------------------------------------------

@app.route('/api/admin/calendar')
@login_required
@csrf.exempt
def admin_calendar_feed():
    start_str = request.args.get('start', '')
    end_str   = request.args.get('end',   '')
    events    = []

    # Bookings
    for b in db.get_all_bookings():
        if b['status'] == 'cancelled':
            continue
        color_map = {'confirmed': '#0d9488', 'pending': '#f97316', 'completed': '#64748b'}
        events.append({
            'id':    f"booking-{b['id']}",
            'title': f"{b['name']} — {b['service_label']}",
            'start': f"{b['date']}T{b['start_time']}",
            'end':   f"{b['date']}T{b['end_time']}",
            'color': color_map.get(b['status'], '#0d9488'),
            'extendedProps': {'type': 'booking', 'booking_id': b['id']},
        })

    # Blocked events
    try:
        start_d = datetime.date.fromisoformat(start_str[:10]) if start_str else None
        end_d   = datetime.date.fromisoformat(end_str[:10])   if end_str   else None
    except ValueError:
        start_d = end_d = None

    for e in db.get_all_blocked_events():
        if e['recurrence'] == 'none':
            if e['date']:
                events.append({
                    'id':    f"block-{e['id']}",
                    'title': e['title'],
                    'start': f"{e['date']}T{e['start_time']}",
                    'end':   f"{e['date']}T{e['end_time']}",
                    'color': e['color'],
                    'extendedProps': {'type': 'block', 'event_id': e['id']},
                })
        else:
            # Expand recurring events into the requested window
            if start_d and end_d:
                cur = start_d
                while cur <= end_d:
                    should_include = False
                    if e['recurrence'] == 'daily':
                        should_include = True
                    elif e['recurrence'] == 'weekly':
                        days = [int(d) for d in e['recurrence_days'].split(',') if d.strip()]
                        should_include = cur.weekday() in days

                    if e['recurrence_end']:
                        try:
                            if cur > datetime.date.fromisoformat(e['recurrence_end']):
                                should_include = False
                        except ValueError:
                            pass

                    if should_include:
                        events.append({
                            'id':    f"block-{e['id']}-{cur.isoformat()}",
                            'title': e['title'],
                            'start': f"{cur.isoformat()}T{e['start_time']}",
                            'end':   f"{cur.isoformat()}T{e['end_time']}",
                            'color': e['color'],
                            'extendedProps': {'type': 'block', 'event_id': e['id']},
                        })
                    cur += datetime.timedelta(days=1)

    return jsonify(events)


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


def _format_working_hours() -> str:
    hours = db.get_working_hours()
    enabled = [h for h in hours if h['enabled']]
    if not enabled:
        return 'By appointment'

    def fmt_time(t: str) -> str:
        h, m = map(int, t.split(':'))
        h12 = h % 12 or 12
        suffix = 'am' if h < 12 else 'pm'
        return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"

    # Group consecutive days with identical hours
    short = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    groups = []
    i = 0
    while i < len(enabled):
        j = i
        while (j + 1 < len(enabled)
               and enabled[j + 1]['start_time'] == enabled[i]['start_time']
               and enabled[j + 1]['end_time']   == enabled[i]['end_time']
               and enabled[j + 1]['day_of_week'] == enabled[j]['day_of_week'] + 1):
            j += 1
        start_day = short[enabled[i]['day_of_week']]
        end_day   = short[enabled[j]['day_of_week']]
        time_str  = f"{fmt_time(enabled[i]['start_time'])}–{fmt_time(enabled[i]['end_time'])}"
        if i == j:
            groups.append(f"{start_day} {time_str}")
        else:
            groups.append(f"{start_day}–{end_day} {time_str}")
        i = j + 1

    return ' · '.join(groups)


@app.context_processor
def inject_globals():
    return {
        'current_year': datetime.datetime.now().year,
        'working_hours_display': _format_working_hours(),
    }


if __name__ == '__main__':
    from waitress import serve
    serve(app, host='127.0.0.1', port=PORT)
