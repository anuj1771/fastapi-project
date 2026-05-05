from datetime import datetime, timedelta
from email.message import EmailMessage
import logging
import os
from pathlib import Path
import smtplib

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, models, schemas
from app.db import Base, engine
from app.deps import get_current_user, get_db, require_admin

Base.metadata.create_all(bind=engine)

app = FastAPI(title="BrandBridge")

base_dir = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(base_dir / "templates"))
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")

TEMPLATES = [
    "Hello! We are interested in collaborating with your profile.",
    "Thanks for reaching out. Let's discuss campaign details.",
    "Can we schedule a short call to discuss partnership?",
]


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
    subject = "Reset your BrandBridge password"
    body = (
        "We received a request to reset your password.\n\n"
        f"Click this link to set a new password: {reset_link}\n\n"
        "This link expires in 30 minutes. If you did not request this, you can ignore this email."
    )
    _send_email(user_email, subject, body)


def _get_user_from_cookie(request: Request, db: Session) -> models.User | None:
    token = request.cookies.get("token")
    if not token:
        return None
    user_id = auth.decode_token(token)
    if not user_id or not user_id.isdigit():
        return None
    return db.query(models.User).filter(models.User.id == int(user_id)).first()


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


@app.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)):
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
    profile.status = models.ProfileStatus.APPROVED
    profile.reviewed_at = datetime.utcnow()
    profile.rejected_until = None
    profile.rejection_reason = None
    profile.updated_at = datetime.utcnow()
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
    profile.status = models.ProfileStatus.REJECTED
    profile.reviewed_at = datetime.utcnow()
    profile.rejected_until = datetime.utcnow() + timedelta(days=30)
    profile.updated_at = datetime.utcnow()
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
    users = db.query(models.User).filter(models.User.id != current_user.id).all()
    allowed_users = [
        user for user in users if _can_chat_by_job_rules(db, current_user.id, user.id)
    ]
    return [schemas.RegisteredUserItem(id=user.id, email=user.email) for user in allowed_users]


