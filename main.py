Вашият Python скрипт е доста обширен и добре структуриран за скрейпване на Imot.bg, включително функционалности като извличане от множество каталози, обработка на пагинация, правене на скрийншоти и запис на данни в Google Sheets, както и проверка за дублирани URL адреси.

Основният проблем, който трябва да се реши, за да работи този скрипт във вашата Docker среда на Elest.io, е, че той е **проектиран да работи предимно в Google Colab**. Това е видно от импорти като `from google.colab import drive, files` и използването на `/content/service_account.json` като път за акаунта.

Ето какво трябва да променя, за да го адаптирам за Elest.io (базирана на Docker контейнери) и да интегрирам `Flask` за `healthcheck`, както обсъдихме по-рано:

**Промени, които ще направя:**

1.  **Премахване на Google Colab специфични части:**
      * Премахване на `google.colab` импорти и функционалности (`drive.mount`, `files.upload`).
      * Промяна на пътя за `SERVICE_ACCOUNT_FILE_PATH` да бъде променлива на средата, която ще се подава от Docker.
2.  **Интегриране на Flask за `healthcheck`:**
      * Добавяне на Flask приложение.
      * Дефиниране на `/health` ендпойнт, който връща "OK".
      * Стартиране на Flask сървъра в отделна нишка, за да не блокира основната логика на скрейпъра.
3.  **Конфигуриране чрез променливи на средата:**
      * Използване на `os.getenv()` за `GOOGLE_SHEET_NAME` и `BATCH_SIZE`, `SCHEDULE_HOURS` (които вече са в `docker-compose.yml`), `PORT`.
4.  **Коригиране на логиката за инсталиране на `pyppeteer` и Chromium:**
      * Логиката с `subprocess.check_call([sys.executable, "-m", "pip", "install", "pyppeteer"])` и `pyppeteer-install` (в `Dockerfile`) е правилна за Docker, но ще премахна ръчната проверка и инсталация в самия Python скрипт, тъй като това се грижи `Dockerfile`.
5.  **Локални пътища за скрийншоти:**
      * Промяна на `output_folder` за скрийншоти от `/content/temp_imot_screenshots/` към `/app/screenshots/` (както е дефинирано във `Dockerfile` и `docker-compose.yml` като volume).
      * Промяна на `logs` от `/app/logs` (което вече е ок, но ще го направя явно).
6.  **Управление на сервизния акаунт:** Вместо да търси файл `/content/service_account.json`, ще очаква съдържанието на service account JSON да бъде подадено като променлива на средата `GOOGLE_SERVICE_ACCOUNT_JSON`.
7.  **Настройка на главната функция да работи периодично:** Ще използвам библиотеката `schedule` (която вече е в `requirements.txt`) за да изпълнявам скрейпъра на всеки `SCHEDULE_HOURS` часа.

-----

