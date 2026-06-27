import os
import shutil
from contextlib import asynccontextmanager
from fastapi import FastAPI, Body, HTTPException, status, UploadFile, File, Form
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

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password):
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

exam_collection = database.get_collection("exams")
user_collection = database.get_collection("users")

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    await ping_database()
    await seed_teacher_account()
    yield

app = FastAPI(
    title="Cambridge LMS API",
    description="Hệ thống quản lý bài thi tiếng Anh",
    version="1.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ==========================================
# AUTH API
# ==========================================
@app.get("/")
async def read_root():
    return {"status": "online", "message": "Cambridge LMS API đang hoạt động"}

@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register_student(user: StudentRegister = Body(...)):
    if await user_collection.find_one({"email": user.email}):
        raise HTTPException(status_code=400, detail="Email này đã được đăng ký!")
    user_dict = {"email": user.email, "password": get_password_hash(user.password), "role": "student", "created_at": datetime.now(timezone.utc)}
    new_user = await user_collection.insert_one(user_dict)
    if new_user.inserted_id: return {"message": "Đăng ký thành công!"}
    raise HTTPException(status_code=500, detail="Lỗi đăng ký")

@app.post("/auth/login")
async def login(user_credentials: UserLogin = Body(...)):
    user = await user_collection.find_one({"email": user_credentials.email})
    if not user or not verify_password(user_credentials.password, user["password"]):
        raise HTTPException(status_code=401, detail="Sai thông tin đăng nhập")
    return {"message": "Đăng nhập thành công", "user_id": str(user["_id"]), "email": user["email"], "role": user["role"]}


# ==========================================
# EXAMS API (Cập nhật cho Quản lý Audio)
# ==========================================
@app.get("/exams/")
async def get_all_exams():
    exams = []
    async for document in exam_collection.find().sort("created_at", -1):
        document["_id"] = str(document["_id"])
        exams.append(document)
    return exams

@app.post("/exams/", status_code=status.HTTP_201_CREATED)
async def create_exam(exam: ExamSchema = Body(...)):
    exam_dict = exam.model_dump()
    exam_dict["created_at"] = datetime.now(timezone.utc)
    # Khởi tạo một mảng rỗng để chứa các thư mục Audio sau này
    exam_dict["audio_folders"] = [] 
    
    new_exam = await exam_collection.insert_one(exam_dict)
    if new_exam.inserted_id: return {"exam_id": str(new_exam.inserted_id)}
    raise HTTPException(status_code=500, detail="Không thể lưu")

@app.delete("/exams/{exam_id}")
async def delete_exam(exam_id: str):
    result = await exam_collection.delete_one({"_id": ObjectId(exam_id)})
    if result.deleted_count == 1: return {"message": "Đã xóa bài thi"}
    raise HTTPException(status_code=404, detail="Không tìm thấy")

# --- API UPLOAD FILE PDF (Chỉ giữ lại up PDF) ---
@app.post("/exams/{exam_id}/upload-pdf/")
async def upload_exam_pdf(exam_id: str, pdf_file: UploadFile = File(...)):
    pdf_path = f"uploads/pdfs/{exam_id}_{pdf_file.filename.replace(' ', '_')}"
    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(pdf_file.file, buffer)
    
    await exam_collection.update_one({"_id": ObjectId(exam_id)}, {"$set": {"pdf_file_url": f"/{pdf_path}"}})
    return {"message": "Tải PDF thành công!"}

# ==========================================
# AUDIO MANAGER API (MỚI)
# ==========================================

# 1. API Tạo thư mục Audio
@app.post("/exams/{exam_id}/audio-folders/")
async def create_audio_folder(exam_id: str, folder_name: str = Body(..., embed=True), limit: int = Body(0, embed=True)):
    # Mỗi folder sẽ là một object chứa tên, giới hạn lượt nghe, và mảng các file nhạc
    new_folder = {
        "id": str(ObjectId()),
        "name": folder_name,
        "limit": limit,
        "tracks": []
    }
    await exam_collection.update_one(
        {"_id": ObjectId(exam_id)},
        {"$push": {"audio_folders": new_folder}}
    )
    return {"message": "Tạo thư mục thành công", "folder": new_folder}

# 2. API Tải file Audio vào một thư mục cụ thể
@app.post("/exams/{exam_id}/audio-folders/{folder_id}/upload")
async def upload_audio_to_folder(exam_id: str, folder_id: str, audio_files: list[UploadFile] = File(...)):
    exam = await exam_collection.find_one({"_id": ObjectId(exam_id)})
    if not exam: raise HTTPException(status_code=404)

    uploaded_tracks = []
    for file in audio_files:
        if not file.filename.lower().endswith(('.mp3', '.wav', '.ogg')): continue
        
        safe_name = file.filename.replace(" ", "_")
        path = f"uploads/audios/{exam_id}_{folder_id}_{safe_name}"
        with open(path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        uploaded_tracks.append({
            "id": str(ObjectId()),
            "name": file.filename,
            "url": f"/{path}"
        })

    # Cập nhật danh sách tracks vào đúng folder_id
    await exam_collection.update_one(
        {"_id": ObjectId(exam_id), "audio_folders.id": folder_id},
        {"$push": {"audio_folders.$.tracks": {"$each": uploaded_tracks}}}
    )
    return {"message": "Tải nhạc thành công!"}

# 3. API Xóa 1 file Audio khỏi thư mục
@app.delete("/exams/{exam_id}/audio-folders/{folder_id}/tracks/{track_id}")
async def delete_audio_track(exam_id: str, folder_id: str, track_id: str):
    await exam_collection.update_one(
        {"_id": ObjectId(exam_id), "audio_folders.id": folder_id},
        {"$pull": {"audio_folders.$.tracks": {"id": track_id}}}
    )
    return {"message": "Đã xóa file"}