import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CAMERA_INDEX = 1                  # 0 = first camera, 1 = second, etc.
SNAPSHOT_INTERVAL = 60            # seconds — 60s for testing, change to 600-900 for production
SNAPSHOT_DIR = os.path.join(BASE_DIR, "snapshots")
PORT = 8080

# LakeShore 336
LS336_PORT     = "COM10"
LS336_BAUD     = 57600
LS336_INTERVAL = 60               # seconds between readings
LS336_LOG_DIR  = os.path.join(BASE_DIR, "logs")
