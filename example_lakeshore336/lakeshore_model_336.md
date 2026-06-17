# Lake Shore Model 336 Temperature Controller

## Overview

Lake Shore Model 336 เป็น precision cryogenic temperature controller ระดับ research grade  
ออกแบบมาสำหรับงาน low-temperature physics, semiconductor research, และ cryogenic instrumentation  
ที่ TNO ใช้กับ **FastCCD Camera** cryostat — monitor และ control อุณหภูมิของ CCD detector

---

## Hardware

### Sensor Inputs — 4 ช่อง (A, B, C, D)

แต่ละ input configurable แยกกันอิสระ รองรับ sensor ได้หลายประเภท:

| Sensor Type        | ย่านอุณหภูมิ      | หมายเหตุ                            |
|--------------------|-------------------|--------------------------------------|
| Silicon Diode      | 1.4 K – 500 K     | ทั่วไป ราคาถูก                      |
| GaAlAs Diode       | 1.4 K – 500 K     | magnetic field tolerant               |
| Platinum RTD PT100 | 30 K – 800 K      | room temp ขึ้นไป                     |
| Cernox (NTC)       | 100 mK – 420 K    | cryogenic แม่นยำสูง ใช้กับ ULTRASPEC |
| Rhodium Iron (RhFe)| 0.5 K – 325 K     | ทน magnetic field สูง               |
| Thermocouple       | ขึ้นอยู่กับชนิด   | ใช้ที่ room temp ขึ้นไป             |
| Custom Curve       | กำหนดเอง          | upload calibration curve เข้าเครื่องได้|

### Control Outputs — 4 ช่อง

| Output | ประเภท            | กำลังสูงสุด | ใช้งาน                  |
|--------|-------------------|-------------|--------------------------|
| 1      | DC Current/Voltage| 100 W       | heater หลัก             |
| 2      | DC Current/Voltage| 50 W        | heater รอง              |
| 3      | Analog Voltage    | 0–10 V DC   | valve, external device   |
| 4      | Analog Voltage    | 0–10 V DC   | valve, external device   |

### Front Panel
- จอ 2.8" color LCD
- แสดง temperature readings และ heater status real-time
- ตั้งค่า setpoint, PID, alarm ได้จากหน้าเครื่องโดยตรง

---

## Communication Interfaces

| Interface   | Default Settings      | หมายเหตุ                              |
|-------------|----------------------|---------------------------------------|
| **USB**     | 57,600 baud, 8N1     | emulates RS-232, ใช้อยู่ที่ COM10    |
| RS-232      | 57,600 baud, 8N1     | DB-9 connector                        |
| IEEE-488    | GPIB address 12      | legacy lab instruments                |
| Ethernet    | optional upgrade     | ไม่ได้ติดตั้งมาตรฐาน                 |

**Protocol:** Text-based commands, terminated ด้วย `\r\n` (CR+LF)  
Query commands ใช้ `?` ต่อท้าย, set commands ไม่มี `?`

---

## สิ่งที่ Python ดึงข้อมูลได้ (Query Commands)

### Temperature Readings

| Command        | ตัวอย่าง       | ค่าที่ได้             | หน่วย |
|----------------|----------------|----------------------|-------|
| `KRDG? A`      | KRDG? A        | `77.3500`            | Kelvin |
| `CRDG? A`      | CRDG? A        | `-195.800`           | Celsius |
| `SRDG? A`      | SRDG? A        | `1234.56`            | Ohm หรือ Volt (raw sensor) |

> ในงาน FastCCD Camera: KRDG ที่ดึงออกมาจะเป็นอุณหภูมิของ CCD (เป้าหมายอยู่ที่ ~165 K)

### Reading Status

| Command        | ความหมาย                              |
|----------------|---------------------------------------|
| `RDGST? A`     | สถานะการอ่านค่า: 0=OK, 1=invalid, 2=overrange, 4=underrange |
| `ALARMST? A`   | alarm flags: bit0=high alarm, bit1=low alarm |

