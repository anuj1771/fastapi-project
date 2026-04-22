from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models import ProfileStatus, ProfileType, UserRole


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=64)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: EmailStr
    role: UserRole
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
