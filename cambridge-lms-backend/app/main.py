import os
import shutil
from contextlib import asynccontextmanager
from fastapi import FastAPI, Body, HTTPException, status, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from bson import ObjectId
from passlib.context import CryptContext
from datetime import datetime, timezone

from app.database import ping_database, database
from app.models import ExamSchema, StudentRegister, UserLogin

# --- 1. TẠO THƯ MỤC LƯU TRỮ VẬT LÝ ---
os.makedirs("uploads/pdfs", exist_ok=True)
os.makedirs("uploads/audios", exist_ok=True)

# --- 2. CẤU HÌNH MÃ HÓA MẬT KHẨU ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password):
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

# --- 3. THAM CHIẾU DATABASE ---
exam_collection = database.get_collection("exams")
user_collection = database.get_collection("users")

# --- 4. LOGIC TẠO TÀI KHOẢN GIÁO VIÊN MẶC ĐỊNH ---
async def seed_teacher_account():
    teacher_email = "giaovien@gmail.com"
    teacher = await user_collection.find_one({"email": teacher_email})
    if not teacher:
        teacher_data = {
            "email": teacher_email,
            "password": get_password_hash("123456789!@#"),
            "role": "teacher",
            "created_at": datetime.now(timezone.utc)
        }
        await user_collection.insert_one(teacher_data)
        print("=== ĐÃ TẠO TÀI KHOẢN GIÁO VIÊN MẶC ĐỊNH ===")

# --- 5. LIFESPAN (CHẠY KHI KHỞI ĐỘNG SERVER) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    await ping_database()
    await seed_teacher_account()
    yield

# --- 6. KHỞI TẠO APP VÀ CẤU HÌNH MIDDLEWARE/STATIC FILES ---
app = FastAPI(
    title="Cambridge LMS API",
    description="Hệ thống quản lý bài thi tiếng Anh chuẩn cấu trúc Azota",
    version="1.0.0",
    lifespan=lifespan
)

# Cấu hình CORS công khai cho Frontend gọi API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],
)

# Cấu hình cấp quyền truy cập tài nguyên tĩnh công khai (Xem file PDF/Nghe nhạc)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ==========================================
# CÁC API ENDPOINTS XÁC THỰC (AUTH)
# ==========================================

@app.get("/")
async def read_root():
    return {"status": "online", "message": "Cambridge LMS API đang hoạt động ổn định"}

@app.post("/auth/register", response_description="Đăng ký tài khoản học sinh", status_code=status.HTTP_201_CREATED)
async def register_student(user: StudentRegister = Body(...)):
    existing_user = await user_collection.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email này đã được đăng ký!")
    
    user_dict = {
        "email": user.email,
        "password": get_password_hash(user.password),
        "role": "student",
        "created_at": datetime.now(timezone.utc)
    }
    
    new_user = await user_collection.insert_one(user_dict)
    if new_user.inserted_id:
        return {"message": "Đăng ký tài khoản học sinh thành công!"}
    
    raise HTTPException(status_code=500, detail="Lỗi hệ thống khi đăng ký")

@app.post("/auth/login", response_description="Đăng nhập hệ thống")
async def login(user_credentials: UserLogin = Body(...)):
    user = await user_collection.find_one({"email": user_credentials.email})
    if not user:
        raise HTTPException(status_code=404, detail="Email không tồn tại")
    
    if not verify_password(user_credentials.password, user["password"]):
        raise HTTPException(status_code=401, detail="Mật khẩu không chính xác")
    
    return {
        "message": "Đăng nhập thành công",
        "user_id": str(user["_id"]),
        "email": user["email"],
        "role": user["role"]
    }


# ==========================================
# CÁC API QUẢN LÝ BÀI THI (EXAMS)
# ==========================================

@app.get("/exams/", response_description="Lấy danh sách toàn bộ bài thi")
async def get_all_exams():
    exams = []
    cursor = exam_collection.find().sort("created_at", -1)
    async for document in cursor:
        document["_id"] = str(document["_id"])
        exams.append(document)
    return exams

