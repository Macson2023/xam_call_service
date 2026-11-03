from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CallRecord(Base):
    __tablename__ = "calls"
    __table_args__ = (
        CheckConstraint("media IN ('audio', 'video')", name="ck_calls_media"),
        CheckConstraint(
            "state IN ('ringing', 'active', 'ended')",
            name="ck_calls_state",
        ),
        CheckConstraint(
            "status IN ('ringing', 'active', 'completed', 'missed', 'rejected', "
            "'cancelled', 'failed')",
            name="ck_calls_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    caller_id: Mapped[UUID] = mapped_column(Uuid, index=True, nullable=False)
    callee_id: Mapped[UUID] = mapped_column(Uuid, index=True, nullable=False)
    caller_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caller_display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    callee_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    callee_display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    media: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
