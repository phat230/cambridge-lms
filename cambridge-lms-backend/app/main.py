import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, Body, HTTPException, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext

from app.database import ping_database, database
from app.models import ExamSchema, StudentRegister, UserLogin


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
submission_collection = database.get_collection("submissions")


# ==========================================
# CLOUDFLARE R2 CONFIG
# Render Environment cần có:
# R2_ACCOUNT_ID
# R2_ACCESS_KEY_ID
# R2_SECRET_ACCESS_KEY
# R2_BUCKET_NAME
# R2_PUBLIC_URL
# ==========================================
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

R2_ENDPOINT_URL = (
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    if R2_ACCOUNT_ID else None
)

r2_client = None

if R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
    r2_client = boto3.client(
        service_name="s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def check_r2_config() -> None:
    if not r2_client or not R2_BUCKET_NAME or not R2_PUBLIC_URL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Thiếu cấu hình Cloudflare R2. Hãy kiểm tra "
                "R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
                "R2_BUCKET_NAME, R2_PUBLIC_URL."
            ),
        )


# ==========================================
# HELPERS
# ==========================================
def get_object_id(id_value: str) -> ObjectId:
    try:
        return ObjectId(id_value)
    except (InvalidId, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ID không hợp lệ",
        )


def safe_filename(filename: str) -> str:
    """Làm sạch tên file nhưng vẫn giữ phần mở rộng .pdf/.mp3/.wav/.ogg."""
    filename = filename.strip().replace(" ", "_")
    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return filename or f"file_{ObjectId()}"


def answer_input_id(section_name: str, part_name: str, q_num: str) -> str:
    raw = f"ans_{section_name}_{part_name}_{q_num}"
    return re.sub(r"\s+", "", raw)


def serialize_mongo_doc(doc: dict) -> dict:
    """Chuyển ObjectId trong document MongoDB thành string để FastAPI trả JSON."""
    if not doc:
        return doc

    for key, value in list(doc.items()):
        if isinstance(value, ObjectId):
            doc[key] = str(value)
        elif isinstance(value, list):
            doc[key] = [
                serialize_mongo_doc(item) if isinstance(item, dict) else item
                for item in value
            ]
        elif isinstance(value, dict):
            doc[key] = serialize_mongo_doc(value)

    return doc


def build_public_url(storage_key: str) -> str:
    return f"{R2_PUBLIC_URL.rstrip('/')}/{storage_key}"


async def upload_file_to_r2(file: UploadFile, storage_key: str) -> str:
    """Upload file lên Cloudflare R2 và trả về public URL."""
    check_r2_config()

    try:
        file.file.seek(0)

        extra_args = {}
        if file.content_type:
            extra_args["ContentType"] = file.content_type

        await asyncio.to_thread(
            r2_client.upload_fileobj,
            Fileobj=file.file,
            Bucket=R2_BUCKET_NAME,
            Key=storage_key,
            ExtraArgs=extra_args,
        )

        return build_public_url(storage_key)

    except ClientError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi upload R2: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi upload file: {str(e)}",
        )


async def delete_file_from_r2(storage_key: Optional[str]) -> None:
    """Xóa file khỏi R2 nếu có storage_key. Lỗi xóa chỉ log, không làm crash API."""
    if not storage_key:
        return

    try:
        check_r2_config()
        await asyncio.to_thread(
            r2_client.delete_object,
            Bucket=R2_BUCKET_NAME,
            Key=storage_key,
        )
    except Exception as e:
        print(f"Không thể xóa file R2 {storage_key}: {e}")


# ==========================================
# LIFESPAN
# Đã xóa seed_teacher_account().
# Backend chỉ kiểm tra MongoDB khi khởi động.
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
    version="1.3.0-r2",
    lifespan=lifespan,
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
# ROOT API
# ==========================================
@app.get("/")
async def read_root():
    return {
        "status": "online",
        "message": "Cambridge LMS API đang hoạt động với Cloudflare R2",
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
            detail="Email này đã được đăng ký!",
        )

    user_dict = {
        "email": user.email,
        "password": get_password_hash(user.password),
        "role": "student",
        "created_at": datetime.now(timezone.utc),
    }

    new_user = await user_collection.insert_one(user_dict)

    if new_user.inserted_id:
        return {"message": "Đăng ký thành công!"}

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Lỗi đăng ký",
    )


