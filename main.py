import os
import json
import re
import io
import threading
import holidays
from datetime import datetime, timedelta, timezone
import requests
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from google import genai
from google.genai import types
from PIL import Image

# --- 설정 및 초기화 ---
app = FastAPI()

current_year = datetime.now().year
kr_holidays = holidays.KR(years=[current_year, current_year + 1])
last_holiday_check = 2026

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "default")
client = genai.Client(api_key=GOOGLE_API_KEY)
MODEL_NAME = 'gemini-flash-latest'
JSON_FILE_PATH = "current_menu.json"

KST = timezone(timedelta(hours=9))
is_updating = False
update_lock = threading.Lock()

# --- 유틸리티 함수 ---
def parse_date_range_from_title(title):
    match = re.search(r'\((\d{4})-(\d{4})\)', title)
    if not match:
        return None
    
    start_str, end_str = match.groups()
    try:
        now = datetime.now(KST)
        current_yr = now.year
        start_month = int(start_str[:2])
        
        if now.month == 1 and start_month == 12:
            base_yr = current_yr - 1
        elif now.month == 12 and start_month == 1:
            base_yr = current_yr + 1
        else:
            base_yr = current_yr

        start_date = datetime.strptime(f"{base_yr}{start_str}", "%Y%m%d").date()
        end_date = datetime.strptime(f"{base_yr}{end_str}", "%Y%m%d").date()
        
        if end_date < start_date:
            end_date = end_date.replace(year=end_date.year + 1)
            
        return start_date, end_date
    except:
        return None

def format_date_to_korean(date_str):
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        weekdays = ["월", "화", "수", "목", "금", "토", "일"]
        return f"{date_obj.month:02d}월 {date_obj.day:02d}일 {weekdays[date_obj.weekday()]}요일"
    except:
        return date_str

def unix_timestamp_to_date_str(timestamp_ms):
    try:
        date_obj = datetime.fromtimestamp(timestamp_ms / 1000, tz=KST).date()
        return date_obj.strftime("%Y-%m-%d")
    except:
        return None

def check_date_exists_in_notices(target_date):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    list_url = "https://medicine.korea.ac.kr/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=4&title=식단"
    
    try:
        res = requests.get(list_url, headers=headers, timeout=5)
        if res.status_code != 200:
            print(f"⚠️ 공지사항 접근 실패 (상태 코드: {res.status_code})")
            return False
            
        list_data = res.json()       
        count = 0
        for article in list_data.get("list", []):
            title = article.get("title", "")
            if "식단표" in title:
                date_range = parse_date_range_from_title(title)
                if date_range:
                    start, end = date_range
                    if start <= target_date.date() <= end:
                        return True
                count += 1
                if count == 2: break
    except Exception as e:
        print(f"리스트 확인 중 오류: {e}")
    return False

