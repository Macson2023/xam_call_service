from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import CallRecord
from app.realtime.registry import Call


class CallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, call: Call) -> CallRecord:
        record = CallRecord(
            id=call.id,
            caller_id=call.caller.id,
            callee_id=call.callee.id,
            caller_username=call.caller.username,
            caller_display_name=call.caller.display_name,
            callee_username=call.callee.username,
            callee_display_name=call.callee.display_name,
            media=call.media,
            state=call.state,
            status=call.status,
            end_reason=call.end_reason,
            created_at=call.created_at,
            answered_at=call.answered_at,
            ended_at=call.ended_at,
        )
        self.session.add(record)
        await self.session.commit()
        return record

    async def update(self, call: Call) -> CallRecord | None:
        record = await self.session.get(CallRecord, call.id)
        if record is None:
            return None
        record.state = call.state
        record.status = call.status
        record.end_reason = call.end_reason
        record.answered_at = call.answered_at
        record.ended_at = call.ended_at
        await self.session.commit()
        return record

    async def get_for_user(self, call_id: UUID, user_id: UUID) -> CallRecord | None:
        statement = select(CallRecord).where(
            CallRecord.id == call_id,
            or_(CallRecord.caller_id == user_id, CallRecord.callee_id == user_id),
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list_for_user(
        self, user_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[CallRecord], int]:
        condition = or_(CallRecord.caller_id == user_id, CallRecord.callee_id == user_id)
        count = await self.session.scalar(
            select(func.count()).select_from(CallRecord).where(condition)
        )
        statement = (
            select(CallRecord)
            .where(condition)
            .order_by(CallRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        records = list((await self.session.execute(statement)).scalars())
        return records, int(count or 0)

    async def delete_all(self) -> None:
        await self.session.execute(CallRecord.__table__.delete())
        await self.session.commit()
