import os
import io
import re
import time
from datetime import datetime
import requests
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL")

def push_to_sheets(rows_data):
    if not rows_data:
        print("Tidak ada broken link yang perlu dikirim.")
        return
    
    if not WEBHOOK_URL:
        print("Error: SHEET_WEBHOOK_URL tidak ditemukan.")
        return

    res = requests.post(WEBHOOK_URL, json={"rows": rows_data})
    print(f"Status kirim Sheets: {res.status_code} - {res.text}")

def clean_and_read_captcha(img_bytes):
    img = Image.open(io.BytesIO(img_bytes)).convert("L")
    img = img.resize((img.width * 3, img.height * 3), Image.Resampling.LANCZOS)
    img = img.filter(ImageFilter.SHARPEN)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.5)
    
    threshold = 140
    img = img.point(lambda p: 255 if p > threshold else 0)
    
    code = pytesseract.image_to_string(
        img, 
        config="--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    return re.sub(r"[^A-Za-z0-9]", "", code).strip().upper()

def handle_captcha_if_exists(page):
    captcha_box = page.query_selector('div:has-text("Enter code:"), td:has-text("Enter code:"), #captcha, .captcha')
    captcha_input = page.query_selector('input[name*="code"], input[name*="captcha"], #code')
    captcha_img = page.query_selector('img[src*="captcha"], img[alt*="captcha"]')
    
    if captcha_input and captcha_input.is_visible() and captcha_img:
        print("-> Terdeteksi form CAPTCHA! Mengambil gambar...")
        img_bytes = captcha_img.screenshot()
        code = clean_and_read_captcha(img_bytes)
        print(f"-> Hasil baca OCR: {code}")
        
        captcha_input.fill("")
        captcha_input.fill(code)
        time.sleep(1)
        
        action_btn = page.query_selector('input[value="resume"], button:has-text("resume"), input[value="check"], button:has-text("check")')
        if action_btn:
            action_btn.click()
            time.sleep(3)
        return True
    return False

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled'
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768}
        )
        page = context.new_page()

        print("1. Membuka Dead Link Checker...")
        page.goto("https://www.deadlinkchecker.com/website-dead-link-checker.asp", wait_until="domcontentloaded")
        time.sleep(3)

        print("2. Mengisi URL target...")
        url_input = page.query_selector('input[type="text"][name*="url"], input[type="text"]')
        if url_input:
            url_input.fill("https://www.emservices.com.sg")

        # Klik tombol check awal untuk mentrigger form / scan
        submit_btn = page.query_selector('input[value="check"], button:has-text("check")')
        if submit_btn:
            print("2b. Menekan tombol check awal...")
            submit_btn.click()
            time.sleep(3)

        print("3. Memantau progres pemindaian...")
        max_wait = 1800  # Maksimal 30 menit
        interval = 5
        elapsed = 0
        scan_completed = False

        while elapsed < max_wait:
            time.sleep(interval)
            elapsed += interval

            # Cek jika scan sudah selesai (100% atau Scan completed atau muncul tombol Full report)
            if (page.query_selector('text=Scan completed') or 
                page.query_selector('text=100% scanned') or 
                page.query_selector('a:has-text("Full report")')):
                scan_completed = True
                print("Proses scan selesai 100%!")
                break

            # Cek dan selesaikan CAPTCHA jika muncul (baik di awal atau terjeda di tengah scan)
            handle_captcha_if_exists(page)

            if elapsed % 30 == 0:
                print(f"Scanning berjalan... ({elapsed} detik)")

        page.screenshot(path="debug_final_state.png")

        if not scan_completed:
            raise TimeoutError("Scan tidak mencapai 100% dalam 30 menit.")

        print("4. Mengambil data link yang rusak...")
        rows = page.query_selector_all("table tr")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        to_insert = []

        for row in rows:
            cols = row.query_selector_all("td")
            if len(cols) >= 3:
                status = cols[0].inner_text().strip()
                url = cols[1].inner_text().strip()
                source_text = cols[2].inner_text().strip()
                if any(x in status.lower() for x in ["40", "50", "timeout", "-1", "failed"]):
                    to_insert.append([now, status, url, source_text])

        print(f"Ditemukan {len(to_insert)} link bermasalah.")
        push_to_sheets(to_insert)
        browser.close()

if __name__ == "__main__":
    main()