### Heater & Output Status

| Command        | ตัวอย่างค่า | ความหมาย               |
|----------------|-------------|------------------------|
| `HTR? 1`       | `45.2`      | heater output % (0–100)|
| `HTRST? 1`     | `0`         | heater status (0=OK, 1=open, 2=short, 3=compliance) |
| `ANALOG? 3`    | `0.0`       | analog output % (0–100)|

### Setpoint & Control

| Command        | ตัวอย่างค่า | ความหมาย                    |
|----------------|-------------|------------------------------|
| `SETP? 1`      | `165.000`   | setpoint ของ output 1 (K)   |
| `RANGE? 1`     | `3`         | heater range (0=off, 1=Low, 2=Med, 3=High) |
| `PID? 1`       | `50,20,0`   | P, I, D values              |
| `RAMP? 1`      | `0,1.0`     | ramp on/off, rate (K/min)   |
| `RAMPST? 1`    | `0`         | 0=not ramping, 1=ramping     |

### System & Configuration

| Command        | ตัวอย่างค่า               | ความหมาย                  |
|----------------|---------------------------|---------------------------|
| `*IDN?`        | `LSCI,MODEL336,...`       | device ID string          |
| `INTYPE? A`    | `1,1,0,0,1,0`            | sensor type, autorange, range, compensation, units |
| `OUTMODE? 1`   | `1,1,1,0`                | mode, input, powerup, polarity |
| `ALARM? A`     | `1,200,100,0,0,1`        | alarm config: on, high, low, deadband, latch, audible |

---

## ข้อมูลที่ Python Monitor ได้ — สรุป

```
สิ่งที่ดึงได้จาก Input A (ที่เราใช้อยู่):
  - อุณหภูมิปัจจุบัน (K และ °C)
  - ค่า raw sensor (Ohm)
  - สถานะ reading (OK / error)
  - สถานะ alarm (high/low)

สิ่งที่ดึงได้จาก Output:
  - heater output % (กำลังที่ใช้อยู่)
  - setpoint ปัจจุบัน
  - heater range
  - PID parameters
  - สถานะ ramp

สิ่งที่ดึงได้จาก System:
  - device ID
  - sensor configuration
  - alarm configuration
```

---

## ⚠️ Critical Serial Quirks (พบจากการทดสอบจริง — อ่านก่อนเขียนโค้ด)

### 1. เครื่องจริงใช้ 7O1 ไม่ใช่ 8N1
Manual บอก 8N1 แต่เครื่องจริงส่งข้อมูลแบบ **7-bit data + Odd Parity (7O1)**
วิธีแก้: เปิด port เป็น 8N1 ตามปกติ แล้ว **strip parity bit ด้วย `& 0x7F`** ทุก byte:
```python
raw = ser.read(n)
decoded = bytes(b & 0x7F for b in raw).decode("ascii", errors="replace").strip()
```

### 2. ห้ามใช้ `readline()` — ต้องใช้ `read(in_waiting)` แทน
เครื่องส่ง newline เป็น `0x8A` (parity-encoded `\n`) แทน `0x0A`
`readline()` หา `0x0A` ไม่เจอ → **รอ timeout 2s ทุก command** → 12 commands = 26s/รอบ
วิธีถูกต้อง:
```python
def query(ser, cmd):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    time.sleep(0.3)          # รอ device ตอบ
    n = ser.in_waiting
    raw = ser.read(n) if n > 0 else b""
    return bytes(b & 0x7F for b in raw).decode("ascii", errors="replace").strip()
```

### 3. Input B ไม่มี sensor
`RDGST? B` คืนค่า `128` (no sensor) — ค่าอุณหภูมิ B จะเป็น 0.000 K เสมอ ปกติ

---

## Python Library ที่เลือกใช้

**pyserial** (ไม่ใช้ official lakeshore library) เพราะ:
- lakeshore library assume 8N1 อาจไม่รองรับ parity quirk ของเครื่องนี้
- pyserial ควบคุมได้ทุก byte, debug ง่ายกว่า
- สำหรับ monitor + log อย่างเดียว pyserial เกินพอ

