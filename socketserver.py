import socketio
from fastapi import FastAPI
import uvicorn

# Initialize Socket.IO and FastAPI
# cors_allowed_origins='*' allows your frontend to connect from anywhere during development
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()

# Wrap the FastAPI app with the Socket.IO ASGI app
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

# --- IN-MEMORY STATE MANAGEMENT ---
# Note: For a scaled production app, you would move these to a Redis database.
# Structure: { "meeting_code": { "host_sid": "socket_id", "participants": ["sid1", "sid2"] } }
meetings = {} 

# Structure: { "socket_id": { "user_id": "user123", "meeting_code": "abc-def", "role": "host|participant" } }
active_users = {}

@sio.event
async def connect(sid, environ, auth):
    print(f"Client connected: {sid}")
    # You can validate authentication tokens here via the 'auth' dictionary

@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")
    user_data = active_users.get(sid)
    
    if user_data:
        meeting_code = user_data["meeting_code"]
        role = user_data["role"]
        
        if meeting_code in meetings:
            if role == "host":
                # If host drops, end the meeting for everyone
                await sio.emit("meeting_ended", room=meeting_code)
                del meetings[meeting_code]
            else:
                # If participant drops, remove them and notify others
                if sid in meetings[meeting_code]["participants"]:
                    meetings[meeting_code]["participants"].remove(sid)
                await sio.emit("participant_left", {"sid": sid}, room=meeting_code)
        
        del active_users[sid]

# --- 1. STARTING A MEETING (HOST) ---
@sio.event
async def start_meeting(sid, data):
    meeting_code = data.get("meeting_code")
    user_id = data.get("user_id")
    
    # Register the meeting and the host
    meetings[meeting_code] = {"host_sid": sid, "participants": []}
    active_users[sid] = {"user_id": user_id, "meeting_code": meeting_code, "role": "host"}
    
    sio.enter_room(sid, meeting_code)
    await sio.emit("meeting_started", {"status": "success", "meeting_code": meeting_code}, to=sid)
    print(f"Meeting {meeting_code} started by Host {sid}")

# --- 2. JOINING A MEETING (PARTICIPANT WORKFLOW) ---
@sio.event
async def request_join(sid, data):
    meeting_code = data.get("meeting_code")
    user_info = data.get("user_info") # e.g., Name to show the host
    
    if meeting_code not in meetings:
        await sio.emit("join_error", {"message": "Invalid meeting code"}, to=sid)
        return

    host_sid = meetings[meeting_code]["host_sid"]
    
    # Notify host that someone is in the waiting room
    await sio.emit("join_request_received", {"sid": sid, "user_info": user_info}, to=host_sid)
    await sio.emit("waiting_for_host", {"message": "Waiting for host to let you in..."}, to=sid)

@sio.event
async def host_response(sid, data):
    """Host allows or denies a participant"""
    requester_sid = data.get("requester_sid")
    action = data.get("action") # "allow" or "deny"
    meeting_code = active_users.get(sid, {}).get("meeting_code")
    
    if action == "allow":
        sio.enter_room(requester_sid, meeting_code)
        meetings[meeting_code]["participants"].append(requester_sid)
        active_users[requester_sid] = {"user_id": data.get("user_id"), "meeting_code": meeting_code, "role": "participant"}
        
        # Tell the participant they are in, and tell the room someone joined
        await sio.emit("join_accepted", {"meeting_code": meeting_code}, to=requester_sid)
        await sio.emit("participant_joined", {"sid": requester_sid}, room=meeting_code, skip_sid=requester_sid)
    else:
        await sio.emit("join_denied", {"message": "The host declined your request."}, to=requester_sid)

# --- 3. HOST CONTROLS (KICK & END) ---
@sio.event
async def kick_participant(sid, data):
    target_sid = data.get("target_sid")
    user_data = active_users.get(sid)
    
    # Security check: Only host can kick
    if user_data and user_data["role"] == "host":
        meeting_code = user_data["meeting_code"]
        if target_sid in meetings[meeting_code]["participants"]:
            await sio.emit("kicked_by_host", to=target_sid)
            sio.leave_room(target_sid, meeting_code)
            meetings[meeting_code]["participants"].remove(target_sid)
            await sio.emit("participant_left", {"sid": target_sid}, room=meeting_code)

@sio.event
async def end_meeting(sid, data):
    user_data = active_users.get(sid)
    if user_data and user_data["role"] == "host":
        meeting_code = user_data["meeting_code"]
        await sio.emit("meeting_ended", room=meeting_code)
        # Clean up state
        if meeting_code in meetings:
            del meetings[meeting_code]

# --- 4. WEBRTC SIGNALING (THE GLUE FOR VIDEO/AUDIO) ---
# Browsers use these to exchange connection data directly with each other
@sio.event
async def webrtc_signal(sid, data):
    target_sid = data.get("target_sid")
    payload = data.get("payload") # This contains the WebRTC Offer/Answer/ICE candidate
    
    # Relay the signal to the specific target peer
    await sio.emit("webrtc_signal", {"sender_sid": sid, "payload": payload}, to=target_sid)
