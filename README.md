# วิถีจรชะตา ทำนาย สำหรับ Android

## เปิดเว็บทดลองผ่าน Chrome

1. แตกไฟล์ ZIP ด้วยแอปจัดการไฟล์ เปิด Chrome บน Android
2. สร้าง GitHub repository แบบ Private แล้วอัปโหลด `04_app.py`, `03_pdf_generator.py`, `requirements.txt` และ `packages.txt` ไว้ที่โฟลเดอร์บนสุด อัปโหลดฟอนต์ `Sarabun-Regular.ttf` เพิ่มได้เมื่อมีไฟล์
3. ลงชื่อเข้าใช้ Streamlit Community Cloud ที่ https://share.streamlit.io เชื่อมบัญชี GitHub ที่ดูแล repository แล้วกด Create app เลือก repository และ branch ที่เพิ่งอัปโหลด ตั้ง Main file path เป็น `04_app.py`
4. เปิด URL ที่ Streamlit แสดง ทดสอบกดสร้างและดาวน์โหลด PDF ตัวอย่าง แอปใช้ข้อมูลสมมติเมื่อยังไม่มีส่วน `[wichi]` ใน Secrets

## เชื่อมออเดอร์จริง

1. เตรียมฐานและ Apps Script ตามไฟล์ `01_unified_schema.sql` และ `02_Code.gs` ในชุด 1–5 เดิม ระบุคีย์ใหม่ใน Script Properties และทดสอบ Form Submit trigger ด้วยข้อมูลสมมติก่อน
2. ตั้ง Google OAuth สำหรับล็อกอิน Streamlit โดยใช้ URL จริงของแอปต่อท้ายด้วย `/oauth2callback` เป็น Authorized redirect URI
3. ตั้ง Gmail OAuth ให้บัญชีบริการ `wichiforecast.service@gmail.com` ได้ refresh token สำหรับ `openid email https://www.googleapis.com/auth/gmail.send` ตามตัวอย่างใน `04_app.py`
4. คัดโครงจาก `secrets.example.toml` และเติมค่าจริงใน Streamlit App settings → Secrets เก็บคีย์และ token ไว้ใน Secrets หรือ Script Properties ตามบทบาทของแต่ละส่วน ค่าตัวอย่างในไฟล์ยังใช้เปิดระบบจริงไม่ได้
5. ใช้ `prediction-reports-private` สำหรับรายงานลูกค้าตามค่าในตัวอย่าง และกำหนดให้ผู้ดูแลที่อนุญาตเท่านั้นเปิดหลังบ้าน

## ทำงานจากหน้าจอโทรศัพท์

เปิด URL หลังบ้านด้วย Chrome → ล็อกอิน → เลือกออเดอร์ → ตรวจหลักฐานชำระ → กรอกคำพยากรณ์และแนบภาพตามแพ็กเกจ → สร้าง PDF หรือส่งอีเมลด้วยปุ่มในหน้าออเดอร์ เมนูสรุปรายรับเปิดแยกจากออเดอร์ เมื่อมี URL ที่ใช้งานได้แล้ว เพิ่มทางลัดจากเมนู Chrome ไปหน้าจอหลักได้

Google Forms เป็นหน้ารับคำตอบลูกค้า ส่วนออเดอร์และรายงานอยู่ในหลังบ้าน ผู้ดูแลยังต้องตรวจหลักฐานชำระและกรอกเนื้อหาพยากรณ์ที่ต้องการส่ง

## เอกสารอ้างอิง

- [Google Forms บน Android](https://support.google.com/docs/answer/6281888?co=GENIE.Platform%3DAndroid&hl=en-GB)
- [เปิดแอปบน Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [เก็บค่าลับของแอป](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
