import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGODB_URL = os.getenv("MONGODB_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "cambridge_lms_db")

if not MONGODB_URL:
    raise RuntimeError("Thiếu biến môi trường MONGODB_URL")

client = AsyncIOMotorClient(MONGODB_URL)
database = client[DATABASE_NAME]

async def ping_database():
    try:
        await client.admin.command("ping")
        print("=== KẾT NỐI MONGODB ATLAS THÀNH CÔNG! ===")
    except Exception as e:
        print("=== LỖI KẾT NỐI MONGODB ATLAS ===")
        print(f"Chi tiết lỗi: {e}")
        raise e