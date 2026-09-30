import os
import io
import re
import time
from datetime import datetime
import requests
from PIL import Image
import pytesseract
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL")

def push_to_sheets(rows_data):
    if not rows_data:
        print("Tidak ada broken link ditemukan.")
        return
    
    if not WEBHOOK_URL:
        print("Error: SHEET_WEBHOOK_URL tidak ditemukan.")
        return

    res = requests.post(WEBHOOK_URL, json={"rows": rows_data})
    print(f"Status kirim Sheets: {res.status_code} - {res.text}")

def solve_captcha_text(page):
    captcha_img = page.query_selector('img[src*="captcha"], .captcha img, img[alt*="captcha"]')
    if not captcha_img:
        return ""
    
    img_bytes = captcha_img.screenshot()
    img = Image.open(io.BytesIO(img_bytes)).convert("L")  # grayscale untuk akurasi OCR lebih tinggi
    code = pytesseract.image_to_string(img, config="--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return re.sub(r"[^A-Za-z]", "", code).strip().upper()

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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
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

        # Handle CAPTCHA loop (coba sampai 3 kali jika salah baca)
        for attempt in range(1, 4):
            captcha_input = page.query_selector('input[name*="code"], input[name*="captcha"], #code')
            if not captcha_input or not captcha_input.is_visible():
                break

            clean_code = solve_captcha_text(page)
            print(f"Percobaan CAPTCHA #{attempt}: {clean_code}")
            captcha_input.fill(clean_code)

            submit_btn = page.query_selector('input[value="check"], input[value="resume"], button:has-text("check")')
            if submit_btn:
                submit_btn.click()

            time.sleep(4)

            # Cek jika proses scan sudah berhasil dimulai
            if page.query_selector('text=scanned') or page.query_selector('text=Scanning'):
                print("CAPTCHA berhasil, proses scan berjalan...")
                break

        # Simpan screenshot awal untuk monitoring status berjalan
        page.screenshot(path="debug_running.png")

        print("3. Menunggu pemindaian selesai...")
        try:
            page.wait_for_selector("text=100% scanned", timeout=1800000)
            page.wait_for_selector("text=Scan completed", timeout=60000)
            print("Scan selesai 100%!")
        except Exception as e:
            print(f"Error saat scan: {e}")
            page.screenshot(path="debug_error.png")
            raise e

        # Ekstrak link bermasalah
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

        print(f"Ditemukan {len(to_insert)} link mati.")
        push_to_sheets(to_insert)
        browser.close()

if __name__ == "__main__":
    main()
