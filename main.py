import os
import json
import re
import threading
import holidays
from datetime import datetime, timedelta, timezone
import requests
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.exceptions import RequestValidationError
from google import genai
from google.genai import types
from PIL import Image
from json import JSONDecodeError

# --- 설정 및 초기화 ---
app = FastAPI()

current_year = datetime.now().year
kr_holidays = holidays.KR(years=[current_year, current_year + 1])

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "default")
client = genai.Client(api_key=GOOGLE_API_KEY)
MODEL_NAME = 'gemini-flash-latest'
JSON_FILE_PATH = "current_menu.json"

KST = timezone(timedelta(hours=9))
is_updating = False
update_lock = threading.Lock()

# --- 유틸리티 함수 ---
def parse_date_range_from_title(title):
    """제목에서 (MMDD-MMDD) 형태의 날짜 범위를 추출하여 date 객체 리스트로 반환"""
    match = re.search(r'\((\d{4})-(\d{4})\)', title)
    if not match:
        return None
    
    start_str, end_str = match.groups()
    try:
        current_yr = datetime.now(KST).year
        start_date = datetime.strptime(f"{current_yr}{start_str}", "%Y%m%d").date()
        end_date = datetime.strptime(f"{current_yr}{end_str}", "%Y%m%d").date()
        
        # 연도 교체기(12월-1월) 대응
        if end_date < start_date:
            end_date = end_date.replace(year=current_yr + 1)
            
        return start_date, end_date
    except:
        return None

def check_date_exists_in_notices(target_date):
    """게시판 리스트를 조회하여 target_date가 포함된 게시글이 있는지 확인"""
    headers = {"User-Agent": "Mozilla/5.0"}
    list_url = "https://medicine.korea.ac.kr/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=10"
    
    try:
        res = requests.get(list_url, headers=headers, timeout=5)
        list_data = res.json()
        
        count = 0
        for article in list_data.get("list", []):
            title = article.get("title", "")
            if "식단표" in title:
                date_range = parse_date_range_from_title(title)
                print(f"Extracted date range: {date_range}")
                if date_range:
                    start, end = date_range
                    if start <= target_date.date() <= end:
                        return True
                count += 1
                if count == 2: break # 상위 2개만 확인
    except Exception as e:
        print(f"리스트 확인 중 오류: {e}")
    return False