Ето модифицираният `main.py` файл:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Imot.bg Каталог Scraper - Автоматично извличане от цели каталози
Адаптиран за Docker/Elest.io среда с Flask healthcheck
и конфигурация чрез променливи на средата.
"""

import nest_asyncio
nest_asyncio.apply()

import os
import sys
import subprocess
import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import datetime
import gspread
import asyncio
import time
import traceback
import json
import random
from urllib.parse import urljoin, urlparse

# Flask за healthcheck
from flask import Flask
import threading
import schedule # За планиране на задачи

print("🆕 ЗАПОЧВАМ КАТАЛОЖЕН КОД - АВТОМАТИЧНО ИЗВЛИЧАНЕ ОТ МНОЖЕСТВО КАТАЛОЗИ!")

# Проверка и инсталиране на pyppeteer - тази част вече е в Dockerfile, но я оставям за сигурност, ако се изпълнява извън Docker
try:
    import pyppeteer
    print("✅ pyppeteer е наличен.")
except ImportError:
    print("📦 Инсталирам pyppeteer (трябва да е инсталиран от Dockerfile)...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyppeteer"])
        import pyppeteer
        print("✅ pyppeteer успешно инсталиран.")
    except Exception as e:
        print(f"❌ Грешка при инсталиране на pyppeteer: {e}")
        print("Моля, уверете се, че pip е наличен и работи.")

try:
    from pyppeteer import launch
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    print("✅ Всички библиотеки за скрийншоти и Google Drive са налични.")
except ImportError as e:
    print(f"⚠️ Липсва библиотека за скрийншоти/Google Drive API: {e}")
    print("📦 Моля инсталирайте: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib pyppeteer")

# КОНФИГУРАЦИЯ ОТ ПРОМЕНЛИВИ НА СРЕДАТА
# (Тези променливи се подават от docker-compose.yml/Elest.io)
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
GOOGLE_SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME', 'Imot Data Extractor')
BATCH_SIZE = int(os.getenv('BATCH_SIZE', 20))
SCHEDULE_HOURS = int(os.getenv('SCHEDULE_HOURS', 6))
WEB_SERVER_PORT = int(os.getenv('PORT', 3000))

# Локални пътища за логове и скрийншоти в Docker контейнера
LOGS_DIR = '/app/logs'
SCREENSHOTS_DIR = '/app/screenshots'
# Уверете се, че директориите съществуват
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# НОВА КОНСТАНТА: ID НА ПАПКАТА ЗА СКРИЙНШОТИ (ще се намери автоматично)
SCREENSHOTS_FOLDER_ID = None

# SERVICE_ACCOUNT_FILE_PATH - създаваме временен файл, ако JSON е подаден като ENV
SERVICE_ACCOUNT_FILE_PATH = os.path.join(LOGS_DIR, 'service_account.json') # Използваме LOGS_DIR за временен файл

# Flask App за Healthcheck
app = Flask(__name__)

@app.route('/health')
def health_check():
    return "OK", 200

# Функции за Google Drive и Sheets (без промени в логиката, само в SERVICE_ACCOUNT_FILE_PATH)
def find_screenshots_folder_id():
    """ПОДОБРЕНА ФУНКЦИЯ: Намира или създава папките за скрийншоти в Google Drive"""
    global SCREENSHOTS_FOLDER_ID

    print("\n🔍 ТЪРСЯ ПАПКИ В GOOGLE DRIVE...")

    try:
        from googleapiclient.discovery import build
        from google.oauth2.service_account import Credentials

        # Използваме пътя към временния service_account.json
        SERVICE_ACCOUNT_FILE = SERVICE_ACCOUNT_FILE_PATH
        SCOPES = ['https://www.googleapis.com/auth/drive']

        credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        drive_service = build('drive', 'v3', credentials=credentials)

        # Търсим папката "Imot Data Extractor" (гъвкаво търсене)
        main_folder_id = None
        main_folder_name = None

        possible_names = ["Imot Data Extractor", "ImotDataExtractor", "Imot_Data_Extractor", "imot data extractor"]

        for name in possible_names:
            main_folder_query = f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
            main_folder_result = drive_service.files().list(q=main_folder_query, fields="files(id, name)").execute()
            main_folders = main_folder_result.get('files', [])

            if main_folders:
                main_folder_id = main_folders[0]['id']
                main_folder_name = main_folders[0]['name']
                print(f"✅ Намерих главна папка '{main_folder_name}' с ID: {main_folder_id}")
                break

        if not main_folder_id:
            print("📁 Не намерих папка 'Imot Data Extractor'. Създавам я...")

            main_folder_metadata = {
                'name': 'Imot Data Extractor',
                'mimeType': 'application/vnd.google-apps.folder'
            }

            created_main_folder = drive_service.files().create(body=main_folder_metadata, fields='id').execute()
            main_folder_id = created_main_folder.get('id')
            main_folder_name = 'Imot Data Extractor'
            print(f"✅ Създадох главна папка 'Imot Data Extractor' с ID: {main_folder_id}")

        screenshots_folder_query = f"name='ScreenShotsScraper' and mimeType='application/vnd.google-apps.folder' and '{main_folder_id}' in parents"
        screenshots_result = drive_service.files().list(q=screenshots_folder_query, fields="files(id, name)").execute()
        screenshots_folders = screenshots_result.get('files', [])

        if screenshots_folders:
            SCREENSHOTS_FOLDER_ID = screenshots_folders[0]['id']
            print(f"✅ Намерих 'ScreenShotsScraper' с ID: {SCREENSHOTS_FOLDER_ID}")
            return SCREENSHOTS_FOLDER_ID
        else:
            print("📁 Не намерих 'ScreenShotsScraper'. Създавам я...")

            screenshots_folder_metadata = {
                'name': 'ScreenShotsScraper',
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [main_folder_id]
            }

            created_screenshots_folder = drive_service.files().create(body=screenshots_folder_metadata, fields='id').execute()
            SCREENSHOTS_FOLDER_ID = created_screenshots_folder.get('id')
            print(f"✅ Създадох 'ScreenShotsScraper' с ID: {SCREENSHOTS_FOLDER_ID}")

            print(f"\n📁 ФИНАЛНА СТРУКТУРА:")
            print(f"   My Drive")
            print(f"   └── {main_folder_name} (ID: {main_folder_id})")
            print(f"       └── ScreenShotsScraper (ID: {SCREENSHOTS_FOLDER_ID})")

            return SCREENSHOTS_FOLDER_ID

    except Exception as e:
        print(f"❌ Грешка при търсене/създаване на папки: {e}")
        traceback.print_exc()
        return None

def get_catalog_name_from_url(url):
    # Оригинална функция
    try:
        if "/briz/" in url:
            return "Бриз"
        elif "/avtogara/" in url:
            return "Автогара"
        elif "/gratska-mahala/" in url:
            return "Градска махала"
        elif "/troshevo/" in url:
            return "Трошево"
        elif "/kaysieva-gradina/" in url:
            return "Кайсиева градина"
        elif "/m-t-evksinograd/" in url:
            return "м-т Евксиноград"
        elif "/asparuhovo/" in url:
            return "Аспарухово"
        else:
            parts = url.split("/")
            for i, part in enumerate(parts):
                if part == "grad-varna" and i + 1 < len(parts):
                    return parts[i + 1].replace("-", " ").title()
            return "Неизвестен район"
    except:
        return "Неизвестен район"

def get_existing_urls_from_sheet(spreadsheet_name):
    print(f"\n🔍 ИЗВЛИЧАМ СЪЩЕСТВУВАЩИ URL-И ОТ ТАБЛИЦАТА '{spreadsheet_name}'")

    try:
        # Използваме пътя към временния service_account.json
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE_PATH)
        spreadsheet = gc.open(spreadsheet_name)
        worksheet = spreadsheet.sheet1

        column_r_values = worksheet.col_values(18)
        existing_urls = set()
        for i, url in enumerate(column_r_values):
            if i == 0:
                continue
            if url and url.strip() and url.startswith('http'):
                existing_urls.add(url.strip())

        print(f"✅ Намерих {len(existing_urls)} съществуващи URL-и в таблицата")

        if existing_urls:
            print("📋 Примерни съществуващи URL-и:")
            for i, url in enumerate(list(existing_urls)[:3]):
                print(f"   {i+1}. {url}")
            if len(existing_urls) > 3:
                print(f"   ... и още {len(existing_urls) - 3}")

        return existing_urls

    except Exception as e:
        print(f"❌ Грешка при извличане на съществуващи URL-и: {e}")
        traceback.print_exc()
        return set()

def is_url_already_processed(url, existing_urls):
    return url.strip() in existing_urls

def upload_to_drive_screenshots_folder(local_path):
    print(f"⬆️ Качвам скрийншот '{os.path.basename(local_path)}' в Google Drive...")
    try:
        from googleapiclient.discovery import build
        from google.oauth2.service_account import Credentials
        from googleapiclient.http import MediaFileUpload

        folder_id = SCREENSHOTS_FOLDER_ID
        if not folder_id:
            print("❌ Няма ID на папка ScreenShotsScraper")
            return None

        # Използваме пътя към временния service_account.json
        SERVICE_ACCOUNT_FILE = SERVICE_ACCOUNT_FILE_PATH
        SCOPES = ['https://www.googleapis.com/auth/drive']

        credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        drive_service = build('drive', 'v3', credentials=credentials)

        file_metadata = {
            'name': os.path.basename(local_path),
            'parents': [folder_id]
        }

        media = MediaFileUpload(local_path, mimetype='image/jpeg')

        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = file.get('id')

        drive_service.permissions().create(
            fileId=file_id,
            body={'role': 'reader', 'type': 'anyone'}
        ).execute()

        drive_link = f"https://drive.google.com/file/d/{file_id}/view"
        print(f"✅ Скрийншот качен в ScreenShotsScraper папка: {drive_link}")
        return drive_link

    except Exception as e:
        print(f"❌ Грешка при качване в ScreenShotsScraper папка: {e}")
        traceback.print_exc()
        return None

async def screenshot_final(url, output_folder=SCREENSHOTS_DIR):
    print(f"📸 Правя скрийншот на: {url}")
    browser = None

    try:
        browser = await launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-features=VizDisplayCompositor'
            ]
        )
        page = await browser.newPage()
        await page.setViewport({'width': 1920, 'height': 1080})

        await page.goto(url, {'waitUntil': ['networkidle0'], 'timeout': 60000})
        await asyncio.sleep(3)

        safe_filename = re.sub(r'[^a-zA-Z0-9_\-.]', '_', url.split('/')[-1])[:40]
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        screenshot_name = f"imot_{safe_filename}_{timestamp}.jpeg"
        screenshot_path = os.path.join(output_folder, screenshot_name)

        os.makedirs(output_folder, exist_ok=True)

        await page.screenshot({
            'path': screenshot_path,
            'fullPage': True,
            'type': 'jpeg',
            'quality': 60
        })
        print(f"✅ Скрийншот запазен локално (JPEG, качество 60): {screenshot_path}")

        drive_link = upload_to_drive_screenshots_folder(screenshot_path)

        try:
            os.remove(screenshot_path)
            print(f"🗑️ Локалният файл е изтрит")
        except Exception as e:
            print(f"⚠️ Грешка при изтриване на локален скрийншот: {e}")

        return drive_link if drive_link else f"Локално: {screenshot_path}"

    except Exception as e:
        print(f"❌ Грешка при скрийншот: {e}")
        traceback.print_exc()
        return None
    finally:
        if browser:
            await browser.close()

def fetch_html_final(url):
    # Оригинална функция
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'bg-BG,bg;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

    print(f"🌐 Извличане на HTML от: {url}")
    try:
        response = requests.get(url, headers=headers, allow_redirects=True, timeout=25)
        response.raise_for_status()

        for encoding in ['utf-8', 'windows-1251', 'iso-8859-1']:
            try:
                test_content = response.content.decode(encoding)
                if 'имот' in test_content.lower() or 'продава' in test_content.lower() or 'цена' in test_content.lower():
                    response.encoding = encoding
                    print(f"✅ HTML успешно извлечен (encoding: {encoding})")
                    return response.text
            except UnicodeDecodeError:
                continue

        response.encoding = 'utf-8'
        return response.text

    except Exception as e:
        print(f"❌ Грешка при извличане на HTML за {url}: {e}")
        return None

def clean_text_final(text):
    # Оригинална функция
    if not isinstance(text, str):
        return "Не е намерено"
    try:
        if '' in text:
            text = text.replace('', '')
        cleaned_text = re.sub(r'[^\w\s\.,\-_\/\(\):;?!%&#+=\[\]{}|~^<>`@*а-яА-ЯёЁ]', '', text)
        cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()
        return cleaned_text if cleaned_text else "Не е намерено"
    except Exception:
        return "Не е намерено"

def extract_full_description(soup):
    # Оригинална функция
    print("   🔍 Търся пълното описание...")

    description_div = soup.find('div', id='description_div')
    if description_div:
        description_html = str(description_div)
        description_html = re.sub(r'<br\s*/?>', '\n', description_html)
        temp_soup = BeautifulSoup(description_html, 'html.parser')
        description_text = temp_soup.get_text()
        description_text = re.sub(r'\n+', ' ', description_text)
        description_text = re.sub(r'\s+', ' ', description_text).strip()
        if description_text and len(description_text) > 50:
            print(f"   ✅ ОТ description_div: '{description_text[:100]}...' (общо {len(description_text)} символа)")
            return clean_text_final(description_text)

    desc_candidates = soup.find_all('div', id=True)
    for div in desc_candidates:
        div_id = div.get('id', '')
        if 'desc' in div_id.lower():
            desc_text = div.get_text(strip=True)
            if len(desc_text) > 100:
                desc_text = re.sub(r'\n+', ' ', desc_text)
                desc_text = re.sub(r'\s+', ' ', desc_text).strip()
                print(f"   ✅ ОТ {div_id}: '{desc_text[:100]}...'")
                return clean_text_final(desc_text)

    table_cells = soup.find_all('td')
    for cell in table_cells:
        cell_text = cell.get_text(strip=True)
        if (len(cell_text) > 200 and
            any(word in cell_text.lower() for word in ['етаж', 'сграда', 'апартамент', 'коридор', 'спални'])):
            desc_text = re.sub(r'\n+', ' ', cell_text)
            desc_text = re.sub(r'\s+', ' ', desc_text).strip()
            print(f"   ✅ ОТ таблица: '{desc_text[:100]}...'")
            return clean_text_final(desc_text)

    print("   ❌ Описание НЕ е намерено")
    return "Не е намерено"

def analyze_property_complete(url, html_content):
    # Оригинална функция
    default_date = datetime.datetime.now().strftime("%d/%m/%Y")

    data = {
        "Тип имот": "Не е намерено",
        "Квартал": "Не е намерено",
        "Цена (EUR)": "Не е намерено",
        "Цена на м² (EUR)": "",
        "Площ": "Не е намерено",
        "Етаж": "Не е намерено",
        "Дата на въвеждане": default_date,
        "Тип строителство": "Не е намерено",
        "Година на строителство": "Не е намерено",
        "Особености": "Не е намерено",
        "Продавач": "Не е намерено",
        "Телефон на продавача": "Не е намерено",
        "Описание": "Не е намерено",
        "Състояние": "Не е намерено",
        "Скрийншот": "",
        "Цена на сделката": "",
        "Коментари": "",
        "URL за анализ": ""
    }

    if not html_content:
        print(f"❌ HTML е празно за {url}")
        data["URL за анализ"] = url
        return {"success": False, "error": "HTML е празно.", "data": data}

    soup = BeautifulSoup(html_content, 'html.parser')

    try:
        print(f"\n🔍 ЗАПОЧВАМ ПОДОБРЕН АНАЛИЗ НА: {url}")
        print("="*60)

        print("1️⃣ ТИП ИМОТ:")
        adv_header = soup.find('div', class_='advHeader')
        if adv_header:
            title_div = adv_header.find('div', class_='title')
            if title_div:
                title_text = ''
                for content in title_div.contents:
                    if hasattr(content, 'name') and content.name == 'div':
                        break
                    elif hasattr(content, 'strip'):
                        title_text += content.strip() + ' '

                title_text = title_text.strip()
                if 'Продава' in title_text:
                    property_type = re.sub(r'^.*?Продава\s+', '', title_text).strip()
                    data["Тип имот"] = clean_text_final(property_type)
                    print(f"   ✅ '{property_type}'")

        print("2️⃣ КВАРТАЛ:")
        location_div = soup.find('div', class_='location')
        if location_div:
            location_text = location_div.get_text(strip=True)
            location_text = re.sub(r'\s+', ' ', location_text).strip()
            location_text = re.sub(r'^град\s+Варна,\s*', '', location_text, flags=re.IGNORECASE)
            data["Квартал"] = clean_text_final(location_text)
            print(f"   ✅ (след премахване на 'град Варна, ') '{location_text}'")
        else:
            breadcrumbs = soup.find('div', class_='breadcrumbs')
            if breadcrumbs:
                links = breadcrumbs.find_all('a')
                if len(links) >= 2:
                    location_parts = []
                    for link in links[-2:]:
                        text = clean_text_final(link.get_text().strip())
                        if text and text != "Не е намерено":
                            location_parts.append(text)

                    if location_parts:
                        location_combined = ", ".join(location_parts)
                        location_combined = re.sub(r'^град\s+Варна,\s*', '', location_combined, flags=re.IGNORECASE)
                        data["Квартал"] = location_combined
                        print(f"   ✅ (от breadcrumbs) '{data['Квартал']}'")

        print("3️⃣ ЦЕНА:")
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.get_text()
            eur_match = re.search(r'([\d\s]+)\s*EUR', title_text)
            if eur_match:
                price_num = re.sub(r'\s+', '', eur_match.group(1))
                if price_num.isdigit():
                    data["Цена (EUR)"] = price_num
                    print(f"   ✅ {price_num} EUR")

        print("4️⃣ ХАРАКТЕРИСТИКИ ОТ adParams:")
        ad_params = soup.find('div', class_='adParams')
        if ad_params:
            param_divs = ad_params.find_all('div', recursive=False)
            for i, param_div in enumerate(param_divs):
                div_text = param_div.get_text(strip=True)

                if 'Площ' in div_text:
                    area_html = str(param_div)
                    area_html = re.sub(r'<sup>2</sup>', '²', area_html)
                    temp_soup = BeautifulSoup(area_html, 'html.parser')
                    full_area_text = temp_soup.get_text(strip=True).replace('\n', ' ')
                    full_area_text = re.sub(r'\s+', ' ', full_area_text).strip()

                    area_match = re.search(r'(\d+)', full_area_text)
                    if area_match:
                        data["Площ"] = area_match.group(1)
                        print(f"   ✅ ПЛОЩ: '{area_match.group(1)} m²'")

                elif 'Етаж' in div_text:
                    full_floor_text = div_text.replace('\n', ' ')
                    full_floor_text = re.sub(r'\s+', ' ', full_floor_text).strip()
                    floor_cleaned = re.sub(r'^Етаж:\s*', '', full_floor_text, flags=re.IGNORECASE)
                    data["Етаж"] = clean_text_final(floor_cleaned)
                    print(f"   ✅ ЕТАЖ (без 'Етаж:'): '{floor_cleaned}'")

                elif 'Строителство' in div_text:
                    strong_tags = param_div.find_all('strong')

                    if len(strong_tags) >= 1:
                        first_strong = strong_tags[0].get_text(strip=True).rstrip(', ')
                        data["Тип строителство"] = clean_text_final(first_strong)
                        print(f"   ✅ СТРОИТЕЛСТВО: '{first_strong}'")

                    if len(strong_tags) >= 2:
                        second_strong = strong_tags[1].get_text(strip=True)
                        data["Година на строителство"] = clean_text_final(second_strong)
                        print(f"   ✅ ГОДИНА: '{second_strong}'")

        print("5️⃣ ЦЕНА НА М² (ще бъде формула в Google Sheets):")
        if (data["Цена (EUR)"] != "Не е намерено" and str(data["Цена (EUR)"]).isdigit() and
            data["Площ"] != "Не е намерено" and str(data["Площ"]).isdigit()):
            data["Цена на м² (EUR)"] = "=ROUND(C{row}/D{row},0)"
            print(f"   ✅ Ще се изчисли с формула: =ROUND(C/D,0)")
        else:
            data["Цена на м² (EUR)"] = ""

        print("6️⃣ ТЕЛЕФОН:")
        phone_div = soup.find('div', class_='phone')
        if phone_div:
            phone_text = phone_div.get_text(strip=True)
            phone_text = re.sub(r'Изпрати\s+E-mail\s+на\s+продавача', '', phone_text).strip()
            phone_numbers = re.findall(r'(\d{10})', phone_text)
            if phone_numbers:
                data["Телефон на продавача"] = phone_numbers[0]
                print(f"   ✅ ОТ div.phone: '{phone_numbers[0]}'")
            else:
                data["Телефон на продавача"] = clean_text_final(phone_text)
                print(f"   ✅ ОТ div.phone (текст): '{phone_text}'")
        else:
            phone_candidates = soup.find_all('div', class_=True)
            found_phone = False
            for div in phone_candidates:
                div_classes = ' '.join(div.get('class', []))
                if 'phone' in div_classes.lower():
                    phone_text = div.get_text(strip=True)
                    phone_text = re.sub(r'Изпрати\s+E-mail\s+на\s+продавача', '', phone_text).strip()
                    if phone_text:
                        phone_numbers = re.findall(r'(\d{10})', phone_text)
                        if phone_numbers:
                            data["Телефон на продавача"] = phone_numbers[0]
                            print(f"   ✅ ОТ {div_classes}: '{phone_numbers[0]}'")
                        else:
                            data["Телефон на продавача"] = clean_text_final(phone_text)
                            print(f"   ✅ ОТ {div_classes}: '{phone_text}'")
                        found_phone = True
                        break

            if not found_phone:
                phone_patterns = [r'(0\d{9})']
                all_text = soup.get_text()
                for pattern in phone_patterns:
                    phone_match = re.search(pattern, all_text)
                    if phone_match:
                        found_phone_num = phone_match.group(1)
                        data["Телефон на продавача"] = found_phone_num
                        print(f"   ✅ ОТ regex: '{found_phone_num}'")
                        break

        print("7️⃣ ОПИСАНИЕ:")
        description = extract_full_description(soup)
        data["Описание"] = description

        print("8️⃣ ПРОДАВАЧ:")
        agency_divs = soup.find_all('div', class_='agency')
        if agency_divs:
            for agency_div in agency_divs:
                agency_text = agency_div.get_text().strip()
                if agency_text:
                    data["Продавач"] = clean_text_final(agency_text[:100])
                    print(f"   ✅ Продавач: {data['Продавач']}")
                    break

        print("9️⃣ ПОДГОТОВКА ЗА ЛИНКОВЕ:")
        data["Скрийншот"] = ""
        data["URL за анализ"] = url
        print(f"   ⏳ Скрийншот ще се направи след анализа")
        print(f"   ✅ URL в колона R: {url}")

        print(f"\n📊 ФИНАЛЕН РЕЗУЛТАТ ОТ ПОДОБРЕНИЯ АНАЛИЗ:")
        for key, value in data.items():
            status = "✅" if value not in ["Не е намерено", "Не е указано", ""] else "❌"
            display_value = str(value)[:50] + "..." if len(str(value)) > 50 else str(value)
            print(f"   {status} {key}: '{display_value}'")

        return {"success": True, "data": data}

    except Exception as e_analyze:
        print(f"❌ Грешка в анализа за {url}: {e_analyze}")
        print(traceback.format_exc())
        data["URL за анализ"] = url
        return {"success": False, "error": f"Грешка при анализ: {e_analyze}", "data": data}

def extract_ad_links_from_catalog(catalog_url):
    # Оригинална функция
    print(f"\n🔍 ИЗВЛИЧАМ ЛИНКОВЕ ОТ КАТАЛОГА: {catalog_url}")

    html_content = fetch_html_final(catalog_url)
    if not html_content:
        print("❌ Не успях да заредя каталогова страница")
        return []

    soup = BeautifulSoup(html_content, 'html.parser')

    ads_container = soup.find('div', class_='ads2023')
    if not ads_container:
        print("❌ Не намерих ads2023 контейнер")
        return []

    ad_links = []
    ad_items = ads_container.find_all('div', class_='item')
    print(f"📋 Намерих {len(ad_items)} обяви в каталога")

    for i, item in enumerate(ad_items):
        try:
            zaglavie_div = item.find('div', class_='zaglavie')
            if zaglavie_div:
                title_link = zaglavie_div.find('a', class_='title saveSlink')
                if title_link and title_link.get('href'):
                    href = title_link.get('href')

                    if href.startswith('//'):
                        full_url = 'https:' + href
                    elif href.startswith('/'):
                        full_url = 'https://www.imot.bg' + href
                    else:
                        full_url = href

                    ad_links.append(full_url)
                    title_text = title_link.get_text(strip=True)
                    print(f"   {i+1}. {title_text[:60]}...")
                else:
                    print(f"   {i+1}. ❌ Няма линк в zaglavie")
            else:
                print(f"   {i+1}. ❌ Няма zaglavie div")

        except Exception as e:
            print(f"   {i+1}. ❌ Грешка при обработка: {e}")

    print(f"✅ Извлечени {len(ad_links)} валидни линка от каталога")
    return ad_links

def find_next_page_url(catalog_url):
    # Оригинална функция
    print(f"\n🔍 ТЪРСЯ СЛЕДВАЩА СТРАНИЦА ОТ: {catalog_url}")

    html_content = fetch_html_final(catalog_url)
    if not html_content:
        print("❌ Не успях да заредя страницата за пагинация")
        return None

    soup = BeautifulSoup(html_content, 'html.parser')

    pagination_wrapper = soup.find('div', class_='pagination-wrapper')
    if not pagination_wrapper:
        print("❌ Не намерих pagination-wrapper")
        return None

    pagination = pagination_wrapper.find('div', class_='pagination')
    if not pagination:
        print("❌ Не намерих pagination div")
        return None

    next_link = pagination.find('a', class_='next')
    if not next_link:
        all_links = pagination.find_all('a')
        for link in all_links:
            if 'напред' in link.get_text().lower():
                next_link = link
                break

    if next_link and next_link.get('href'):
        next_href = next_link.get('href')

        if next_href.startswith('//'):
            next_url = 'https:' + next_href
        elif next_href.startswith('/'):
            next_url = 'https://www.imot.bg' + next_href
        else:
            next_url = next_href

        print(f"✅ Намерих следваща страница: {next_url}")
        return next_url
    else:
        print("✅ Няма следваща страница - достигнах последната")
        return None

def check_sheet_access(spreadsheet_name):
    # Оригинална функция
    print(f"\n📋 ПРОВЕРКА НА ДОСТЪПА ДО ТАБЛИЦАТА '{spreadsheet_name}'")

    try:
        # Използваме пътя към временния service_account.json
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE_PATH)
        spreadsheet = gc.open(spreadsheet_name)
        worksheet = spreadsheet.sheet1

        print(f"✅ Таблицата '{spreadsheet_name}' е достъпна")
        print(f"📝 Активен лист: '{worksheet.title}'")

        all_values = worksheet.get_all_values()
        print(f"📊 Текущи редове в таблицата: {len(all_values)}")

        return True

    except Exception as e:
        print(f"❌ Грешка при достъп до таблицата: {e}")
        traceback.print_exc()
        return False

def save_data_to_sheets(spreadsheet_name, data_list):
    # Оригинална функция
    print(f"\n📝 ДОБАВЯНЕ НА ДАННИ В GOOGLE SHEET: '{spreadsheet_name}'")

    if not data_list:
        print("⚠️ НЯМА ДАННИ за запис.")
        return True

    try:
        # Използваме пътя към временния service_account.json
        with open(SERVICE_ACCOUNT_FILE_PATH, 'r') as f:
            sa_info = json.load(f)
        client_email = sa_info.get('client_email')
        print(f"ℹ️ Service account email: {client_email}")

        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE_PATH)
        spreadsheet = gc.open(spreadsheet_name)
        worksheet = spreadsheet.sheet1
        print(f"✅ Отворена таблица '{spreadsheet_name}'")

        all_values = worksheet.get_all_values()
        current_row_count = len(all_values)
        print(f"📊 Текущи редове в таблицата: {current_row_count}")

        exact_column_order = [
            "Тип имот",              # A
            "Квартал",               # B
            "Цена (EUR)",            # C
            "Площ",                  # D
            "Цена на м² (EUR)",      # E
            "Етаж",                  # F
            "Дата на въвеждане",     # G
            "Тип строителство",      # H
            "Година на строителство",# I
            "Особености",            # J
            "Продавач",              # K
            "Телефон на продавача",  # L
            "Описание",              # M
            "Състояние",             # N
            "Скрийншот",             # O
            "Цена на сделката",      # P
            "Коментари",             # Q
            "URL за анализ"          # R
        ]

        print(f"\n🔍 ПРОВЕРКА НА СТРУКТУРАТА:")
        print(f"   Очаквани колони: {len(exact_column_order)} (A-R)")
        for i, col in enumerate(exact_column_order):
            letter = chr(65 + i)
            print(f"   {letter}: {col}")

        rows_to_write = []
        for i, data_dict in enumerate(data_list):
            row = []
            next_row_number = current_row_count + i + 1

            print(f"\n📝 Подготвям ред {i+1} (ще бъде ред {next_row_number} в таблицата):")

            for j, column_name in enumerate(exact_column_order):
                column_letter = chr(65 + j)

                if column_name == "Цена на м² (EUR)":
                    price = data_dict.get("Цена (EUR)", "")
                    area = data_dict.get("Площ", "")
                    if (price not in ["Не е намерено", ""] and
                        area not in ["Не е намерено", ""] and
                        str(price).isdigit() and str(area).isdigit()):
                        value = f"=ROUND(C{next_row_number}/D{next_row_number},0)"
                    else:
                        value = ""
                elif column_name == "Скрийншот":
                    value = str(data_dict.get(column_name, ""))
                    if not value or value == "":
                        value = "❌ Няма скрийншот"
                else:
                    value = str(data_dict.get(column_name, ""))
                    if value == "Не е намерено":
                        value = ""

                row.append(value)

                if j < 8:
                    print(f"   {column_letter} ({column_name}): '{value}'")

            if len(row) != len(exact_column_order):
                print(f"⚠️ ВНИМАНИЕ: Ред {i+1} има {len(row)} колони, очаквани {len(exact_column_order)}")

            rows_to_write.append(row)

        print(f"\nℹ️ Добавям {len(rows_to_write)} ред(а) с ТОЧНО {len(exact_column_order)} колони всеки")
        print(f"📍 Данните ще започнат от колона A и ще завършат в колона R")

        for i, row in enumerate(rows_to_write):
            try:
                worksheet.append_row(row, value_input_option='USER_ENTERED')
                print(f"✅ Записан ред {i+1}")
                time.sleep(0.5)
            except Exception as e_row:
                print(f"❌ Грешка при запис на ред {i+1}: {e_row}")

        time.sleep(2)
        all_values_after = worksheet.get_all_values()
        print(f"✅✅✅ УСПЕШНО ДОБАВЕНИ {len(rows_to_write)} РЕДА!")
        print(f"📊 Общо редове в таблицата сега: {len(all_values_after)}")
        print(f"📍 Данните са в колони A-R (не V!)")

        return True

    except Exception as e_save:
        print(f"❌ КРИТИЧНО: Грешка при запис: {e_save}")
        traceback.print_exc()
        return False

# НОВА КОНСТАНТА: СПИСЪК С ВСИЧКИ КАТАЛОЖНИ URL-И
# (Този списък може да се подава и като променлива на средата, ако е твърде дълъг, но засега го оставям тук)
CATALOG_URLS = [
    "https://www.imot.bg/obiavi/prodazhbi/grad-varna/briz/ednostaen/do-2022?type_home=2~3~4~5~6~8~&raioni=5494~5500~5506~5510~5511~5513~5515~5517~5519~5520~5784~5840~6290~6291~&sort=2",
    "https://www.imot.bg/obiavi/prodazhbi/grad-varna/avtogara/ednostaen/do-2022?type_home=2~3~4~5~6~8~&raioni=5502~5505~5512~5516~5518~5784~5786~5847~6183~6186~&sort=2",
    "https://www.imot.bg/obiavi/prodazhbi/grad-varna/gratska-mahala/ednostaen/do-2022?type_home=2~3~4~5~6~8~&raioni=5848~6183~6185~6186~6292~&sort=2",
    "https://www.imot.bg/obiavi/prodazhbi/grad-varna/troshevo/ednostaen/do-2022?type_home=2~3~4~5~6~8~&raioni=5783~5787~5842~5843~5844~5850~&sort=2",
    "https://www.imot.bg/obiavi/prodazhbi/grad-varna/kaysieva-gradina/ednostaen/do-2022?type_home=2~3~4~5~6~8~&raioni=5782~5853~6289~&sort=2",
    "https://www.imot.bg/obiavi/prodazhbi/grad-varna/m-t-evksinograd/ednostaen/do-2022?type_home=2~3~4~5~6~8~&raioni=5040~5042~5053~5056~5493~6297~&sort=2",
    "https://www.imot.bg/obiavi/prodazhbi/grad-varna/asparuhovo/ednostaen/do-2022?type_home=2~3~4~5~6~8~&raioni=5496~&sort=2"
]

async def process_single_catalog(catalog_url, catalog_index, total_catalogs, existing_urls, google_sheet_name):
    # Оригинална функция
    catalog_name = get_catalog_name_from_url(catalog_url)

    print(f"\n" + "="*80)
    print(f"🏪 ЗАПОЧВАМ КАТАЛОГ {catalog_index}/{total_catalogs}: {catalog_name}")
    print(f"🔗 {catalog_url}")
    print("="*80)

    current_catalog_url = catalog_url
    page_number = 1
    catalog_processed_ads = 0
    catalog_skipped_ads = 0

    accumulated_data = []
    batch_counter = 0

    while current_catalog_url:
        print(f"\n" + "-"*60)
        print(f"📄 КАТАЛОГ {catalog_name} - СТРАНИЦА {page_number}")
        print(f"🔗 {current_catalog_url}")
        print("-"*60)

        ad_links = extract_ad_links_from_catalog(current_catalog_url)

        if not ad_links:
            print(f"⚠️ Няма обяви на страница {page_number} от каталог '{catalog_name}' - преминавам към следващата")
            current_catalog_url = find_next_page_url(current_catalog_url)
            page_number += 1
            continue

        print(f"\n🔄 ПРОВЕРКА ЗА ДУБЛИРАНИ URL-И НА СТРАНИЦА {page_number} ОТ '{catalog_name}':")
        new_ad_links = []
        skipped_count = 0

        for i, ad_url in enumerate(ad_links):
            if is_url_already_processed(ad_url, existing_urls):
                print(f"   ⏭️ {i+1}. ПРОПУСКАМ (вече е обработена): {ad_url[:60]}...")
                skipped_count += 1
                catalog_skipped_ads += 1
            else:
                print(f"   ✅ {i+1}. НОВА ОБЯВА: {ad_url[:60]}...")
                new_ad_links.append(ad_url)

        print(f"\n📊 РЕЗУЛТАТ ОТ ФИЛТРИРАНЕТО НА СТРАНИЦА {page_number} ОТ '{catalog_name}':")
        print(f"   📋 Общо обяви в каталога: {len(ad_links)}")
        print(f"   ⏭️ Прескочени (вече съществуват): {skipped_count}")
        print(f"   ✅ Нови за обработка: {len(new_ad_links)}")

        if not new_ad_links:
            print(f"⚠️ Няма нови обяви на страница {page_number} от '{catalog_name}' - преминавам към следващата")
            current_catalog_url = find_next_page_url(current_catalog_url)
            page_number += 1
            continue

        print(f"\n🔄 ОБРАБОТВАМ {len(new_ad_links)} НОВИ ОБЯВИ ОТ СТРАНИЦА {page_number} НА '{catalog_name}'")

        for index, ad_url in enumerate(new_ad_links):
            print(f"\n" + "."*40)
            print(f"🔄 КАТАЛОГ '{catalog_name}' - ОБЯВА {index + 1}/{len(new_ad_links)} ОТ СТРАНИЦА {page_number}")
            print(f"📊 BATCH: {len(accumulated_data)}/{BATCH_SIZE}")
            print(f"🔗 {ad_url}")
            print("."*40)

            html_content = fetch_html_final(ad_url)
            if not html_content:
                print(f"❌ Не можах да извлека HTML от обявата")
                error_data = {
                    "Тип имот": "HTML грешка", "Квартал": "HTML грешка",
                    "Цена (EUR)": "HTML грешка", "Площ": "HTML грешка",
                    "Цена на м² (EUR)": "", "Етаж": "HTML грешка",
                    "Дата на въвеждане": datetime.datetime.now().strftime("%d/%m/%Y"),
                    "Тип строителство": "HTML грешка", "Година на строителство": "HTML грешка",
                    "Особености": "HTML грешка", "Продавач": "HTML грешка",
                    "Телефон на продавача": "HTML грешка", "Описание": "HTML грешка",
                    "Състояние": "HTML грешка",
                    "Скрийншот": "❌ Няма HTML за скрийншот" if SCREENSHOTS_FOLDER_ID else "⚠️ Няма папка за скрийншоти",
                    "Цена на сделката": "", "Коментари": "", "URL за анализ": ad_url
                }
                accumulated_data.append(error_data)
                existing_urls.add(ad_url)
            else:
                analysis_result = analyze_property_complete(ad_url, html_content)
                property_data = analysis_result["data"]

                if not analysis_result.get("success"):
                    print(f"❌ Частична грешка при анализ на обявата")

                print(f"\n📸 ПРАВЯ СКРИЙНШОТ И КАЧВАМ В ПАПКА ScreenShotsScraper: {ad_url}")

                if SCREENSHOTS_FOLDER_ID:
                    screenshot_link = await screenshot_final(ad_url)

                    if screenshot_link:
                        property_data["Скрийншот"] = f'=HYPERLINK("{screenshot_link}","📸 Виж скрийншот")'
                        print(f"✅ Скрийншот hyperlink готов: {screenshot_link}")
                    else:
                        property_data["Скрийншот"] = "❌ Грешка при скрийншот"
                        print(f"❌ Не успях да направя скрийншот")
                else:
                    print("⚠️ Няма папка за скрийншоти - прескачам скрийншота")
                    property_data["Скрийншот"] = "⚠️ Няма папка за скрийншоти"

                accumulated_data.append(property_data)
                existing_urls.add(ad_url)

            catalog_processed_ads += 1

            if len(accumulated_data) >= BATCH_SIZE:
                batch_counter += 1
                print(f"\n" + "🚀"*60)
                print(f"📊 ЗАПИСВАМ BATCH #{batch_counter} - {len(accumulated_data)} ОБЯВИ В GOOGLE SHEETS")
                print("🚀"*60)

                success = save_data_to_sheets(google_sheet_name, accumulated_data)

                if success:
                    print(f"✅ Успешно записах batch #{batch_counter} с {len(accumulated_data)} обяви")
                    accumulated_data = []
                else:
                    print(f"❌ Грешка при запис на batch #{batch_counter}")

            if index < len(new_ad_links) - 1:
                delay_seconds = random.uniform(0, 1)
                print(f"\n⏳ Изчаквам {delay_seconds:.1f} сек. преди следващата обява...")
                await asyncio.sleep(delay_seconds)

        print(f"\n🔍 ТЪРСЯ СЛЕДВАЩА СТРАНИЦА СЛЕД {page_number} В КАТАЛОГ '{catalog_name}'...")
        next_catalog_url = find_next_page_url(current_catalog_url)

        if next_catalog_url:
            current_catalog_url = next_catalog_url
            page_number += 1
            page_delay = random.uniform(3, 8)
            print(f"⏳ Изчаквам {page_delay:.1f} сек. преди следващата страница...")
            await asyncio.sleep(page_delay)
        else:
            print(f"✅ Няма повече страници в каталог '{catalog_name}' - приключих с този каталог!")
            current_catalog_url = None

    if accumulated_data:
        batch_counter += 1
        print(f"\n" + "🏁"*60)
        print(f"📊 ЗАПИСВАМ ФИНАЛЕН BATCH #{batch_counter} - {len(accumulated_data)} ОСТАНАЛИ ОБЯВИ ЗА '{catalog_name}'")
        print("🏁"*60)

        success = save_data_to_sheets(google_sheet_name, accumulated_data)

        if success:
            print(f"✅ Успешно записах финален batch #{batch_counter} с {len(accumulated_data)} обяви за '{catalog_name}'")
        else:
            print(f"❌ Грешка при запис на финален batch #{batch_counter} за '{catalog_name}'")

    print(f"\n" + "="*80)
    print(f"🏁 ЗАВЪРШИХ КАТАЛОГ {catalog_index}/{total_catalogs}: {catalog_name}")
    print(f"📊 Обработени нови обяви от този каталог: {catalog_processed_ads}")
    print(f"⏭️ Прескочени обяви от този каталог: {catalog_skipped_ads}")
    print(f"📄 Обработени страници от този каталог: {page_number}")
    print(f"📦 Общо batch записвания за този каталог: {batch_counter}")
    print("="*80)

    return catalog_processed_ads, catalog_skipped_ads, page_number

async def main_catalog_ultimate():
    """ГЛАВНА ФУНКЦИЯ ЗА ИЗВЛИЧАНЕ ОТ МНОЖЕСТВО КАТАЛОЗИ С ПРОВЕРКА ЗА ДУБЛИРАНИ URL-И"""
    print("🚀 СТАРТИРАНЕ НА МУЛТИКАТАЛОЖЕН КОД - АВТОМАТИЧНО ИЗВЛИЧАНЕ!")
    print("="*80)

    # Записваме service account JSON от променливата на средата във временен файл
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        try:
            with open(SERVICE_ACCOUNT_FILE_PATH, 'w') as f:
                json.dump(json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), f)
            print(f"✅ Service account JSON записан във временен файл: {SERVICE_ACCOUNT_FILE_PATH}")
            os.chmod(SERVICE_ACCOUNT_FILE_PATH, 0o600) # Задаване на правилни пермишъни
        except Exception as e:
            print(f"❌ Грешка при записване на service account JSON: {e}")
            print("❗ Скриптът може да не може да достъпи Google Sheets/Drive без това.")
            return # Прекъсваме изпълнението, ако не можем да настроим Service Account

    # ПЪРВО - проверяваме достъпа до таблицата
    print("\n🛠️ ПРОВЕРКА НА ДОСТЪПА ДО ТАБЛИЦАТА...")
    if not check_sheet_access(GOOGLE_SHEET_NAME):
        print("❌ ГРЕШКА ПРИ ДОСТЪП ДО ТАБЛИЦАТА!")
        return

    # НОВО: Намираме папката ScreenShotsScraper в Google Drive
    print("\n📁 НАСТРОЙКА НА ПАПКА ЗА СКРИЙНШОТИ...")
    screenshots_folder_id = find_screenshots_folder_id()
    if not screenshots_folder_id:
        print("⚠️ ПРЕДУПРЕЖДЕНИЕ: Не можах да настроя папка за скрийншоти!")
        print("⚠️ Скрийншотите няма да се правят, но данните ще се извличат!")
    else:
        print(f"✅ Папката за скрийншоти е готова с ID: {screenshots_folder_id}")

    print("\n🔍 ИЗВЛИЧАМ СЪЩЕСТВУВАЩИ URL-И ЗА ПРОВЕРКА НА ДУБЛИРАНИ...")
    existing_urls = get_existing_urls_from_sheet(GOOGLE_SHEET_NAME)
    print(f"📋 Заредих {len(existing_urls)} съществуващи URL-и за проверка")

    print(f"\n📋 ПЛАН ЗА ОБРАБОТКА НА {len(CATALOG_URLS)} КАТАЛОГА:")
    for i, catalog_url in enumerate(CATALOG_URLS, 1):
        catalog_name = get_catalog_name_from_url(catalog_url)
        print(f"   {i}. {catalog_name}")

    total_processed_ads = 0
    total_skipped_ads = 0
    total_pages = 0

    for catalog_index, catalog_url in enumerate(CATALOG_URLS, 1):
        try:
            catalog_stats = await process_single_catalog(
                catalog_url,
                catalog_index,
                len(CATALOG_URLS),
                existing_urls,
                GOOGLE_SHEET_NAME
            )

            catalog_processed, catalog_skipped, catalog_pages = catalog_stats
            total_processed_ads += catalog_processed
            total_skipped_ads += catalog_skipped
            total_pages += catalog_pages

            if catalog_index < len(CATALOG_URLS):
                catalog_name = get_catalog_name_from_url(catalog_url)
                next_catalog_name = get_catalog_name_from_url(CATALOG_URLS[catalog_index])

                catalog_delay = random.uniform(5, 15)
                print(f"\n⏳ ИЗЧАКВАМ {catalog_delay:.1f} СЕК. ПРЕДИ СЛЕДВАЩИЯ КАТАЛОГ...")
                print(f"🔄 СЛЕДВАЩ КАТАЛОГ: {next_catalog_name}")
                await asyncio.sleep(catalog_delay)

        except Exception as e:
            catalog_name = get_catalog_name_from_url(catalog_url)
            print(f"\n❌ КРИТИЧНА ГРЕШКА В КАТАЛОГ '{catalog_name}': {e}")
            print(traceback.format_exc())
            print(f"⏭️ ПРЕМИНАВАМ КЪМ СЛЕДВАЩИЯ КАТАЛОГ...")
            continue

    print(f"\n" + "="*80)
    print(f"🏁🎉 ЗАВЪРШИХ ИЗВЛИЧАНЕТО ОТ ВСИЧКИ {len(CATALOG_URLS)} КАТАЛОГА! 🎉🏁")
    print("="*80)
    print(f"📊 ОБЩИ СТАТИСТИКИ:")
    print(f"   ✅ Общо НОВИ обработени обяви: {total_processed_ads}")
    print(f"   ⏭️ Общо ПРЕСКОЧЕНИ обяви (дублирани): {total_skipped_ads}")
    print(f"   📄 Общо обработени страници: {total_pages}")
    print(f"   🏪 Общо обработени каталози: {len(CATALOG_URLS)}")
    print(f"   📦 ОПТИМИЗАЦИЯ: Batch размер {BATCH_SIZE} обяви = ~{13}x по-бърз запис!")
    print(f"📋 Проверете таблицата '{GOOGLE_SHEET_NAME}' в Google Sheets")
    print(f"✅ Всички нови данни са добавени към съществуващите записи")
    print(f"✅ Колона B: без 'град Варна, '")
    print(f"✅ Колона E: формула =ROUND(C/D,0)")
    print(f"✅ Колона F: без 'Етаж:'")
    print(f"✅ Колона O: {'кликаеми линкове към СКРИЙНШОТИ в папка ScreenShotsScraper' if SCREENSHOTS_FOLDER_ID else 'НЯМА СКРИЙНШОТИ (проблем с папката)'}")
    print(f"✅ Колона R: URL адреси")
    if SCREENSHOTS_FOLDER_ID:
        print(f"📸 Скрийншотите са качени в папка 'ScreenShotsScraper' в Google Drive")
    else:
        print(f"⚠️ СКРИЙНШОТИТЕ НЕ СА НАПРАВЕНИ поради проблем с папката в Google Drive")
    print(f"🔄 АВТОМАТИЧНА ПРОВЕРКА ЗА ДУБЛИРАНИ URL-И РАБОТИ ПЕРФЕКТНО!")
    print(f"🚀 BATCH ЛОГИКА: {BATCH_SIZE} обяви наведнъж = ЗНАЧИТЕЛНО ПО-БЪРЗ ЗАПИС!")

    print(f"\n🏪 ОБРАБОТЕНИ КАТАЛОЗИ:")
    for i, catalog_url in enumerate(CATALOG_URLS, 1):
        catalog_name = get_catalog_name_from_url(catalog_url)
        print(f"   ✅ {i}. {catalog_name}")

    print("="*80)

def run_scraper_scheduled():
    """Функция, която ще се изпълнява от планировчика"""
    print(f"\n--- СТАРТИРАНЕ НА ПЛАНИРАНА ЗАДАЧА ЗА СКРЕЙПВАНЕ (на всеки {SCHEDULE_HOURS} часа) ---")
    asyncio.run(main_catalog_ultimate())
    print(f"--- ПЛАНИРАНА ЗАДАЧА ЗА СКРЕЙПВАНЕ ПРИКЛЮЧИ ---")

if __name__ == "__main__":
    print("🆕 СТАРТИРАМ ГЛАВНО ПРИЛОЖЕНИЕ ЗА ELTEST.IO / DOCKER!")
    print("📁 СКРИЙНШОТИТЕ ЩЕ СЕ ЗАПИСВАТ В ПАПКА ScreenShotsScraper В GOOGLE DRIVE!")

    # Стартираме Flask сървъра в отделна нишка
    print(f"🌐 Стартирам Flask healthcheck сървър на порт {WEB_SERVER_PORT}...")
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=WEB_SERVER_PORT, debug=False, use_reloader=False)).start()

    # Изпълняваме скрипта веднага при стартиране
    print("\n--- ИЗПЪЛНЯВАМ СКРЕЙПЪРА ВЕДНАГА ПРИ СТАРТ ---")
    asyncio.run(main_catalog_ultimate())

    # След това планираме изпълнението
    print(f"\n--- НАСТРОЙВАМ ПЛАНИРАНЕ НА СКРЕЙПЪРА НА ВСЕКИ {SCHEDULE_HOURS} ЧАСА ---")
    schedule.every(SCHEDULE_HOURS).hours.do(run_scraper_scheduled)

    # Безкраен цикъл за планировчика
    while True:
        schedule.run_pending()
        time.sleep(1) # Проверява на всяка секунда
```