@app.post("/auth/login")
async def login(user_credentials: UserLogin = Body(...)):
    user = await user_collection.find_one({"email": user_credentials.email})

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sai thông tin đăng nhập",
        )

    if not verify_password(user_credentials.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sai thông tin đăng nhập",
        )

    return {
        "message": "Đăng nhập thành công",
        "user_id": str(user["_id"]),
        "email": user["email"],
        "role": user["role"],
    }


# ==========================================
# EXAMS API
# ==========================================
@app.get("/exams/")
async def get_all_exams():
    exams = []

    async for document in exam_collection.find().sort("created_at", -1):
        document = serialize_mongo_doc(document)
        exams.append(document)

    return exams


@app.post("/exams/", status_code=status.HTTP_201_CREATED)
async def create_exam(exam: ExamSchema = Body(...)):
    exam_dict = exam.model_dump()
    exam_dict["created_at"] = datetime.now(timezone.utc)
    exam_dict["audio_folders"] = []

    new_exam = await exam_collection.insert_one(exam_dict)

    if new_exam.inserted_id:
        return {"exam_id": str(new_exam.inserted_id)}

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Không thể lưu bài thi",
    )


@app.delete("/exams/{exam_id}")
async def delete_exam(exam_id: str):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    # Xóa PDF trên R2 nếu có
    await delete_file_from_r2(exam.get("pdf_storage_key"))

    # Xóa toàn bộ audio trên R2 nếu có
    for folder in exam.get("audio_folders", []):
        for track in folder.get("tracks", []):
            await delete_file_from_r2(track.get("storage_key"))

    result = await exam_collection.delete_one({"_id": exam_object_id})
    await submission_collection.delete_many({"exam_id": exam_object_id})

    if result.deleted_count == 1:
        return {"message": "Đã xóa bài thi, file R2 và kết quả liên quan"}

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Không thể xóa bài thi",
    )


# ==========================================
# PDF API - CLOUDFLARE R2
# ==========================================
@app.post("/exams/{exam_id}/upload-pdf/")
async def upload_exam_pdf(exam_id: str, pdf_file: UploadFile = File(...)):
    """
    Upload hoặc thay PDF.
    Nếu bài thi đã có PDF cũ trên R2, backend sẽ xóa file cũ sau khi upload file mới thành công.
    """
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    filename = pdf_file.filename or ""

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chỉ cho phép tải file PDF",
        )

    safe_name = safe_filename(filename)
    storage_key = f"pdfs/{exam_id}/{ObjectId()}_{safe_name}"

    pdf_url = await upload_file_to_r2(pdf_file, storage_key)

    # Upload file mới thành công rồi mới xóa file cũ để tránh mất file nếu upload lỗi
    old_storage_key = exam.get("pdf_storage_key")
    if old_storage_key and old_storage_key != storage_key:
        await delete_file_from_r2(old_storage_key)

    await exam_collection.update_one(
        {"_id": exam_object_id},
        {
            "$set": {
                "pdf_file_url": pdf_url,
                "pdf_storage_key": storage_key,
                "pdf_storage_provider": "cloudflare_r2",
            },
            "$unset": {
                # Xóa field Cloudinary cũ nếu từng dùng Cloudinary
                "pdf_public_id": "",
                "pdf_resource_type": "",
            },
        },
    )

    return {
        "message": "Tải PDF thành công!",
        "pdf_file_url": pdf_url,
    }


@app.delete("/exams/{exam_id}/pdf")
async def delete_exam_pdf(exam_id: str):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    await delete_file_from_r2(exam.get("pdf_storage_key"))

    await exam_collection.update_one(
        {"_id": exam_object_id},
        {
            "$unset": {
                "pdf_file_url": "",
                "pdf_storage_key": "",
                "pdf_storage_provider": "",
                "pdf_public_id": "",
                "pdf_resource_type": "",
            }
        },
    )

    return {"message": "Đã xóa PDF khỏi bài thi và Cloudflare R2"}


# ==========================================
# AUDIO MANAGER API - CLOUDFLARE R2
# ==========================================
@app.post("/exams/{exam_id}/audio-folders/")
async def create_audio_folder(
    exam_id: str,
    folder_name: str = Body(..., embed=True),
    limit: int = Body(0, embed=True),
):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    if not folder_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tên thư mục không được để trống",
        )

    if limit < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Giới hạn lượt nghe không hợp lệ",
        )

    new_folder = {
        "id": str(ObjectId()),
        "name": folder_name.strip(),
        "limit": limit,
        "tracks": [],
    }

    await exam_collection.update_one(
        {"_id": exam_object_id},
        {"$push": {"audio_folders": new_folder}},
    )

    return {
        "message": "Tạo thư mục thành công",
        "folder": new_folder,
    }


