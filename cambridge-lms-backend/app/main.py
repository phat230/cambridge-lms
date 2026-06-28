import io
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import boto3
import fitz  # PyMuPDF
from botocore.config import Config
from botocore.exceptions import ClientError
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, Body, HTTPException, status, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from PIL import Image

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

# Trạng thái xử lý PDF tách trang.
# Lưu trong RAM để frontend có thể hỏi tiến trình.
# Nếu backend restart thì job đang chạy sẽ mất, lúc đó giáo viên upload lại PDF.
pdf_processing_jobs = {}



# ==========================================
# CLOUDFLARE R2 CONFIG
# ==========================================
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")
R2_ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else None

r2_client = None
if R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
    r2_client = boto3.client(
        service_name="s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def check_r2_config() -> None:
    if not r2_client or not R2_BUCKET_NAME or not R2_PUBLIC_URL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Thiếu cấu hình Cloudflare R2. Hãy kiểm tra R2_ACCOUNT_ID, "
                "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL."
            ),
        )


# ==========================================
# HELPERS
# ==========================================
def get_object_id(id_value: str) -> ObjectId:
    try:
        return ObjectId(id_value)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ID không hợp lệ")


def safe_filename(filename: str) -> str:
    filename = (filename or "file").strip().replace(" ", "_")
    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return filename or "file"


def serialize_mongo_doc(doc: dict) -> dict:
    if not doc:
        return doc
    for key, value in list(doc.items()):
        if isinstance(value, ObjectId):
            doc[key] = str(value)
        elif isinstance(value, datetime):
            doc[key] = value.isoformat()
        elif isinstance(value, list):
            doc[key] = [serialize_mongo_doc(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            doc[key] = serialize_mongo_doc(value)
    return doc


def public_url_for_key(key: str) -> str:
    return f"{R2_PUBLIC_URL.rstrip('/')}/{quote(key, safe='/')}"


def normalize_answer(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def split_correct_answers(answer: str) -> list[str]:
    raw = str(answer or "")
    # Giáo viên có thể nhập nhiều đáp án đúng bằng dấu | hoặc /
    parts = re.split(r"\s*(?:\||/)\s*", raw)
    return [normalize_answer(p) for p in parts if normalize_answer(p)]


def all_page_storage_keys(exam: dict) -> list[str]:
    keys = []
    if exam.get("book_pdf_storage_key"):
        keys.append(exam["book_pdf_storage_key"])
    for page in exam.get("pages", []):
        if page.get("storage_key"):
            keys.append(page["storage_key"])
        for audio in page.get("audio_tracks", []):
            if audio.get("storage_key"):
                keys.append(audio["storage_key"])
    return keys


async def delete_file_from_r2(key: Optional[str]) -> None:
    if not key:
        return
    check_r2_config()
    try:
        r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
    except Exception as e:
        print(f"Không thể xóa file R2 {key}: {e}")


async def upload_bytes_to_r2(data: bytes, key: str, content_type: str, content_disposition: str = "inline") -> str:
    check_r2_config()
    try:
        r2_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            Body=data,
            ContentType=content_type,
            ContentDisposition=content_disposition,
        )
        return public_url_for_key(key)
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"Lỗi upload R2: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi upload file: {str(e)}")


async def upload_file_to_r2(file: UploadFile, key: str) -> str:
    check_r2_config()
    try:
        file.file.seek(0)
        lower = key.lower()
        if lower.endswith(".pdf"):
            content_type = "application/pdf"
        elif lower.endswith(".mp3"):
            content_type = "audio/mpeg"
        elif lower.endswith(".wav"):
            content_type = "audio/wav"
        elif lower.endswith(".ogg"):
            content_type = "audio/ogg"
        else:
            content_type = file.content_type or "application/octet-stream"

        r2_client.upload_fileobj(
            Fileobj=file.file,
            Bucket=R2_BUCKET_NAME,
            Key=key,
            ExtraArgs={"ContentType": content_type, "ContentDisposition": "inline"},
        )
        return public_url_for_key(key)
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"Lỗi upload R2: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi upload file: {str(e)}")




