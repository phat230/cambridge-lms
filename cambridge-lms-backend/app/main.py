import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, Body, HTTPException, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext

from app.database import ping_database, database
from app.models import ExamSchema, StudentRegister, UserLogin


# ==========================================
# TẠO THƯ MỤC LƯU TRỮ FILE
# ==========================================
os.makedirs("uploads/pdfs", exist_ok=True)
os.makedirs("uploads/audios", exist_ok=True)


# ==========================================
# PASSWORD HASH
# ==========================================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ==========================================
# DATABASE COLLECTIONS
# ==========================================
exam_collection = database.get_collection("exams")
user_collection = database.get_collection("users")


# ==========================================
# HELPER: KIỂM TRA OBJECT ID
# ==========================================
def get_object_id(id_value: str) -> ObjectId:
    try:
        return ObjectId(id_value)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ID không hợp lệ"
        )


# ==========================================
# LIFESPAN
# Lưu ý:
# Đã xóa seed_teacher_account().
# Backend chỉ kiểm tra kết nối MongoDB khi khởi động.
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await ping_database()
    yield


# ==========================================
# APP CONFIG
# ==========================================
app = FastAPI(
    title="Cambridge LMS API",
    description="Hệ thống quản lý bài thi tiếng Anh",
    version="1.1.0",
    lifespan=lifespan
)


# ==========================================
# CORS CONFIG
# ==========================================
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if FRONTEND_URL == "*" else [FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================
# STATIC FILES
# ==========================================
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ==========================================
# ROOT API
# ==========================================
@app.get("/")
async def read_root():
    return {
        "status": "online",
        "message": "Cambridge LMS API đang hoạt động"
    }


# ==========================================
# AUTH API
# ==========================================
@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register_student(user: StudentRegister = Body(...)):
    existing_user = await user_collection.find_one({"email": user.email})

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email này đã được đăng ký!"
        )

    user_dict = {
        "email": user.email,
        "password": get_password_hash(user.password),
        "role": "student",
        "created_at": datetime.now(timezone.utc)
    }

    new_user = await user_collection.insert_one(user_dict)

    if new_user.inserted_id:
        return {"message": "Đăng ký thành công!"}

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Lỗi đăng ký"
    )


@app.post("/auth/login")
async def login(user_credentials: UserLogin = Body(...)):
    user = await user_collection.find_one({"email": user_credentials.email})

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sai thông tin đăng nhập"
        )

    if not verify_password(user_credentials.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sai thông tin đăng nhập"
        )

    return {
        "message": "Đăng nhập thành công",
        "user_id": str(user["_id"]),
        "email": user["email"],
        "role": user["role"]
    }


# ==========================================
# EXAMS API
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

    # Khi tạo bài thi mới, khởi tạo danh sách thư mục audio rỗng
    exam_dict["audio_folders"] = []

    new_exam = await exam_collection.insert_one(exam_dict)

    if new_exam.inserted_id:
        return {"exam_id": str(new_exam.inserted_id)}

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Không thể lưu bài thi"
    )


@app.delete("/exams/{exam_id}")
async def delete_exam(exam_id: str):
    exam_object_id = get_object_id(exam_id)

    result = await exam_collection.delete_one({"_id": exam_object_id})

    if result.deleted_count == 1:
        return {"message": "Đã xóa bài thi"}

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Không tìm thấy bài thi"
    )


# ==========================================
# UPLOAD PDF API
# ==========================================
@app.post("/exams/{exam_id}/upload-pdf/")
async def upload_exam_pdf(exam_id: str, pdf_file: UploadFile = File(...)):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi"
        )

    if not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chỉ cho phép tải file PDF"
        )

    safe_filename = pdf_file.filename.replace(" ", "_")
    pdf_path = f"uploads/pdfs/{exam_id}_{safe_filename}"

    with open(pdf_path, "wb") as buffer:
        shutil.copyfileobj(pdf_file.file, buffer)

    await exam_collection.update_one(
        {"_id": exam_object_id},
        {"$set": {"pdf_file_url": f"/{pdf_path}"}}
    )

    return {"message": "Tải PDF thành công!"}


# ==========================================
# AUDIO MANAGER API
# ==========================================

# 1. Tạo thư mục audio
@app.post("/exams/{exam_id}/audio-folders/")
async def create_audio_folder(
    exam_id: str,
    folder_name: str = Body(..., embed=True),
    limit: int = Body(0, embed=True)
):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi"
        )

    if not folder_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tên thư mục không được để trống"
        )

    if limit < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Giới hạn lượt nghe không hợp lệ"
        )

    new_folder = {
        "id": str(ObjectId()),
        "name": folder_name.strip(),
        "limit": limit,
        "tracks": []
    }

    await exam_collection.update_one(
        {"_id": exam_object_id},
        {"$push": {"audio_folders": new_folder}}
    )

    return {
        "message": "Tạo thư mục thành công",
        "folder": new_folder
    }


# 2. Tải file audio vào thư mục
@app.post("/exams/{exam_id}/audio-folders/{folder_id}/upload")
async def upload_audio_to_folder(
    exam_id: str,
    folder_id: str,
    audio_files: list[UploadFile] = File(...)
):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi"
        )

    folder_exists = any(
        folder.get("id") == folder_id
        for folder in exam.get("audio_folders", [])
    )

    if not folder_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy thư mục audio"
        )

    uploaded_tracks = []

    for file in audio_files:
        filename = file.filename or ""

        if not filename.lower().endswith((".mp3", ".wav", ".ogg")):
            continue

        safe_name = filename.replace(" ", "_")
        path = f"uploads/audios/{exam_id}_{folder_id}_{safe_name}"

        with open(path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        uploaded_tracks.append({
            "id": str(ObjectId()),
            "name": filename,
            "url": f"/{path}"
        })

    if not uploaded_tracks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Không có file audio hợp lệ. Chỉ hỗ trợ .mp3, .wav, .ogg"
        )

    await exam_collection.update_one(
        {"_id": exam_object_id, "audio_folders.id": folder_id},
        {"$push": {"audio_folders.$.tracks": {"$each": uploaded_tracks}}}
    )

    return {
        "message": "Tải nhạc thành công!",
        "tracks": uploaded_tracks
    }


# 3. Xóa một file audio khỏi thư mục
@app.delete("/exams/{exam_id}/audio-folders/{folder_id}/tracks/{track_id}")
async def delete_audio_track(exam_id: str, folder_id: str, track_id: str):
    exam_object_id = get_object_id(exam_id)

    result = await exam_collection.update_one(
        {"_id": exam_object_id, "audio_folders.id": folder_id},
        {"$pull": {"audio_folders.$.tracks": {"id": track_id}}}
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy file audio để xóa"
        )

    return {"message": "Đã xóa file"}