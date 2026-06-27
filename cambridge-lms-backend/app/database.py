import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# Tải các biến môi trường từ file .env
load_dotenv()

MONGODB_URL = os.getenv("MONGODB_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "cambridge_lms_db")

# Khởi tạo client kết nối tới MongoDB Atlas
client = AsyncIOMotorClient(MONGODB_URL)
database = client[DATABASE_NAME]

async def ping_database():
    """Kiểm tra kết nối và xác thực với MongoDB Atlas"""
    try:
        # Gửi lệnh ping tới admin database để kiểm tra quyền truy cập
        await client.admin.command('ping')
        print("=== KẾT NỐI MONGODB ATLAS THÀNH CÔNG! ===")
    except Exception as e:
        print("=== LỖI KẾT NỐI: Vui lòng kiểm tra lại tài khoản/mật khẩu hoặc IP Whitelist ===")
        print(f"Chi tiết lỗi: {e}")