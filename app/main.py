from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
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


def _get_user_from_cookie(request: Request, db: Session) -> models.User | None:
    token = request.cookies.get("token")
    if not token:
        return None
    user_id = auth.decode_token(token)
    if not user_id or not user_id.isdigit():
        return None
    return db.query(models.User).filter(models.User.id == int(user_id)).first()


def _ensure_no_profile(user: models.User, db: Session):
    existing = db.query(models.Profile).filter(models.Profile.user_id == user.id).first()
    if existing:
        raise HTTPException(status_code=400, detail="User already has a profile")


def _get_basic_profile_map(db: Session, user_id: int) -> dict[str, models.BasicProfile]:
    rows = db.query(models.BasicProfile).filter(models.BasicProfile.user_id == user_id).all()
    return {row.profile_type.value: row for row in rows}


def _is_basic_profile_complete(profile: models.BasicProfile | None) -> bool:
    if not profile:
        return False
    return bool(profile.name and profile.name.strip() and profile.phone_number and profile.phone_number.strip())


def _get_approved_profile(db: Session, user_id: int) -> models.Profile:
    profile = db.query(models.Profile).filter(models.Profile.user_id == user_id).first()
    if not profile or profile.status != models.ProfileStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only approved users can interact with chat",
        )
    return profile


def _validate_chat_pair(sender_profile: models.Profile, receiver_profile: models.Profile):
    if sender_profile.profile_type == receiver_profile.profile_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only Brand <-> Advertiser chat is allowed",
        )


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


@app.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user


@app.post("/advertiser-profile", response_model=schemas.ProfileOut)
def create_advertiser_profile(
    payload: schemas.AdvertiserProfileCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != models.UserRole.USER:
        raise HTTPException(status_code=403, detail="Admin cannot create advertiser profile")
    _ensure_no_profile(current_user, db)
    profile = models.Profile(
        user_id=current_user.id,
        profile_type=models.ProfileType.ADVERTISER,
        instagram_id=payload.instagram_id,
        profile_url=payload.profile_url,
        followers=payload.followers,
        status=models.ProfileStatus.PENDING,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@app.post("/brand-profile", response_model=schemas.ProfileOut)
def create_brand_profile(
    payload: schemas.BrandProfileCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != models.UserRole.USER:
        raise HTTPException(status_code=403, detail="Admin cannot create brand profile")
    _ensure_no_profile(current_user, db)
    profile = models.Profile(
        user_id=current_user.id,
        profile_type=models.ProfileType.BRAND,
        brand_name=payload.brand_name,
        brand_url=payload.brand_url,
        website_link=payload.website_link,
        status=models.ProfileStatus.PENDING,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


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
    profiles = db.query(models.Profile, models.User).join(models.User).all()
    response = []
    for profile, user in profiles:
        response.append(
            {
                "id": profile.id,
                "user_id": user.id,
                "user_email": user.email,
                "profile_type": profile.profile_type,
                "status": profile.status,
            }
        )
    return response


@app.post("/admin/approve/{profile_id}", response_model=schemas.ProfileOut)
def admin_approve(
    profile_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    profile.status = models.ProfileStatus.APPROVED
    db.commit()
    db.refresh(profile)
    return profile


@app.post("/admin/reject/{profile_id}", response_model=schemas.ProfileOut)
def admin_reject(
    profile_id: int, _: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    profile.status = models.ProfileStatus.REJECTED
    db.commit()
    db.refresh(profile)
    return profile


@app.get("/admin/stats", response_model=schemas.AdminStats)
def admin_stats(_: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    total_users = db.query(func.count(models.User.id)).scalar() or 0
    total_advertisers = (
        db.query(func.count(models.Profile.id))
        .filter(
            and_(
                models.Profile.profile_type == models.ProfileType.ADVERTISER,
                models.Profile.status == models.ProfileStatus.APPROVED,
            )
        )
        .scalar()
        or 0
    )
    total_brands = (
        db.query(func.count(models.Profile.id))
        .filter(
            and_(
                models.Profile.profile_type == models.ProfileType.BRAND,
                models.Profile.status == models.ProfileStatus.APPROVED,
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
    my_profile = _get_approved_profile(db, current_user.id)
    opposite_type = (
        models.ProfileType.BRAND
        if my_profile.profile_type == models.ProfileType.ADVERTISER
        else models.ProfileType.ADVERTISER
    )
    users = (
        db.query(models.User, models.Profile)
        .join(models.Profile, models.Profile.user_id == models.User.id)
        .filter(
            and_(
                models.Profile.status == models.ProfileStatus.APPROVED,
                models.Profile.profile_type == opposite_type,
            )
        )
        .all()
    )
    return [
        schemas.UserListItem(id=user.id, email=user.email, profile_type=profile.profile_type)
        for user, profile in users
        if user.id != current_user.id
    ]


@app.post("/chat/send", response_model=schemas.MessageOut)
def send_message(
    payload: schemas.ChatSendRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sender_profile = _get_approved_profile(db, current_user.id)
    receiver = db.query(models.User).filter(models.User.id == payload.receiver_id).first()
    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")
    receiver_profile = _get_approved_profile(db, receiver.id)
    _validate_chat_pair(sender_profile, receiver_profile)

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


@app.get("/chat/{user_id}", response_model=list[schemas.MessageOut])
def chat_history(
    user_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
):
    sender_profile = _get_approved_profile(db, current_user.id)
    other = db.query(models.User).filter(models.User.id == user_id).first()
    if not other:
        raise HTTPException(status_code=404, detail="User not found")
    other_profile = _get_approved_profile(db, other.id)
    _validate_chat_pair(sender_profile, other_profile)

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


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    profile = None
    if user:
        profile = db.query(models.Profile).filter(models.Profile.user_id == user.id).first()
    display_name = user.email.split("@")[0] if user else None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "profile": profile,
            "templates": TEMPLATES,
            "display_name": display_name,
        },
    )
