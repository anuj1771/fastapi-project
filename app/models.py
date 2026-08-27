from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db import Base


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


class ProfileType(str, Enum):
    ADVERTISER = "advertiser"
    BRAND = "brand"


class ProfileStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AdvertiserVerificationStage(str, Enum):
    """Advertiser-only workflow on ProfileApprovalRequest."""
    INITIAL_REVIEW = "initial_review"
    OTP_SENT = "otp_sent"
    FINAL_REVIEW = "final_review"


class OtpVerificationStatus(str, Enum):
    NOT_SENT = "not_sent"
    PENDING = "pending"
    VERIFIED = "verified"


class AdvertiserVerificationRequestStatus(str, Enum):
    """High-level advertiser verification state shown to the user."""
    DRAFT = "draft"
    SUBMITTED = "submitted"
    OTP_PENDING = "otp_pending"
    OTP_VERIFIED = "otp_verified"
    PENDING_FINAL = "pending_final"
    APPROVED = "approved"
    REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(SqlEnum(UserRole), default=UserRole.USER, nullable=False)
    coins = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    profile = relationship("Profile", back_populates="user", uselist=False)
    basic_profiles = relationship("BasicProfile", back_populates="user")
    approval_requests = relationship("ProfileApprovalRequest", back_populates="user")
    advertiser_profile_detail = relationship(
        "AdvertiserProfileDetail", back_populates="user", uselist=False
    )
    brand_profile_detail = relationship(
        "BrandProfileDetail", back_populates="user", uselist=False
    )
    sent_messages = relationship(
        "Message", back_populates="sender", foreign_keys="Message.sender_id"
    )
    received_messages = relationship(
        "Message", back_populates="receiver", foreign_keys="Message.receiver_id"
    )


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    profile_type = Column(SqlEnum(ProfileType), nullable=False)
    status = Column(SqlEnum(ProfileStatus), default=ProfileStatus.PENDING, nullable=False)

    instagram_id = Column(String(150), nullable=True)
    profile_url = Column(String(255), nullable=True)
    followers = Column(Integer, nullable=True)

    brand_name = Column(String(150), nullable=True)
    brand_url = Column(String(255), nullable=True)
    website_link = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="profile")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    is_template = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    sender = relationship("User", foreign_keys=[sender_id], back_populates="sent_messages")
    receiver = relationship(
        "User", foreign_keys=[receiver_id], back_populates="received_messages"
    )


class BasicProfile(Base):
    __tablename__ = "basic_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "profile_type", name="uq_basic_profile_user_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    profile_type = Column(SqlEnum(ProfileType), nullable=False)
    name = Column(String(120), nullable=False)
    phone_number = Column(String(30), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="basic_profiles")


class ProfileApprovalRequest(Base):
    __tablename__ = "profile_approval_requests"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "profile_type", name="uq_profile_approval_request_user_type"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    profile_type = Column(SqlEnum(ProfileType), nullable=False, index=True)
    status = Column(SqlEnum(ProfileStatus), default=ProfileStatus.PENDING, nullable=False)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)
    rejected_until = Column(DateTime, nullable=True)
    rejection_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Advertiser OTP workflow (null for brand requests)
    advertiser_verification_stage = Column(
        SqlEnum(AdvertiserVerificationStage), nullable=True, index=True
    )
    generated_otp = Column(Integer, nullable=True)
    user_entered_otp = Column(Integer, nullable=True)

    user = relationship("User", back_populates="approval_requests")


class AdvertiserProfileDetail(Base):
    __tablename__ = "advertiser_profile_details"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    instagram_id = Column(String(150), nullable=True)
    instagram_profile_url = Column(String(500), nullable=True)
    reel_cost = Column(Integer, nullable=True)
    collaboration_cost = Column(Integer, nullable=True)
    story_cost = Column(Integer, nullable=True)
    post_cost = Column(Integer, nullable=True)
    instagram_followers = Column(Integer, nullable=True)
    otp_verification_status = Column(
        SqlEnum(OtpVerificationStatus),
        default=OtpVerificationStatus.NOT_SENT,
        nullable=False,
    )
    verification_request_status = Column(
        SqlEnum(AdvertiserVerificationRequestStatus),
        default=AdvertiserVerificationRequestStatus.DRAFT,
        nullable=False,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="advertiser_profile_detail")


