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


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(SqlEnum(UserRole), default=UserRole.USER, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    profile = relationship("Profile", back_populates="user", uselist=False)
    basic_profiles = relationship("BasicProfile", back_populates="user")
    approval_requests = relationship("ProfileApprovalRequest", back_populates="user")
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

    user = relationship("User", back_populates="approval_requests")


class ChatConnection(Base):
    __tablename__ = "chat_connections"
    __table_args__ = (
        UniqueConstraint("user_one_id", "user_two_id", name="uq_chat_connection_pair"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_one_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    user_two_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    brand_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(150), nullable=False)
    promotion_requirement = Column(Text, nullable=False)
    budget = Column(String(80), nullable=False)
    target_instagram_profiles = Column(Text, nullable=False)
    promotion_tags = Column(String(255), nullable=False)
    profile_image_url = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    brand_user = relationship("User")
    applications = relationship("JobApplication", back_populates="job", cascade="all, delete-orphan")


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