@app.post("/exams/", response_description="Tạo cấu trúc bài thi mới", status_code=status.HTTP_201_CREATED)
async def create_exam(exam: ExamSchema = Body(...)):
    exam_dict = exam.model_dump()
    exam_dict["created_at"] = datetime.now(timezone.utc)
    
    new_exam = await exam_collection.insert_one(exam_dict)
    if new_exam.inserted_id:
        return {
            "message": "Khởi tạo ma trận đề thành công! Hãy tiếp tục upload file đa phương tiện.",
            "exam_id": str(new_exam.inserted_id)
        }
    raise HTTPException(status_code=500, detail="Không thể lưu cấu trúc bài thi")

@app.delete("/exams/{exam_id}", response_description="Xóa bài thi")
async def delete_exam(exam_id: str):
    if not ObjectId.is_valid(exam_id):
        raise HTTPException(status_code=400, detail="ID bài thi không hợp lệ")
    
    result = await exam_collection.delete_one({"_id": ObjectId(exam_id)})
    if result.deleted_count == 1:
        return {"message": "Đã xóa bài thi khỏi hệ thống"}
    
    raise HTTPException(status_code=404, detail="Không tìm thấy bài thi để xóa")
@app.post("/exams/{exam_id}/upload-files/", response_description="Tải lên tài nguyên đề thi")
async def upload_exam_files(
    exam_id: str,
    pdf_file: UploadFile = File(None, description="Đề thi định dạng PDF"),
    audio_files: list[UploadFile] = File(None, description="Danh sách các file âm thanh bài nghe")
):
    if not ObjectId.is_valid(exam_id):
        raise HTTPException(status_code=400, detail="ID bài thi không hợp lệ")

    update_data = {}

    # 1. Xử lý lưu trữ file đề PDF
    if pdf_file:
        pdf_path = f"uploads/pdfs/{exam_id}_{pdf_file.filename}"
        with open(pdf_path, "wb") as buffer:
            shutil.copyfileobj(pdf_file.file, buffer)
        update_data["pdf_file_url"] = f"/uploads/pdfs/{exam_id}_{pdf_file.filename}"

    # 2. Xử lý lưu album nhiều file âm thanh (CÓ BỘ LỌC FILE RÁC MACOS)
    if audio_files and len(audio_files) > 0 and audio_files[0].filename != "":
        saved_audio_urls = []
        for file in audio_files:
            # --- BỘ LỌC BẢO VỆ ---
            # Bỏ qua ngay lập tức các thư mục __MACOSX và các file ẩn bắt đầu bằng '._'
            if "__MACOSX" in file.filename or "/._" in file.filename or file.filename.startswith("._"):
                continue
            
            # Chỉ cho phép các file thực sự là âm thanh đi qua
            if not file.filename.lower().endswith(('.mp3', '.wav', '.ogg', '.m4a')):
                continue
            # ----------------------

            safe_filename = file.filename.replace(" ", "_")
            audio_path = f"uploads/audios/{exam_id}_{safe_filename}"
            
            os.makedirs(os.path.dirname(audio_path), exist_ok=True)
            
            with open(audio_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
                
            saved_audio_urls.append(f"/uploads/audios/{exam_id}_{safe_filename}")
        
        # Chỉ cập nhật nếu thực sự có file hợp lệ được lưu
        if len(saved_audio_urls) > 0:
            update_data["audio_file_urls"] = saved_audio_urls
            update_data["audio_file_url"] = saved_audio_urls[0]

    if not update_data:
        raise HTTPException(status_code=400, detail="Vui lòng lựa chọn tài liệu để tải lên")

    result = await exam_collection.update_one(
        {"_id": ObjectId(exam_id)},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Không tìm thấy mã đề thi trên hệ thống.")

    return {
        "message": "Đồng bộ tài nguyên tệp tin thành công!",
        "file_urls": update_data
    }