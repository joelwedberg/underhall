import builtins
import hashlib
import hmac
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from io import BytesIO
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request,
    send_from_directory, session, url_for
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from PIL import Image
from sqlalchemy import func, select, text
from werkzeug.middleware.proxy_fix import ProxyFix

UTC = timezone.utc
ph = PasswordHasher()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config["SECRET_KEY"] = os.environ.get("APP_SECRET", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://underhall:underhall@db:5432/underhall"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
max_mb = int(os.environ.get("MAX_UPLOAD_MB", "8"))
app.config["MAX_CONTENT_LENGTH"] = max_mb * 1024 * 1024

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
limiter = Limiter(key_func=get_remote_address, app=app, default_limits=[])

BASE_URL = os.environ.get("BASE_URL", "http://localhost:18090").rstrip("/")
DASHBOARD_ACCESS_CODE = os.environ.get("DASHBOARD_ACCESS_CODE", "246810")
SMTP_HOST = os.environ.get("SMTP_HOST", "mailpit")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
SMTP_FROM = os.environ.get("SMTP_FROM", "underhall@demo.local")
UPLOAD_DIR = Path("/app/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ROLE_ADMIN = "admin"
ROLE_TECH = "technician"

REPORT_STATUS = {
    "new": "Ny",
    "assigned": "Tilldelad",
    "in_progress": "Pågår",
    "resolved": "Klar",
}

PRIORITY_LABELS = {
    1: "Prio 1",
    2: "Prio 2",
    3: "Prio 3",
}


def now_utc():
    return datetime.now(UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_password(value: str) -> bool:
    return (
        len(value) >= 8
        and bool(re.search(r"[A-Za-zÅÄÖåäö]", value))
        and bool(re.search(r"\d", value))
    )


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    phone = db.Column(db.String(50))
    role = db.Column(db.String(30), nullable=False, default=ROLE_TECH)
    password_hash = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    activation_token_hash = db.Column(db.String(64), index=True)
    activation_expires_at = db.Column(db.DateTime(timezone=True))
    reset_token_hash = db.Column(db.String(64), index=True)
    reset_expires_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)

    def set_password(self, password):
        self.password_hash = ph.hash(password)

    def verify_password(self, password):
        if not self.password_hash:
            return False
        try:
            ph.verify(self.password_hash, password)
            return True
        except VerifyMismatchError:
            return False

    @property
    def role_label(self):
        return "Admin" if self.role == ROLE_ADMIN else "Tekniker"


class Property(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    places = db.relationship("Place", backref="property", lazy=True, cascade="all, delete-orphan")


class Place(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("property.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)


class FaultReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("property.id"), nullable=False, index=True)
    place_id = db.Column(db.Integer, db.ForeignKey("place.id"), nullable=False, index=True)
    summary = db.Column(db.String(180), nullable=False)
    details = db.Column(db.Text)
    custom_place = db.Column(db.String(180))
    reporter_email = db.Column(db.String(255), nullable=False, index=True)
    image_filename = db.Column(db.String(255))
    status = db.Column(db.String(30), nullable=False, default="new", index=True)
    assignee_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    tracking_token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    source_task_item_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)
    resolved_at = db.Column(db.DateTime(timezone=True))
    resolved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    property = db.relationship("Property")
    place = db.relationship("Place")
    assignee = db.relationship("User", foreign_keys=[assignee_id])
    resolved_by = db.relationship("User", foreign_keys=[resolved_by_id])

    @builtins.property
    def status_label(self):
        return REPORT_STATUS.get(self.status, self.status)

    @builtins.property
    def location_label(self):
        return self.custom_place.strip() if self.custom_place else self.place.name

    @builtins.property
    def is_recent(self):
        try:
            return (now_utc() - self.created_at) <= timedelta(minutes=30)
        except TypeError:
            return False


class TaskTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    kind = db.Column(db.String(10), nullable=False, index=True)  # FU / EG
    category = db.Column(db.String(80), nullable=False)
    priority = db.Column(db.Integer, nullable=False, default=2)
    recurrence_days = db.Column(db.Integer, nullable=False, default=30)
    property_id = db.Column(db.Integer, db.ForeignKey("property.id"), nullable=False)
    place_id = db.Column(db.Integer, db.ForeignKey("place.id"), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)

    property = db.relationship("Property")
    place = db.relationship("Place")
    items = db.relationship(
        "TemplateItem", backref="template", lazy=True,
        order_by="TemplateItem.sort_order", cascade="all, delete-orphan"
    )

    @builtins.property
    def priority_label(self):
        return PRIORITY_LABELS.get(self.priority, f"Prio {self.priority}")


class TemplateItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("task_template.id"), nullable=False)
    label = db.Column(db.String(220), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    instructions = db.Column(db.Text)
    reference_url = db.Column(db.Text)
    help_image_url = db.Column(db.Text)
    value_min = db.Column(db.Float)
    value_max = db.Column(db.Float)
    value_unit = db.Column(db.String(40))

    @builtins.property
    def is_numeric(self):
        return self.value_min is not None or self.value_max is not None

    def value_in_range(self, value):
        if value is None:
            return None
        if self.value_min is not None and value < self.value_min:
            return False
        if self.value_max is not None and value > self.value_max:
            return False
        return True

    @builtins.property
    def range_label(self):
        if self.value_min is not None and self.value_max is not None:
            base = f"{self.value_min:g}–{self.value_max:g}"
        elif self.value_min is not None:
            base = f"min {self.value_min:g}"
        elif self.value_max is not None:
            base = f"max {self.value_max:g}"
        else:
            return ""
        return f"{base} {self.value_unit}".strip()


class TaskInstance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("task_template.id"), nullable=False, index=True)
    due_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default="open", index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)
    completed_at = db.Column(db.DateTime(timezone=True))
    completed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    template = db.relationship("TaskTemplate")
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    completed_by = db.relationship("User", foreign_keys=[completed_by_id])
    items = db.relationship(
        "TaskItem", backref="task", lazy=True,
        order_by="TaskItem.sort_order", cascade="all, delete-orphan"
    )

    @property
    def checked_count(self):
        return sum(1 for i in self.items if i.checked_at)

    @property
    def total_count(self):
        return len(self.items)

    @property
    def progress_percent(self):
        return int((self.checked_count / self.total_count) * 100) if self.total_count else 0

    @property
    def is_overdue(self):
        return self.status != "done" and self.due_at < now_utc()

    def template_item_for(self, sort_order):
        for item in self.template.items:
            if item.sort_order == sort_order:
                return item
        return None


class TaskItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task_instance.id"), nullable=False, index=True)
    label = db.Column(db.String(220), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    checked_at = db.Column(db.DateTime(timezone=True))
    checked_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    comment = db.Column(db.Text)
    numeric_value = db.Column(db.Float)

    checked_by = db.relationship("User")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event = db.Column(db.String(80), nullable=False, index=True)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.Integer)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    actor_label = db.Column(db.String(120))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc, index=True)

    actor_user = db.relationship("User")