async def upload_path_to_r2(file_path: str, key: str, content_type: str, content_disposition: str = "inline") -> str:
    check_r2_config()
    try:
        with open(file_path, "rb") as f:
            r2_client.upload_fileobj(
                Fileobj=f,
                Bucket=R2_BUCKET_NAME,
                Key=key,
                ExtraArgs={
                    "ContentType": content_type,
                    "ContentDisposition": content_disposition,
                },
            )
        return public_url_for_key(key)
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"Lỗi upload R2: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi upload file: {str(e)}")


async def render_pdf_pages_to_r2(pdf_path: str, exam_id: str, batch_id: str, job_id: str, zoom: float = 1.15, quality: int = 78) -> list[dict]:
    """
    Tách PDF thành ảnh từng trang và upload từng ảnh ngay lên R2.
    Không giữ toàn bộ ảnh trong RAM để tránh crash khi PDF lớn.
    """
    pages = []
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)
        pdf_processing_jobs[job_id]["total_pages"] = total_pages

        for index, page in enumerate(doc, start=1):
            pdf_processing_jobs[job_id]["stage"] = "rendering"
            pdf_processing_jobs[job_id]["current_page"] = index
            pdf_processing_jobs[job_id]["message"] = f"Đang tách trang {index}/{total_pages}..."

            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            image_bytes = buffer.getvalue()

            page_id = str(ObjectId())
            storage_key = f"page-images/{exam_id}/{batch_id}/page_{index:03d}.jpg"

            image_url = await upload_bytes_to_r2(image_bytes, storage_key, "image/jpeg")

            pages.append({
                "id": page_id,
                "page_number": index,
                "image_url": image_url,
                "storage_key": storage_key,
                "is_active": True,
                "questions": [],
                "audio_tracks": [],
            })

            pdf_processing_jobs[job_id]["uploaded_pages"] = index

    return pages