# --- 코어 로직 ---
def update_menu_data():
    global is_updating
    print(f"[{datetime.now()}] 🔄 백그라운드 업데이트 시작...")

    # 메뉴 가져오기
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    list_url = "https://medicine.korea.ac.kr/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=4&title=식단"

    try:
        print(f"[{datetime.now()}] 목록 API 요청 시작: {list_url}")
        res_list = requests.get(list_url, headers=headers, timeout=10)
        if res_list.status_code != 200:
            print(f"⚠️ 목록 불러오기 실패 (상태 코드: {res_list.status_code})")
            return
            
        try:
            list_data = res_list.json()
            print(f"[{datetime.now()}] 목록 JSON 파싱 성공, 총 항목: {len(list_data.get('list', []))}")
        except Exception:
            print("⚠️ API 응답이 JSON 형식이 아닙니다. 사이트가 HTML 에러 페이지를 반환했을 수 있습니다.")
            print(f"응답 내용 일부: {res_list.text[:300]}")
            return

        target_article_nos = [a.get("articleNo") for a in list_data.get("list", []) if "식단표" in a.get("title", "")][:2]
        print(f"[{datetime.now()}] 대상 식단표 articleNo: {target_article_nos}")

        new_menus = {}
        for article_no in target_article_nos:
            print(f"[{datetime.now()}] articleNo {article_no} 처리 시작")
            detail_url = f"https://medicine.korea.ac.kr/api/article/157/{article_no}?instNo=4&boardNo=157&articleNo={article_no}"
            res_detail = requests.get(detail_url, headers=headers)
            if res_detail.status_code != 200:
                print(f"⚠️ 상세 조회 실패 articleNo={article_no}, 상태 코드={res_detail.status_code}")
                continue
            
            try:
                content_html = res_detail.json().get("content", "")
                print(f"[{datetime.now()}] 상세 JSON 파싱 성공 articleNo={article_no}, content 길이={len(content_html)}")
            except Exception as e:
                print(f"⚠️ 상세 JSON 파싱 실패 articleNo={article_no}: {e}")
                continue

            img_match = re.search(r'<img[^>]+src="([^">]+)"', content_html)
            if not img_match:
                print(f"⚠️ 이미지 태그를 찾을 수 없음 articleNo={article_no}")
                continue
            
            img_path = img_match.group(1) if img_match.group(1).startswith("/") else "/" + img_match.group(1)
            print(f"[{datetime.now()}] 이미지 경로 발견 articleNo={article_no}: {img_path}")
            img_res = requests.get(f"https://medicine.korea.ac.kr{img_path}", headers=headers)
            print(f"[{datetime.now()}] 이미지 다운로드 상태(articleNo={article_no}): {img_res.status_code}")

            img = Image.open(io.BytesIO(img_res.content))

            # 게시물 업로드 날짜 추출
            article = next((a for a in list_data.get("list", []) if a.get("articleNo") == article_no), {})
            created_dt = article.get("createdDt")
            upload_date = unix_timestamp_to_date_str(created_dt) if created_dt else None
            
            print(f"[{datetime.now()}] Gemini 처리 시작 : {img_path}")
            prompt = f"""
                당신은 데이터 추출 전문가입니다. 주간 식단표 이미지에서 데이터를 추출하세요.
                1. 정중앙의 '1페이지' 워터마크 무시
                2. 파란색 칼로리(kcal) 수치 추출
                3. 날짜는 반드시 YYYY-MM-DD 형식으로 변환 (예: 2026-04-13)
                4. 아래 JSON 구조로 출력:
                {{
                    "daily_menus": {{
                        "2026-04-13": {{
                        "lunch_korean": {{"items": ["메뉴1", "메뉴2"], "calories": 989, "price": 6000}},
                        "lunch_international": {{"items": ["메뉴"], "calories": 1000, "price": 7500}},
                        "dinner_korean": {{"items": ["메뉴"], "calories": 800, "price": 6000}}
                        }}
                    }}
                }}
                빈 식단은 items에 ["미운영"] 삽입, calories는 null 처리.
                게시물 업로드 날짜를 고려하세요 : {upload_date}
                """
            response = client.models.generate_content(
                model=MODEL_NAME, contents=[img, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            print(f"[{datetime.now()}] Gemini 응답 수신 articleNo={article_no}, 길이={len(response.text)}")
            try:
                extracted = json.loads(response.text)
                new_menus.update(extracted.get("daily_menus", {}))
                print(f"[{datetime.now()}] 추출 item 수: {len(extracted.get('daily_menus', {}))} (articleNo={article_no})")
            except Exception as e:
                print(f"⚠️ JSON 파싱 실패 articleNo={article_no}: {e}")
                print(f"응답 원문: {response.text[:500]}")

        if not new_menus:
            print(f"[{datetime.now()}] 새로 추출된 메뉴가 없습니다.")

        existing_data = {"daily_menus": {}}
        if os.path.exists(JSON_FILE_PATH):
            try:
                with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
                    print(f"[{datetime.now()}] 기존 JSON 읽기 성공, 메뉴 항목 수: {len(existing_data.get('daily_menus', {}))}")
            except Exception:
                print(f"⚠️ 기존 JSON 파싱 실패, 빈 데이터로 초기화합니다: {JSON_FILE_PATH}")
        
        all_menus = existing_data.get("daily_menus", {})
        all_menus.update(new_menus)
        print(f"[{datetime.now()}] 전체 메뉴 병합 완료, 총 항목 수: {len(all_menus)}")

        now = datetime.now(KST)
        today = now.date()
        cleaned_menus = {}
        
        for key, value in all_menus.items():
            try:
                # yyyy-mm-dd 형식으로 파싱
                dt = datetime.strptime(key, "%Y-%m-%d").date()
                if dt >= today:
                    cleaned_menus[key] = value
            except Exception as e:
                cleaned_menus[key] = value
                print(f"[{datetime.now()}] 키 파싱 실패({key}) - 유지: {e}")

        print(f"[{datetime.now()}] 필터링된 메뉴 수: {len(cleaned_menus)}")
        temp_json_path = JSON_FILE_PATH + ".tmp"
        with open(temp_json_path, 'w', encoding='utf-8') as f:
            json.dump({"daily_menus": cleaned_menus}, f, indent=2, ensure_ascii=False)
        os.replace(temp_json_path, JSON_FILE_PATH)
        print(f"[{datetime.now()}] JSON 파일 저장 완료: {JSON_FILE_PATH}")
            
    finally:
        with update_lock:
            is_updating = False

def generate_kakao_response(days_offset: int, background_tasks: BackgroundTasks):
    global is_updating, last_holiday_check, kr_holidays

    # holiday check (연도 변경시)
    current_year = datetime.now(KST).year
    if(last_holiday_check != current_year):
        kr_holidays = holidays.KR(years=[current_year, current_year + 1])
        last_holiday_check = current_year

    # response 형성부
    target_date = datetime.now(KST) + timedelta(days=days_offset)
    target_key = target_date.strftime("%Y-%m-%d")  # JSON 키는 yyyy-mm-dd 형식

    if target_date.weekday() >= 5 or target_date.date() in kr_holidays:
        return simple_text_response("❗ 주말이거나 공휴일이에요.")

    menu_data = {}
    if os.path.exists(JSON_FILE_PATH):
        try:
            with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
                menu_data = json.load(f)
        except Exception:
            print("⚠️ JSON 파일 읽기 실패. 빈 데이터로 처리합니다.")
            menu_data = {}
    
    today_menu = menu_data.get("daily_menus", {}).get(target_key)
    if today_menu:
        return format_menu_text(target_key, today_menu)

    exists_in_notices = check_date_exists_in_notices(target_date)
    
    if exists_in_notices:
        with update_lock:
            if not is_updating:
                is_updating = True
                background_tasks.add_task(update_menu_data)
        return simple_text_response("🔄 서버가 식단표를 업데이트 중이에요.\n1~2분 뒤에 다시 시도해 주세요!")
    else:
        return simple_text_response(f"❌ {target_key} 식단은 아직 업로드되지 않았어요.")

def simple_text_response(text):
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def format_menu_text(date_key, menu):
    # date_key를 yyyy-mm-dd에서 한글 형식으로 변환
    korean_date = format_date_to_korean(date_key)
    res = f"🍽️  {korean_date} 학식\n\n"
    for k, label in [("lunch_korean", "🍚 점심(한식)"), ("lunch_international", "🍝 점심(인터)"), ("dinner_korean", "🥘 저녁")]:
        if k in menu:
            m = menu[k]
            items = ", ".join(m.get("items", []))
            res += f"{label}\n{items}\n"
            if "미운영" not in items and m.get("calories"):
                res += f"({m['calories']} kcal)\n\n"
            else: res += "\n"
    return simple_text_response(res.strip())

# --- API 엔드포인트 유지 ---
@app.post("/api/menu")
async def get_menu_chatbot(request: Request, background_tasks: BackgroundTasks):
    return generate_kakao_response(0, background_tasks)

@app.post("/api/menu_tm1")
async def get_menu_tm1_chatbot(request: Request, background_tasks: BackgroundTasks):
    return generate_kakao_response(1, background_tasks)

@app.post("/api/menu_tm2")
async def get_menu_tm2_chatbot(request: Request, background_tasks: BackgroundTasks):
    return generate_kakao_response(2, background_tasks)

@app.get("/api/showjson")
async def get_show_json(request: Request):
    menu_data = {}
    if os.path.exists(JSON_FILE_PATH):
        try:
            with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
                menu_data = json.load(f)
                return menu_data
        except Exception as e:
            return {"status": "error", "message": f"파일 읽기/파싱 실패: {e}"}
    return {"status": "error", "message": f"식단 파일이 생성되지 않았습니다."}

@app.head("/api/alive")
async def for_uptime():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    if not os.path.exists(JSON_FILE_PATH):
        update_menu_data()
    uvicorn.run(app, host="0.0.0.0", port=8000)