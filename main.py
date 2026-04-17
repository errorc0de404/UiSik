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
    match = re.search(r'\((\d{4})-(\d{4})\)', title)
    if not match:
        return None
    
    start_str, end_str = match.groups()
    try:
        current_yr = datetime.now(KST).year
        start_date = datetime.strptime(f"{current_yr}{start_str}", "%Y%m%d").date()
        end_date = datetime.strptime(f"{current_yr}{end_str}", "%Y%m%d").date()
        
        if end_date < start_date:
            end_date = end_date.replace(year=current_yr + 1)
            
        return start_date, end_date
    except:
        return None

def check_date_exists_in_notices(target_date):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    list_url = "https://medicine.korea.ac.kr/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=10"
    
    try:
        res = requests.get(list_url, headers=headers, timeout=5)
        # 1. 응답이 정상인지 확인 (IP 차단 방어)
        if res.status_code != 200:
            print(f"⚠️ 사이트 접근 실패 (상태 코드: {res.status_code}) - IP 차단 의심")
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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    list_url = "https://medicine.korea.ac.kr/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=10"
    
    try:
        print(f"[{datetime.now()}] 목록 API 요청 시작: {list_url}")
        res_list = requests.get(list_url, headers=headers, timeout=10)
        if res_list.status_code != 200:
            print(f"⚠️ 백그라운드 업데이트 실패 (상태 코드: {res_list.status_code})")
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

            temp_img = f"temp_{article_no}.jpg"
            with open(temp_img, 'wb') as f:
                f.write(img_res.content)
            print(f"[{datetime.now()}] 임시 이미지 저장 완료: {temp_img}")

            print(f"[{datetime.now()}] Gemini 처리 시작 : {temp_img}")
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
            print(f"[{datetime.now()}] Gemini 응답 수신 articleNo={article_no}, 길이={len(response.text)}")
            try:
                extracted = json.loads(response.text)
                new_menus.update(extracted.get("daily_menus", {}))
                print(f"[{datetime.now()}] 추출 item 수: {len(extracted.get('daily_menus', {}))} (articleNo={article_no})")
            except Exception as e:
                print(f"⚠️ JSON 파싱 실패 articleNo={article_no}: {e}")
                print(f"응답 원문: {response.text[:500]}")
            finally:
                if os.path.exists(temp_img):
                    os.remove(temp_img)
                    print(f"[{datetime.now()}] 임시 이미지 삭제 완료: {temp_img}")

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

        today = datetime.now(KST).date()
        current_yr = today.year
        cleaned_menus = {}
        
        for key, value in all_menus.items():
            try:
                date_part = key.split(" ")[0] + key.split(" ")[1]
                dt = datetime.strptime(f"{current_yr}{date_part}", "%Y%m월%d일").date()
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
    global is_updating
    target_date = datetime.now(KST) + timedelta(days=days_offset)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    target_key = f"{target_date.month:02d}월 {target_date.day:02d}일 {weekdays[target_date.weekday()]}요일"

    if target_date.weekday() >= 5 or target_date.date() in kr_holidays:
        return simple_text_response("🛋️  주말이거나 공휴일이에요.")

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
        return simple_text_response("🔄 서버가 최신 식단표를 업데이트 중이에요.\n1~2분 뒤에 다시 시도해 주세요!")
    else:
        return simple_text_response(f"❌ {target_key} 식단은 아직 업로드되지 않았어요.")

def simple_text_response(text):
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def format_menu_text(date_key, menu):
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

# --- API 엔드포인트 유지 ---
@app.post("/api/menu_new")
async def get_menu_with_offset(request: Request, background_tasks: BackgroundTasks):
    try:
        try:
            data = await request.json()
        except Exception:
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