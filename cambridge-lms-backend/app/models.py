from pydantic import BaseModel, EmailStr, Field, model_validator
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

class ExamSchema(BaseModel):
    title: str = Field(...)
    level: str = Field(...)
    duration_minutes: int = Field(...)
    pdf_file_url: Optional[str] = Field(None)
    total_score: float = Field(default=100.0)
    answer_key: Optional[Dict[str, Any]] = Field(None)
    audio_folders: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        populate_by_name = True

class StudentRegister(BaseModel):
    email: EmailStr = Field(...)
    password: str = Field(..., min_length=6)
    confirm_password: str = Field(...)

    @model_validator(mode="after")
    def check_passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Mật khẩu nhập lại không khớp")
        return self

class UserLogin(BaseModel):
    email: EmailStr = Field(...)
    password: str = Field(...)