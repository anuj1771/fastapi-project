from datetime import datetime, timedelta
from email.message import EmailMessage
import base64
import io
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote, urlencode
import secrets
import smtplib
import uuid

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
import qrcode
from sqlalchemy import and_, func, inspect, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import auth, models, schemas
from app.db import Base, engine
from app.deps import get_current_user, get_db, require_admin

load_dotenv()

Base.metadata.create_all(bind=engine)

app = FastAPI(title="AffeeSo")

base_dir = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(base_dir / "templates"))
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")

TEMPLATES = [
    "Hello! We are interested in collaborating with your profile.",
    "Thanks for reaching out. Let's discuss campaign details.",
    "Can we schedule a short call to discuss partnership?",
]
ADVERTISER_JOB_VISIBILITY_DAYS = 2
DEFAULT_COIN_PACKAGES: list[tuple[int, int]] = [
    (20, 20),
    (40, 35),
    (70, 50),
]

COIN_ACTION_CREATE_JOB = "create_job_cost"
COIN_ACTION_APPLY_JOB = "apply_job_cost"
COIN_ACTION_EXPLORE_PROFILE = "explore_profile_cost"
COIN_ACTION_FIRST_CHAT = "first_chat_cost"

DEFAULT_COIN_COST_SETTINGS: dict[str, tuple[int, bool, str]] = {
    COIN_ACTION_CREATE_JOB: (20, True, "Coins charged when a brand posts a new job"),
    COIN_ACTION_APPLY_JOB: (10, True, "Coins charged when an advertiser applies to a job"),
    COIN_ACTION_EXPLORE_PROFILE: (10, True, "Coins charged when exploring advertiser or brand profiles"),
    COIN_ACTION_FIRST_CHAT: (15, True, "Coins charged only once per user pair (first interaction)"),
}

def _ensure_runtime_schema():
    inspector = inspect(engine)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "coins" not in user_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN coins INTEGER NOT NULL DEFAULT 0"))


