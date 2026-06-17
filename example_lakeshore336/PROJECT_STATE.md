# PROJECT STATE — LakeShore 336 Temperature Monitor
## อ่านไฟล์นี้ตอนเริ่ม session ใหม่ทุกครั้ง

**Last updated:** 2026-06-09
**Target:** Monitor & log CCD temperature ของ FastCCD Camera → CSV → web dashboard
**Working dir:** `C:\TNO\Project_python\lakeshore336`

---

## Project Identity

LakeShore Model 336 Temperature Controller ต่อจาก USB → COM10 (57,600 baud, **7O1**)
ใช้ **Input A** (Cernox sensor) วัดอุณหภูมิ CCD ของ FastCCD Camera ที่ TNO
Target temperature ขณะสังเกตการณ์: **~165 K (−108 °C)**
Phase นี้: explore + log ก่อน ยังไม่ทำ control
อุปกรณ์ชุดนี้ใช้กับ **FastCCD Camera เท่านั้น**

---

## Current Status

| Task | Status |
|---|---|
| ศึกษา LakeShore 336 commands & capabilities | ✅ |
| เขียน `lakeshore_model_336.md` | ✅ |
| Probe script (ส่ง command จริง ดู response) | ✅ `probe_lakeshore336.py` |
| CSV logger script | ✅ `logger_lakeshore336.py` |
| ทดสอบกับเครื่องจริง แล้วปรับ fields | ✅ ยืนยัน Input A + Output 1 & 2 |
| แก้ bug readline() → read(in_waiting) | ✅ ไม่ block 2s ต่อ command แล้ว |
| แก้ bug unpack index setp2/htr2 | ✅ console display ถูกต้อง |
| Mockup web (Flask + plot temp vs time) | ✅ app.py + templates/index.html |
| รวมกับ webcam_to_grafana webapp | ⏳ ทำ session หน้า |

---

## Pending Tasks

- [ ] นำ logger concept ไปรวมกับ `webcam_to_grafana` Flask webapp (session หน้า)

---

## Key Numbers

| Parameter | Value |
|---|---|
| COM Port | COM10 |
| Baud rate | 57,600 |
| Serial settings | **7O1** (7 data bits, odd parity, 1 stop bit) |
| Decode trick | `byte & 0x7F` ถอด parity bit ออกก่อน decode |
| Sensor input | A (Cernox) — Input B ไม่มี sensor ต่อ |
| Target temp (observe) | ~165 K / −108 °C |
| Output 1 SETP | 160 K (ค่าเก่าที่ตั้งทิ้งไว้) |
| Output 2 SETP | 299 K (ค่าที่ใช้งานอยู่ปัจจุบัน) |
| Log interval | 60 s |

---

## Files to Know

| File | Purpose |
|---|---|
| `lakeshore_model_336.md` | Reference doc: hardware, commands, sensor types, CSV structure, phase plan |
| `probe_lakeshore336.py` | ส่ง command ทีละตัวดู response จริง |
| `logger_lakeshore336.py` | CSV logger 60s interval → `logs/lakeshore336_YYYY-MM-DD.csv` |
| `logs/` | CSV data รายวัน |
| `PROJECT_STATE.md` | State file นี้ |

---

## Decisions Log

| Decision | Rationale |
|---|---|
| Phase นี้ไม่ทำ control ก่อน | ต้องการ explore ข้อมูลก่อน ลด risk จากการส่ง command ผิดพลาด |
| ใช้ pyserial ตรง แทน lakeshore library | ง่ายกว่าสำหรับ simple logging ไม่ต้องพึ่ง dependency มาก |
| เก็บข้อมูลลง CSV ก่อน แล้วค่อย web | ได้ข้อมูลจริงก่อนออกแบบ dashboard |
| Serial 7O1 ไม่ใช่ 8N1 ตาม manual | ค้นพบจากการ probe จริง — decode ด้วย `byte & 0x7F` |
| Monitor Output 1 และ Output 2 ทั้งคู่ | Input B ไม่มี sensor, sensor มีเฉพาะ Input A — log ทั้ง 2 output ไว้ครบ |

---

## Session Log

| Session | Date | สิ่งที่ทำ |
|---------|------|----------|
| Session 1 | 2026-06-09 | เริ่มโปรเจกต์ใหม่ — ศึกษา LakeShore 336 commands & capabilities, เขียน `lakeshore_model_336.md` ครบ |
| Session 2 | 2026-06-09 | แก้ไขชื่อ camera จาก ULTRASPEC → FastCCD Camera, เขียน `probe_lakeshore336.py` |
| Session 3 | 2026-06-09 | รัน probe + logger กับเครื่องจริง — พบ serial 7O1 parity แก้ด้วย strip bit 7, ยืนยัน sensor มีเฉพาะ Input A, Output 2 SETP=299K ตรงหน้าเครื่อง, logger เขียน CSV ทุก 60s ลง `logs/` ทำงานได้จริง |
| Session 4 | 2026-06-17 | แก้ bug readline() timeout 2s/cmd → read(in_waiting), แก้ unpack index setp2/htr2, ยืนยัน logger รันได้ อุณหภูมิปัจจุบัน ~229K (warm) |