# --- 코어 로직 ---
def update_menu_data():
    """크롤링 및 OCR 후 JSON 업데이트 (지난 날짜 삭제 포함)"""
    global is_updating
    print(f"[{datetime.now()}] 🔄 백그라운드 업데이트 시작...")
    headers = {"User-Agent": "Mozilla/5.0"}
    list_url = "https://medicine.korea.ac.kr/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=10"
    
    try:
        res_list = requests.get(list_url, headers=headers)
        list_data = res_list.json()
        target_article_nos = [a.get("articleNo") for a in list_data.get("list", []) if "식단표" in a.get("title", "")][:2]

        new_menus = {}
        for article_no in target_article_nos:
            # ... (기존 이미지 다운로드 및 Gemini OCR 로직 동일) ...
            # 생략된 부분은 기존 코드의 OCR 호출 및 menu_data 추출 로직을 그대로 사용합니다.
            detail_url = f"https://medicine.korea.ac.kr/api/article/157/{article_no}?instNo=4&boardNo=157&articleNo={article_no}"
            res_detail = requests.get(detail_url, headers=headers)
            content_html = res_detail.json().get("content", "")
            img_match = re.search(r'<img[^>]+src="([^">]+)"', content_html)
            
            if img_match:
                img_path = img_match.group(1) if img_match.group(1).startswith("/") else "/" + img_match.group(1)
                img_res = requests.get(f"https://medicine.korea.ac.kr{img_path}", headers=headers)
                temp_img = f"temp_{article_no}.jpg"
                with open(temp_img, 'wb') as f: f.write(img_res.content)
                
                # Gemini OCR (기존 프롬프트 활용)
                img = Image.open(temp_img)
                prompt = """
                당신은 데이터 추출 전문가입니다. 주간 식단표 이미지에서 데이터를 추출하세요.
                1. 정중앙의 '1페이지' 워터마크 무시
                2. 파란색 칼로리(kcal) 수치 추출
                3. 아래 JSON 구조로 출력:
                {
                    "daily_menus": {
                        "04월 13일 월요일": {
                        "lunch_korean": {"items": ["메뉴1", "메뉴2"], "calories": 989, "price": 6000},
                        "lunch_international": {"items": ["메뉴"], "calories": 1000, "price": 7500},
                        "dinner_korean": {"items": ["메뉴"], "calories": 800, "price": 6000}
                        }
                    }
                }
                빈 식단은 items에 ["미운영"] 삽입, calories는 null 처리.
                """
                response = client.models.generate_content(
                    model=MODEL_NAME, contents=[img, prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                try:
                    extracted = json.loads(response.text)
                    new_menus.update(extracted.get("daily_menus", {}))
                except: pass
                os.remove(temp_img)

        # 기존 데이터 로드 및 병합
        existing_data = {"daily_menus": {}}
        if os.path.exists(JSON_FILE_PATH):
            with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
        
        all_menus = existing_data.get("daily_menus", {})
        all_menus.update(new_menus)

        # 지난 날짜 삭제 로직
        today = datetime.now(KST).date()
        current_yr = today.year
        cleaned_menus = {}
        
        for key, value in all_menus.items():
            try:
                # "04월 13일 월요일" 형식 파싱
                date_part = key.split(" ")[0] + key.split(" ")[1] # "04월13일"
                dt = datetime.strptime(f"{current_yr}{date_part}", "%Y%m월%d일").date()
                if dt >= today:
                    cleaned_menus[key] = value
            except:
                cleaned_menus[key] = value # 파싱 실패시 일단 유지

        with open(JSON_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump({"daily_menus": cleaned_menus}, f, indent=2, ensure_ascii=False)
            
    finally:
        with update_lock:
            is_updating = False

def generate_kakao_response(days_offset: int, background_tasks: BackgroundTasks):
    global is_updating
    target_date = datetime.now(KST) + timedelta(days=days_offset)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    target_key = f"{target_date.month:02d}월 {target_date.day:02d}일 {weekdays[target_date.weekday()]}요일"

    if target_date.weekday() >= 5 or target_date.date() in kr_holidays:
        return simple_text_response("🛋️  주말이거나 공휴일이에요.")

    # 1. JSON 확인
    menu_data = {}
    if os.path.exists(JSON_FILE_PATH):
        with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
            menu_data = json.load(f)
    
    today_menu = menu_data.get("daily_menus", {}).get(target_key)
    if today_menu:
        return format_menu_text(target_key, today_menu)

    # 2. JSON에 없을 경우 게시판 리스트 확인
    exists_in_notices = check_date_exists_in_notices(target_date)
    
    if exists_in_notices:
        with update_lock:
            if not is_updating:
                is_updating = True
                background_tasks.add_task(update_menu_data)
        return simple_text_response("🔄 서버가 최신 식단표를 업데이트 중이에요.\n1~2분 뒤에 다시 시도해 주세요!")
    else:
        return simple_text_response(f"❌ {target_key} 식단은 아직 업로드되지 않았어요.")

def simple_text_response(text):
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def format_menu_text(date_key, menu):
    # 기존 response_text 생성 로직과 동일
    res = f"🍽️  {date_key} 학식\n\n"
    for k, label in [("lunch_korean", "🍚 점심(한식)"), ("lunch_international", "🍝 점심(인터)"), ("dinner_korean", "🥘 저녁")]:
        if k in menu:
            m = menu[k]
            items = ", ".join(m.get("items", []))
            res += f"{label}\n{items}\n"
            if "미운영" not in items and m.get("calories"):
                res += f"({m['calories']} kcal)\n\n"
            else: res += "\n"
    return simple_text_response(res.strip())

# --- 카카오톡 챗봇 API 엔드포인트 ---
@app.post("/api/menu_new")
async def get_menu_with_offset(request: Request, background_tasks: BackgroundTasks):
    try:
        try:
            data = await request.json()
        except JSONDecodeError:
            return simple_text_response("잘못된 요청 형식입니다. 다시 시도해주세요.")

        days_offset = data.get("days_offset")

        if days_offset is None:
            days_offset = 0
        elif not isinstance(days_offset, int):
            try:
                days_offset = int(days_offset)
            except (ValueError, TypeError):
                return simple_text_response("날짜 형식이 올바르지 않습니다.")

        if not (0 <= days_offset <= 5):
            return simple_text_response("조회 가능한 날짜 범위를 벗어났습니다. (0~5일 사이)")

        return generate_kakao_response(days_offset, background_tasks)

    except Exception as e:
        print(f"Unexpected Error: {e}") 
        return simple_text_response("식단을 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    
# --- legacy API 엔드포인트 (호환성 유지용) ---
@app.post("/api/menu")
async def get_menu_chatbot(request: Request, background_tasks: BackgroundTasks):
    """오늘 식단"""
    return generate_kakao_response(0, background_tasks)

@app.post("/api/menu_tm1")
async def get_menu_tm1_chatbot(request: Request, background_tasks: BackgroundTasks):
    """내일 식단"""
    return generate_kakao_response(1, background_tasks)

@app.post("/api/menu_tm2")
async def get_menu_tm2_chatbot(request: Request, background_tasks: BackgroundTasks):
    """내일 모레 식단"""
    return generate_kakao_response(2, background_tasks)

@app.get("/api/showjson")
async def get_show_json(request: Request):
    """디버깅용 json 전체 보기"""
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