async def process_book_pdf_job(job_id: str, exam_id: str, tmp_path: str, filename: str):
    """Xử lý PDF ở nền để tránh request bị timeout gây Failed to fetch."""
    try:
        exam_object_id = get_object_id(exam_id)
        exam = await exam_collection.find_one({"_id": exam_object_id})
        if not exam:
            raise Exception("Không tìm thấy bài thi")

        old_keys = all_page_storage_keys(exam)
        safe_name = safe_filename(filename)
        batch_id = job_id
        original_pdf_key = f"books/{exam_id}/{batch_id}_{safe_name}"

        pdf_processing_jobs[job_id].update({
            "status": "processing",
            "stage": "uploading_pdf",
            "message": "Đang upload PDF gốc lên R2...",
        })

        book_pdf_url = await upload_path_to_r2(tmp_path, original_pdf_key, "application/pdf")

        pdf_processing_jobs[job_id].update({
            "stage": "rendering",
            "message": "Đang tách PDF thành từng ảnh trang...",
        })

        pages = await render_pdf_pages_to_r2(tmp_path, exam_id, batch_id, job_id)

        pdf_processing_jobs[job_id].update({
            "stage": "saving",
            "message": "Đang lưu danh sách trang vào MongoDB...",
        })

        await exam_collection.update_one(
            {"_id": exam_object_id},
            {"$set": {
                "book_pdf_url": book_pdf_url,
                "book_pdf_storage_key": original_pdf_key,
                "book_pdf_name": filename,
                "pages": pages,
                "updated_at": datetime.now(timezone.utc),
            }},
        )

        for key in old_keys:
            await delete_file_from_r2(key)

        pdf_processing_jobs[job_id].update({
            "status": "done",
            "stage": "done",
            "message": f"Đã upload PDF và tách thành {len(pages)} trang.",
            "page_count": len(pages),
            "done_at": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        pdf_processing_jobs[job_id].update({
            "status": "error",
            "stage": "error",
            "message": f"Lỗi tách PDF: {str(e)}",
            "error": str(e),
        })
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def render_pdf_pages_to_jpegs(pdf_path: str, exam_id: str, zoom: float = 1.35, quality: int = 82) -> list[dict]:
    pages = []
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            image_bytes = buffer.getvalue()

            page_id = str(ObjectId())
            storage_key = f"page-images/{exam_id}/page_{index:03d}.jpg"
            pages.append({
                "id": page_id,
                "page_number": index,
                "image_bytes": image_bytes,
                "storage_key": storage_key,
            })
    return pages


def find_page(exam: dict, page_id: str) -> Optional[dict]:
    for page in exam.get("pages", []):
        if page.get("id") == page_id:
            return page
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ping_database()
    yield


app = FastAPI(
    title="Cambridge LMS API",
    description="Hệ thống quản lý bài thi tiếng Anh dạng lật trang",
    version="2.0.0",
    lifespan=lifespan,
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if FRONTEND_URL == "*" else [FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def read_root():
    return {"status": "online", "message": "Cambridge LMS API đang hoạt động"}


# ==========================================
# AUTH API
# ==========================================
@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register_student(user: StudentRegister = Body(...)):
    existing_user = await user_collection.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email này đã được đăng ký!")

    user_dict = {
        "email": user.email,
        "password": get_password_hash(user.password),
        "role": "student",
        "created_at": datetime.now(timezone.utc),
    }
    result = await user_collection.insert_one(user_dict)
    if result.inserted_id:
        return {"message": "Đăng ký thành công!"}
    raise HTTPException(status_code=500, detail="Lỗi đăng ký")


@app.post("/auth/login")
async def login(user_credentials: UserLogin = Body(...)):
    user = await user_collection.find_one({"email": user_credentials.email})
    if not user or not verify_password(user_credentials.password, user["password"]):
        raise HTTPException(status_code=401, detail="Sai thông tin đăng nhập")

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
    async for doc in exam_collection.find().sort("created_at", -1):
        doc = serialize_mongo_doc(doc)
        exams.append(doc)
    return exams


@app.get("/exams/{exam_id}")
async def get_exam(exam_id: str):
    exam = await exam_collection.find_one({"_id": get_object_id(exam_id)})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")
    return serialize_mongo_doc(exam)


@app.post("/exams/", status_code=status.HTTP_201_CREATED)
async def create_exam(exam: ExamSchema = Body(...)):
    exam_dict = exam.model_dump()
    exam_dict["created_at"] = datetime.now(timezone.utc)
    exam_dict["pages"] = []
    exam_dict["version"] = "flip_pages"
    result = await exam_collection.insert_one(exam_dict)
    if result.inserted_id:
        return {"exam_id": str(result.inserted_id)}
    raise HTTPException(status_code=500, detail="Không thể lưu bài thi")


@app.delete("/exams/{exam_id}")
async def delete_exam(exam_id: str):
    exam_object_id = get_object_id(exam_id)
    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")

    for key in all_page_storage_keys(exam):
        await delete_file_from_r2(key)

    result = await exam_collection.delete_one({"_id": exam_object_id})
    await submission_collection.delete_many({"exam_id": exam_object_id})

    if result.deleted_count == 1:
        return {"message": "Đã xóa bài thi, trang, audio và kết quả liên quan"}
    raise HTTPException(status_code=500, detail="Không thể xóa bài thi")


# ==========================================
# BOOK PDF → PAGE IMAGES
# ==========================================
@app.post("/exams/{exam_id}/upload-book-pdf/", status_code=status.HTTP_202_ACCEPTED)
async def upload_book_pdf(
    exam_id: str,
    background_tasks: BackgroundTasks,
    pdf_file: UploadFile = File(...)
):
    """
    Nhận PDF, lưu tạm, trả response ngay, rồi tách trang ở background.
    Cách này tránh lỗi trình duyệt báo Failed to fetch khi file lớn xử lý quá lâu.
    """
    exam_object_id = get_object_id(exam_id)
    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")

    filename = pdf_file.filename or "book.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Chỉ cho phép tải file PDF")

    check_r2_config()

    job_id = str(ObjectId())
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = tmp.name
            pdf_file.file.seek(0)
            while True:
                chunk = pdf_file.file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)

        pdf_processing_jobs[job_id] = {
            "job_id": job_id,
            "exam_id": exam_id,
            "filename": filename,
            "status": "queued",
            "stage": "queued",
            "message": "Đã nhận PDF. Đang chuẩn bị tách trang...",
            "current_page": 0,
            "uploaded_pages": 0,
            "total_pages": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        background_tasks.add_task(process_book_pdf_job, job_id, exam_id, tmp_path, filename)

        return {
            "message": "Đã nhận PDF. Hệ thống đang tách trang ở nền.",
            "job_id": job_id,
            "status": "queued",
        }

    except Exception as e:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Lỗi nhận PDF: {str(e)}")


@app.get("/jobs/{job_id}")
async def get_processing_job(job_id: str):
    job = pdf_processing_jobs.get(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail="Không tìm thấy tiến trình. Nếu backend vừa redeploy/restart, hãy upload lại PDF."
        )
    return job


@app.get("/exams/{exam_id}/pages")
async def get_exam_pages(exam_id: str):
    exam = await exam_collection.find_one({"_id": get_object_id(exam_id)})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")
    return serialize_mongo_doc({"pages": exam.get("pages", [])})


@app.patch("/exams/{exam_id}/pages/{page_id}/toggle")
async def toggle_page(exam_id: str, page_id: str, payload: dict = Body(...)):
    exam_object_id = get_object_id(exam_id)
    is_active = bool(payload.get("is_active", True))

    result = await exam_collection.update_one(
        {"_id": exam_object_id, "pages.id": page_id},
        {"$set": {"pages.$.is_active": is_active, "updated_at": datetime.now(timezone.utc)}},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Không tìm thấy trang")
    return {"message": "Đã cập nhật trạng thái trang", "is_active": is_active}


@app.delete("/exams/{exam_id}/pages/{page_id}")
async def delete_page(exam_id: str, page_id: str):
    exam_object_id = get_object_id(exam_id)
    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")

    page = find_page(exam, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy trang")

    await delete_file_from_r2(page.get("storage_key"))
    for audio in page.get("audio_tracks", []):
        await delete_file_from_r2(audio.get("storage_key"))

    result = await exam_collection.update_one(
        {"_id": exam_object_id},
        {"$pull": {"pages": {"id": page_id}}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Không thể xóa trang")
    return {"message": "Đã xóa trang và file trên R2"}


# ==========================================
# PAGE QUESTIONS
# ==========================================
@app.post("/exams/{exam_id}/pages/{page_id}/questions")
async def add_page_question(exam_id: str, page_id: str, payload: dict = Body(...)):
    exam_object_id = get_object_id(exam_id)
    number = str(payload.get("number", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    score = float(payload.get("score", 1) or 1)
    prompt = str(payload.get("prompt", "")).strip()

    if not number:
        raise HTTPException(status_code=400, detail="Thiếu số câu")
    if not answer:
        raise HTTPException(status_code=400, detail="Thiếu đáp án đúng")
    if score <= 0:
        raise HTTPException(status_code=400, detail="Điểm phải lớn hơn 0")

    question = {"id": str(ObjectId()), "number": number, "answer": answer, "score": score, "prompt": prompt}

    result = await exam_collection.update_one(
        {"_id": exam_object_id, "pages.id": page_id},
        {"$push": {"pages.$.questions": question}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Không tìm thấy trang")
    return {"message": "Đã thêm câu hỏi", "question": question}


@app.put("/exams/{exam_id}/pages/{page_id}/questions/{question_id}")
async def update_page_question(exam_id: str, page_id: str, question_id: str, payload: dict = Body(...)):
    exam_object_id = get_object_id(exam_id)
    number = str(payload.get("number", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    score = float(payload.get("score", 1) or 1)
    prompt = str(payload.get("prompt", "")).strip()

    if not number or not answer or score <= 0:
        raise HTTPException(status_code=400, detail="Câu hỏi không hợp lệ")

    new_question = {"id": question_id, "number": number, "answer": answer, "score": score, "prompt": prompt}

    result = await exam_collection.update_one(
        {"_id": exam_object_id, "pages.id": page_id},
        {"$set": {"pages.$.questions.$[q]": new_question, "updated_at": datetime.now(timezone.utc)}},
        array_filters=[{"q.id": question_id}],
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return {"message": "Đã cập nhật câu hỏi", "question": new_question}


@app.delete("/exams/{exam_id}/pages/{page_id}/questions/{question_id}")
async def delete_page_question(exam_id: str, page_id: str, question_id: str):
    result = await exam_collection.update_one(
        {"_id": get_object_id(exam_id), "pages.id": page_id},
        {"$pull": {"pages.$.questions": {"id": question_id}}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return {"message": "Đã xóa câu hỏi"}


# ==========================================
# PAGE AUDIO
# ==========================================
@app.post("/exams/{exam_id}/pages/{page_id}/audio")
async def upload_page_audio(
    exam_id: str,
    page_id: str,
    audio_file: UploadFile = File(...),
    limit: int = Form(0),
):
    exam_object_id = get_object_id(exam_id)
    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")

    page = find_page(exam, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy trang")

    filename = audio_file.filename or "audio.mp3"
    if not filename.lower().endswith((".mp3", ".wav", ".ogg")):
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ .mp3, .wav, .ogg")
    if limit < 0:
        raise HTTPException(status_code=400, detail="Giới hạn lượt nghe không hợp lệ")

    audio_id = str(ObjectId())
    key = f"page-audios/{exam_id}/{page_id}/{audio_id}_{safe_filename(filename)}"
    audio_url = await upload_file_to_r2(audio_file, key)
    audio = {"id": audio_id, "name": filename, "url": audio_url, "storage_key": key, "limit": limit}

    result = await exam_collection.update_one(
        {"_id": exam_object_id, "pages.id": page_id},
        {"$push": {"pages.$.audio_tracks": audio}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
    if result.modified_count == 0:
        await delete_file_from_r2(key)
        raise HTTPException(status_code=500, detail="Không thể lưu audio")
    return {"message": "Đã upload audio cho trang", "audio": audio}


@app.delete("/exams/{exam_id}/pages/{page_id}/audio/{audio_id}")
async def delete_page_audio(exam_id: str, page_id: str, audio_id: str):
    exam_object_id = get_object_id(exam_id)
    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")

    page = find_page(exam, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy trang")

    audio = None
    for item in page.get("audio_tracks", []):
        if item.get("id") == audio_id:
            audio = item
            break
    if not audio:
        raise HTTPException(status_code=404, detail="Không tìm thấy audio")

    await delete_file_from_r2(audio.get("storage_key"))

    result = await exam_collection.update_one(
        {"_id": exam_object_id, "pages.id": page_id},
        {"$pull": {"pages.$.audio_tracks": {"id": audio_id}}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Không thể xóa audio")
    return {"message": "Đã xóa audio"}


# ==========================================
# STUDENT VIEW + SUBMISSION
# ==========================================
@app.get("/exams/{exam_id}/student-view")
async def get_student_exam_view(exam_id: str):
    exam = await exam_collection.find_one({"_id": get_object_id(exam_id)})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")

    pages = []
    total_questions = 0
    total_score = 0.0

    for page in sorted(exam.get("pages", []), key=lambda p: p.get("page_number", 0)):
        if not page.get("is_active", True):
            continue

        safe_questions = []
        for q in page.get("questions", []):
            safe_questions.append({
                "id": q.get("id"),
                "number": q.get("number"),
                "score": q.get("score", 1),
                "prompt": q.get("prompt", ""),
            })
            total_questions += 1
            total_score += float(q.get("score", 1) or 1)

        pages.append({
            "id": page.get("id"),
            "page_number": page.get("page_number"),
            "image_url": page.get("image_url"),
            "questions": safe_questions,
            "audio_tracks": page.get("audio_tracks", []),
        })

    return {
        "_id": str(exam["_id"]),
        "title": exam.get("title"),
        "level": exam.get("level"),
        "duration_minutes": exam.get("duration_minutes"),
        "total_score": round(total_score, 2) if total_score else exam.get("total_score", 0),
        "total_questions": total_questions,
        "pages": pages,
    }


@app.get("/exams/{exam_id}/my-submission/{user_id}")
async def get_my_submission(exam_id: str, user_id: str):
    submission = await submission_collection.find_one({
        "exam_id": get_object_id(exam_id),
        "user_id": get_object_id(user_id),
    })
    if not submission:
        return {"submitted": False}
    return {"submitted": True, "submission": serialize_mongo_doc(submission)}


@app.post("/exams/{exam_id}/submit")
async def submit_exam(exam_id: str, payload: dict = Body(...)):
    exam_object_id = get_object_id(exam_id)
    user_id = payload.get("user_id")
    student_email = payload.get("student_email")
    answers = payload.get("answers", {})
    time_spent_seconds = int(payload.get("time_spent_seconds", 0) or 0)

    if not user_id:
        raise HTTPException(status_code=400, detail="Thiếu user_id")
    user_object_id = get_object_id(user_id)

    exam = await exam_collection.find_one({"_id": exam_object_id})
    if not exam:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài thi")

    existed = await submission_collection.find_one({"exam_id": exam_object_id, "user_id": user_object_id})
    if existed:
        raise HTTPException(status_code=400, detail="Bạn đã làm bài thi này rồi. Mỗi học sinh chỉ được làm 1 lần.")

    detail_rows = []
    total_score = 0.0
    correct_count = 0
    total_questions = 0

    for page in sorted(exam.get("pages", []), key=lambda p: p.get("page_number", 0)):
        if not page.get("is_active", True):
            continue
        for q in page.get("questions", []):
            total_questions += 1
            qid = q.get("id")
            student_answer = normalize_answer(answers.get(qid, ""))
            correct_options = split_correct_answers(q.get("answer", ""))
            is_correct = bool(student_answer) and student_answer in correct_options
            score = float(q.get("score", 1) or 1)
            earned = score if is_correct else 0.0
            if is_correct:
                correct_count += 1
                total_score += earned

            detail_rows.append({
                "page_id": page.get("id"),
                "page_number": page.get("page_number"),
                "question_id": qid,
                "question_number": q.get("number"),
                "student_answer": student_answer,
                "correct_answer": q.get("answer", ""),
                "is_correct": is_correct,
                "earned_score": round(earned, 2),
                "max_score": score,
            })

    if total_questions == 0:
        raise HTTPException(status_code=400, detail="Bài thi chưa có câu hỏi")

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
async def get_teacher_submissions(level: Optional[str] = None, exam_id: Optional[str] = None):
    query = {}
    if level and level != "all":
        query["exam_level"] = level
    if exam_id and exam_id != "all":
        query["exam_id"] = get_object_id(exam_id)

    cursor = submission_collection.find(query).sort([
        ("correct_count", -1),
        ("score", -1),
        ("time_spent_seconds", 1),
        ("submitted_at", 1),
    ])

    submissions = []
    rank = 1
    async for doc in cursor:
        doc = serialize_mongo_doc(doc)
        doc["rank"] = rank
        submissions.append(doc)
        rank += 1
    return submissions
