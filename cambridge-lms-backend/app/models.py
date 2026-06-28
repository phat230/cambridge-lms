from typing import Any, Dict, Optional
from pydantic import BaseModel, EmailStr, Field


class StudentRegister(BaseModel):
    email: EmailStr = Field(...)
    password: str = Field(..., min_length=6)
    confirm_password: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr = Field(...)
    password: str = Field(...)


class ExamSchema(BaseModel):
    title: str = Field(..., min_length=1)
    level: str = Field(..., min_length=1)
    duration_minutes: int = Field(..., gt=0)
    total_score: Optional[float] = 0
    answer_key: Optional[Dict[str, Any]] = None