def audit(event, entity_type, entity_id=None, actor_user=None, actor_label=None, details=None):
    db.session.add(AuditLog(
        event=event,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user.id if actor_user else None,
        actor_label=actor_label,
        details=details,
    ))


def send_email(to_addr, subject, body):
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=5) as smtp:
            smtp.send_message(msg)
    except Exception as exc:
        app.logger.warning("Kunde inte skicka mejl till %s: %s", to_addr, exc)


def make_activation(user):
    token = secrets.token_urlsafe(32)
    user.activation_token_hash = token_hash(token)
    user.activation_expires_at = now_utc() + timedelta(hours=24)
    db.session.commit()
    link = f"{BASE_URL}/activate/{token}"
    send_email(
        user.email,
        "Aktivera ditt konto i Driftunderhåll",
        f"Hej {user.name}!\n\nDitt konto är skapat.\n"
        f"Aktivera det och välj lösenord/telefonnummer här:\n{link}\n\n"
        "Länken gäller i 24 timmar."
    )


def make_reset(user):
    token = secrets.token_urlsafe(32)
    user.reset_token_hash = token_hash(token)
    user.reset_expires_at = now_utc() + timedelta(hours=1)
    db.session.commit()
    link = f"{BASE_URL}/reset/{token}"
    send_email(
        user.email,
        "Återställ lösenord – Driftunderhåll",
        f"Hej {user.name}!\n\nVälj ett nytt lösenord här:\n{link}\n\n"
        "Länken gäller i 1 timme."
    )


def get_current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


