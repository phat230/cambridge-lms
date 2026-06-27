from pydantic import BaseModel, EmailStr, Field, model_validator
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

class ExamSchema(BaseModel):
    title: str = Field(..., description="Tên bài thi (VD: Starters Practice Test 1)")
    level: str = Field(..., description="Cấp độ (VD: Starters, Movers, Flyers)")
    duration_minutes: int = Field(..., description="Thời gian làm bài tính bằng phút")
    pdf_file_url: Optional[str] = Field(None, description="Đường dẫn hoặc link lưu file PDF đề thi")
    
    # Giữ lại url đơn cho tính tương thích ngược, thêm mảng urls cho chế độ nhiều file
    audio_file_url: Optional[str] = Field(None, description="Đường dẫn file mp3 (mặc định file đầu tiên)")
    audio_file_urls: Optional[List[str]] = Field(None, description="Danh sách đường dẫn các file Audio")
    
    total_score: float = Field(default=100.0, description="Tổng điểm của bài thi")
    
    # THÊM MỚI QUAN TRỌNG: Trường nhận cài đặt giới hạn lượt nghe từ giáo viên
    audio_limit: int = Field(default=0, description="Giới hạn số lần nghe file âm thanh (0 là vô hạn)")
    
    # Trường nhận ma trận đáp án từ giao diện Azota
    answer_key: Optional[Dict[str, Any]] = Field(None, description="Cấu trúc ma trận đáp án chuẩn")
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "title": "Movers Listening Test 2",
                "level": "Movers",
                "duration_minutes": 30,
                "pdf_file_url": "/uploads/pdfs/movers_test_2.pdf",
                "audio_file_url": "/uploads/audios/movers_test_2.mp3",
                "audio_file_urls": ["/uploads/audios/cd1.mp3", "/uploads/audios/cd2.mp3"],
                "total_score": 100.0,
                "audio_limit": 2,
                "answer_key": {
                    "Phần 1: Listening (Nghe)": {
                        "Part 1": {
                            "num_questions": 5,
                            "part_score": 25,
                            "questions": [{"qNum": 1, "answer": "a"}]
                        }
                    }
                }
            }
        }

class StudentRegister(BaseModel):
    email: EmailStr = Field(..., description="Email của học sinh")
    password: str = Field(..., min_length=6, description="Mật khẩu (ít nhất 6 ký tự)")
    confirm_password: str = Field(..., description="Nhập lại mật khẩu")

    @model_validator(mode='after')
    def check_passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError('Mật khẩu và Nhập lại mật khẩu không khớp!')
        return self

class UserLogin(BaseModel):
    email: EmailStr = Field(...)
    password: str = Field(...)