@app.delete("/exams/{exam_id}/audio-folders/{folder_id}")
async def delete_audio_folder(exam_id: str, folder_id: str):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    target_folder = None
    for folder in exam.get("audio_folders", []):
        if folder.get("id") == folder_id:
            target_folder = folder
            break

    if not target_folder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy thư mục audio",
        )

    for track in target_folder.get("tracks", []):
        await delete_file_from_r2(track.get("storage_key"))

    result = await exam_collection.update_one(
        {"_id": exam_object_id},
        {"$pull": {"audio_folders": {"id": folder_id}}},
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Không thể xóa thư mục audio",
        )

    return {"message": "Đã xóa thư mục audio và toàn bộ file trên Cloudflare R2"}


@app.post("/exams/{exam_id}/audio-folders/{folder_id}/upload")
async def upload_audio_to_folder(
    exam_id: str,
    folder_id: str,
    audio_files: list[UploadFile] = File(...),
):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    folder_exists = any(
        folder.get("id") == folder_id
        for folder in exam.get("audio_folders", [])
    )

    if not folder_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy thư mục audio",
        )

    uploaded_tracks = []

    for file in audio_files:
        filename = file.filename or ""

        if not filename.lower().endswith((".mp3", ".wav", ".ogg")):
            continue

        track_id = str(ObjectId())
        safe_name = safe_filename(filename)
        storage_key = f"audios/{exam_id}/{folder_id}/{track_id}_{safe_name}"

        audio_url = await upload_file_to_r2(file, storage_key)

        uploaded_tracks.append(
            {
                "id": track_id,
                "name": filename,
                "url": audio_url,
                "storage_key": storage_key,
                "storage_provider": "cloudflare_r2",
            }
        )

    if not uploaded_tracks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Không có file audio hợp lệ. Chỉ hỗ trợ .mp3, .wav, .ogg",
        )

    await exam_collection.update_one(
        {"_id": exam_object_id, "audio_folders.id": folder_id},
        {"$push": {"audio_folders.$.tracks": {"$each": uploaded_tracks}}},
    )

    return {
        "message": "Tải nhạc thành công!",
        "tracks": uploaded_tracks,
    }


@app.put("/exams/{exam_id}/audio-folders/{folder_id}/tracks/{track_id}/replace")
async def replace_audio_track(
    exam_id: str,
    folder_id: str,
    track_id: str,
    audio_file: UploadFile = File(...),
):
    """
    Thay file audio.
    Backend upload audio mới lên R2, sau đó xóa audio cũ để tiết kiệm dung lượng.
    """
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    target_track = None

    for folder in exam.get("audio_folders", []):
        if folder.get("id") == folder_id:
            for track in folder.get("tracks", []):
                if track.get("id") == track_id:
                    target_track = track
                    break
            break

    if not target_track:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy file audio",
        )

    filename = audio_file.filename or ""

    if not filename.lower().endswith((".mp3", ".wav", ".ogg")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chỉ hỗ trợ .mp3, .wav, .ogg",
        )

    safe_name = safe_filename(filename)
    storage_key = f"audios/{exam_id}/{folder_id}/{track_id}_{safe_name}"

    audio_url = await upload_file_to_r2(audio_file, storage_key)

    old_storage_key = target_track.get("storage_key")
    if old_storage_key and old_storage_key != storage_key:
        await delete_file_from_r2(old_storage_key)

    new_track = {
        "id": track_id,
        "name": filename,
        "url": audio_url,
        "storage_key": storage_key,
        "storage_provider": "cloudflare_r2",
    }

    await exam_collection.update_one(
        {"_id": exam_object_id, "audio_folders.id": folder_id},
        {"$set": {"audio_folders.$.tracks.$[track]": new_track}},
        array_filters=[{"track.id": track_id}],
    )

    return {
        "message": "Đã thay audio thành công",
        "track": new_track,
    }


@app.delete("/exams/{exam_id}/audio-folders/{folder_id}/tracks/{track_id}")
async def delete_audio_track(exam_id: str, folder_id: str, track_id: str):
    exam_object_id = get_object_id(exam_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    target_track = None

    for folder in exam.get("audio_folders", []):
        if folder.get("id") == folder_id:
            for track in folder.get("tracks", []):
                if track.get("id") == track_id:
                    target_track = track
                    break
            break

    if not target_track:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy file audio để xóa",
        )

    await delete_file_from_r2(target_track.get("storage_key"))

    result = await exam_collection.update_one(
        {"_id": exam_object_id, "audio_folders.id": folder_id},
        {"$pull": {"audio_folders.$.tracks": {"id": track_id}}},
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy file audio để xóa",
        )

    return {"message": "Đã xóa file audio khỏi bài thi và Cloudflare R2"}


