import os
import io
import re
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
        print("Error: SHEET_WEBHOOK_URL tidak ditemukan di environment.")
        return

    response = requests.post(WEBHOOK_URL, json={"rows": rows_data})
    if response.status_code == 200:
        print(f"Berhasil menambahkan {len(rows_data)} baris ke Google Sheets via Webhook!")
    else:
        print(f"Gagal kirim ke Google Sheets: {response.text}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        page = browser.new_page()

        print("Membuka website deadlinkchecker...")
        page.goto("https://www.deadlinkchecker.com/website-dead-link-checker.asp", wait_until="networkidle")
        page.fill('input[type="text"][name*="url"], input[type="text"]', "https://www.emservices.com.sg")

        captcha_img = page.query_selector('img[src*="captcha"], .captcha img, img[alt*="captcha"]')
        captcha_input = page.query_selector('input[name*="code"], input[name*="captcha"], #code')

        if captcha_img and captcha_input:
            print("Membaca CAPTCHA...")
            img_bytes = captcha_img.screenshot()
            img = Image.open(io.BytesIO(img_bytes))
            code_text = pytesseract.image_to_string(img, config="--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            clean_code = re.sub(r"[^A-Za-z]", "", code_text).strip().upper()
            print(f"Kode CAPTCHA: {clean_code}")
            captcha_input.fill(clean_code)

        submit_btn = page.query_selector('input[value="check"], input[value="resume"], button:has-text("check")')
        if submit_btn:
            submit_btn.click()

        print("Menunggu scan selesai...")
        page.wait_for_selector("text=100% scanned", timeout=1800000)
        page.wait_for_selector("text=Scan completed", timeout=60000)

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

        push_to_sheets(to_insert)
        browser.close()

if __name__ == "__main__":
    main()