def _ensure_profile_verification_schema():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "advertiser_profile_details" not in tables:
        models.AdvertiserProfileDetail.__table__.create(bind=engine)
    if "brand_profile_details" not in tables:
        models.BrandProfileDetail.__table__.create(bind=engine)
    par_cols = {c["name"] for c in inspector.get_columns("profile_approval_requests")}
    alters: list[str] = []
    if "advertiser_verification_stage" not in par_cols:
        alters.append(
            "ALTER TABLE profile_approval_requests ADD COLUMN advertiser_verification_stage VARCHAR(32)"
        )
    if "generated_otp" not in par_cols:
        alters.append("ALTER TABLE profile_approval_requests ADD COLUMN generated_otp INTEGER")
    if "user_entered_otp" not in par_cols:
        alters.append("ALTER TABLE profile_approval_requests ADD COLUMN user_entered_otp INTEGER")
    if alters:
        with engine.begin() as conn:
            for stmt in alters:
                conn.execute(text(stmt))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE profile_approval_requests
                SET advertiser_verification_stage = 'initial_review'
                WHERE profile_type = 'advertiser'
                  AND status = 'pending'
                  AND advertiser_verification_stage IS NULL
                """
            )
        )


def _ensure_job_tag_schema():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table_name in (
        "promotion_tags",
        "target_profile_tags",
        "job_promotion_tag_links",
        "job_target_profile_tag_links",
    ):
        if table_name not in tables:
            Base.metadata.tables[table_name].create(bind=engine)


def _ensure_payment_schema():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "coin_packages" not in tables:
        models.CoinPackage.__table__.create(bind=engine)
    if "payments" not in tables:
        models.Payment.__table__.create(bind=engine)


_ensure_runtime_schema()
_ensure_profile_verification_schema()
_ensure_job_tag_schema()
_ensure_payment_schema()


def _tags_to_json(tags: list) -> str:
    return json.dumps([{"id": tag.id, "name": tag.name} for tag in tags])


def _normalize_tag_name(name: str) -> str:
    return " ".join(name.strip().split())


def _resolve_promotion_tags(db: Session, tag_ids: list[int]) -> list[models.PromotionTag]:
    unique_ids = list(dict.fromkeys(tag_ids))
    tags = db.query(models.PromotionTag).filter(models.PromotionTag.id.in_(unique_ids)).all()
    if len(tags) != len(unique_ids):
        raise HTTPException(status_code=400, detail="One or more promotion tags are invalid.")
    return tags


def _resolve_target_profile_tags(db: Session, tag_ids: list[int]) -> list[models.TargetProfileTag]:
    unique_ids = list(dict.fromkeys(tag_ids))
    tags = db.query(models.TargetProfileTag).filter(models.TargetProfileTag.id.in_(unique_ids)).all()
    if len(tags) != len(unique_ids):
        raise HTTPException(status_code=400, detail="One or more target profile tags are invalid.")
    return tags


def _tag_names_csv(tags: list) -> str:
    return ", ".join(tag.name for tag in tags)


def _job_query_with_tags(db: Session):
    return db.query(models.Job).options(
        joinedload(models.Job.promotion_tag_items),
        joinedload(models.Job.target_profile_tag_items),
    )


def _ensure_default_coin_cost_settings(db: Session) -> None:
    existing = {
        row.key: row
        for row in db.query(models.CoinCostSetting).filter(
            models.CoinCostSetting.key.in_(list(DEFAULT_COIN_COST_SETTINGS.keys()))
        )
    }
    changed = False
    for key, (cost, enabled, description) in DEFAULT_COIN_COST_SETTINGS.items():
        if key in existing:
            continue
        db.add(
            models.CoinCostSetting(
                key=key, cost=cost, enabled=enabled, description=description, updated_at=datetime.utcnow()
            )
        )
        changed = True
    if changed:
        db.commit()


def _get_coin_setting(db: Session, key: str) -> models.CoinCostSetting | None:
    return db.query(models.CoinCostSetting).filter(models.CoinCostSetting.key == key).first()


def _get_coin_cost(db: Session, key: str) -> int:
    setting = _get_coin_setting(db, key)
    if not setting or not setting.enabled:
        return 0
    return max(int(setting.cost or 0), 0)


def _is_admin_user(user: models.User | None) -> bool:
    return bool(user is not None and user.role == models.UserRole.ADMIN)


def _coin_cost_for_user(db: Session, user: models.User | None, key: str) -> int:
    if _is_admin_user(user):
        return 0
    return _get_coin_cost(db, key)


def _deduct_coins_or_raise(db: Session, user_id: int, cost: int, action_key: str) -> None:
    if cost <= 0:
        return
    updated = (
        db.query(models.User)
        .filter(models.User.id == user_id, models.User.coins >= cost)
        .update({models.User.coins: models.User.coins - cost})
    )
    if updated != 1:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Not enough coins for {action_key}. Please purchase more coins.",
        )


def _charge_action_or_raise(db: Session, user: models.User, action_key: str) -> int:
    if _is_admin_user(user):
        return 0
    cost = _get_coin_cost(db, action_key)
    _deduct_coins_or_raise(db, user.id, cost, action_key)
    return cost


def _ensure_default_coin_packages(db: Session) -> None:
    existing_coins = {
        row.coins for row in db.query(models.CoinPackage.coins).all()
    }
    changed = False
    now = datetime.utcnow()
    for coins, price in DEFAULT_COIN_PACKAGES:
        if coins in existing_coins:
            continue
        db.add(
            models.CoinPackage(
                coins=coins,
                price=price,
                currency="INR",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        changed = True
    if changed:
        db.commit()


def _get_upi_config() -> tuple[str, str]:
    upi_id = os.getenv("UPI_ID", "").strip()
    payee_name = os.getenv("UPI_PAYEE_NAME", "").strip()
    if not upi_id or not payee_name:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="UPI is not configured. Set UPI_ID and UPI_PAYEE_NAME in .env.",
        )
    return upi_id, payee_name


def _build_upi_uri(amount: int) -> str:
    upi_id, payee_name = _get_upi_config()
    query = urlencode(
        {
            "pa": upi_id,
            "pn": payee_name,
            "am": f"{int(amount):.2f}",
            "cu": "INR",
        },
        quote_via=quote,
    )
    return f"upi://pay?{query}"


def _qr_data_url(payload: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _payment_to_out(
    payment: models.Payment, *, include_upi: bool = False
) -> schemas.PaymentOut:
    upi_id = None
    payee_name = None
    upi_uri = None
    qr_data_url = None
    if include_upi and payment.status == models.PaymentStatus.PENDING:
        upi_id, payee_name = _get_upi_config()
        upi_uri = _build_upi_uri(payment.amount)
        qr_data_url = _qr_data_url(upi_uri)
    return schemas.PaymentOut(
        payment_id=payment.payment_id,
        user_id=payment.user_id,
        package_id=payment.package_id,
        coins=payment.coins,
        amount=payment.amount,
        currency=payment.currency,
        status=payment.status,
        upi_id=upi_id,
        payee_name=payee_name,
        upi_uri=upi_uri,
        qr_data_url=qr_data_url,
        submitted_at=payment.submitted_at,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
    )


def _get_owned_payment_or_404(
    db: Session, payment_id: str, user: models.User
) -> models.Payment:
    payment = (
        db.query(models.Payment)
        .filter(models.Payment.payment_id == payment_id)
        .first()
    )
    if not payment or payment.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    return payment


def _verify_payment_and_credit(db: Session, payment_id: str) -> models.Payment:
    payment = (
        db.query(models.Payment)
        .filter(models.Payment.payment_id == payment_id)
        .first()
    )
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")

    now = datetime.utcnow()
    updated = (
        db.query(models.Payment)
        .filter(
            models.Payment.id == payment.id,
            models.Payment.status == models.PaymentStatus.PENDING,
        )
        .update(
            {
                models.Payment.status: models.PaymentStatus.PAID,
                models.Payment.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payment is not pending or was already processed.",
        )

    db.query(models.User).filter(models.User.id == payment.user_id).update(
        {models.User.coins: models.User.coins + int(payment.coins)},
        synchronize_session=False,
    )
    db.commit()
    payment.status = models.PaymentStatus.PAID
    payment.updated_at = now
    return payment


def _reject_payment(db: Session, payment_id: str) -> models.Payment:
    payment = (
        db.query(models.Payment)
        .filter(models.Payment.payment_id == payment_id)
        .first()
    )
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")

    now = datetime.utcnow()
    updated = (
        db.query(models.Payment)
        .filter(
            models.Payment.id == payment.id,
            models.Payment.status == models.PaymentStatus.PENDING,
        )
        .update(
            {
                models.Payment.status: models.PaymentStatus.FAILED,
                models.Payment.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payment is not pending or was already processed.",
        )
    db.commit()
    payment.status = models.PaymentStatus.FAILED
    payment.updated_at = now
    return payment


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        user_connections = self.active_connections.get(user_id)
        if not user_connections:
            return
        user_connections.discard(websocket)
        if not user_connections:
            self.active_connections.pop(user_id, None)

    async def send_to_user(self, user_id: int, message: dict):
        sockets = self.active_connections.get(user_id, set())
        disconnected: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(message)
            except RuntimeError:
                disconnected.append(socket)
        for socket in disconnected:
            self.disconnect(user_id, socket)

    async def send_to_pair(self, sender_id: int, receiver_id: int, message: dict):
        await self.send_to_user(receiver_id, message)
        await self.send_to_user(sender_id, message)


manager = ConnectionManager()
logger = logging.getLogger(__name__)


def _build_app_url(path: str) -> str:
    base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    return f"{base_url}{path}"


def _send_email(to_email: str, subject: str, body: str):
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM_EMAIL", "").strip() or smtp_user
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"

    if not smtp_host or not smtp_from or not smtp_password:
        raise RuntimeError("SMTP is not configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = to_email
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        if use_tls:
            server.starttls()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.send_message(message)


def _send_password_reset_email(user_email: str, token: str):
    reset_link = _build_app_url(f"/reset-password?token={token}")
    subject = "Reset your AffeeSo password"
    body = (
        "We received a request to reset your password.\n\n"
        f"Click this link to set a new password: {reset_link}\n\n"
        "This link expires in 30 minutes. If you did not request this, you can ignore this email."
    )
    _send_email(user_email, subject, body)


def _send_advertiser_otp_email(user_email: str) -> None:
    subject = "AffeeSo advertiser verification — OTP sent via Instagram"
    body = (
        "Please check your Instagram messages.\n\n"
        "An OTP has been shared with you by our verification team.\n"
        "Please enter the OTP in your profile verification section to continue verification.\n\n"
        "If you did not request verification, you can ignore this email."
    )
    try:
        _send_email(user_email, subject, body)
    except Exception:
        logger.exception("Failed to send advertiser OTP notification email")


def _get_user_from_cookie(request: Request, db: Session) -> models.User | None:
    token = request.cookies.get("token")
    if not token:
        return None
    user_id = auth.decode_token(token)
    if not user_id or not user_id.isdigit():
        return None
    return db.query(models.User).filter(models.User.id == int(user_id)).first()


def _profile_redirect_url(
    *,
    tab: str | None = None,
    success: str | None = None,
    error: str | None = None,
) -> str:
    params: dict[str, str] = {}
    if tab in ("advertiser", "brand"):
        params["tab"] = tab
    if success:
        params["success"] = success
    if error:
        params["error"] = error
    if not params:
        return "/profile"
    return f"/profile?{urlencode(params)}"


def _get_basic_profile_map(db: Session, user_id: int) -> dict[str, models.BasicProfile]:
    rows = db.query(models.BasicProfile).filter(models.BasicProfile.user_id == user_id).all()
    return {row.profile_type.value: row for row in rows}


def _is_basic_profile_complete(profile: models.BasicProfile | None) -> bool:
    if not profile:
        return False
    return bool(profile.name and profile.name.strip() and profile.phone_number and profile.phone_number.strip())


def _require_completed_basic_profile(
    db: Session, user_id: int, profile_type: models.ProfileType
) -> models.BasicProfile:
    profile = (
        db.query(models.BasicProfile)
        .filter(
            models.BasicProfile.user_id == user_id,
            models.BasicProfile.profile_type == profile_type,
        )
        .first()
    )
    if not _is_basic_profile_complete(profile):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{profile_type.value.title()} profile must be completed first",
        )
    return profile


def _get_advertiser_detail(db: Session, user_id: int) -> models.AdvertiserProfileDetail | None:
    return (
        db.query(models.AdvertiserProfileDetail)
        .filter(models.AdvertiserProfileDetail.user_id == user_id)
        .first()
    )


def _get_or_create_advertiser_detail(db: Session, user_id: int) -> models.AdvertiserProfileDetail:
    row = _get_advertiser_detail(db, user_id)
    if row:
        return row
    row = models.AdvertiserProfileDetail(user_id=user_id)
    db.add(row)
    db.flush()
    return row


def _get_brand_detail(db: Session, user_id: int) -> models.BrandProfileDetail | None:
    return db.query(models.BrandProfileDetail).filter(models.BrandProfileDetail.user_id == user_id).first()


def _get_or_create_brand_detail(db: Session, user_id: int) -> models.BrandProfileDetail:
    row = _get_brand_detail(db, user_id)
    if row:
        return row
    row = models.BrandProfileDetail(user_id=user_id)
    db.add(row)
    db.flush()
    return row


def _is_advertiser_detail_complete(detail: models.AdvertiserProfileDetail | None) -> bool:
    if not detail:
        return False

    def nonempty(value: str | None) -> bool:
        return bool(value and str(value).strip())

    if not nonempty(detail.instagram_id) or not nonempty(detail.instagram_profile_url):
        return False
    if detail.instagram_followers is None or detail.instagram_followers < 0:
        return False
    for cost in (
        detail.reel_cost,
        detail.collaboration_cost,
        detail.story_cost,
        detail.post_cost,
    ):
        if cost is None or cost < 0:
            return False
    return True


def _is_brand_detail_complete(detail: models.BrandProfileDetail | None) -> bool:
    if not detail:
        return False

    def nonempty(value: str | None) -> bool:
        return bool(value and str(value).strip())

    if not nonempty(detail.brand_name):
        return False
    if not nonempty(detail.website_url):
        return False
    if not nonempty(detail.brand_email) or "@" not in detail.brand_email:
        return False
    if not nonempty(detail.contact_person_name):
        return False
    phone = (detail.contact_person_phone or "").strip().replace(" ", "")
    if not phone.isdigit() or len(phone) < 10 or len(phone) > 15:
        return False
    pan = (detail.pan_number or "").strip().upper().replace(" ", "")
    if len(pan) < 10 or len(pan) > 12:
        return False
    return True


def _is_advertiser_fully_complete(db: Session, user_id: int) -> bool:
    profile_map = _get_basic_profile_map(db, user_id)
    basic = profile_map.get(models.ProfileType.ADVERTISER.value)
    if not _is_basic_profile_complete(basic):
        return False
    return _is_advertiser_detail_complete(_get_advertiser_detail(db, user_id))


def _is_brand_fully_complete(db: Session, user_id: int) -> bool:
    profile_map = _get_basic_profile_map(db, user_id)
    basic = profile_map.get(models.ProfileType.BRAND.value)
    if not _is_basic_profile_complete(basic):
        return False
    return _is_brand_detail_complete(_get_brand_detail(db, user_id))


def _advertiser_instagram_locked(approval: models.ProfileApprovalRequest | None) -> bool:
    if not approval or approval.profile_type != models.ProfileType.ADVERTISER:
        return False
    if approval.generated_otp is not None:
        return True
    if approval.advertiser_verification_stage in (
        models.AdvertiserVerificationStage.OTP_SENT,
        models.AdvertiserVerificationStage.FINAL_REVIEW,
    ):
        return True
    return False


def _require_brand_fully_complete(db: Session, user_id: int) -> None:
    if not _is_brand_fully_complete(db, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Brand profile must be completed first",
        )


def _require_advertiser_fully_complete(db: Session, user_id: int) -> None:
    if not _is_advertiser_fully_complete(db, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Advertiser profile must be completed first",
        )


def _get_approved_profile(db: Session, user_id: int, profile_type: models.ProfileType):
    approval = (
        db.query(models.ProfileApprovalRequest)
        .filter(
            models.ProfileApprovalRequest.user_id == user_id,
            models.ProfileApprovalRequest.profile_type == profile_type,
            models.ProfileApprovalRequest.status == models.ProfileStatus.APPROVED,
        )
        .first()
    )
    if not approval:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only approved users can interact with chat",
        )
    return approval


def _get_approval_request(
    db: Session, user_id: int, profile_type: models.ProfileType
) -> models.ProfileApprovalRequest | None:
    return (
        db.query(models.ProfileApprovalRequest)
        .filter(
            models.ProfileApprovalRequest.user_id == user_id,
            models.ProfileApprovalRequest.profile_type == profile_type,
        )
        .first()
    )


def _is_profile_type_approved(db: Session, user_id: int, profile_type: models.ProfileType) -> bool:
    approved = (
        db.query(models.ProfileApprovalRequest.id)
        .filter(
            models.ProfileApprovalRequest.user_id == user_id,
            models.ProfileApprovalRequest.profile_type == profile_type,
            models.ProfileApprovalRequest.status == models.ProfileStatus.APPROVED,
        )
        .first()
    )
    return bool(approved)


def _get_ws_user(websocket: WebSocket, db: Session) -> models.User:
    token = websocket.query_params.get("token") or websocket.cookies.get("token")
    user_id = auth.decode_token(token) if token else None
    if not user_id or not user_id.isdigit():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def _has_existing_conversation(db: Session, user_a_id: int, user_b_id: int) -> bool:
    existing = (
        db.query(models.Message.id)
        .filter(
            or_(
                and_(
                    models.Message.sender_id == user_a_id,
                    models.Message.receiver_id == user_b_id,
                ),
                and_(
                    models.Message.sender_id == user_b_id,
                    models.Message.receiver_id == user_a_id,
                ),
            )
        )
        .first()
    )
    if existing:
        return True
    user_one_id, user_two_id = sorted((user_a_id, user_b_id))
    connection = (
        db.query(models.ChatConnection.id)
        .filter(
            models.ChatConnection.user_one_id == user_one_id,
            models.ChatConnection.user_two_id == user_two_id,
        )
        .first()
    )
    return bool(connection)


def _can_chat_by_job_rules(db: Session, current_user_id: int, other_user_id: int) -> bool:
    if current_user_id == other_user_id:
        return False

    if _has_existing_conversation(db, current_user_id, other_user_id):
        return True

    current_brand_profile = (
        db.query(models.BasicProfile)
        .filter(
            models.BasicProfile.user_id == current_user_id,
            models.BasicProfile.profile_type == models.ProfileType.BRAND,
        )
        .first()
    )
    current_advertiser_profile = (
        db.query(models.BasicProfile)
        .filter(
            models.BasicProfile.user_id == current_user_id,
            models.BasicProfile.profile_type == models.ProfileType.ADVERTISER,
        )
        .first()
    )
    other_brand_profile = (
        db.query(models.BasicProfile)
        .filter(
            models.BasicProfile.user_id == other_user_id,
            models.BasicProfile.profile_type == models.ProfileType.BRAND,
        )
        .first()
    )
    other_advertiser_profile = (
        db.query(models.BasicProfile)
        .filter(
            models.BasicProfile.user_id == other_user_id,
            models.BasicProfile.profile_type == models.ProfileType.ADVERTISER,
        )
        .first()
    )

    current_is_brand = _is_basic_profile_complete(current_brand_profile)
    current_is_advertiser = _is_basic_profile_complete(current_advertiser_profile)
    other_is_brand = _is_basic_profile_complete(other_brand_profile)
    other_is_advertiser = _is_basic_profile_complete(other_advertiser_profile)

    if current_is_brand and other_is_advertiser:
        approved = (
            db.query(models.JobApplication.id)
            .join(models.Job, models.Job.id == models.JobApplication.job_id)
            .filter(
                models.Job.brand_user_id == current_user_id,
                models.JobApplication.advertiser_user_id == other_user_id,
                models.JobApplication.is_selected.is_(True),
            )
            .first()
        )
        return bool(approved)

    if current_is_advertiser and other_is_brand:
        selected = (
            db.query(models.JobApplication.id)
            .join(models.Job, models.Job.id == models.JobApplication.job_id)
            .filter(
                models.Job.brand_user_id == other_user_id,
                models.JobApplication.advertiser_user_id == current_user_id,
                models.JobApplication.is_selected.is_(True),
            )
            .first()
        )
        return bool(selected)

    return False


def _normalize_instagram_handle(value: str | None) -> str:
    cleaned = (value or "").strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()
    return cleaned


def _display_instagram_id(value: str | None) -> str | None:
    handle = _normalize_instagram_handle(value)
    if not handle:
        return None
    return f"@{handle}"


def _chat_display_for_user(user: models.User) -> schemas.RegisteredUserItem:
    profile_types = {row.profile_type for row in (user.basic_profiles or [])}
    ad_detail = user.advertiser_profile_detail
    brand_detail = user.brand_profile_detail
    has_instagram = models.ProfileType.ADVERTISER in profile_types or ad_detail is not None
    has_company = models.ProfileType.BRAND in profile_types or brand_detail is not None
    instagram_id = _display_instagram_id(ad_detail.instagram_id if ad_detail else None)
    company_name = (brand_detail.brand_name or "").strip() if brand_detail else ""
    return schemas.RegisteredUserItem(
        id=user.id,
        has_company=has_company,
        has_instagram=has_instagram,
        company_name=company_name or None,
        instagram_id=instagram_id,
    )


def _chat_user_query(db: Session):
    return db.query(models.User).options(
        joinedload(models.User.advertiser_profile_detail),
        joinedload(models.User.brand_profile_detail),
        joinedload(models.User.basic_profiles),
    )


def _interaction_display_name(user: models.User) -> str:
    item = _chat_display_for_user(user)
    parts: list[str] = []
    if item.has_company and item.company_name:
        parts.append(item.company_name)
    if item.has_instagram and item.instagram_id:
        parts.append(item.instagram_id)
    if parts:
        return " · ".join(parts)
    return f"User {user.id}"


def _complete_profile_user_ids(db: Session, profile_type: models.ProfileType) -> set[int]:
    rows = (
        db.query(models.BasicProfile)
        .filter(models.BasicProfile.profile_type == profile_type)
        .all()
    )
    return {row.user_id for row in rows if _is_basic_profile_complete(row)}


def _allowed_undirected_chat_pairs(db: Session) -> set[tuple[int, int]]:
    """Undirected pairs that currently share chat permission (same source as _can_chat_by_job_rules)."""
    pairs: set[tuple[int, int]] = set()
    connections = db.query(
        models.ChatConnection.user_one_id, models.ChatConnection.user_two_id
    ).all()
    for one_id, two_id in connections:
        if one_id != two_id:
            pairs.add(tuple(sorted((one_id, two_id))))

    messages = db.query(models.Message.sender_id, models.Message.receiver_id).distinct().all()
    for sender_id, receiver_id in messages:
        if sender_id != receiver_id:
            pairs.add(tuple(sorted((sender_id, receiver_id))))

    brand_ids = _complete_profile_user_ids(db, models.ProfileType.BRAND)
    advertiser_ids = _complete_profile_user_ids(db, models.ProfileType.ADVERTISER)
    selected_apps = (
        db.query(models.Job.brand_user_id, models.JobApplication.advertiser_user_id)
        .join(models.Job, models.Job.id == models.JobApplication.job_id)
        .filter(models.JobApplication.is_selected.is_(True))
        .all()
    )
    for brand_id, advertiser_id in selected_apps:
        if brand_id == advertiser_id:
            continue
        if brand_id in brand_ids and advertiser_id in advertiser_ids:
            pairs.add(tuple(sorted((brand_id, advertiser_id))))
    return pairs


def _chat_degree_map(db: Session) -> dict[int, int]:
    degrees: dict[int, int] = {}
    for left_id, right_id in _allowed_undirected_chat_pairs(db):
        degrees[left_id] = degrees.get(left_id, 0) + 1
        degrees[right_id] = degrees.get(right_id, 0) + 1
    return degrees


def _interaction_user_ref(
    user: models.User, contact_count: int, *, include_email: bool = False
) -> schemas.InteractionUserRef:
    display = _chat_display_for_user(user)
    return schemas.InteractionUserRef(
        id=user.id,
        display_name=_interaction_display_name(user),
        company_name=display.company_name,
        instagram_id=display.instagram_id,
        email=user.email if include_email else None,
        contact_count=contact_count,
    )


def _pair_job_rows(db: Session, user_a_id: int, user_b_id: int):
    return (
        db.query(models.Job, models.JobApplication)
        .join(models.JobApplication, models.JobApplication.job_id == models.Job.id)
        .filter(
            or_(
                and_(
                    models.Job.brand_user_id == user_a_id,
                    models.JobApplication.advertiser_user_id == user_b_id,
                ),
                and_(
                    models.Job.brand_user_id == user_b_id,
                    models.JobApplication.advertiser_user_id == user_a_id,
                ),
            )
        )
        .all()
    )


def _pair_chat_connection(db: Session, user_a_id: int, user_b_id: int):
    one_id, two_id = sorted((user_a_id, user_b_id))
    return (
        db.query(models.ChatConnection)
        .filter(
            models.ChatConnection.user_one_id == one_id,
            models.ChatConnection.user_two_id == two_id,
        )
        .first()
    )


def _pair_message_bounds(db: Session, user_a_id: int, user_b_id: int):
    return (
        db.query(func.min(models.Message.created_at), func.max(models.Message.created_at))
        .filter(
            or_(
                and_(
                    models.Message.sender_id == user_a_id,
                    models.Message.receiver_id == user_b_id,
                ),
                and_(
                    models.Message.sender_id == user_b_id,
                    models.Message.receiver_id == user_a_id,
                ),
            )
        )
        .first()
    )


def _build_interaction_connection(
    db: Session,
    selected_user: models.User,
    other_user: models.User,
    degrees: dict[int, int],
) -> schemas.InteractionConnection | None:
    can_out = _can_chat_by_job_rules(db, selected_user.id, other_user.id)
    can_in = _can_chat_by_job_rules(db, other_user.id, selected_user.id)
    job_rows = _pair_job_rows(db, selected_user.id, other_user.id)
    pending_jobs = [job for job, application in job_rows if not application.is_selected]
    selected_jobs = [job for job, application in job_rows if application.is_selected]

    if can_out and can_in:
        direction = "bidirectional"
        status = "allowed"
    elif can_out:
        direction = "outgoing"
        status = "allowed"
    elif can_in:
        direction = "incoming"
        status = "allowed"
    elif pending_jobs:
        selected_is_advertiser = any(
            job.brand_user_id == other_user.id for job in pending_jobs
        )
        selected_is_brand = any(job.brand_user_id == selected_user.id for job in pending_jobs)
        if selected_is_advertiser and not selected_is_brand:
            direction = "outgoing"
        elif selected_is_brand and not selected_is_advertiser:
            direction = "incoming"
        else:
            direction = "bidirectional"
        status = "pending"
    else:
        return None

    connection = _pair_chat_connection(db, selected_user.id, other_user.id)
    first_at, last_at = _pair_message_bounds(db, selected_user.id, other_user.id)
    timestamps = [
        value
        for value in (
            connection.created_at if connection else None,
            first_at,
            last_at,
            *(job.created_at for job, _application in job_rows),
            *(application.created_at for _job, application in job_rows),
            *(application.updated_at for _job, application in job_rows),
        )
        if value is not None
    ]
    related_jobs = [job.title for job, _application in job_rows]
    if status == "allowed":
        reasons = []
        if connection or first_at:
            reasons.append("Existing conversation")
        if selected_jobs:
            titles = ", ".join(job.title for job in selected_jobs)
            reasons.append(f"Selected on job: {titles}")
        note = ". ".join(reasons) if reasons else "Chat is allowed by current conversation rules."
    else:
        titles = ", ".join(job.title for job in pending_jobs)
        note = f"Job application awaiting brand selection: {titles}"

    return schemas.InteractionConnection(
        user=_interaction_user_ref(other_user, degrees.get(other_user.id, 0)),
        direction=direction,
        status=status,
        can_selected_contact=can_out,
        can_contact_selected=can_in,
        created_at=min(timestamps) if timestamps else None,
        updated_at=max(timestamps) if timestamps else None,
        note=note,
        related_jobs=related_jobs,
    )


def _direct_relationship_partner_ids(db: Session, user_id: int) -> set[int]:
    partner_ids: set[int] = set()
    connections = (
        db.query(models.ChatConnection.user_one_id, models.ChatConnection.user_two_id)
        .filter(
            or_(
                models.ChatConnection.user_one_id == user_id,
                models.ChatConnection.user_two_id == user_id,
            )
        )
        .all()
    )
    for one_id, two_id in connections:
        partner_ids.add(two_id if one_id == user_id else one_id)

    messages = (
        db.query(models.Message.sender_id, models.Message.receiver_id)
        .filter(
            or_(models.Message.sender_id == user_id, models.Message.receiver_id == user_id)
        )
        .distinct()
        .all()
    )
    for sender_id, receiver_id in messages:
        partner_ids.add(receiver_id if sender_id == user_id else sender_id)

    job_pairs = (
        db.query(models.Job.brand_user_id, models.JobApplication.advertiser_user_id)
        .join(models.Job, models.Job.id == models.JobApplication.job_id)
        .filter(
            or_(
                models.Job.brand_user_id == user_id,
                models.JobApplication.advertiser_user_id == user_id,
            )
        )
        .all()
    )
    for brand_id, advertiser_id in job_pairs:
        partner_ids.add(advertiser_id if brand_id == user_id else brand_id)

    partner_ids.discard(user_id)
    return partner_ids


def _build_interaction_map(db: Session, selected_user: models.User) -> schemas.InteractionMapOut:
    degrees = _chat_degree_map(db)
    partner_ids = _direct_relationship_partner_ids(db, selected_user.id)
    partners = (
        _chat_user_query(db)
        .filter(
            models.User.id.in_(partner_ids),
            models.User.role != models.UserRole.ADMIN,
        )
        .all()
        if partner_ids
        else []
    )
    connections: list[schemas.InteractionConnection] = []
    for partner in partners:
        item = _build_interaction_connection(db, selected_user, partner, degrees)
        if item:
            connections.append(item)
    connections.sort(key=lambda row: row.user.display_name.lower())

    summary = schemas.InteractionSummary(
        can_contact=sum(1 for row in connections if row.can_selected_contact),
        can_be_contacted_by=sum(1 for row in connections if row.can_contact_selected),
        two_way=sum(
            1
            for row in connections
            if row.can_selected_contact and row.can_contact_selected
        ),
        blocked=0,
        pending=sum(1 for row in connections if row.status == "pending"),
    )
    return schemas.InteractionMapOut(
        selected_user=_interaction_user_ref(
            selected_user, degrees.get(selected_user.id, 0), include_email=True
        ),
        connections=connections,
        summary=summary,
    )


@app.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    display_name = user.email.split("@")[0] if user else None
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
            "user": user,
            "display_name": display_name,
        },
    )


@app.post("/register", response_model=schemas.UserOut)
def register(payload: schemas.UserRegister, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    role = models.UserRole.ADMIN if payload.email.endswith("@admin.com") else models.UserRole.USER
    user = models.User(
        email=payload.email, password_hash=auth.hash_password(payload.password), role=role
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email already registered")
    db.refresh(user)
    return user


@app.post("/login", response_model=schemas.Token)
def login(payload: schemas.UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not auth.verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = auth.create_access_token(str(user.id))
    return schemas.Token(access_token=token)


@app.post("/forgot-password")
def forgot_password(payload: schemas.ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email.lower()).first()
    if user:
        try:
            reset_token = auth.create_password_reset_token(user.email)
            _send_password_reset_email(user.email, reset_token)
        except Exception:
            logger.exception("Failed to send password reset email")
            # Avoid leaking server email configuration details to clients.
            raise HTTPException(status_code=500, detail="Could not send reset email right now")
    return {"message": "If an account with this email exists, a reset link has been sent."}


@app.post("/reset-password")
def reset_password(payload: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    email = auth.decode_password_reset_token(payload.token)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid reset token")
    user.password_hash = auth.hash_password(payload.new_password)
    db.commit()
    return {"message": "Password has been reset successfully"}


@app.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user


@app.post("/advertiser-profile", response_model=schemas.ProfileOut)
def create_advertiser_profile(
    payload: schemas.AdvertiserProfileCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise HTTPException(
        status_code=410,
        detail="Deprecated endpoint. Use /basic-profiles and send profile for approval.",
    )


@app.post("/brand-profile", response_model=schemas.ProfileOut)
def create_brand_profile(
    payload: schemas.BrandProfileCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise HTTPException(
        status_code=410,
        detail="Deprecated endpoint. Use /basic-profiles and send profile for approval.",
    )


@app.get("/profiles", response_model=schemas.ProfileOut)
def get_my_profile(
    current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
):
    profile = db.query(models.Profile).filter(models.Profile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@app.get("/admin/profiles")
def admin_profiles(
    _: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    profiles = (
        db.query(models.ProfileApprovalRequest, models.User)
        .join(models.User)
        .order_by(models.ProfileApprovalRequest.requested_at.desc())
        .all()
    )
    response = []
    for profile, user in profiles:
        response.append(
            {
                "id": profile.id,
                "user_id": user.id,
                "user_email": user.email,
                "profile_type": profile.profile_type,
                "status": profile.status,
                "requested_at": profile.requested_at,
                "reviewed_at": profile.reviewed_at,
                "rejected_until": profile.rejected_until,
            }
        )
    return response


@app.post("/admin/approve/{profile_id}")
def admin_approve(
    profile_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    profile = (
        db.query(models.ProfileApprovalRequest)
        .filter(models.ProfileApprovalRequest.id == profile_id)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile.profile_type == models.ProfileType.ADVERTISER:
        if (
            profile.advertiser_verification_stage
            != models.AdvertiserVerificationStage.FINAL_REVIEW
        ):
            raise HTTPException(
                status_code=400,
                detail="Advertiser can only be approved after OTP verification final review",
            )
        if (
            profile.generated_otp is None
            or profile.user_entered_otp is None
            or profile.user_entered_otp != profile.generated_otp
        ):
            raise HTTPException(status_code=400, detail="Invalid OTP verification state")
    now = datetime.utcnow()
    profile.status = models.ProfileStatus.APPROVED
    profile.reviewed_at = now
    profile.rejected_until = None
    profile.rejection_reason = None
    profile.updated_at = now
    if profile.profile_type == models.ProfileType.ADVERTISER:
        ad_detail = _get_advertiser_detail(db, profile.user_id)
        if ad_detail:
            ad_detail.verification_request_status = (
                models.AdvertiserVerificationRequestStatus.APPROVED
            )
            ad_detail.updated_at = now
    db.commit()
    db.refresh(profile)
    return {"message": "Approval request approved", "request_id": profile.id}


@app.post("/admin/reject/{profile_id}")
def admin_reject(
    profile_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    profile = (
        db.query(models.ProfileApprovalRequest)
        .filter(models.ProfileApprovalRequest.id == profile_id)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    now = datetime.utcnow()
    if profile.profile_type == models.ProfileType.ADVERTISER:
        ad_detail = _get_advertiser_detail(db, profile.user_id)
        if ad_detail:
            ad_detail.otp_verification_status = models.OtpVerificationStatus.NOT_SENT
            ad_detail.verification_request_status = models.AdvertiserVerificationRequestStatus.REJECTED
            ad_detail.updated_at = now
        profile.generated_otp = None
        profile.user_entered_otp = None
        profile.advertiser_verification_stage = None
    profile.status = models.ProfileStatus.REJECTED
    profile.reviewed_at = now
    profile.rejected_until = now + timedelta(days=30)
    profile.updated_at = now
    db.commit()
    db.refresh(profile)
    return {"message": "Approval request rejected", "request_id": profile.id}


@app.get("/admin/stats", response_model=schemas.AdminStats)
def admin_stats(_: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    total_users = db.query(func.count(models.User.id)).scalar() or 0
    total_advertisers = (
        db.query(func.count(models.ProfileApprovalRequest.id))
        .filter(
            and_(
                models.ProfileApprovalRequest.profile_type == models.ProfileType.ADVERTISER,
                models.ProfileApprovalRequest.status == models.ProfileStatus.APPROVED,
            )
        )
        .scalar()
        or 0
    )
    total_brands = (
        db.query(func.count(models.ProfileApprovalRequest.id))
        .filter(
            and_(
                models.ProfileApprovalRequest.profile_type == models.ProfileType.BRAND,
                models.ProfileApprovalRequest.status == models.ProfileStatus.APPROVED,
            )
        )
        .scalar()
        or 0
    )
    templates_sent = (
        db.query(func.count(models.Message.id)).filter(models.Message.is_template.is_(True)).scalar()
        or 0
    )
    total_messages = db.query(func.count(models.Message.id)).scalar() or 0
    return schemas.AdminStats(
        total_users=total_users,
        total_advertisers=total_advertisers,
        total_brands=total_brands,
        templates_sent=templates_sent,
        total_messages=total_messages,
    )


@app.get("/admin/interaction-map/users", response_model=list[schemas.InteractionUserRef])
def admin_interaction_map_users(
    _: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    degrees = _chat_degree_map(db)
    users = (
        _chat_user_query(db)
        .filter(models.User.role != models.UserRole.ADMIN)
        .order_by(models.User.id.asc())
        .all()
    )
    return [
        _interaction_user_ref(user, degrees.get(user.id, 0), include_email=True)
        for user in users
    ]


@app.get("/admin/users/{user_id}/interaction-map", response_model=schemas.InteractionMapOut)
def admin_user_interaction_map(
    user_id: int,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    selected_user = _chat_user_query(db).filter(models.User.id == user_id).first()
    if not selected_user:
        raise HTTPException(status_code=404, detail="User not found")
    if selected_user.role == models.UserRole.ADMIN:
        raise HTTPException(status_code=400, detail="Select a non-admin user")
    return _build_interaction_map(db, selected_user)


@app.get("/admin/interaction-map", response_class=HTMLResponse)
def admin_interaction_map_page(request: Request, db: Session = Depends(get_db)):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_interaction_map.html",
        {
            "request": request,
            "user": admin_user,
            "display_name": admin_user.email.split("@")[0],
        },
    )


@app.get("/users", response_model=list[schemas.UserListItem])
def available_users(
    current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
):
    my_approved = (
        db.query(models.ProfileApprovalRequest)
        .filter(
            models.ProfileApprovalRequest.user_id == current_user.id,
            models.ProfileApprovalRequest.status == models.ProfileStatus.APPROVED,
        )
        .first()
    )
    if not my_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User needs approved advertiser or brand profile",
        )
    opposite_type = (
        models.ProfileType.BRAND
        if my_approved.profile_type == models.ProfileType.ADVERTISER
        else models.ProfileType.ADVERTISER
    )
    users = (
        db.query(models.User, models.ProfileApprovalRequest)
        .join(
            models.ProfileApprovalRequest,
            models.ProfileApprovalRequest.user_id == models.User.id,
        )
        .filter(
            and_(
                models.ProfileApprovalRequest.status == models.ProfileStatus.APPROVED,
                models.ProfileApprovalRequest.profile_type == opposite_type,
            )
        )
        .all()
    )
    return [
        schemas.UserListItem(id=user.id, email=user.email, profile_type=profile.profile_type)
        for user, profile in users
        if user.id != current_user.id
    ]


@app.get("/users/discovery", response_model=list[schemas.RegisteredUserItem])
def discover_users(
    current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
):
    if _is_admin_user(current_user):
        users = (
            _chat_user_query(db)
            .filter(models.User.id != current_user.id)
            .order_by(models.User.email.asc())
            .all()
        )
        return [_chat_display_for_user(user) for user in users]

    users = _chat_user_query(db).filter(models.User.id != current_user.id).all()
    allowed_users = [
        user for user in users if _can_chat_by_job_rules(db, current_user.id, user.id)
    ]
    return [_chat_display_for_user(user) for user in allowed_users]


@app.post("/chat/send", response_model=schemas.MessageOut)
def send_message(
    payload: schemas.ChatSendRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_default_coin_cost_settings(db)
    receiver = db.query(models.User).filter(models.User.id == payload.receiver_id).first()
    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")
    if not _can_chat_with_user(db, current_user, payload.receiver_id):
        raise HTTPException(status_code=403, detail="Chat is not allowed for this user yet")

    if not _has_existing_conversation(db, current_user.id, payload.receiver_id):
        _charge_action_or_raise(db, current_user, COIN_ACTION_FIRST_CHAT)
        user_one_id, user_two_id = sorted((current_user.id, payload.receiver_id))
        db.add(models.ChatConnection(user_one_id=user_one_id, user_two_id=user_two_id))

    content = payload.content
    if payload.use_template:
        index = int(payload.content) if payload.content.isdigit() else 0
        if index < 0 or index >= len(TEMPLATES):
            raise HTTPException(status_code=400, detail="Invalid template index")
        content = TEMPLATES[index]

    message = models.Message(
        sender_id=current_user.id,
        receiver_id=payload.receiver_id,
        content=content,
        is_template=payload.use_template,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket, db: Session = Depends(get_db)):
    try:
        current_user = _get_ws_user(websocket, db)
    except HTTPException:
        await websocket.close(code=1008)
        return
    _ensure_default_coin_cost_settings(db)

    await manager.connect(current_user.id, websocket)
    try:
        while True:
            payload = await websocket.receive_json()
            receiver_id = payload.get("receiver_id")
            content = payload.get("content", "")
            use_template = bool(payload.get("use_template", False))

            if not isinstance(receiver_id, int):
                await websocket.send_json({"type": "error", "detail": "receiver_id must be an integer"})
                continue
            if not isinstance(content, str) or not content.strip():
                await websocket.send_json({"type": "error", "detail": "content is required"})
                continue

            receiver = db.query(models.User).filter(models.User.id == receiver_id).first()
            if not receiver:
                await websocket.send_json({"type": "error", "detail": "Receiver not found"})
                continue
            if not _can_chat_with_user(db, current_user, receiver_id):
                await websocket.send_json(
                    {"type": "error", "detail": "Chat is not allowed for this user yet"}
                )
                continue

            if not _has_existing_conversation(db, current_user.id, receiver_id):
                try:
                    _charge_action_or_raise(db, current_user, COIN_ACTION_FIRST_CHAT)
                    user_one_id, user_two_id = sorted((current_user.id, receiver_id))
                    db.add(models.ChatConnection(user_one_id=user_one_id, user_two_id=user_two_id))
                    db.commit()
                except HTTPException as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc.detail)})
                    continue

            final_content = content.strip()
            if use_template:
                template_index = int(final_content) if final_content.isdigit() else 0
                if template_index < 0 or template_index >= len(TEMPLATES):
                    await websocket.send_json({"type": "error", "detail": "Invalid template index"})
                    continue
                final_content = TEMPLATES[template_index]

            message = models.Message(
                sender_id=current_user.id,
                receiver_id=receiver_id,
                content=final_content,
                is_template=use_template,
            )
            db.add(message)
            db.commit()
            db.refresh(message)

            event = {
                "type": "private_message",
                "id": message.id,
                "sender_id": message.sender_id,
                "receiver_id": message.receiver_id,
                "content": message.content,
                "is_template": message.is_template,
                "created_at": message.created_at.isoformat(),
            }
            await manager.send_to_pair(current_user.id, receiver_id, event)
    except WebSocketDisconnect:
        manager.disconnect(current_user.id, websocket)
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "detail": str(exc.detail)})
        manager.disconnect(current_user.id, websocket)
        await websocket.close(code=1008)
    except Exception:
        manager.disconnect(current_user.id, websocket)
        await websocket.close(code=1011)


@app.get("/chat/{user_id}", response_model=list[schemas.MessageOut])
def chat_history(
    user_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
):
    other = db.query(models.User).filter(models.User.id == user_id).first()
    if not other:
        raise HTTPException(status_code=404, detail="User not found")
    if not _can_chat_with_user(db, current_user, user_id):
        raise HTTPException(status_code=403, detail="Chat is not allowed for this user yet")

    messages = (
        db.query(models.Message)
        .filter(
            or_(
                and_(
                    models.Message.sender_id == current_user.id,
                    models.Message.receiver_id == user_id,
                ),
                and_(
                    models.Message.sender_id == user_id,
                    models.Message.receiver_id == current_user.id,
                ),
            )
        )
        .order_by(models.Message.created_at.asc())
        .all()
    )
    return messages


@app.get("/templates", response_model=list[str])
def templates_list(_: models.User = Depends(get_current_user)):
    return TEMPLATES


@app.get("/basic-profiles", response_model=list[schemas.BasicProfileOut])
def get_basic_profiles(
    current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return db.query(models.BasicProfile).filter(models.BasicProfile.user_id == current_user.id).all()


@app.post("/basic-profiles", response_model=schemas.BasicProfileOut)
def upsert_basic_profile(
    payload: schemas.BasicProfileUpsert,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = (
        db.query(models.BasicProfile)
        .filter(
            models.BasicProfile.user_id == current_user.id,
            models.BasicProfile.profile_type == payload.profile_type,
        )
        .first()
    )
    if profile:
        profile.name = payload.name.strip()
        profile.phone_number = payload.phone_number.strip()
        profile.updated_at = datetime.utcnow()
    else:
        profile = models.BasicProfile(
            user_id=current_user.id,
            profile_type=payload.profile_type,
            name=payload.name.strip(),
            phone_number=payload.phone_number.strip(),
        )
        db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@app.post("/ui/register")
def ui_register(
    email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
):
    try:
        register(schemas.UserRegister(email=email, password=password), db)
    except ValidationError:
        return RedirectResponse(
            url="/?error=Please enter a valid email and password (min 6 characters).",
            status_code=303,
        )
    except HTTPException as exc:
        return RedirectResponse(url=f"/?error={exc.detail}", status_code=303)
    except Exception:
        return RedirectResponse(
            url="/?error=Unexpected registration error. Please try again.",
            status_code=303,
        )
    return RedirectResponse(url="/?success=Account created successfully. Please login.", status_code=303)


@app.post("/ui/login")
def ui_login(
    request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
):
    try:
        token = login(schemas.UserLogin(email=email, password=password), db)
    except Exception:
        return RedirectResponse(url="/?error=Invalid login credentials.", status_code=303)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("token", token.access_token, httponly=True, path="/")
    return response


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {
            "request": request,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


@app.post("/ui/forgot-password")
def ui_forgot_password(email: str = Form(...), db: Session = Depends(get_db)):
    try:
        forgot_password(schemas.ForgotPasswordRequest(email=email), db)
    except ValidationError:
        return RedirectResponse(url="/forgot-password?error=Please enter a valid email.", status_code=303)
    except HTTPException as exc:
        return RedirectResponse(url=f"/forgot-password?error={exc.detail}", status_code=303)
    return RedirectResponse(
        url="/forgot-password?success=If your email exists, we sent a reset link.",
        status_code=303,
    )


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str | None = None):
    if not token:
        return RedirectResponse(url="/forgot-password?error=Missing reset token.", status_code=303)
    return templates.TemplateResponse(
        request,
        "reset_password.html",
        {
            "request": request,
            "token": token,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


@app.post("/ui/reset-password")
def ui_reset_password(token: str = Form(...), new_password: str = Form(...), db: Session = Depends(get_db)):
    try:
        reset_password(schemas.ResetPasswordRequest(token=token, new_password=new_password), db)
    except ValidationError:
        return RedirectResponse(
            url=f"/reset-password?token={token}&error=Password must be at least 6 characters.",
            status_code=303,
        )
    except HTTPException as exc:
        return RedirectResponse(url=f"/reset-password?token={token}&error={exc.detail}", status_code=303)
    return RedirectResponse(url="/?success=Password reset successful. Please login.", status_code=303)


def _logout_response() -> RedirectResponse:
    response = RedirectResponse(url="/?success=Logged out successfully.", status_code=303)
    response.delete_cookie("token", path="/")
    return response


@app.post("/ui/logout")
def ui_logout_post():
    return _logout_response()


@app.get("/ui/logout")
def ui_logout_get():
    return _logout_response()


@app.post("/ui/coins/earn")
def ui_earn_coins(
    request: Request,
    amount: int = Form(0),
    next_path: str = Form("/dashboard"),
    db: Session = Depends(get_db),
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    redirect_to = next_path if next_path.startswith("/") else "/dashboard"
    return RedirectResponse(
        url=f"{redirect_to}?error=Coin+purchases+require+UPI+payment.+Select+a+package+to+pay.",
        status_code=303,
    )


@app.get("/coin-packages", response_model=list[schemas.CoinPackageOut])
def list_coin_packages(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_default_coin_packages(db)
    query = db.query(models.CoinPackage)
    if not _is_admin_user(current_user):
        query = query.filter(models.CoinPackage.is_active.is_(True))
    return query.order_by(models.CoinPackage.coins.asc()).all()


@app.post("/payments/create", response_model=schemas.PaymentOut)
def create_payment(
    payload: schemas.PaymentCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if _is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin users cannot purchase coins.")
    _ensure_default_coin_packages(db)
    package = (
        db.query(models.CoinPackage)
        .filter(
            models.CoinPackage.id == payload.package_id,
            models.CoinPackage.is_active.is_(True),
        )
        .first()
    )
    if not package:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Coin package is not available.")
    now = datetime.utcnow()
    payment = models.Payment(
        payment_id=str(uuid.uuid4()),
        user_id=current_user.id,
        package_id=package.id,
        coins=int(package.coins),
        amount=int(package.price),
        currency=package.currency or "INR",
        status=models.PaymentStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return _payment_to_out(payment, include_upi=True)


@app.get("/payments/{payment_id}", response_model=schemas.PaymentOut)
def get_payment(
    payment_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payment = _get_owned_payment_or_404(db, payment_id, current_user)
    return _payment_to_out(payment, include_upi=payment.status == models.PaymentStatus.PENDING)


@app.post("/payments/{payment_id}/submit", response_model=schemas.PaymentOut)
def submit_payment(
    payment_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payment = _get_owned_payment_or_404(db, payment_id, current_user)
    if payment.status != models.PaymentStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending payments can be submitted for verification.",
        )
    payment.submitted_at = datetime.utcnow()
    payment.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(payment)
    return _payment_to_out(payment, include_upi=False)


@app.get("/admin/payments", response_model=list[schemas.AdminPaymentOut])
def admin_list_payments(
    status_filter: str | None = Query(default=None, alias="status"),
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(models.Payment, models.User.email).join(
        models.User, models.User.id == models.Payment.user_id
    )
    if status_filter:
        try:
            status_value = models.PaymentStatus(status_filter.upper())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payment status.")
        query = query.filter(models.Payment.status == status_value)
    rows = query.order_by(models.Payment.created_at.desc()).all()
    return [
        schemas.AdminPaymentOut(
            payment_id=payment.payment_id,
            user_id=payment.user_id,
            user_email=email,
            package_id=payment.package_id,
            coins=payment.coins,
            amount=payment.amount,
            currency=payment.currency,
            status=payment.status,
            submitted_at=payment.submitted_at,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
        )
        for payment, email in rows
    ]


@app.post("/admin/payments/{payment_id}/verify", response_model=schemas.AdminPaymentOut)
def admin_verify_payment(
    payment_id: str,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    payment = _verify_payment_and_credit(db, payment_id)
    user = db.query(models.User).filter(models.User.id == payment.user_id).first()
    return schemas.AdminPaymentOut(
        payment_id=payment.payment_id,
        user_id=payment.user_id,
        user_email=user.email if user else "",
        package_id=payment.package_id,
        coins=payment.coins,
        amount=payment.amount,
        currency=payment.currency,
        status=payment.status,
        submitted_at=payment.submitted_at,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
    )


@app.post("/admin/payments/{payment_id}/reject", response_model=schemas.AdminPaymentOut)
def admin_reject_payment(
    payment_id: str,
    _: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    payment = _reject_payment(db, payment_id)
    user = db.query(models.User).filter(models.User.id == payment.user_id).first()
    return schemas.AdminPaymentOut(
        payment_id=payment.payment_id,
        user_id=payment.user_id,
        user_email=user.email if user else "",
        package_id=payment.package_id,
        coins=payment.coins,
        amount=payment.amount,
        currency=payment.currency,
        status=payment.status,
        submitted_at=payment.submitted_at,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
    )


@app.post("/ui/admin/coins/add")
def ui_admin_add_coins(
    request: Request,
    target_user_id: int = Form(...),
    amount: int = Form(...),
    db: Session = Depends(get_db),
):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    if amount <= 0:
        return RedirectResponse(url="/dashboard?error=Coins amount must be greater than zero.", status_code=303)
    target_user = (
        db.query(models.User)
        .filter(models.User.id == target_user_id, models.User.role != models.UserRole.ADMIN)
        .first()
    )
    if not target_user:
        return RedirectResponse(url="/dashboard?error=Target user not found.", status_code=303)
    target_user.coins = (target_user.coins or 0) + amount
    db.commit()
    return RedirectResponse(
        url=f"/dashboard?success=Added+{amount}+coins+to+{target_user.email}.",
        status_code=303,
    )


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    display_name = user.email.split("@")[0]
    profile_map = _get_basic_profile_map(db, user.id)
    approval_rows = (
        db.query(models.ProfileApprovalRequest)
        .filter(models.ProfileApprovalRequest.user_id == user.id)
        .all()
    )
    approval_map = {row.profile_type.value: row for row in approval_rows}
    advertiser_profile = profile_map.get(models.ProfileType.ADVERTISER.value)
    brand_profile = profile_map.get(models.ProfileType.BRAND.value)
    advertiser_detail = _get_advertiser_detail(db, user.id)
    brand_detail = _get_brand_detail(db, user.id)
    advertiser_approval = approval_map.get(models.ProfileType.ADVERTISER.value)
    instagram_locked = _advertiser_instagram_locked(advertiser_approval)
    show_advertiser_otp_box = bool(
        advertiser_approval
        and advertiser_approval.status == models.ProfileStatus.PENDING
        and advertiser_approval.advertiser_verification_stage
        == models.AdvertiserVerificationStage.OTP_SENT
    )
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "request": request,
            "user": user,
            "display_name": display_name,
            "advertiser_profile": advertiser_profile,
            "brand_profile": brand_profile,
            "advertiser_detail": advertiser_detail,
            "brand_detail": brand_detail,
            "advertiser_request": advertiser_approval,
            "brand_request": approval_map.get(models.ProfileType.BRAND.value),
            "advertiser_complete": _is_advertiser_fully_complete(db, user.id),
            "brand_complete": _is_brand_fully_complete(db, user.id),
            "instagram_locked": instagram_locked,
            "show_advertiser_otp_box": show_advertiser_otp_box,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/profile/save")
def ui_profile_save(
    request: Request,
    profile_type: str = Form(...),
    name: str = Form(...),
    phone_number: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        parsed_type = models.ProfileType(profile_type)
        payload = schemas.BasicProfileUpsert(
            profile_type=parsed_type, name=name, phone_number=phone_number
        )
        upsert_basic_profile(payload, user, db)
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Invalid profile input."
        return RedirectResponse(
            url=_profile_redirect_url(tab=profile_type, error=first_error),
            status_code=303,
        )
    except Exception:
        return RedirectResponse(
            url=_profile_redirect_url(tab=profile_type, error="Failed to save profile details."),
            status_code=303,
        )
    return RedirectResponse(
        url=_profile_redirect_url(tab=profile_type, success="Profile saved successfully."),
        status_code=303,
    )


@app.post("/ui/profile/advertiser-details")
def ui_save_advertiser_profile_details(
    request: Request,
    name: str = Form(...),
    phone_number: str = Form(...),
    instagram_id: str = Form(""),
    instagram_profile_url: str = Form(""),
    reel_cost: int = Form(...),
    collaboration_cost: int = Form(...),
    story_cost: int = Form(...),
    post_cost: int = Form(...),
    instagram_followers: int = Form(...),
    db: Session = Depends(get_db),
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        payload = schemas.BasicProfileUpsert(
            profile_type=models.ProfileType.ADVERTISER,
            name=name,
            phone_number=phone_number,
        )
        upsert_basic_profile(payload, user, db)
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Invalid profile input."
        return RedirectResponse(
            url=_profile_redirect_url(tab="advertiser", error=first_error),
            status_code=303,
        )
    except Exception:
        return RedirectResponse(
            url=_profile_redirect_url(
                tab="advertiser",
                error="Failed to save advertiser profile.",
            ),
            status_code=303,
        )

    approval = _get_approval_request(db, user.id, models.ProfileType.ADVERTISER)
    locked = _advertiser_instagram_locked(approval)
    detail = _get_or_create_advertiser_detail(db, user.id)
    now = datetime.utcnow()
    if locked:
        detail.reel_cost = reel_cost
        detail.collaboration_cost = collaboration_cost
        detail.story_cost = story_cost
        detail.post_cost = post_cost
        for c in (
            detail.reel_cost,
            detail.collaboration_cost,
            detail.story_cost,
            detail.post_cost,
        ):
            if c is None or c < 0:
                return RedirectResponse(
                    url=_profile_redirect_url(
                        tab="advertiser",
                        error="Costs must be non-negative integers.",
                    ),
                    status_code=303,
                )
    else:
        detail.instagram_id = instagram_id.strip()
        detail.instagram_profile_url = instagram_profile_url.strip()
        detail.reel_cost = reel_cost
        detail.collaboration_cost = collaboration_cost
        detail.story_cost = story_cost
        detail.post_cost = post_cost
        detail.instagram_followers = instagram_followers
        if not detail.instagram_id or not detail.instagram_profile_url:
            return RedirectResponse(
                url=_profile_redirect_url(
                    tab="advertiser",
                    error="Instagram ID and profile URL are required.",
                ),
                status_code=303,
            )
        if detail.instagram_followers is None or detail.instagram_followers < 0:
            return RedirectResponse(
                url=_profile_redirect_url(
                    tab="advertiser",
                    error="Followers must be a non-negative number.",
                ),
                status_code=303,
            )
        for c in (
            detail.reel_cost,
            detail.collaboration_cost,
            detail.story_cost,
            detail.post_cost,
        ):
            if c is None or c < 0:
                return RedirectResponse(
                    url=_profile_redirect_url(
                        tab="advertiser",
                        error="All rate fields must be non-negative integers.",
                    ),
                    status_code=303,
                )
    detail.updated_at = now
    db.commit()
    return RedirectResponse(
        url=_profile_redirect_url(tab="advertiser", success="Advertiser profile saved."),
        status_code=303,
    )


@app.post("/ui/profile/brand-details")
def ui_save_brand_profile_details(
    request: Request,
    name: str = Form(...),
    phone_number: str = Form(...),
    brand_name: str = Form(...),
    website_url: str = Form(...),
    brand_email: str = Form(...),
    contact_person_name: str = Form(...),
    contact_person_phone: str = Form(...),
    pan_number: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        payload = schemas.BasicProfileUpsert(
            profile_type=models.ProfileType.BRAND,
            name=name,
            phone_number=phone_number,
        )
        upsert_basic_profile(payload, user, db)
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Invalid profile input."
        return RedirectResponse(
            url=_profile_redirect_url(tab="brand", error=first_error),
            status_code=303,
        )
    except Exception:
        return RedirectResponse(
            url=_profile_redirect_url(tab="brand", error="Failed to save brand profile."),
            status_code=303,
        )

    detail = _get_or_create_brand_detail(db, user.id)
    phone = contact_person_phone.strip().replace(" ", "")
    if not phone.isdigit() or len(phone) < 10 or len(phone) > 15:
        return RedirectResponse(
            url=_profile_redirect_url(
                tab="brand",
                error="Contact phone must be 10-15 digits.",
            ),
            status_code=303,
        )
    pan = pan_number.strip().upper().replace(" ", "")
    if len(pan) < 10:
        return RedirectResponse(
            url=_profile_redirect_url(tab="brand", error="PAN number looks invalid."),
            status_code=303,
        )
    email_clean = brand_email.strip().lower()
    if "@" not in email_clean:
        return RedirectResponse(
            url=_profile_redirect_url(tab="brand", error="Enter a valid brand email."),
            status_code=303,
        )
    detail.brand_name = brand_name.strip()
    detail.website_url = website_url.strip()
    detail.brand_email = email_clean
    detail.contact_person_name = contact_person_name.strip()
    detail.contact_person_phone = phone
    detail.pan_number = pan
    detail.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(
        url=_profile_redirect_url(tab="brand", success="Brand profile saved."),
        status_code=303,
    )


@app.post("/ui/profile/advertiser-verify-otp")
def ui_advertiser_verify_otp(
    request: Request,
    otp: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    approval = _get_approval_request(db, user.id, models.ProfileType.ADVERTISER)
    if (
        not approval
        or approval.status != models.ProfileStatus.PENDING
        or approval.advertiser_verification_stage != models.AdvertiserVerificationStage.OTP_SENT
    ):
        return RedirectResponse(
            url=_profile_redirect_url(
                tab="advertiser",
                error="OTP verification is not available right now.",
            ),
            status_code=303,
        )
    try:
        entered = int(str(otp).strip())
    except ValueError:
        return RedirectResponse(
            url=_profile_redirect_url(tab="advertiser", error="Enter a valid 6-digit OTP."),
            status_code=303,
        )
    if approval.generated_otp is None or entered != approval.generated_otp:
        return RedirectResponse(
            url=_profile_redirect_url(tab="advertiser", error="Invalid OTP. Please try again."),
            status_code=303,
        )
    now = datetime.utcnow()
    approval.user_entered_otp = entered
    approval.advertiser_verification_stage = models.AdvertiserVerificationStage.FINAL_REVIEW
    approval.updated_at = now
    ad_detail = _get_or_create_advertiser_detail(db, user.id)
    ad_detail.otp_verification_status = models.OtpVerificationStatus.VERIFIED
    ad_detail.verification_request_status = models.AdvertiserVerificationRequestStatus.PENDING_FINAL
    ad_detail.updated_at = now
    db.commit()
    return RedirectResponse(
        url=_profile_redirect_url(
            tab="advertiser",
            success="OTP verified. Your profile is pending final admin verification.",
        ),
        status_code=303,
    )


@app.post("/ui/profile/send-approval")
def ui_send_profile_approval(
    request: Request,
    profile_type: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        parsed_type = models.ProfileType(profile_type)
        if parsed_type == models.ProfileType.ADVERTISER:
            _require_advertiser_fully_complete(db, user.id)
        else:
            _require_brand_fully_complete(db, user.id)
    except HTTPException:
        return RedirectResponse(
            url=_profile_redirect_url(
                tab=profile_type,
                error=f"Complete your {profile_type} profile before sending approval request.",
            ),
            status_code=303,
        )
    except Exception:
        return RedirectResponse(
            url=_profile_redirect_url(
                tab=profile_type,
                error=f"Complete your {profile_type} profile before sending approval request.",
            ),
            status_code=303,
        )

    approval_request = _get_approval_request(db, user.id, parsed_type)
    now = datetime.utcnow()
    if approval_request:
        if approval_request.status == models.ProfileStatus.PENDING:
            return RedirectResponse(
                url=_profile_redirect_url(
                    tab=profile_type,
                    error="Approval request is already pending for this profile.",
                ),
                status_code=303,
            )
        if approval_request.status == models.ProfileStatus.APPROVED:
            return RedirectResponse(
                url=_profile_redirect_url(
                    tab=profile_type,
                    error="This profile is already approved.",
                ),
                status_code=303,
            )
        if approval_request.rejected_until and approval_request.rejected_until > now:
            return RedirectResponse(
                url=_profile_redirect_url(
                    tab=profile_type,
                    error="Your request was rejected. You can send again after one month.",
                ),
                status_code=303,
            )
        approval_request.status = models.ProfileStatus.PENDING
        approval_request.requested_at = now
        approval_request.reviewed_at = None
        approval_request.rejection_reason = None
        approval_request.updated_at = now
        approval_request.rejected_until = None
        if parsed_type == models.ProfileType.ADVERTISER:
            approval_request.advertiser_verification_stage = (
                models.AdvertiserVerificationStage.INITIAL_REVIEW
            )
            approval_request.generated_otp = None
            approval_request.user_entered_otp = None
            ad_detail = _get_or_create_advertiser_detail(db, user.id)
            ad_detail.otp_verification_status = models.OtpVerificationStatus.NOT_SENT
            ad_detail.verification_request_status = (
                models.AdvertiserVerificationRequestStatus.SUBMITTED
            )
            ad_detail.updated_at = now
    else:
        approval_request = models.ProfileApprovalRequest(
            user_id=user.id,
            profile_type=parsed_type,
            status=models.ProfileStatus.PENDING,
            requested_at=now,
            updated_at=now,
        )
        if parsed_type == models.ProfileType.ADVERTISER:
            approval_request.advertiser_verification_stage = (
                models.AdvertiserVerificationStage.INITIAL_REVIEW
            )
            approval_request.generated_otp = None
            approval_request.user_entered_otp = None
            ad_detail = _get_or_create_advertiser_detail(db, user.id)
            ad_detail.otp_verification_status = models.OtpVerificationStatus.NOT_SENT
            ad_detail.verification_request_status = (
                models.AdvertiserVerificationRequestStatus.SUBMITTED
            )
            ad_detail.updated_at = now
        db.add(approval_request)
    db.commit()
    return RedirectResponse(
        url=_profile_redirect_url(
            tab=profile_type,
            success=f"{parsed_type.value.title()} profile sent for admin approval.",
        ),
        status_code=303,
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    _ensure_default_coin_packages(db)
    user = _get_user_from_cookie(request, db)
    advertiser_complete = False
    brand_complete = False
    advertiser_request = None
    brand_request = None
    advertiser_approved = False
    brand_approved = False
    non_admin_users = []
    if user:
        profile_map = _get_basic_profile_map(db, user.id)
        approval_map = {
            row.profile_type.value: row
            for row in db.query(models.ProfileApprovalRequest)
            .filter(models.ProfileApprovalRequest.user_id == user.id)
            .all()
        }
        advertiser_complete = _is_advertiser_fully_complete(db, user.id)
        brand_complete = _is_brand_fully_complete(db, user.id)
        advertiser_request = approval_map.get(models.ProfileType.ADVERTISER.value)
        brand_request = approval_map.get(models.ProfileType.BRAND.value)
        advertiser_approved = bool(
            advertiser_request and advertiser_request.status == models.ProfileStatus.APPROVED
        )
        brand_approved = bool(brand_request and brand_request.status == models.ProfileStatus.APPROVED)
        if user.role == models.UserRole.ADMIN:
            non_admin_users = (
                db.query(models.User)
                .filter(models.User.role != models.UserRole.ADMIN)
                .order_by(models.User.email.asc())
                .all()
            )
    display_name = user.email.split("@")[0] if user else None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "templates": TEMPLATES,
            "display_name": display_name,
            "advertiser_complete": advertiser_complete,
            "brand_complete": brand_complete,
            "advertiser_request": advertiser_request,
            "brand_request": brand_request,
            "advertiser_approved": advertiser_approved,
            "brand_approved": brand_approved,
            "is_admin": bool(user and user.role == models.UserRole.ADMIN),
            "non_admin_users": non_admin_users,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


def _admin_profile_approval_page(
    request: Request, db: Session, profile_type: models.ProfileType
):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    requests = (
        db.query(
            models.ProfileApprovalRequest,
            models.User,
            models.BasicProfile,
            models.AdvertiserProfileDetail,
            models.BrandProfileDetail,
        )
        .join(models.User, models.User.id == models.ProfileApprovalRequest.user_id)
        .outerjoin(
            models.BasicProfile,
            and_(
                models.BasicProfile.user_id == models.ProfileApprovalRequest.user_id,
                models.BasicProfile.profile_type == models.ProfileApprovalRequest.profile_type,
            ),
        )
        .outerjoin(
            models.AdvertiserProfileDetail,
            models.AdvertiserProfileDetail.user_id == models.ProfileApprovalRequest.user_id,
        )
        .outerjoin(
            models.BrandProfileDetail,
            models.BrandProfileDetail.user_id == models.ProfileApprovalRequest.user_id,
        )
        .filter(
            models.ProfileApprovalRequest.profile_type == profile_type,
            models.ProfileApprovalRequest.status == models.ProfileStatus.PENDING,
        )
        .order_by(models.ProfileApprovalRequest.requested_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request,
        "admin_approval_requests.html",
        {
            "request": request,
            "user": admin_user,
            "display_name": admin_user.email.split("@")[0],
            "requests": requests,
            "profile_type": profile_type,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/admin/approval-requests/advertiser", response_class=HTMLResponse)
def advertiser_approval_requests_page(request: Request, db: Session = Depends(get_db)):
    return _admin_profile_approval_page(request, db, models.ProfileType.ADVERTISER)


@app.get("/admin/approval-requests/brand", response_class=HTMLResponse)
def brand_approval_requests_page(request: Request, db: Session = Depends(get_db)):
    return _admin_profile_approval_page(request, db, models.ProfileType.BRAND)


@app.get("/explore/advertisers", response_class=HTMLResponse)
def explore_advertisers_page(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    if not _is_admin_user(user) and not _is_profile_type_approved(db, user.id, models.ProfileType.BRAND):
        return RedirectResponse(
            url="/dashboard?error=Only approved brands can explore advertisers.",
            status_code=303,
        )
    users = (
        db.query(models.User, models.BasicProfile, models.AdvertiserProfileDetail)
        .join(
            models.ProfileApprovalRequest,
            and_(
                models.ProfileApprovalRequest.user_id == models.User.id,
                models.ProfileApprovalRequest.profile_type == models.ProfileType.ADVERTISER,
                models.ProfileApprovalRequest.status == models.ProfileStatus.APPROVED,
            ),
        )
        .outerjoin(
            models.BasicProfile,
            and_(
                models.BasicProfile.user_id == models.User.id,
                models.BasicProfile.profile_type == models.ProfileType.ADVERTISER,
            ),
        )
        .outerjoin(
            models.AdvertiserProfileDetail,
            models.AdvertiserProfileDetail.user_id == models.User.id,
        )
        .filter(models.User.id != user.id)
        .order_by(models.User.email.asc())
        .all()
    )
    items = []
    for list_user, basic_profile, ad_detail in users:
        items.append(
            {
                "user": list_user,
                "basic_profile": basic_profile,
                "ad_detail": ad_detail,
                "has_chat": _has_existing_conversation(db, user.id, list_user.id),
            }
        )
    return templates.TemplateResponse(
        request,
        "explore_users.html",
        {
            "request": request,
            "user": user,
            "display_name": user.email.split("@")[0],
            "title": "Explore Advertisers",
            "empty_message": "No approved advertisers available right now.",
            "items": items,
            "start_chat_endpoint": "/ui/chat/start",
            "coin_costs": {
                "first_chat_cost": _coin_cost_for_user(db, user, COIN_ACTION_FIRST_CHAT),
                "explore_profile_cost": _coin_cost_for_user(db, user, COIN_ACTION_EXPLORE_PROFILE),
            },
            "is_admin": _is_admin_user(user),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/explore/brands", response_class=HTMLResponse)
def explore_brands_page(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    if not _is_admin_user(user) and not _is_profile_type_approved(db, user.id, models.ProfileType.ADVERTISER):
        return RedirectResponse(
            url="/dashboard?error=Only approved advertisers can explore brands.",
            status_code=303,
        )
    users = (
        db.query(models.User, models.BasicProfile, models.BrandProfileDetail)
        .join(
            models.ProfileApprovalRequest,
            and_(
                models.ProfileApprovalRequest.user_id == models.User.id,
                models.ProfileApprovalRequest.profile_type == models.ProfileType.BRAND,
                models.ProfileApprovalRequest.status == models.ProfileStatus.APPROVED,
            ),
        )
        .outerjoin(
            models.BasicProfile,
            and_(
                models.BasicProfile.user_id == models.User.id,
                models.BasicProfile.profile_type == models.ProfileType.BRAND,
            ),
        )
        .outerjoin(
            models.BrandProfileDetail,
            models.BrandProfileDetail.user_id == models.User.id,
        )
        .filter(models.User.id != user.id)
        .order_by(models.User.email.asc())
        .all()
    )
    items = []
    for list_user, basic_profile, brand_detail in users:
        items.append(
            {
                "user": list_user,
                "basic_profile": basic_profile,
                "brand_detail": brand_detail,
                "has_chat": _has_existing_conversation(db, user.id, list_user.id),
            }
        )
    return templates.TemplateResponse(
        request,
        "explore_users.html",
        {
            "request": request,
            "user": user,
            "display_name": user.email.split("@")[0],
            "title": "Explore Brands",
            "empty_message": "No approved brands available right now.",
            "items": items,
            "start_chat_endpoint": "/ui/chat/start",
            "coin_costs": {
                "first_chat_cost": _coin_cost_for_user(db, user, COIN_ACTION_FIRST_CHAT),
                "explore_profile_cost": _coin_cost_for_user(db, user, COIN_ACTION_EXPLORE_PROFILE),
            },
            "is_admin": _is_admin_user(user),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/chat/start/{target_user_id}")
def ui_start_chat(target_user_id: int, request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    if target_user_id == user.id:
        return RedirectResponse(url="/dashboard?error=Cannot start chat with yourself.", status_code=303)
    target_user = db.query(models.User).filter(models.User.id == target_user_id).first()
    if not target_user:
        return RedirectResponse(url="/dashboard?error=User not found.", status_code=303)

    current_is_brand = _is_profile_type_approved(db, user.id, models.ProfileType.BRAND)
    current_is_advertiser = _is_profile_type_approved(db, user.id, models.ProfileType.ADVERTISER)
    target_is_brand = _is_profile_type_approved(db, target_user_id, models.ProfileType.BRAND)
    target_is_advertiser = _is_profile_type_approved(db, target_user_id, models.ProfileType.ADVERTISER)
    is_valid_pair = (current_is_brand and target_is_advertiser) or (
        current_is_advertiser and target_is_brand
    )
    if not is_valid_pair and not _is_admin_user(user):
        return RedirectResponse(
            url="/dashboard?error=Chat can be started only between approved brand and approved advertiser.",
            status_code=303,
        )

    user_one_id, user_two_id = sorted((user.id, target_user_id))
    connection = (
        db.query(models.ChatConnection)
        .filter(
            models.ChatConnection.user_one_id == user_one_id,
            models.ChatConnection.user_two_id == user_two_id,
        )
        .first()
    )
    if not connection:
        try:
            _charge_action_or_raise(db, user, COIN_ACTION_FIRST_CHAT)
            db.add(models.ChatConnection(user_one_id=user_one_id, user_two_id=user_two_id))
            db.commit()
        except HTTPException as exc:
            return RedirectResponse(url=f"/dashboard?error={exc.detail}", status_code=303)
    return RedirectResponse(url=f"/chat-demo?user_id={target_user_id}", status_code=303)


@app.post("/ui/admin/advertiser/{request_id}/send-otp")
def ui_admin_send_advertiser_otp(request_id: int, request: Request, db: Session = Depends(get_db)):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    approval_request = (
        db.query(models.ProfileApprovalRequest)
        .filter(
            models.ProfileApprovalRequest.id == request_id,
            models.ProfileApprovalRequest.profile_type == models.ProfileType.ADVERTISER,
            models.ProfileApprovalRequest.status == models.ProfileStatus.PENDING,
        )
        .first()
    )
    if not approval_request:
        return RedirectResponse(url="/dashboard?error=Request not found.", status_code=303)
    if (
        approval_request.advertiser_verification_stage
        != models.AdvertiserVerificationStage.INITIAL_REVIEW
    ):
        return RedirectResponse(
            url="/admin/approval-requests/advertiser?error=Send+OTP+is+only+available+for+initial+review+requests.",
            status_code=303,
        )
    target_user = db.query(models.User).filter(models.User.id == approval_request.user_id).first()
    if not target_user:
        return RedirectResponse(url="/dashboard?error=User not found.", status_code=303)
    otp_value = secrets.randbelow(900_000) + 100_000
    now = datetime.utcnow()
    approval_request.generated_otp = otp_value
    approval_request.user_entered_otp = None
    approval_request.advertiser_verification_stage = models.AdvertiserVerificationStage.OTP_SENT
    approval_request.updated_at = now
    ad_detail = _get_or_create_advertiser_detail(db, approval_request.user_id)
    ad_detail.otp_verification_status = models.OtpVerificationStatus.PENDING
    ad_detail.verification_request_status = models.AdvertiserVerificationRequestStatus.OTP_PENDING
    ad_detail.updated_at = now
    db.commit()
    try:
        _send_advertiser_otp_email(target_user.email)
    except Exception:
        logger.exception("Advertiser OTP email skipped or failed (check SMTP)")
    return RedirectResponse(
        url="/admin/approval-requests/advertiser?success=OTP+generated+and+notification+email+sent.",
        status_code=303,
    )


@app.post("/ui/admin/approval/{request_id}/approve")
def ui_admin_approve_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    approval_request = (
        db.query(models.ProfileApprovalRequest)
        .filter(models.ProfileApprovalRequest.id == request_id)
        .first()
    )
    if not approval_request:
        return RedirectResponse(url="/dashboard?error=Approval request not found.", status_code=303)
    if approval_request.profile_type == models.ProfileType.ADVERTISER:
        if (
            approval_request.advertiser_verification_stage
            != models.AdvertiserVerificationStage.FINAL_REVIEW
        ):
            return RedirectResponse(
                url="/admin/approval-requests/advertiser?error=Advertiser+profiles+can+only+be+approved+after+the+advertiser+verifies+OTP+and+the+request+is+in+final+review.",
                status_code=303,
            )
        if (
            approval_request.generated_otp is None
            or approval_request.user_entered_otp is None
            or approval_request.user_entered_otp != approval_request.generated_otp
        ):
            return RedirectResponse(
                url="/admin/approval-requests/advertiser?error=OTP+verification+records+are+invalid.",
                status_code=303,
            )
    now = datetime.utcnow()
    approval_request.status = models.ProfileStatus.APPROVED
    approval_request.reviewed_at = now
    approval_request.rejected_until = None
    approval_request.rejection_reason = None
    approval_request.updated_at = now
    if approval_request.profile_type == models.ProfileType.ADVERTISER:
        ad_detail = _get_advertiser_detail(db, approval_request.user_id)
        if ad_detail:
            ad_detail.verification_request_status = (
                models.AdvertiserVerificationRequestStatus.APPROVED
            )
            ad_detail.updated_at = now
    db.commit()
    return RedirectResponse(
        url=f"/admin/approval-requests/{approval_request.profile_type.value}?success=Request approved.",
        status_code=303,
    )


@app.post("/ui/admin/approval/{request_id}/reject")
def ui_admin_reject_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    approval_request = (
        db.query(models.ProfileApprovalRequest)
        .filter(models.ProfileApprovalRequest.id == request_id)
        .first()
    )
    if not approval_request:
        return RedirectResponse(url="/dashboard?error=Approval request not found.", status_code=303)
    now = datetime.utcnow()
    if approval_request.profile_type == models.ProfileType.ADVERTISER:
        ad_detail = _get_advertiser_detail(db, approval_request.user_id)
        if ad_detail:
            ad_detail.otp_verification_status = models.OtpVerificationStatus.NOT_SENT
            ad_detail.verification_request_status = models.AdvertiserVerificationRequestStatus.REJECTED
            ad_detail.updated_at = now
        approval_request.generated_otp = None
        approval_request.user_entered_otp = None
        approval_request.advertiser_verification_stage = None
    approval_request.status = models.ProfileStatus.REJECTED
    approval_request.reviewed_at = now
    approval_request.rejected_until = now + timedelta(days=30)
    approval_request.updated_at = now
    db.commit()
    return RedirectResponse(
        url=f"/admin/approval-requests/{approval_request.profile_type.value}?success=Request rejected for one month.",
        status_code=303,
    )


@app.get("/jobs/create", response_class=HTMLResponse)
def create_job_page(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    if not _is_admin_user(user):
        try:
            _require_brand_fully_complete(db, user.id)
        except HTTPException:
            return RedirectResponse(
                url="/dashboard?error=Complete your brand profile to create jobs.", status_code=303
            )
    promotion_tags = db.query(models.PromotionTag).order_by(models.PromotionTag.name.asc()).all()
    target_profile_tags = (
        db.query(models.TargetProfileTag).order_by(models.TargetProfileTag.name.asc()).all()
    )
    return templates.TemplateResponse(
        request,
        "job_create.html",
        {
            "request": request,
            "user": user,
            "display_name": user.email.split("@")[0],
            "promotion_tags_json": _tags_to_json(promotion_tags),
            "target_profile_tags_json": _tags_to_json(target_profile_tags),
            "coin_costs": {
                "create_job_cost": _coin_cost_for_user(db, user, COIN_ACTION_CREATE_JOB),
            },
            "is_admin": _is_admin_user(user),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/jobs/create")
def ui_create_job(
    request: Request,
    title: str = Form(...),
    promotion_requirement: str = Form(...),
    budget: str = Form(...),
    promotion_tag_ids: list[int] = Form(default=[]),
    target_profile_tag_ids: list[int] = Form(default=[]),
    profile_image_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    if not _is_admin_user(user):
        try:
            _require_brand_fully_complete(db, user.id)
        except HTTPException:
            return RedirectResponse(
                url="/dashboard?error=Complete your brand profile to create jobs.", status_code=303
            )
    try:
        payload = schemas.JobCreate(
            title=title,
            promotion_requirement=promotion_requirement,
            budget=budget,
            promotion_tag_ids=promotion_tag_ids,
            target_profile_tag_ids=target_profile_tag_ids,
            profile_image_url=profile_image_url or None,
        )
        promotion_tags = _resolve_promotion_tags(db, payload.promotion_tag_ids)
        target_profile_tags = _resolve_target_profile_tags(db, payload.target_profile_tag_ids)
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Invalid job input."
        return RedirectResponse(url=f"/jobs/create?error={first_error}", status_code=303)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_400_BAD_REQUEST:
            return RedirectResponse(url=f"/jobs/create?error={exc.detail}", status_code=303)
        return RedirectResponse(
            url="/dashboard?error=Complete your brand profile to create jobs.", status_code=303
        )

    job = models.Job(
        brand_user_id=user.id,
        title=payload.title.strip(),
        promotion_requirement=payload.promotion_requirement.strip(),
        budget=payload.budget.strip(),
        promotion_tags=_tag_names_csv(promotion_tags),
        target_instagram_profiles=_tag_names_csv(target_profile_tags),
        profile_image_url=(payload.profile_image_url.strip() if payload.profile_image_url else None),
        promotion_tag_items=promotion_tags,
        target_profile_tag_items=target_profile_tags,
    )
    try:
        _charge_action_or_raise(db, user, COIN_ACTION_CREATE_JOB)
    except HTTPException as exc:
        return RedirectResponse(url=f"/jobs/create?error={exc.detail}", status_code=303)
    db.add(job)
    db.commit()
    return RedirectResponse(url="/jobs/create?success=Job posted successfully.", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    is_admin = user.role == models.UserRole.ADMIN
    if not is_admin:
        try:
            _require_advertiser_fully_complete(db, user.id)
        except HTTPException:
            return RedirectResponse(
                url="/dashboard?error=Complete your advertiser profile to see jobs.", status_code=303
            )

    if is_admin:
        jobs = _job_query_with_tags(db).order_by(models.Job.created_at.desc()).all()
    else:
        visible_after = datetime.utcnow() - timedelta(days=ADVERTISER_JOB_VISIBILITY_DAYS)
        jobs = (
            _job_query_with_tags(db)
            .filter(models.Job.created_at >= visible_after)
            .order_by(models.Job.created_at.desc())
            .all()
        )
    my_applications = (
        db.query(models.JobApplication).filter(models.JobApplication.advertiser_user_id == user.id).all()
    )
    applied_map = {item.job_id: item for item in my_applications}

    return templates.TemplateResponse(
        request,
        "jobs_list.html",
        {
            "request": request,
            "user": user,
            "display_name": user.email.split("@")[0],
            "jobs": jobs,
            "applied_map": applied_map,
            "is_admin": is_admin,
            "advertiser_visibility_days": ADVERTISER_JOB_VISIBILITY_DAYS,
            "coin_costs": {
                "apply_job_cost": _coin_cost_for_user(db, user, COIN_ACTION_APPLY_JOB),
            },
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/jobs/{job_id}/apply")
def ui_apply_job(
    job_id: int,
    request: Request,
    description: str = Form(...),
    db: Session = Depends(get_db),
):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        return RedirectResponse(url="/jobs?error=Job not found.", status_code=303)
    if not _is_admin_user(user) and job.created_at < (
        datetime.utcnow() - timedelta(days=ADVERTISER_JOB_VISIBILITY_DAYS)
    ):
        return RedirectResponse(
            url="/jobs?error=This job is older than 2 days and is no longer open for advertiser applications.",
            status_code=303,
        )
    try:
        if not _is_admin_user(user):
            _require_advertiser_fully_complete(db, user.id)
        payload = schemas.JobApplicationCreate(description=description)
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Invalid application."
        return RedirectResponse(url=f"/jobs?error={first_error}", status_code=303)
    except HTTPException:
        return RedirectResponse(
            url="/dashboard?error=Complete your advertiser profile to apply jobs.", status_code=303
        )

    application = models.JobApplication(
        job_id=job.id,
        advertiser_user_id=user.id,
        description=payload.description.strip(),
    )
    try:
        _charge_action_or_raise(db, user, COIN_ACTION_APPLY_JOB)
    except HTTPException as exc:
        return RedirectResponse(url=f"/jobs?error={exc.detail}", status_code=303)
    db.add(application)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/jobs?error=You already applied to this job.", status_code=303)
    return RedirectResponse(url="/jobs?success=Applied successfully.", status_code=303)


@app.get("/admin/coin-costs", response_class=HTMLResponse)
def admin_coin_costs_page(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    _ensure_default_coin_packages(db)
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    settings = db.query(models.CoinCostSetting).order_by(models.CoinCostSetting.key.asc()).all()
    packages = db.query(models.CoinPackage).order_by(models.CoinPackage.coins.asc()).all()
    return templates.TemplateResponse(
        request,
        "admin_coin_costs.html",
        {
            "request": request,
            "user": admin_user,
            "display_name": admin_user.email.split("@")[0],
            "settings": settings,
            "packages": packages,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/admin/coin-packages/save")
async def ui_admin_save_coin_packages(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_packages(db)
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)

    packages = db.query(models.CoinPackage).all()
    data = await request.form()
    changed = False
    for row in packages:
        enabled_raw = data.get(f"active__{row.id}")
        price_raw = data.get(f"price__{row.id}")
        is_active = str(enabled_raw).lower() in {"1", "true", "on", "yes"}
        try:
            price_val = int(price_raw) if price_raw is not None and str(price_raw).strip() else row.price
        except ValueError:
            return RedirectResponse(
                url=f"/admin/coin-costs?error=Invalid+price+for+{row.coins}+coins.",
                status_code=303,
            )
        if price_val < 1:
            return RedirectResponse(
                url="/admin/coin-costs?error=Package+price+must+be+at+least+1.",
                status_code=303,
            )
        if row.is_active != is_active or int(row.price or 0) != price_val:
            row.is_active = is_active
            row.price = price_val
            row.updated_at = datetime.utcnow()
            changed = True

    if changed:
        db.commit()
    return RedirectResponse(url="/admin/coin-costs?success=Coin+packages+updated.", status_code=303)


@app.get("/admin/upi-payments", response_class=HTMLResponse)
def admin_upi_payments_page(request: Request, db: Session = Depends(get_db)):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    status_filter = (request.query_params.get("status") or "").upper().strip()
    query = (
        db.query(models.Payment, models.User)
        .join(models.User, models.User.id == models.Payment.user_id)
        .order_by(models.Payment.created_at.desc())
    )
    if status_filter in {item.value for item in models.PaymentStatus}:
        query = query.filter(models.Payment.status == models.PaymentStatus(status_filter))
    rows = query.all()
    return templates.TemplateResponse(
        request,
        "admin_payments.html",
        {
            "request": request,
            "user": admin_user,
            "display_name": admin_user.email.split("@")[0],
            "payments": rows,
            "status_filter": status_filter,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/admin/payments/{payment_id}/verify")
def ui_admin_verify_payment(payment_id: str, request: Request, db: Session = Depends(get_db)):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    try:
        _verify_payment_and_credit(db, payment_id)
    except HTTPException as exc:
        return RedirectResponse(url=f"/admin/upi-payments?error={exc.detail}", status_code=303)
    return RedirectResponse(
        url="/admin/upi-payments?success=Payment+verified+and+coins+credited.",
        status_code=303,
    )


@app.post("/ui/admin/payments/{payment_id}/reject")
def ui_admin_reject_payment(payment_id: str, request: Request, db: Session = Depends(get_db)):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    try:
        _reject_payment(db, payment_id)
    except HTTPException as exc:
        return RedirectResponse(url=f"/admin/upi-payments?error={exc.detail}", status_code=303)
    return RedirectResponse(
        url="/admin/upi-payments?success=Payment+rejected.",
        status_code=303,
    )


@app.post("/ui/admin/coin-costs/save")
async def ui_admin_save_coin_costs(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)

    settings = db.query(models.CoinCostSetting).all()
    setting_by_key = {row.key: row for row in settings}
    changed = False
    data = await request.form()
    for key, row in setting_by_key.items():
        enabled_raw = data.get(f"enabled__{key}")
        cost_raw = data.get(f"cost__{key}")
        description_raw = data.get(f"description__{key}")
        enabled = str(enabled_raw).lower() in {"1", "true", "on", "yes"}
        try:
            cost_val = int(cost_raw) if cost_raw is not None and str(cost_raw).strip() else row.cost
        except ValueError:
            return RedirectResponse(
                url=f"/admin/coin-costs?error=Invalid+cost+value+for+{key}.",
                status_code=303,
            )
        cost_val = max(cost_val, 0)
        description_val = str(description_raw).strip() if description_raw is not None else row.description
        if row.enabled != enabled or int(row.cost or 0) != cost_val or (row.description or "") != (description_val or ""):
            row.enabled = enabled
            row.cost = cost_val
            row.description = description_val
            row.updated_at = datetime.utcnow()
            changed = True

    if changed:
        db.commit()
    return RedirectResponse(url="/admin/coin-costs?success=Coin+settings+updated.", status_code=303)


@app.get("/admin/job-tags", response_class=HTMLResponse)
def admin_job_tags_page(request: Request, db: Session = Depends(get_db)):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    promotion_tags = db.query(models.PromotionTag).order_by(models.PromotionTag.name.asc()).all()
    target_profile_tags = (
        db.query(models.TargetProfileTag).order_by(models.TargetProfileTag.name.asc()).all()
    )
    return templates.TemplateResponse(
        request,
        "admin_job_tags.html",
        {
            "request": request,
            "user": admin_user,
            "display_name": admin_user.email.split("@")[0],
            "promotion_tags": promotion_tags,
            "target_profile_tags": target_profile_tags,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/admin/job-tags/promotion/create")
def ui_admin_create_promotion_tag(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    try:
        payload = schemas.JobTagCreate(name=name)
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Invalid tag name."
        return RedirectResponse(url=f"/admin/job-tags?error={first_error}", status_code=303)
    normalized = _normalize_tag_name(payload.name)
    if not normalized:
        return RedirectResponse(url="/admin/job-tags?error=Tag name is required.", status_code=303)
    db.add(models.PromotionTag(name=normalized))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url="/admin/job-tags?error=Promotion tag already exists.",
            status_code=303,
        )
    return RedirectResponse(url="/admin/job-tags?success=Promotion tag added.", status_code=303)


@app.post("/ui/admin/job-tags/promotion/{tag_id}/delete")
def ui_admin_delete_promotion_tag(
    tag_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    tag = db.query(models.PromotionTag).filter(models.PromotionTag.id == tag_id).first()
    if not tag:
        return RedirectResponse(url="/admin/job-tags?error=Promotion tag not found.", status_code=303)
    db.delete(tag)
    db.commit()
    return RedirectResponse(url="/admin/job-tags?success=Promotion tag deleted.", status_code=303)


@app.post("/ui/admin/job-tags/target-profile/create")
def ui_admin_create_target_profile_tag(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    try:
        payload = schemas.JobTagCreate(name=name)
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Invalid tag name."
        return RedirectResponse(url=f"/admin/job-tags?error={first_error}", status_code=303)
    normalized = _normalize_tag_name(payload.name)
    if not normalized:
        return RedirectResponse(url="/admin/job-tags?error=Tag name is required.", status_code=303)
    db.add(models.TargetProfileTag(name=normalized))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url="/admin/job-tags?error=Target profile tag already exists.",
            status_code=303,
        )
    return RedirectResponse(url="/admin/job-tags?success=Target profile tag added.", status_code=303)


@app.post("/ui/admin/job-tags/target-profile/{tag_id}/delete")
def ui_admin_delete_target_profile_tag(
    tag_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin_user = _get_user_from_cookie(request, db)
    if not admin_user or admin_user.role != models.UserRole.ADMIN:
        return RedirectResponse(url="/?error=Admin access required.", status_code=303)
    tag = db.query(models.TargetProfileTag).filter(models.TargetProfileTag.id == tag_id).first()
    if not tag:
        return RedirectResponse(
            url="/admin/job-tags?error=Target profile tag not found.",
            status_code=303,
        )
    db.delete(tag)
    db.commit()
    return RedirectResponse(url="/admin/job-tags?success=Target profile tag deleted.", status_code=303)


@app.get("/brand/applications", response_class=HTMLResponse)
def brand_applications_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        _require_brand_fully_complete(db, user.id)
    except HTTPException:
        return RedirectResponse(
            url="/dashboard?error=Complete your brand profile to review applicants.", status_code=303
        )

    jobs = (
        _job_query_with_tags(db)
        .filter(models.Job.brand_user_id == user.id)
        .order_by(models.Job.created_at.desc())
        .all()
    )

    job_cards = []
    for job in jobs:
        applications = (
            db.query(models.JobApplication, models.User)
            .join(models.User, models.User.id == models.JobApplication.advertiser_user_id)
            .filter(models.JobApplication.job_id == job.id)
            .order_by(models.JobApplication.created_at.desc())
            .all()
        )
        job_cards.append({"job": job, "applications": applications})

    return templates.TemplateResponse(
        request,
        "brand_applications.html",
        {
            "request": request,
            "user": user,
            "display_name": user.email.split("@")[0],
            "job_cards": job_cards,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/applications/{application_id}/approve")
def ui_approve_application(application_id: int, request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    application = (
        db.query(models.JobApplication)
        .join(models.Job, models.Job.id == models.JobApplication.job_id)
        .filter(
            models.JobApplication.id == application_id,
            models.Job.brand_user_id == user.id,
        )
        .first()
    )
    if not application:
        return RedirectResponse(url="/brand/applications?error=Application not found.", status_code=303)

    application.is_selected = True
    application.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(
        url=f"/chat-demo?user_id={application.advertiser_user_id}&success=Applicant approved.",
        status_code=303,
    )


@app.get("/chat-demo", response_class=HTMLResponse)
def chat_demo(request: Request, db: Session = Depends(get_db)):
    _ensure_default_coin_cost_settings(db)
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    chat_user = _chat_user_query(db).filter(models.User.id == user.id).first() or user
    current_chat_profile = _chat_display_for_user(chat_user).model_dump()
    access_token = auth.create_access_token(str(user.id))
    response = templates.TemplateResponse(
        request,
        "chat_demo.html",
        {
            "request": request,
            "user": user,
            "current_chat_profile": current_chat_profile,
            "initial_partner_id": request.query_params.get("user_id"),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
            "ws_token": access_token,
        },
    )
    response.set_cookie("token", access_token, httponly=True, path="/")
    return response