# ==========================================
# SUBMISSION API
# ==========================================
@app.get("/exams/{exam_id}/my-submission/{user_id}")
async def get_my_submission(exam_id: str, user_id: str):
    exam_object_id = get_object_id(exam_id)
    user_object_id = get_object_id(user_id)

    submission = await submission_collection.find_one(
        {
            "exam_id": exam_object_id,
            "user_id": user_object_id,
        }
    )

    if not submission:
        return {"submitted": False}

    submission = serialize_mongo_doc(submission)

    return {
        "submitted": True,
        "submission": submission,
    }


@app.post("/exams/{exam_id}/submit")
async def submit_exam(exam_id: str, payload: dict = Body(...)):
    exam_object_id = get_object_id(exam_id)

    user_id = payload.get("user_id")
    student_email = payload.get("student_email")
    answers = payload.get("answers", {})
    time_spent_seconds = int(payload.get("time_spent_seconds", 0))

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Thiếu user_id",
        )

    user_object_id = get_object_id(user_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy bài thi",
        )

    existed_submission = await submission_collection.find_one(
        {
            "exam_id": exam_object_id,
            "user_id": user_object_id,
        }
    )

    if existed_submission:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bạn đã làm bài thi này rồi. Mỗi học sinh chỉ được làm 1 lần.",
        )

    answer_key = exam.get("answer_key")
    if not answer_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bài thi chưa có đáp án",
        )

    total_score = 0.0
    correct_count = 0
    total_questions = 0
    detail_rows = []

    for section_name, parts in answer_key.items():
        for part_name, part_data in parts.items():
            num_questions = int(part_data.get("num_questions", 0))
            part_score = float(part_data.get("part_score", 0))
            points_per_question = part_score / num_questions if num_questions > 0 else 0

            for question in part_data.get("questions", []):
                total_questions += 1

                q_num = str(question.get("qNum"))
                correct_answer = str(question.get("answer", "")).strip().lower()

                input_id = answer_input_id(section_name, part_name, q_num)
                student_answer = str(answers.get(input_id, "")).strip().lower()

                is_correct = student_answer != "" and student_answer == correct_answer
                earned_score = points_per_question if is_correct else 0

                if is_correct:
                    correct_count += 1
                    total_score += earned_score

                detail_rows.append(
                    {
                        "section": section_name,
                        "part": part_name,
                        "question": q_num,
                        "student_answer": student_answer,
                        "correct_answer": correct_answer,
                        "is_correct": is_correct,
                        "earned_score": round(earned_score, 2),
                    }
                )

    submission_doc = {
        "exam_id": exam_object_id,
        "exam_title": exam.get("title"),
        "exam_level": exam.get("level"),
        "user_id": user_object_id,
        "student_email": student_email,
        "answers": answers,
        "details": detail_rows,
        "score": round(total_score, 2),
        "correct_count": correct_count,
        "total_questions": total_questions,
        "time_spent_seconds": time_spent_seconds,
        "submitted_at": datetime.now(timezone.utc),
    }

    result = await submission_collection.insert_one(submission_doc)

    return {
        "message": "Nộp bài thành công",
        "submission_id": str(result.inserted_id),
        "score": round(total_score, 2),
        "correct_count": correct_count,
        "total_questions": total_questions,
        "time_spent_seconds": time_spent_seconds,
        "details": detail_rows,
    }


@app.get("/teacher/submissions")
async def get_teacher_submissions(
    level: Optional[str] = None,
    exam_id: Optional[str] = None,
):
    query = {}

    if level and level != "all":
        query["exam_level"] = level

    if exam_id and exam_id != "all":
        query["exam_id"] = get_object_id(exam_id)

    cursor = submission_collection.find(query).sort(
        [
            ("correct_count", -1),
            ("time_spent_seconds", 1),
            ("submitted_at", 1),
        ]
    )

    submissions = []
    rank = 1

    async for doc in cursor:
        doc = serialize_mongo_doc(doc)
        doc["rank"] = rank
        submissions.append(doc)
        rank += 1

    return submissions
