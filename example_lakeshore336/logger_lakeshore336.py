"""
CSV Logger for Lake Shore Model 336 Temperature Controller
FastCCD Camera -- TNO/NARIT

Logs every INTERVAL seconds to a daily CSV file.
Columns: timestamp, Input A (K/C/Ohm/status), Output 1 & 2 (setpoint/heater%/status/range)
"""

import sys
import serial
import time
import csv
import os
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---- Config ----
COM_PORT   = "COM10"
BAUD_RATE  = 57600
TIMEOUT    = 2       # serial timeout (s)
INTERVAL   = 60      # log interval (s)
LOG_DIR    = "logs"  # subfolder for CSV files

CSV_HEADER = [
    "timestamp",
    "input_a_K", "input_a_C", "input_a_raw_ohm", "rdgst_a",
    "setp1_K", "htr1_pct", "htrst1", "range1",
    "setp2_K", "htr2_pct", "htrst2", "range2",
]


def query(ser, cmd):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    time.sleep(0.3)
    n = ser.in_waiting
    raw = ser.read(n) if n > 0 else b""
    return bytes(b & 0x7F for b in raw).decode("ascii", errors="replace").strip()


def get_log_path():
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"lakeshore336_{date_str}.csv")


def write_row(filepath, row):
    file_exists = os.path.isfile(filepath)
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)


def read_all(ser):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    input_a_K       = query(ser, "KRDG? A")
    input_a_C       = query(ser, "CRDG? A")
    input_a_raw_ohm = query(ser, "SRDG? A")
    rdgst_a         = query(ser, "RDGST? A")

    setp1  = query(ser, "SETP? 1")
    htr1   = query(ser, "HTR? 1")
    htrst1 = query(ser, "HTRST? 1")
    range1 = query(ser, "RANGE? 1")

    setp2  = query(ser, "SETP? 2")
    htr2   = query(ser, "HTR? 2")
    htrst2 = query(ser, "HTRST? 2")
    range2 = query(ser, "RANGE? 2")

    return [
        ts,
        input_a_K, input_a_C, input_a_raw_ohm, rdgst_a,
        setp1, htr1, htrst1, range1,
        setp2, htr2, htrst2, range2,
    ]


def main():
    print("=" * 60)
    print("  Lake Shore 336 - CSV Logger  (FastCCD Camera / TNO)")
    print(f"  Port: {COM_PORT}   Interval: {INTERVAL}s   Log dir: {LOG_DIR}/")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    try:
        ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=TIMEOUT)
        print(f"Connected: {ser.name}\n")
    except serial.SerialException as e:
        print(f"[ERROR] Cannot open {COM_PORT}: {e}")
        return

    print(f"{'Timestamp':<22} {'A(K)':<12} {'A(C)':<12} {'Ohm':<12} {'Htr1%':<8} {'Htr2%':<8} {'File'}")
    print("-" * 95)

    try:
        while True:
            loop_start = time.time()

            row = read_all(ser)
            filepath = get_log_path()
            write_row(filepath, row)

            ts, a_K, a_C, a_ohm, rdgst, setp1, htr1, htrst1, range1, setp2, htr2, *_ = row
            filename = os.path.basename(filepath)
            print(f"{ts:<22} {a_K:<12} {a_C:<12} {a_ohm:<12} {htr1:<8} {htr2:<8} {filename}")

            elapsed = time.time() - loop_start
            sleep_time = max(0, INTERVAL - elapsed)
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        ser.close()
        print("Port closed.")


if __name__ == "__main__":
    main()