@app.context_processor
def inject_globals():
    return {
        "current_user": get_current_user(),
        "report_status": REPORT_STATUS,
        "priority_labels": PRIORITY_LABELS,
        "now_utc": now_utc,
    }


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user or not user.is_active:
            session.pop("user_id", None)
            flash("Logga in för att fortsätta.", "warning")
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if user.role != ROLE_ADMIN:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def dashboard_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("dashboard_ok"):
            return redirect(url_for("dashboard_unlock", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def save_image(file):
    if not file or not file.filename:
        return None
    raw = file.read()
    if not raw:
        return None
    try:
        image = Image.open(BytesIO(raw))
        image.verify()
        fmt = (image.format or "").upper()
    except Exception:
        raise ValueError("Bilden kunde inte läsas.")

    ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
    if fmt not in ext_map:
        raise ValueError("Endast JPG, PNG och WebP är tillåtna.")
    filename = f"{secrets.token_hex(16)}{ext_map[fmt]}"
    (UPLOAD_DIR / filename).write_bytes(raw)
    return filename



def ensure_schema_v02():
    """Minimal prototyp-migrering så befintlig PostgreSQL-volym kan återanvändas."""
    statements = [
        "ALTER TABLE fault_report ADD COLUMN IF NOT EXISTS custom_place VARCHAR(180)",
        "ALTER TABLE template_item ADD COLUMN IF NOT EXISTS instructions TEXT",
        "ALTER TABLE template_item ADD COLUMN IF NOT EXISTS reference_url TEXT",
        "ALTER TABLE template_item ADD COLUMN IF NOT EXISTS help_image_url TEXT",
        "ALTER TABLE template_item ADD COLUMN IF NOT EXISTS value_min DOUBLE PRECISION",
        "ALTER TABLE template_item ADD COLUMN IF NOT EXISTS value_max DOUBLE PRECISION",
        "ALTER TABLE template_item ADD COLUMN IF NOT EXISTS value_unit VARCHAR(40)",
        "ALTER TABLE task_item ADD COLUMN IF NOT EXISTS numeric_value DOUBLE PRECISION",
    ]
    for statement in statements:
        db.session.execute(text(statement))
    db.session.commit()


def ensure_other_place(prop):
    place = db.session.execute(
        select(Place).where(
            Place.property_id == prop.id,
            Place.name == "Annan plats"
        )
    ).scalar_one_or_none()
    if not place:
        place = Place(property_id=prop.id, name="Annan plats")
        db.session.add(place)
        db.session.flush()
    return place


def seed_v02_details():
    """Lägger bara till demonstrationsinstruktioner – ändrar inte användarens befintliga data."""
    filter_item = db.session.execute(
        select(TemplateItem)
        .join(TaskTemplate)
        .where(
            TaskTemplate.name == "Kvartalskontroll – ventilation",
            TemplateItem.label == "Filterstatus",
        )
    ).scalar_one_or_none()

    if filter_item and not filter_item.instructions:
        filter_item.instructions = (
            "1. Kontrollera filterindikering/tryckfall på aggregatet.\n"
            "2. Jämför värdet mot aggregatets tillåtna sluttryck.\n"
            "3. Kontrollera att filtren sitter tätt och att ingen synlig bypass finns.\n"
            "4. Om värdet är tveksamt eller filter är skadat: skriv kommentar och skapa en avvikelse."
        )
        filter_item.help_image_url = "/static/filterstatus_example.svg"

    if filter_item and filter_item.value_min is None and filter_item.value_max is None:
        # Demonstrationsvärde för chefsversionen. I mallbyggaren ska detta vara valfritt per moment.
        filter_item.value_min = 0
        filter_item.value_max = 10
        filter_item.value_unit = "nivå"

    alarm_item = db.session.execute(
        select(TemplateItem)
        .join(TaskTemplate)
        .where(
            TaskTemplate.name == "Månadsrond – omklädningsrum",
            TemplateItem.label == "Larmsändare testad",
        )
    ).scalar_one_or_none()

    if alarm_item and not alarm_item.instructions:
        alarm_item.instructions = (
            "Genomför ett funktionsprov enligt lokal rutin. "
            "Bocka momentet när kontrollen är utförd även om resultatet är fel. "
            "Beskriv felet i kommentaren och skapa därefter en felanmälan direkt från momentet."
        )

    db.session.commit()


def dashboard_signature():
    latest_report_id = db.session.execute(select(func.max(FaultReport.id))).scalar() or 0
    report_update = db.session.execute(select(func.max(FaultReport.updated_at))).scalar()
    item_update = db.session.execute(select(func.max(TaskItem.checked_at))).scalar()
    task_complete = db.session.execute(select(func.max(TaskInstance.completed_at))).scalar()

    open_tasks = db.session.execute(
        select(TaskInstance.id, TaskInstance.assigned_to_id, TaskInstance.status)
        .where(TaskInstance.status != "done")
        .order_by(TaskInstance.id)
    ).all()
    task_assignment_state = ",".join(
        f"{row.id}:{row.assigned_to_id or 0}:{row.status}" for row in open_tasks
    )

    def dt(v):
        return v.isoformat() if v else "-"

    return (
        f"{latest_report_id}|{dt(report_update)}|{dt(item_update)}|"
        f"{dt(task_complete)}|{task_assignment_state}"
    )


def create_fault_from_task_item(task, item, user, assignee_id=0, details_override=None):
    existing = db.session.execute(
        select(FaultReport).where(
            FaultReport.source_task_item_id == item.id,
            FaultReport.status != "resolved",
        )
    ).scalar_one_or_none()
    if existing:
        return existing, False

    assignee = db.session.get(User, assignee_id) if assignee_id else None
    raw_token = secrets.token_urlsafe(32)
    report = FaultReport(
        property_id=task.template.property_id,
        place_id=task.template.place_id,
        summary=f"{task.template.name}: {item.label}"[:180],
        details=details_override or item.comment or "Avvikelse skapad från kontrollmoment.",
        reporter_email=user.email,
        status="assigned" if assignee else "new",
        assignee_id=assignee.id if assignee else None,
        tracking_token_hash=token_hash(raw_token),
        source_task_item_id=item.id,
    )
    db.session.add(report)
    db.session.flush()
    audit(
        "report_created_from_task", "fault_report", report.id,
        actor_user=user, details=f"Från delmoment #{item.id}"
    )
    if assignee:
        send_email(
            assignee.email,
            f"Avvikelse/felanmälan #{report.id} tilldelad",
            f"En avvikelse från {task.template.kind} har blivit ett ärende:\n"
            f"{report.summary}\n\n{BASE_URL}/work"
        )
    return report, True


def seed_data():
    if db.session.execute(select(Property)).scalars().first():
        return

    admin = User(name="Demo Admin", email="admin@demo.local", role=ROLE_ADMIN, is_active=False)
    tech = User(name="Pelle Tekniker", email="pelle@demo.local", role=ROLE_TECH, is_active=False)
    db.session.add_all([admin, tech])

    f1 = Property(name="Fastighet 1")
    f2 = Property(name="Fastighet 2")
    db.session.add_all([f1, f2])
    db.session.flush()

    p11 = Place(property_id=f1.id, name="Entré")
    p12 = Place(property_id=f1.id, name="Omklädningsrum 1")
    p13 = Place(property_id=f1.id, name="Teknikrum")
    p21 = Place(property_id=f2.id, name="Entré")
    p22 = Place(property_id=f2.id, name="Omklädningsrum 1")
    p23 = Place(property_id=f2.id, name="Fläktrum")
    db.session.add_all([p11, p12, p13, p21, p22, p23])
    db.session.flush()

    eg1 = TaskTemplate(
        name="Månadsrond – omklädningsrum", kind="EG",
        category="Säkerhet & funktion", priority=1, recurrence_days=30,
        property_id=f1.id, place_id=p12.id
    )
    fu1 = TaskTemplate(
        name="Kvartalskontroll – ventilation", kind="FU",
        category="Ventilation", priority=2, recurrence_days=90,
        property_id=f1.id, place_id=p13.id
    )
    eg2 = TaskTemplate(
        name="Snabbrond – trivsel", kind="EG",
        category="Trivsel", priority=3, recurrence_days=14,
        property_id=f2.id, place_id=p22.id
    )
    fu2 = TaskTemplate(
        name="Funktionskontroll – entrédörrar", kind="FU",
        category="Dörr & lås", priority=1, recurrence_days=180,
        property_id=f2.id, place_id=p21.id
    )
    db.session.add_all([eg1, fu1, eg2, fu2])
    db.session.flush()

    for idx, label in enumerate([
        "Nödutgång fri och öppningsbar",
        "Larmsändare testad",
        "Duschblandare kontrollerade",
        "Lås och beslag kontrollerade",
        "Belysning kontrollerad",
        "Golv och väggar utan uppenbar skada",
    ], 1):
        db.session.add(TemplateItem(template_id=eg1.id, label=label, sort_order=idx))

    for idx, label in enumerate([
        "Filterstatus",
        "Remmar och spänning",
        "Onormalt ljud/vibration",
        "Dränering och kondens",
        "Dokumentera driftanmärkning",
    ], 1):
        db.session.add(TemplateItem(template_id=fu1.id, label=label, sort_order=idx))

    for idx, label in enumerate([
        "Belysning",
        "Städskador",
        "Bänkar och krokar",
        "Dörrar och lås",
    ], 1):
        db.session.add(TemplateItem(template_id=eg2.id, label=label, sort_order=idx))

    for idx, label in enumerate([
        "Dörr 1",
        "Dörr 2",
        "Dörr 3",
        "Dörr 4",
        "Dörr 5",
        "Dörr 6",
        "Dörr 7",
        "Dörr 8",
        "Dörr 9",
        "Dörr 10",
        "Dörr 11",
        "Dörr 12",
    ], 1):
        db.session.add(TemplateItem(template_id=fu2.id, label=label, sort_order=idx))

    db.session.flush()

    def create_instance(template, due_at, assignee=None):
        inst = TaskInstance(
            template_id=template.id,
            due_at=due_at,
            assigned_to_id=assignee.id if assignee else None,
        )
        db.session.add(inst)
        db.session.flush()
        for ti in template.items:
            db.session.add(TaskItem(task_id=inst.id, label=ti.label, sort_order=ti.sort_order))
        return inst

    create_instance(eg1, now_utc() - timedelta(days=1), tech)
    create_instance(fu1, now_utc() + timedelta(days=3), tech)
    create_instance(eg2, now_utc() + timedelta(days=6))
    create_instance(fu2, now_utc() + timedelta(days=9), tech)

    def add_report(prop, place, summary, details, reporter, assignee=None):
        raw = secrets.token_urlsafe(32)
        report = FaultReport(
            property_id=prop.id,
            place_id=place.id,
            summary=summary,
            details=details,
            reporter_email=reporter,
            assignee_id=assignee.id if assignee else None,
            status="assigned" if assignee else "new",
            tracking_token_hash=token_hash(raw),
        )
        db.session.add(report)

    add_report(f1, p12, "Kran droppar", "Kranen vid vänstra tvättstället droppar hela tiden.", "hyresgast1@demo.local")
    add_report(f2, p21, "Belysning släckt", "Två armaturer vid entrén lyser inte.", "hyresgast2@demo.local", tech)
    add_report(f1, p11, "Dörrstängare tar dåligt", "Entrédörren blir ibland stående öppen.", "hyresgast3@demo.local")

    db.session.commit()

    # Första lokala starten: skicka aktiveringsmejl för de två demokontona till Mailpit.
    make_activation(admin)
    make_activation(tech)


with app.app_context():
    db.create_all()
    ensure_schema_v02()
    seed_data()
    seed_v02_details()


@app.get("/")
def index():
    return redirect(url_for("external_report"))


@app.route("/report", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def external_report():
    properties = db.session.execute(select(Property).order_by(Property.name)).scalars().all()
    places = db.session.execute(
        select(Place)
        .where(Place.name != "Annan plats")
        .order_by(Place.property_id, Place.name)
    ).scalars().all()

    if request.method == "POST":
        try:
            property_id = int(request.form.get("property_id", "0"))
        except ValueError:
            property_id = 0

        prop = db.session.get(Property, property_id)
        summary = request.form.get("summary", "").strip()
        details = request.form.get("details", "").strip()
        email = request.form.get("email", "").strip().lower()
        custom_place = request.form.get("custom_place", "").strip()[:180]
        place_value = request.form.get("place_id", "").strip()

        if not prop:
            flash("Välj en giltig fastighet.", "danger")
            return render_template("report.html", properties=properties, places=places)

        place = None
        if custom_place:
            place = ensure_other_place(prop)
        elif place_value:
            try:
                place = db.session.get(Place, int(place_value))
            except (ValueError, TypeError):
                place = None

        if not place or place.property_id != prop.id:
            flash("Välj en plats eller skriv platsen själv.", "danger")
            return render_template("report.html", properties=properties, places=places)

        if not summary or len(summary) > 180:
            flash("Skriv en kort beskrivning av felet.", "danger")
            return render_template("report.html", properties=properties, places=places)
        if "@" not in email or len(email) > 255:
            flash("Ange en giltig e-postadress.", "danger")
            return render_template("report.html", properties=properties, places=places)

        try:
            image_filename = save_image(request.files.get("image"))
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("report.html", properties=properties, places=places)

        raw_token = secrets.token_urlsafe(32)
        report = FaultReport(
            property_id=prop.id,
            place_id=place.id,
            custom_place=custom_place or None,
            summary=summary,
            details=details,
            reporter_email=email,
            image_filename=image_filename,
            status="new",
            tracking_token_hash=token_hash(raw_token),
        )
        db.session.add(report)
        db.session.flush()
        audit("report_created", "fault_report", report.id, actor_label=email, details=summary)
        db.session.commit()

        track_url = f"{BASE_URL}/track/{raw_token}"
        send_email(
            email,
            f"Felanmälan #{report.id} mottagen",
            f"Din felanmälan är mottagen.\n\n"
            f"Ärende: {summary}\n"
            f"Fastighet: {prop.name}\n"
            f"Plats: {report.location_label}\n\n"
            f"Följ ärendet här utan inloggning:\n{track_url}\n"
        )

        return render_template("report_received.html", report=report, track_url=track_url)

    return render_template("report.html", properties=properties, places=places)


@app.get("/track/<token>")
@limiter.limit("60 per hour")
def track_report(token):
    report = db.session.execute(
        select(FaultReport).where(FaultReport.tracking_token_hash == token_hash(token))
    ).scalar_one_or_none()
    if not report:
        abort(404)
    return render_template("track.html", report=report)


@app.get("/uploads/<filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if not user or not user.is_active or not user.verify_password(password):
            flash("Fel e-post, lösenord eller ännu inte aktiverat konto.", "danger")
            return render_template("login.html")
        session.clear()
        session["user_id"] = user.id
        session.permanent = True
        audit("login", "user", user.id, actor_user=user)
        db.session.commit()
        return redirect(request.args.get("next") or url_for("work"))
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.pop("user_id", None)
    flash("Du är utloggad.", "success")
    return redirect(url_for("login"))


@app.route("/resend-activation", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def resend_activation():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = db.session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user and not user.is_active:
            make_activation(user)
        flash("Om kontot finns och inte är aktiverat har ett nytt mejl skickats.", "success")
        return redirect(url_for("login"))
    return render_template("resend_activation.html")


@app.route("/activate/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def activate(token):
    user = db.session.execute(
        select(User).where(User.activation_token_hash == token_hash(token))
    ).scalar_one_or_none()

    if not user or not user.activation_expires_at or user.activation_expires_at < now_utc():
        flash("Aktiveringslänken är ogiltig eller har gått ut.", "danger")
        return redirect(url_for("resend_activation"))

    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if password != password2:
            flash("Lösenorden matchar inte.", "danger")
            return render_template("activate.html", user=user)
        if not valid_password(password):
            flash("Minst 8 tecken, minst en bokstav och minst en siffra.", "danger")
            return render_template("activate.html", user=user)
        user.phone = phone
        user.set_password(password)
        user.is_active = True
        user.activation_token_hash = None
        user.activation_expires_at = None
        audit("user_activated", "user", user.id, actor_user=user)
        db.session.commit()
        flash("Kontot är aktiverat. Du kan nu logga in.", "success")
        return redirect(url_for("login"))

    return render_template("activate.html", user=user)


@app.route("/forgot", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = db.session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user and user.is_active:
            make_reset(user)
        flash("Om kontot finns har ett återställningsmejl skickats.", "success")
        return redirect(url_for("login"))
    return render_template("forgot.html")


@app.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def reset_password(token):
    user = db.session.execute(
        select(User).where(User.reset_token_hash == token_hash(token))
    ).scalar_one_or_none()
    if not user or not user.reset_expires_at or user.reset_expires_at < now_utc():
        flash("Återställningslänken är ogiltig eller har gått ut.", "danger")
        return redirect(url_for("forgot"))

    if request.method == "POST":
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if password != password2:
            flash("Lösenorden matchar inte.", "danger")
            return render_template("reset.html")
        if not valid_password(password):
            flash("Minst 8 tecken, minst en bokstav och minst en siffra.", "danger")
            return render_template("reset.html")
        user.set_password(password)
        user.reset_token_hash = None
        user.reset_expires_at = None
        audit("password_reset", "user", user.id, actor_user=user)
        db.session.commit()
        flash("Lösenordet är ändrat.", "success")
        return redirect(url_for("login"))

    return render_template("reset.html")


@app.route("/dashboard/unlock", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def dashboard_unlock():
    if request.method == "POST":
        code = request.form.get("code", "")
        if hmac.compare_digest(code, DASHBOARD_ACCESS_CODE):
            session["dashboard_ok"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Fel dashboard-kod.", "danger")
    return render_template("dashboard_unlock.html")


@app.get("/dashboard")
@dashboard_required
def dashboard():
    techs = db.session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.name)
    ).scalars().all()

    latest_reports = db.session.execute(
        select(FaultReport).where(FaultReport.status != "resolved")
        .order_by(FaultReport.created_at.desc()).limit(5)
    ).scalars().all()

    fu = db.session.execute(
        select(TaskInstance).join(TaskTemplate)
        .where(TaskTemplate.kind == "FU", TaskInstance.status != "done")
        .order_by(TaskTemplate.priority.asc(), TaskInstance.due_at.asc()).limit(5)
    ).scalars().all()

    eg = db.session.execute(
        select(TaskInstance).join(TaskTemplate)
        .where(TaskTemplate.kind == "EG", TaskInstance.status != "done")
        .order_by(TaskTemplate.priority.asc(), TaskInstance.due_at.asc()).limit(5)
    ).scalars().all()

    critical = db.session.execute(
        select(TaskInstance).join(TaskTemplate)
        .where(
            TaskTemplate.kind == "EG",
            TaskTemplate.priority == 1,
            TaskInstance.status != "done",
            TaskInstance.due_at < now_utc(),
        )
        .order_by(TaskInstance.due_at.asc())
    ).scalars().all()

    return render_template(
        "dashboard.html",
        techs=techs, latest_reports=latest_reports, fu=fu, eg=eg, critical=critical,
        dashboard_sig=dashboard_signature(),
    )


@app.get("/dashboard/state")
@dashboard_required
def dashboard_state():
    return jsonify(signature=dashboard_signature())


@app.get("/dashboard/uploads/<filename>")
@dashboard_required
def dashboard_uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.post("/dashboard/report/<int:report_id>/assign")
@dashboard_required
def dashboard_assign(report_id):
    report = db.session.get(FaultReport, report_id) or abort(404)
    try:
        uid = int(request.form.get("user_id", "0"))
    except ValueError:
        uid = 0
    user = db.session.get(User, uid)
    if not user or not user.is_active:
        flash("Välj en aktiv användare.", "danger")
        return redirect(url_for("dashboard"))

    report.assignee_id = user.id
    report.status = "assigned"
    report.updated_at = now_utc()
    audit(
        "report_assigned", "fault_report", report.id,
        actor_label="Central dashboard",
        details=f"Tilldelad {user.name}"
    )
    db.session.commit()

    send_email(
        user.email,
        f"Felanmälan #{report.id} tilldelad till dig",
        f"Hej {user.name}!\n\nDu har fått ett nytt ärende:\n"
        f"{report.summary}\n{report.property.name} – {report.location_label}\n\n"
        f"Öppna arbetssidan:\n{BASE_URL}/work\n"
    )
    flash(f"Ärende #{report.id} tilldelades {user.name}.", "success")
    return redirect(url_for("dashboard"))


@app.post("/dashboard/task/<int:task_id>/assign")
@dashboard_required
def dashboard_assign_task(task_id):
    task = db.session.get(TaskInstance, task_id) or abort(404)
    try:
        uid = int(request.form.get("user_id", "0"))
    except ValueError:
        uid = 0

    user = db.session.get(User, uid)
    if not user or not user.is_active:
        flash("Välj en aktiv användare.", "danger")
        return redirect(url_for("dashboard"))

    task.assigned_to_id = user.id
    audit(
        "task_assigned", "task_instance", task.id,
        actor_label="Central dashboard",
        details=f"Tilldelad {user.name}"
    )
    db.session.commit()

    send_email(
        user.email,
        f"{task.template.kind} tilldelad – {task.template.name}",
        f"Hej {user.name}!\n\nDu har fått uppgiften:\n"
        f"{task.template.name}\n"
        f"{task.template.property.name} – {task.template.place.name}\n"
        f"Förfallo: {task.due_at:%Y-%m-%d}\n\n"
        f"Öppna arbetssidan:\n{BASE_URL}/work\n"
    )

    flash(f"{task.template.kind} tilldelades {user.name}.", "success")
    return redirect(url_for("dashboard"))


@app.get("/work")
@login_required
def work():
    user = get_current_user()
    tab = request.args.get("tab", "mine")
    techs = db.session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.name)
    ).scalars().all()

    report_stmt = select(FaultReport).where(FaultReport.status != "resolved")
    task_stmt = select(TaskInstance).join(TaskTemplate).where(TaskInstance.status != "done")

    if user.role != ROLE_ADMIN and tab == "mine":
        report_stmt = report_stmt.where(FaultReport.assignee_id == user.id)
        task_stmt = task_stmt.where(TaskInstance.assigned_to_id == user.id)

    if tab == "reports":
        task_instances = []
        reports = db.session.execute(report_stmt.order_by(FaultReport.created_at.desc())).scalars().all()
    elif tab in ("fu", "eg"):
        kind = tab.upper()
        reports = []
        task_instances = db.session.execute(
            task_stmt.where(TaskTemplate.kind == kind).order_by(TaskInstance.due_at.asc())
        ).scalars().all()
    else:
        reports = db.session.execute(report_stmt.order_by(FaultReport.created_at.desc())).scalars().all()
        task_instances = db.session.execute(task_stmt.order_by(TaskInstance.due_at.asc())).scalars().all()

    return render_template(
        "work.html", tab=tab, reports=reports, tasks=task_instances, techs=techs
    )


@app.post("/work/report/<int:report_id>/assign")
@login_required
def work_assign_report(report_id):
    report = db.session.get(FaultReport, report_id) or abort(404)
    try:
        uid = int(request.form.get("user_id", "0"))
    except ValueError:
        uid = 0
    user = db.session.get(User, uid)
    if not user or not user.is_active:
        flash("Välj en aktiv användare.", "danger")
        return redirect(request.referrer or url_for("work"))

    report.assignee_id = user.id
    report.status = "assigned"
    report.updated_at = now_utc()
    actor = get_current_user()
    audit("report_assigned", "fault_report", report.id, actor_user=actor, details=f"Tilldelad {user.name}")
    db.session.commit()

    send_email(
        user.email,
        f"Felanmälan #{report.id} tilldelad till dig",
        f"Hej {user.name}!\n\nDu har fått ärendet:\n{report.summary}\n\n{BASE_URL}/work"
    )
    flash(f"Ärende #{report.id} tilldelades {user.name}.", "success")
    return redirect(request.referrer or url_for("work"))


@app.post("/work/report/<int:report_id>/claim")
@login_required
def claim_report(report_id):
    report = db.session.get(FaultReport, report_id) or abort(404)
    user = get_current_user()
    report.assignee_id = user.id
    report.status = "in_progress"
    report.updated_at = now_utc()
    audit("report_claimed", "fault_report", report.id, actor_user=user)
    db.session.commit()
    flash(f"Du tog ärende #{report.id}.", "success")
    return redirect(request.referrer or url_for("work"))


@app.post("/work/report/<int:report_id>/resolve")
@login_required
def resolve_report(report_id):
    report = db.session.get(FaultReport, report_id) or abort(404)
    user = get_current_user()
    if user.role != ROLE_ADMIN and report.assignee_id not in (None, user.id):
        abort(403)

    report.status = "resolved"
    report.resolved_at = now_utc()
    report.resolved_by_id = user.id
    report.updated_at = now_utc()
    audit("report_resolved", "fault_report", report.id, actor_user=user)
    db.session.commit()

    send_email(
        report.reporter_email,
        f"Felanmälan #{report.id} är markerad som klar",
        f"Ärendet '{report.summary}' är markerat som klart.\n\n"
        "Din tidigare spårningslänk visar aktuell status."
    )
    flash(f"Ärende #{report.id} markerades som klart.", "success")
    return redirect(request.referrer or url_for("work"))


@app.get("/work/task/<int:task_id>")
@login_required
def task_detail(task_id):
    task = db.session.get(TaskInstance, task_id) or abort(404)
    help_by_order = {item.sort_order: item for item in task.template.items}
    techs = db.session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.name)
    ).scalars().all()
    return render_template(
        "task_detail.html", task=task, help_by_order=help_by_order, techs=techs
    )


@app.post("/work/task/<int:task_id>/assign")
@login_required
def assign_task(task_id):
    task = db.session.get(TaskInstance, task_id) or abort(404)
    try:
        uid = int(request.form.get("user_id", "0"))
    except ValueError:
        uid = 0
    user = db.session.get(User, uid)
    if not user or not user.is_active:
        flash("Välj en aktiv användare.", "danger")
        return redirect(request.referrer or url_for("work"))

    task.assigned_to_id = user.id
    actor = get_current_user()
    audit("task_assigned", "task_instance", task.id, actor_user=actor, details=f"Tilldelad {user.name}")
    db.session.commit()

    send_email(
        user.email,
        f"{task.template.kind} tilldelad – {task.template.name}",
        f"Hej {user.name}!\n\nDu har fått uppgiften:\n"
        f"{task.template.name}\n{task.template.property.name} – {task.template.place.name}\n"
        f"Förfallo: {task.due_at:%Y-%m-%d}\n\n{BASE_URL}/work"
    )
    flash(f"Uppgiften tilldelades {user.name}.", "success")
    return redirect(request.referrer or url_for("work"))


@app.post("/work/task/<int:task_id>/save")
@login_required
def save_task(task_id):
    task = db.session.get(TaskInstance, task_id) or abort(404)
    user = get_current_user()
    action = request.form.get("action", "save")
    changed = False
    created_faults = 0
    measurement_faults = 0
    spec_by_order = {spec.sort_order: spec for spec in task.template.items}

    for item in task.items:
        spec = spec_by_order.get(item.sort_order)
        comment = request.form.get(f"comment_{item.id}", "").strip()

        if (item.comment or "") != comment:
            item.comment = comment
            changed = True

        # Mätvärdesmoment: inmatat värde betyder att själva kontrollen är genomförd.
        if spec and spec.is_numeric:
            raw_value = request.form.get(f"value_{item.id}", "").strip().replace(",", ".")
            parsed_value = None

            if raw_value:
                try:
                    parsed_value = float(raw_value)
                except ValueError:
                    flash(f"'{item.label}' måste anges som ett tal.", "danger")
                    return redirect(request.referrer or url_for("work"))

                if item.numeric_value != parsed_value:
                    item.numeric_value = parsed_value
                    changed = True

                if not item.checked_at:
                    item.checked_at = now_utc()
                    item.checked_by_id = user.id
                    changed = True

                if spec.value_in_range(parsed_value) is False:
                    details = (
                        f"Mätvärde utanför tillåtet intervall.\n"
                        f"Moment: {item.label}\n"
                        f"Angivet värde: {parsed_value:g} {spec.value_unit or ''}\n"
                        f"Tillåtet: {spec.range_label}\n"
                        f"Kommentar: {comment or '-'}"
                    )
                    _, created = create_fault_from_task_item(
                        task, item, user, assignee_id=0, details_override=details
                    )
                    created_faults += int(created)
                    measurement_faults += int(created)
            else:
                if item.numeric_value is not None:
                    item.numeric_value = None
                    changed = True
                if item.checked_at:
                    item.checked_at = None
                    item.checked_by_id = None
                    changed = True

        # Vanligt kontrollmoment.
        else:
            checked = request.form.get(f"checked_{item.id}") == "1"
            if checked and not item.checked_at:
                item.checked_at = now_utc()
                item.checked_by_id = user.id
                changed = True
            elif not checked and item.checked_at:
                item.checked_at = None
                item.checked_by_id = None
                changed = True

        # Manuell avvikelse kan skapas oavsett momenttyp.
        if request.form.get(f"create_fault_{item.id}") == "1":
            try:
                assignee_id = int(request.form.get(f"fault_assignee_{item.id}", "0") or "0")
            except ValueError:
                assignee_id = 0
            _, created = create_fault_from_task_item(task, item, user, assignee_id)
            created_faults += int(created)

    if task.status == "open" and (changed or any(i.checked_at for i in task.items)):
        task.status = "in_progress"

    audit(
        "task_progress_saved", "task_instance", task.id,
        actor_user=user,
        details=(
            f"Action={action}; nya avvikelser={created_faults}; "
            f"mätvärdesavvikelser={measurement_faults}"
        )
    )

    all_checked = all(item.checked_at for item in task.items)
    if action == "complete":
        if all_checked:
            task.status = "done"
            task.completed_at = now_utc()
            task.completed_by_id = user.id
            audit("task_completed", "task_instance", task.id, actor_user=user)
            db.session.commit()
            msg = f"{task.template.kind} är slutförd."
            if measurement_faults:
                msg += f" {measurement_faults} mätvärde låg utanför gräns och blev felanmälan."
            flash(msg, "success")
            return redirect(url_for("work", tab=task.template.kind.lower()))
        else:
            db.session.commit()
            flash("Framstegen sparades, men alla delmoment är inte kontrollerade ännu.", "warning")
            return redirect(request.referrer or url_for("work"))

    db.session.commit()
    msg = "Framstegen sparades."
    if created_faults:
        msg += f" {created_faults} ny avvikelse skapades."
    flash(msg, "success")
    return redirect(request.referrer or url_for("work"))


@app.post("/work/task/<int:task_id>/item/<int:item_id>")
@login_required
def update_task_item(task_id, item_id):
    task = db.session.get(TaskInstance, task_id) or abort(404)
    item = db.session.get(TaskItem, item_id) or abort(404)
    if item.task_id != task.id:
        abort(404)

    user = get_current_user()
    comment = request.form.get("comment", "").strip()
    checked = request.form.get("checked") == "1"
    create_fault = request.form.get("create_fault") == "1"

    if checked and not item.checked_at:
        item.checked_at = now_utc()
        item.checked_by_id = user.id
    elif not checked:
        item.checked_at = None
        item.checked_by_id = None

    item.comment = comment
    if task.status == "open":
        task.status = "in_progress"

    audit(
        "task_item_updated", "task_item", item.id,
        actor_user=user,
        details=f"Kontrollerad={checked}; kommentar={comment[:200]}"
    )

    if create_fault:
        try:
            assignee_id = int(request.form.get("fault_assignee_id", "0"))
        except ValueError:
            assignee_id = 0
        assignee = db.session.get(User, assignee_id) if assignee_id else None
        raw_token = secrets.token_urlsafe(32)
        report = FaultReport(
            property_id=task.template.property_id,
            place_id=task.template.place_id,
            summary=f"{task.template.name}: {item.label}"[:180],
            details=comment or "Avvikelse skapad från kontrollmoment.",
            reporter_email=user.email,
            status="assigned" if assignee else "new",
            assignee_id=assignee.id if assignee else None,
            tracking_token_hash=token_hash(raw_token),
            source_task_item_id=item.id,
        )
        db.session.add(report)
        db.session.flush()
        audit(
            "report_created_from_task", "fault_report", report.id,
            actor_user=user, details=f"Från delmoment #{item.id}"
        )
        if assignee:
            send_email(
                assignee.email,
                f"Avvikelse/felanmälan #{report.id} tilldelad",
                f"En avvikelse från {task.template.kind} har blivit ett ärende:\n"
                f"{report.summary}\n\n{BASE_URL}/work"
            )

    db.session.commit()
    flash("Delmomentet sparades.", "success")
    return redirect(request.referrer or url_for("work"))


@app.post("/work/task/<int:task_id>/complete")
@login_required
def complete_task(task_id):
    task = db.session.get(TaskInstance, task_id) or abort(404)
    if any(not item.checked_at for item in task.items):
        flash("Alla delmoment måste vara kontrollerade innan uppgiften avslutas.", "warning")
        return redirect(request.referrer or url_for("work"))

    user = get_current_user()
    task.status = "done"
    task.completed_at = now_utc()
    task.completed_by_id = user.id
    audit("task_completed", "task_instance", task.id, actor_user=user)
    db.session.commit()
    flash(f"{task.template.kind} markerades som klar.", "success")
    return redirect(request.referrer or url_for("work"))


@app.get("/admin/users")
@admin_required
def admin_users():
    users = db.session.execute(select(User).order_by(User.name)).scalars().all()
    return render_template("admin_users.html", users=users)


@app.post("/admin/users/invite")
@admin_required
def invite_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", ROLE_TECH)
    if role not in (ROLE_ADMIN, ROLE_TECH):
        role = ROLE_TECH

    if not name or "@" not in email:
        flash("Namn och giltig e-post krävs.", "danger")
        return redirect(url_for("admin_users"))

    user = db.session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user and user.is_active:
        flash("Det finns redan ett aktivt konto med den adressen.", "warning")
        return redirect(url_for("admin_users"))

    if not user:
        user = User(name=name, email=email, role=role, is_active=False)
        db.session.add(user)
        db.session.commit()
    else:
        user.name = name
        user.role = role
        db.session.commit()

    make_activation(user)
    audit("user_invited", "user", user.id, actor_user=get_current_user())
    db.session.commit()
    flash(f"Aktiveringsmejl skickat till {email}.", "success")
    return redirect(url_for("admin_users"))


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", title="403", message="Du har inte behörighet till den här sidan."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", title="404", message="Sidan eller ärendet kunde inte hittas."), 404