```
pip install pyserial
```

---

## แนวทาง Phase นี้ (Exploration & Logging)

```
Phase 1 — Explore & Log
├── ต่อ USB → COM10
├── ส่ง commands ทีละตัว → ดูว่าได้ค่าอะไรกลับมา
├── เก็บลง CSV: timestamp, Kelvin, Celsius, heater%, setpoint
└── สร้าง mockup web → plot กราฟ temperature vs time

Phase 2 — Monitoring Dashboard (ค่อยทำ)
├── web app real-time
├── alert เมื่ออุณหภูมิผิดปกติ
└── export data

Phase 3 — Control (ถ้าต้องการ)
├── ส่ง SETP (setpoint command)
├── ปรับ PID
└── ramp control
```

---

## CSV Structure (ที่ใช้จริงใน logger_lakeshore336.py)

```
timestamp,input_a_K,input_a_C,input_a_raw_ohm,rdgst_a,setp1_K,htr1_pct,htrst1,range1,setp2_K,htr2_pct,htrst2,range2
2026-06-09 13:11:55,+229.138,-044.012,+082.616,000,+160.000,+000.0,0,0,+299.000,+000.0,0,0
```

| Column | Command | ความหมาย |
|--------|---------|----------|
| `input_a_K` | `KRDG? A` | อุณหภูมิ Kelvin |
| `input_a_C` | `CRDG? A` | อุณหภูมิ Celsius |
| `input_a_raw_ohm` | `SRDG? A` | ค่า resistance จาก Cernox sensor (Ohm) |
| `rdgst_a` | `RDGST? A` | สถานะ: 000=OK, 032=temp not in range |
| `setp1_K` / `setp2_K` | `SETP? 1/2` | Setpoint ของ Output 1 และ 2 (K) |
| `htr1_pct` / `htr2_pct` | `HTR? 1/2` | Heater output % |
| `htrst1` / `htrst2` | `HTRST? 1/2` | Heater status: 0=OK |
| `range1` / `range2` | `RANGE? 1/2` | 0=off, 1=Low, 2=Med, 3=High |

---

## Notes สำหรับ FastCCD Camera

- อุปกรณ์ชุดนี้ใช้กับ **FastCCD Camera เท่านั้น** (ไม่เกี่ยวกับ ULTRASPEC)
- **Output 1** SETP ~160 K — ค่าเก่าที่ตั้งทิ้งไว้
- **Output 2** SETP ~299 K — setpoint ที่ใช้งานอยู่ปัจจุบัน
- Monitor ทั้ง Output 1 และ Output 2
- Warm-up/Cool-down ใช้เวลา ~2–4 ชั่วโมง — logging ช่วง transition สำคัญมาก
- ถ้า vacuum เสีย (pressure สูง) → ความสามารถ cool ลดลง → อุณหภูมิสูงกว่า setpoint
- temperature log กับ vacuum pressure log ควรใช้คู่กัน

---

## Integration Plan (Next Step)

นำ `logger_lakeshore336.py` ไปรวมกับ **`webcam_to_grafana`** Flask webapp
- Logger loop รันใน background thread เก็บ CSV ตาม interval
- Flask endpoint `/api/lakeshore/latest` query เครื่องแบบ on-demand สำหรับ real-time display
- หน้าเว็บ poll ทุก ~5s เพื่ออัปเดตตัวเลขแบบ real-time
- ไม่ใช้ Grafana — custom Flask + Chart.js เหมือนที่ทำใน `app.py` ของ project นี้

---

## References

- Lake Shore Model 336 User's Manual: [lakeshore.com](https://www.lakeshore.com/products/categories/overview/temperature-products/cryogenic-temperature-controllers/model-336-temperature-controller)
- Serial command set: Chapter 6 ของ manual (Remote Operation)
- FastCCD Camera instrument: TNO / NARIT