class BrandProfileDetail(Base):
    __tablename__ = "brand_profile_details"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    brand_name = Column(String(200), nullable=True)
    website_url = Column(String(500), nullable=True)
    brand_email = Column(String(255), nullable=True)
    contact_person_name = Column(String(150), nullable=True)
    contact_person_phone = Column(String(30), nullable=True)
    pan_number = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="brand_profile_detail")


class ChatConnection(Base):
    __tablename__ = "chat_connections"
    __table_args__ = (
        UniqueConstraint("user_one_id", "user_two_id", name="uq_chat_connection_pair"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_one_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    user_two_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


job_promotion_tag_links = Table(
    "job_promotion_tag_links",
    Base.metadata,
    Column("job_id", Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "promotion_tag_id",
        Integer,
        ForeignKey("promotion_tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

job_target_profile_tag_links = Table(
    "job_target_profile_tag_links",
    Base.metadata,
    Column("job_id", Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "target_profile_tag_id",
        Integer,
        ForeignKey("target_profile_tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class PromotionTag(Base):
    __tablename__ = "promotion_tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    jobs = relationship(
        "Job",
        secondary=job_promotion_tag_links,
        back_populates="promotion_tag_items",
    )


class TargetProfileTag(Base):
    __tablename__ = "target_profile_tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    jobs = relationship(
        "Job",
        secondary=job_target_profile_tag_links,
        back_populates="target_profile_tag_items",
    )


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    brand_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(150), nullable=False)
    promotion_requirement = Column(Text, nullable=False)
    budget = Column(String(80), nullable=False)
    target_instagram_profiles = Column(Text, nullable=True)
    promotion_tags = Column(String(255), nullable=True)
    profile_image_url = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    brand_user = relationship("User")
    applications = relationship("JobApplication", back_populates="job", cascade="all, delete-orphan")
    promotion_tag_items = relationship(
        "PromotionTag",
        secondary=job_promotion_tag_links,
        back_populates="jobs",
    )
    target_profile_tag_items = relationship(
        "TargetProfileTag",
        secondary=job_target_profile_tag_links,
        back_populates="jobs",
    )

    @property
    def promotion_tag_labels(self) -> str:
        if self.promotion_tag_items:
            return ", ".join(tag.name for tag in self.promotion_tag_items)
        return self.promotion_tags or ""

    @property
    def target_profile_labels(self) -> str:
        if self.target_profile_tag_items:
            return ", ".join(tag.name for tag in self.target_profile_tag_items)
        return self.target_instagram_profiles or ""


class JobApplication(Base):
    __tablename__ = "job_applications"
    __table_args__ = (
        UniqueConstraint("job_id", "advertiser_user_id", name="uq_job_advertiser_application"),
    )

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    advertiser_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    description = Column(Text, nullable=False)
    is_selected = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    job = relationship("Job", back_populates="applications")
    advertiser_user = relationship("User")


class CoinCostSetting(Base):
    __tablename__ = "coin_cost_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(80), unique=True, nullable=False, index=True)
    cost = Column(Integer, default=0, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"


class CoinPackage(Base):
    __tablename__ = "coin_packages"

    id = Column(Integer, primary_key=True, index=True)
    coins = Column(Integer, nullable=False, unique=True, index=True)
    price = Column(Integer, nullable=False)
    currency = Column(String(8), default="INR", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(String(36), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    package_id = Column(Integer, ForeignKey("coin_packages.id"), nullable=False, index=True)
    coins = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String(8), default="INR", nullable=False)
    status = Column(
        SqlEnum(PaymentStatus),
        default=PaymentStatus.PENDING,
        nullable=False,
        index=True,
    )
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User")
    package = relationship("CoinPackage")