@app.post("/chat/send", response_model=schemas.MessageOut)
def send_message(
    payload: schemas.ChatSendRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    receiver = db.query(models.User).filter(models.User.id == payload.receiver_id).first()
    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")
    if not _can_chat_by_job_rules(db, current_user.id, payload.receiver_id):
        raise HTTPException(status_code=403, detail="Chat is not allowed for this user yet")

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
            if not _can_chat_by_job_rules(db, current_user.id, receiver_id):
                await websocket.send_json(
                    {"type": "error", "detail": "Chat is not allowed for this user yet"}
                )
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
    if not _can_chat_by_job_rules(db, current_user.id, user_id):
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
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "request": request,
            "user": user,
            "display_name": display_name,
            "advertiser_profile": advertiser_profile,
            "brand_profile": brand_profile,
            "advertiser_request": approval_map.get(models.ProfileType.ADVERTISER.value),
            "brand_request": approval_map.get(models.ProfileType.BRAND.value),
            "advertiser_complete": _is_basic_profile_complete(advertiser_profile),
            "brand_complete": _is_basic_profile_complete(brand_profile),
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
        return RedirectResponse(url=f"/profile?error={first_error}", status_code=303)
    except Exception:
        return RedirectResponse(url="/profile?error=Failed to save profile details.", status_code=303)
    return RedirectResponse(url="/profile?success=Profile saved successfully.", status_code=303)


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
        _require_completed_basic_profile(db, user.id, parsed_type)
    except Exception:
        return RedirectResponse(
            url=f"/profile?error=Complete your {profile_type} profile before sending approval request.",
            status_code=303,
        )

    approval_request = _get_approval_request(db, user.id, parsed_type)
    now = datetime.utcnow()
    if approval_request:
        if approval_request.status == models.ProfileStatus.PENDING:
            return RedirectResponse(
                url="/profile?error=Approval request is already pending for this profile.",
                status_code=303,
            )
        if approval_request.status == models.ProfileStatus.APPROVED:
            return RedirectResponse(
                url="/profile?error=This profile is already approved.",
                status_code=303,
            )
        if approval_request.rejected_until and approval_request.rejected_until > now:
            return RedirectResponse(
                url="/profile?error=Your request was rejected. You can send again after one month.",
                status_code=303,
            )
        approval_request.status = models.ProfileStatus.PENDING
        approval_request.requested_at = now
        approval_request.reviewed_at = None
        approval_request.rejection_reason = None
        approval_request.updated_at = now
        approval_request.rejected_until = None
    else:
        approval_request = models.ProfileApprovalRequest(
            user_id=user.id,
            profile_type=parsed_type,
            status=models.ProfileStatus.PENDING,
            requested_at=now,
            updated_at=now,
        )
        db.add(approval_request)
    db.commit()
    return RedirectResponse(
        url=f"/profile?success={parsed_type.value.title()} profile sent for admin approval.",
        status_code=303,
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    advertiser_complete = False
    brand_complete = False
    advertiser_request = None
    brand_request = None
    advertiser_approved = False
    brand_approved = False
    if user:
        profile_map = _get_basic_profile_map(db, user.id)
        approval_map = {
            row.profile_type.value: row
            for row in db.query(models.ProfileApprovalRequest)
            .filter(models.ProfileApprovalRequest.user_id == user.id)
            .all()
        }
        advertiser_complete = _is_basic_profile_complete(
            profile_map.get(models.ProfileType.ADVERTISER.value)
        )
        brand_complete = _is_basic_profile_complete(profile_map.get(models.ProfileType.BRAND.value))
        advertiser_request = approval_map.get(models.ProfileType.ADVERTISER.value)
        brand_request = approval_map.get(models.ProfileType.BRAND.value)
        advertiser_approved = bool(
            advertiser_request and advertiser_request.status == models.ProfileStatus.APPROVED
        )
        brand_approved = bool(brand_request and brand_request.status == models.ProfileStatus.APPROVED)
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
        db.query(models.ProfileApprovalRequest, models.User, models.BasicProfile)
        .join(models.User, models.User.id == models.ProfileApprovalRequest.user_id)
        .outerjoin(
            models.BasicProfile,
            and_(
                models.BasicProfile.user_id == models.ProfileApprovalRequest.user_id,
                models.BasicProfile.profile_type == models.ProfileApprovalRequest.profile_type,
            ),
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
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    if not _is_profile_type_approved(db, user.id, models.ProfileType.BRAND):
        return RedirectResponse(
            url="/dashboard?error=Only approved brands can explore advertisers.",
            status_code=303,
        )
    users = (
        db.query(models.User, models.BasicProfile)
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
        .filter(models.User.id != user.id)
        .order_by(models.User.email.asc())
        .all()
    )
    items = []
    for list_user, basic_profile in users:
        items.append(
            {
                "user": list_user,
                "basic_profile": basic_profile,
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
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/explore/brands", response_class=HTMLResponse)
def explore_brands_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    if not _is_profile_type_approved(db, user.id, models.ProfileType.ADVERTISER):
        return RedirectResponse(
            url="/dashboard?error=Only approved advertisers can explore brands.",
            status_code=303,
        )
    users = (
        db.query(models.User, models.BasicProfile)
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
        .filter(models.User.id != user.id)
        .order_by(models.User.email.asc())
        .all()
    )
    items = []
    for list_user, basic_profile in users:
        items.append(
            {
                "user": list_user,
                "basic_profile": basic_profile,
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
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/ui/chat/start/{target_user_id}")
def ui_start_chat(target_user_id: int, request: Request, db: Session = Depends(get_db)):
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
    if not is_valid_pair:
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
        db.add(models.ChatConnection(user_one_id=user_one_id, user_two_id=user_two_id))
        db.commit()
    return RedirectResponse(url=f"/chat-demo?user_id={target_user_id}", status_code=303)


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
    approval_request.status = models.ProfileStatus.APPROVED
    approval_request.reviewed_at = datetime.utcnow()
    approval_request.rejected_until = None
    approval_request.rejection_reason = None
    approval_request.updated_at = datetime.utcnow()
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
    approval_request.status = models.ProfileStatus.REJECTED
    approval_request.reviewed_at = datetime.utcnow()
    approval_request.rejected_until = datetime.utcnow() + timedelta(days=30)
    approval_request.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(
        url=f"/admin/approval-requests/{approval_request.profile_type.value}?success=Request rejected for one month.",
        status_code=303,
    )


@app.get("/jobs/create", response_class=HTMLResponse)
def create_job_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        _require_completed_basic_profile(db, user.id, models.ProfileType.BRAND)
    except HTTPException:
        return RedirectResponse(
            url="/dashboard?error=Complete your brand profile to create jobs.", status_code=303
        )
    return templates.TemplateResponse(
        request,
        "job_create.html",
        {
            "request": request,
            "user": user,
            "display_name": user.email.split("@")[0],
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
    target_instagram_profiles: str = Form(...),
    promotion_tags: str = Form(...),
    profile_image_url: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        _require_completed_basic_profile(db, user.id, models.ProfileType.BRAND)
        payload = schemas.JobCreate(
            title=title,
            promotion_requirement=promotion_requirement,
            budget=budget,
            target_instagram_profiles=target_instagram_profiles,
            promotion_tags=promotion_tags,
            profile_image_url=profile_image_url or None,
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]["msg"] if exc.errors() else "Invalid job input."
        return RedirectResponse(url=f"/jobs/create?error={first_error}", status_code=303)
    except HTTPException:
        return RedirectResponse(
            url="/dashboard?error=Complete your brand profile to create jobs.", status_code=303
        )

    job = models.Job(
        brand_user_id=user.id,
        title=payload.title.strip(),
        promotion_requirement=payload.promotion_requirement.strip(),
        budget=payload.budget.strip(),
        target_instagram_profiles=payload.target_instagram_profiles.strip(),
        promotion_tags=payload.promotion_tags.strip(),
        profile_image_url=(payload.profile_image_url.strip() if payload.profile_image_url else None),
    )
    db.add(job)
    db.commit()
    return RedirectResponse(url="/jobs/create?success=Job posted successfully.", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        _require_completed_basic_profile(db, user.id, models.ProfileType.ADVERTISER)
    except HTTPException:
        return RedirectResponse(
            url="/dashboard?error=Complete your advertiser profile to see jobs.", status_code=303
        )

    jobs = db.query(models.Job).order_by(models.Job.created_at.desc()).all()
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
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        return RedirectResponse(url="/jobs?error=Job not found.", status_code=303)
    try:
        _require_completed_basic_profile(db, user.id, models.ProfileType.ADVERTISER)
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
    db.add(application)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/jobs?error=You already applied to this job.", status_code=303)
    return RedirectResponse(url="/jobs?success=Applied successfully.", status_code=303)


@app.get("/brand/applications", response_class=HTMLResponse)
def brand_applications_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    try:
        _require_completed_basic_profile(db, user.id, models.ProfileType.BRAND)
    except HTTPException:
        return RedirectResponse(
            url="/dashboard?error=Complete your brand profile to review applicants.", status_code=303
        )

    jobs = (
        db.query(models.Job)
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
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse(url="/?error=Please login first.", status_code=303)
    return templates.TemplateResponse(
        request,
        "chat_demo.html",
        {
            "request": request,
            "user": user,
            "display_name": user.email.split("@")[0],
            "initial_partner_id": request.query_params.get("user_id"),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )
