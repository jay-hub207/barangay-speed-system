from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import datetime
import uvicorn
import cv2
import json
import os
import time

app = FastAPI(title="Barangay Overspeeding Detection & Warning System")

# -------------------------------------------------------------------
# CORS MIDDLEWARE
# -------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------------------
# BASE DIRECTORIES & CONFIG FILE PATHS
# -------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
EVIDENCE_DIR = os.path.join(PUBLIC_DIR, "evidence")
DB_FILE = os.path.join(PUBLIC_DIR, "violations.json")
CONFIG_FILE = os.path.join(PUBLIC_DIR, "admin_config.json")

os.makedirs(EVIDENCE_DIR, exist_ok=True)
os.makedirs(PUBLIC_DIR, exist_ok=True)

# -------------------------------------------------------------------
# GLOBAL SYSTEM STATES & SINGLE ADMIN SECURITY LOCK
# -------------------------------------------------------------------
current_speed_limit = 30.0          # Default Speed Limit (30 km/h)
active_admin_session = False        # Single Admin Session Lock Flag
last_admin_activity = 0             # Timestamp para sa Idle Timeout
SESSION_TIMEOUT_SECONDS = 300       # 5 Minuto (300 seconds) Auto-Logout

# Helper: Check at i-clear ang session kapag lumagpas sa 5 minuto ang pagiging idle
def check_session_timeout():
    global active_admin_session, last_admin_activity
    if active_admin_session:
        if time.time() - last_admin_activity > SESSION_TIMEOUT_SECONDS:
            active_admin_session = False
            last_admin_activity = 0
            print("⏳ SESSION EXPIRED: Awtomatikong na-logout ang Admin matapos ang 5 minutong idle.")

# EasyOCR Engine Initialization
print("⏳ Initializing EasyOCR License Plate Reader...")
try:
    import easyocr
    reader = easyocr.Reader(['en'], gpu=False)
    print("✅ License Plate Reader Ready!")
except Exception as e:
    print("⚠️ Warning: EasyOCR Engine failed to initialize, fallback enabled.", e)
    reader = None

# Helper functions for Admin Configuration (JSON Storage)
def load_admin_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "is_configured": False,
        "admin_pin": "1234",
        "security_question": "Ano ang pangalan ng unang alagang hayop mo?",
        "security_answer": "bantay"
    }

def save_admin_config(pin, question, answer):
    config_data = {
        "is_configured": True,
        "admin_pin": str(pin).strip(),
        "security_question": str(question).strip(),
        "security_answer": str(answer).strip().lower()
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=2)
    return config_data

# Helper functions for Violations DB
def load_saved_violations():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_violation_to_db(violation_data):
    logs = load_saved_violations()
    logs.insert(0, violation_data)
    with open(DB_FILE, "w") as f:
        json.dump(logs, f, indent=2)

# -------------------------------------------------------------------
# WEBSOCKET MANAGER FOR REAL-TIME DASHBOARD UPDATES
# -------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# -------------------------------------------------------------------
# PYDANTIC DATA MODELS
# -------------------------------------------------------------------
class SetupPayload(BaseModel):
    pin: str
    question: str
    answer: str

class PinVerification(BaseModel):
    pin: str

class PinResetPayload(BaseModel):
    answer: str
    new_pin: str

class DirectPinChangePayload(BaseModel):
    old_pin: str
    new_pin: str

class SpeedLimitUpdate(BaseModel):
    speed_limit: float

class ViolationPayload(BaseModel):
    device_id: str = "ROAD_NODE_01"
    vehicle_id: str = "UNKNOWN"
    detected_speed: float
    speed_limit: float = 30.0

# -------------------------------------------------------------------
# CAMERA CAPTURE & OPTICAL CHARACTER RECOGNITION (OCR)
# -------------------------------------------------------------------
def capture_and_read_plate():
    try:
        cap = cv2.VideoCapture(0)
        ret, frame = cap.read()
        cap.release()

        if not ret:
            return "UNREADABLE_PLATE", "#"

        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"violation_{now_str}.jpg"
        filepath = os.path.join(EVIDENCE_DIR, filename)

        cv2.imwrite(filepath, frame)

        if reader:
            results = reader.readtext(frame)
            detected_text = [text.upper() for (bbox, text, prob) in results if prob > 0.35]
            plate_number = " ".join(detected_text) if detected_text else "UNREADABLE_PLATE"
        else:
            plate_number = "ABC 1234"

        return plate_number, f"/evidence/{filename}"
    except Exception as e:
        print("Camera/OCR Exception:", e)
        return "ABC 1234", "#"

# -------------------------------------------------------------------
# PWA DIRECT ENDPOINTS
# -------------------------------------------------------------------
@app.get("/manifest.json")
async def get_manifest():
    manifest_path = os.path.join(PUBLIC_DIR, "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path, media_type="application/json")
    raise HTTPException(status_code=404, detail="manifest.json file not found")

@app.get("/sw.js")
async def get_service_worker():
    sw_path = os.path.join(PUBLIC_DIR, "sw.js")
    if os.path.exists(sw_path):
        return FileResponse(sw_path, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="sw.js file not found")

# -------------------------------------------------------------------
# FASTAPI API ENDPOINTS (AUTHENTICATION & SINGLE SESSION LOCK)
# -------------------------------------------------------------------
@app.get("/api/config-status")
async def check_config_status():
    config = load_admin_config()
    return {"is_configured": config.get("is_configured", False)}

@app.get("/api/admin/session-status")
async def check_admin_session_status():
    check_session_timeout()
    return {"is_locked_by_admin": active_admin_session}

@app.post("/api/setup-admin")
async def setup_admin_credentials(data: SetupPayload):
    if not data.pin or len(data.pin.strip()) < 4:
        raise HTTPException(status_code=400, detail="Ang PIN ay dapat may hindi bababa sa 4 na digit.")
    if not data.question or not data.answer:
        raise HTTPException(status_code=400, detail="Paki-kumpleto ang security question at answer.")

    save_admin_config(data.pin, data.question, data.answer)
    print("✅ FIRST-TIME ADMIN SETUP COMPLETED SUCCESSFULLY")
    return {"status": "success", "message": "Nagawa na ang iyong Admin Account!"}

@app.post("/api/verify-pin")
async def verify_admin_pin(data: PinVerification):
    global active_admin_session, last_admin_activity
    
    check_session_timeout()

    # SINGLE-USER LOCK CHECK: Haharangan ang pangalawang tao kapag may kasalukuyan nang naka-login
    if active_admin_session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="⚠️ May kasalukuyan nang naka-login na Admin sa system. Isang tao lang ang pwedeng humawak ng Control Panel!"
        )

    config = load_admin_config()
    input_pin = str(data.pin).strip()
    target_pin = str(config.get("admin_pin", "1234")).strip()

    if input_pin == target_pin:
        active_admin_session = True
        last_admin_activity = time.time()
        print("🔑 ADMIN PIN VERIFIED & SESSION LOCKED TO SINGLE USER")
        return {"status": "success", "message": "Admin authenticated successfully."}
    else:
        print(f"❌ PIN MISMATCH: Received '{input_pin}', Expected '{target_pin}'")
        raise HTTPException(status_code=401, detail="Mali ang Admin PIN!")

@app.post("/api/admin/logout")
async def admin_logout():
    global active_admin_session, last_admin_activity
    active_admin_session = False
    last_admin_activity = 0
    print("🔒 ADMIN LOGOUT: Na-unlock na ang Control Panel para sa susunod na Admin.")
    return {"status": "success", "message": "Logged out successfully."}

@app.get("/api/get-security-question")
async def get_security_question():
    config = load_admin_config()
    return {"question": config.get("security_question", "Ano ang pangalan ng unang alagang hayop mo?")}

@app.post("/api/reset-pin")
async def reset_admin_pin(data: PinResetPayload):
    config = load_admin_config()
    user_answer = str(data.answer).strip().lower()
    correct_answer = str(config.get("security_answer", "")).strip().lower()

    if user_answer == correct_answer:
        if not data.new_pin or len(data.new_pin.strip()) < 4:
            raise HTTPException(status_code=400, detail="Ang bagong PIN ay dapat may hindi bababa sa 4 na digit.")
        
        save_admin_config(data.new_pin, config.get("security_question"), config.get("security_answer"))
        print(f"🔑 ADMIN PIN RESET TO: {data.new_pin}")
        return {"status": "success", "message": "Matagumpay na napalitan ang Admin PIN!"}
    else:
        print("❌ SECURITY ANSWER MISMATCH")
        raise HTTPException(status_code=401, detail="Mali ang sagot sa security question.")

@app.post("/api/change-pin-direct")
async def change_pin_direct(data: DirectPinChangePayload):
    check_session_timeout()
    if not active_admin_session:
        raise HTTPException(status_code=401, detail="Unauthorized! Kailangan mo munang mag-login bilang Admin.")

    config = load_admin_config()
    current_pin = str(config.get("admin_pin", "")).strip()

    if str(data.old_pin).strip() != current_pin:
        raise HTTPException(status_code=401, detail="Mali ang kasalukuyang PIN!")

    if not data.new_pin or len(data.new_pin.strip()) < 4:
        raise HTTPException(status_code=400, detail="Ang bagong PIN ay dapat may hindi bababa sa 4 na digit.")

    save_admin_config(data.new_pin, config.get("security_question"), config.get("security_answer"))
    last_admin_activity = time.time()
    print(f"🔑 ADMIN PIN UPDATED TO: {data.new_pin}")
    return {"status": "success", "message": "Matagumpay na na-update ang PIN!"}

# -------------------------------------------------------------------
# SPEED LIMIT CONTROL ENDPOINTS (SYNCED WITH ESP32 & WEB)
# -------------------------------------------------------------------
# GET: Malayang makakakuha ang ESP32 at Web Viewer ng kasalukuyang Speed Limit
@app.get("/api/speed-limit")
@app.get("/api/settings/speed-limit")
async def get_speed_limit():
    return {"speed_limit": current_speed_limit}

# POST: STRICT SECURITY - Ang NAKA-LOGIN LANG na Single Admin ang pwedeng magbago nito
@app.post("/api/speed-limit")
@app.post("/api/settings/speed-limit")
async def update_speed_limit(data: SpeedLimitUpdate):
    global current_speed_limit, last_admin_activity
    
    check_session_timeout()
    if not active_admin_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="⚠️ Unauthorized! Isang awtorisadong Admin lamang na kasalukuyang naka-login ang pwedeng magbago ng Speed Limit."
        )

    current_speed_limit = data.speed_limit
    last_admin_activity = time.time() # Refresh idle timer
    print(f"⚙️ SPEED LIMIT UPDATED TO: {current_speed_limit} km/h")
    
    # Broadcast sa WebSockets para mag-update agad ang Dashboard visuals
    await manager.broadcast({"type": "LIMIT_UPDATE", "new_limit": current_speed_limit})
    return {"status": "success", "new_limit": current_speed_limit}

# -------------------------------------------------------------------
# VIOLATIONS LOGGING & WEBSOCKET ENDPOINTS
# -------------------------------------------------------------------
@app.get("/api/violations")
async def get_all_violations():
    return load_saved_violations()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/api/violations")
async def record_violation(data: ViolationPayload):
    now = datetime.datetime.now()
    excess = round(data.detected_speed - current_speed_limit, 1) if data.detected_speed > current_speed_limit else 0.0

    plate_no, image_url = capture_and_read_plate()

    violation = {
        "id": int(now.timestamp()),
        "device_id": data.device_id,
        "vehicle_id": data.vehicle_id,
        "plate_number": plate_no,
        "image_url": image_url,
        "detected_speed": data.detected_speed,
        "speed_limit": current_speed_limit,
        "excess_speed": excess,
        "timestamp": now.strftime("%Y-%m-%d %I:%M:%S %p")
    }

    save_violation_to_db(violation)
    print(f"🚨 OVERSPEED LOGGED & SAVED: {violation['detected_speed']} km/h | Plate: {plate_no}")
    
    # Broadcast ang bagong huli sa dashboard
    await manager.broadcast(violation)
    return {"status": "success", "recorded": violation}

@app.get("/")
async def serve_home():
    index_path = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Overspeeding Backend is running! (index.html not found in public folder)"}

# Mount Static Files sa pinakababa
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("Bcknd:app", host="0.0.0.0", port=3000, reload=True)
