from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models import PaymentStatus, ProfileStatus, ProfileType, UserRole


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=64)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=6, max_length=64)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    role: UserRole
    coins: int
    created_at: datetime

    class Config:
        from_attributes = True


class AdvertiserProfileCreate(BaseModel):
    instagram_id: str
    profile_url: str
    followers: int = Field(ge=0)


class BrandProfileCreate(BaseModel):
    brand_name: str
    brand_url: str
    website_link: str


class ProfileOut(BaseModel):
    id: int
    user_id: int
    profile_type: ProfileType
    status: ProfileStatus

    instagram_id: Optional[str] = None
    profile_url: Optional[str] = None
    followers: Optional[int] = None

    brand_name: Optional[str] = None
    brand_url: Optional[str] = None
    website_link: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProfileWithUser(BaseModel):
    id: int
    status: ProfileStatus
    profile_type: ProfileType
    user_email: EmailStr
    user_id: int

    class Config:
        from_attributes = True


class ChatSendRequest(BaseModel):
    receiver_id: int
    content: str = Field(min_length=1, max_length=2000)
    use_template: bool = False


class MessageOut(BaseModel):
    id: int
    sender_id: int
    receiver_id: int
    content: str
    is_template: bool
    created_at: datetime

    class Config:
        from_attributes = True


class AdminStats(BaseModel):
    total_users: int
    total_advertisers: int
    total_brands: int
    templates_sent: int
    total_messages: int


class UserListItem(BaseModel):
    id: int
    email: EmailStr
    profile_type: ProfileType


class RegisteredUserItem(BaseModel):
    id: int
    has_company: bool = False
    has_instagram: bool = False
    company_name: Optional[str] = None
    instagram_id: Optional[str] = None


class BasicProfileUpsert(BaseModel):
    profile_type: ProfileType
    name: str = Field(max_length=120)
    phone_number: str = Field(max_length=30)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Name is required")
        return cleaned

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: str) -> str:
        cleaned = value.strip().replace(" ", "")
        if not cleaned.isdigit():
            raise ValueError("Phone number must contain digits only")
        if len(cleaned) < 10 or len(cleaned) > 15:
            raise ValueError("Phone number must be between 10 and 15 digits")
        return cleaned


class BasicProfileOut(BaseModel):
    id: int
    user_id: int
    profile_type: ProfileType
    name: str
    phone_number: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class JobCreate(BaseModel):
    title: str = Field(min_length=3, max_length=150)
    promotion_requirement: str = Field(min_length=5, max_length=2000)
    budget: str = Field(min_length=1, max_length=80)
    promotion_tag_ids: list[int] = Field(min_length=1)
    target_profile_tag_ids: list[int] = Field(min_length=1)
    profile_image_url: Optional[str] = Field(default=None, max_length=255)


class JobOut(BaseModel):
    id: int
    brand_user_id: int
    title: str
    promotion_requirement: str
    budget: str
    target_instagram_profiles: Optional[str] = None
    promotion_tags: Optional[str] = None
    profile_image_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class JobTagCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class JobApplicationCreate(BaseModel):
    description: str = Field(min_length=5, max_length=2000)


class JobApplicationOut(BaseModel):
    id: int
    job_id: int
    advertiser_user_id: int
    description: str
    is_selected: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class InteractionUserRef(BaseModel):
    id: int
    display_name: str
    company_name: Optional[str] = None
    instagram_id: Optional[str] = None
    email: Optional[EmailStr] = None
    contact_count: int = 0


class InteractionConnection(BaseModel):
    user: InteractionUserRef
    direction: str
    status: str
    can_selected_contact: bool
    can_contact_selected: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    note: Optional[str] = None
    related_jobs: list[str] = []


class InteractionSummary(BaseModel):
    can_contact: int = 0
    can_be_contacted_by: int = 0
    two_way: int = 0
    blocked: int = 0
    pending: int = 0


class InteractionMapOut(BaseModel):
    selected_user: InteractionUserRef
    connections: list[InteractionConnection]
    summary: InteractionSummary


class CoinPackageOut(BaseModel):
    id: int
    coins: int
    price: int
    currency: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PaymentCreate(BaseModel):
    package_id: int = Field(ge=1)


class PaymentOut(BaseModel):
    payment_id: str
    user_id: int
    package_id: int
    coins: int
    amount: int
    currency: str
    status: PaymentStatus
    upi_id: Optional[str] = None
    payee_name: Optional[str] = None
    upi_uri: Optional[str] = None
    qr_data_url: Optional[str] = None
    submitted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AdminPaymentOut(BaseModel):
    payment_id: str
    user_id: int
    user_email: str
    package_id: int
    coins: int
    amount: int
    currency: str
    status: PaymentStatus
    submitted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
