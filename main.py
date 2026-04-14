import os
import json
import re
import threading
import datetime
from datetime import timedelta, timezone
import requests
from fastapi import FastAPI, Request, BackgroundTasks
import google.generativeai as genai
from PIL import Image

app = FastAPI()

# --- 설정 및 초기화 ---
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "여기에_발급받은_API_KEY를_붙여넣으세요")
genai.configure(api_key=GOOGLE_API_KEY)
MODEL_NAME = 'gemini-3.1-flash-lite'
JSON_FILE_PATH = "current_menu.json"

# --- Time zone 반영 ---
KST = timezone(timedelta(hours=9))

# --- 동시 업데이트 방지 ---
is_updating = False
update_lock = threading.Lock()

# --- 유틸 함수 ---
def get_date_key(days_offset=0):
    target_date = datetime.now(KST) + timedelta(days=days_offset)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{target_date.month:02d}월 {target_date.day:02d}일 {weekdays[target_date.weekday()]}요일"

# --- 코어 로직: 파싱 및 캐싱 ---
def safe_update_menu_data():
    """백그라운드 태스크 실행 및 상태 해제를 보장하는 래퍼 함수"""
    global is_updating
    try:
        update_menu_data()
    finally:
        with update_lock:
            is_updating = False

def update_menu_data():
    """크롤링 및 Gemini API를 통해 최신 식단(상위 2개)을 파싱하여 JSON으로 저장하는 함수"""
    print(f"[{datetime.datetime.now()}] 🔄 식단표 파싱 업데이트 시작...")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    list_url = "https://medicine.korea.ac.kr/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=10"
    
    try:
        res_list = requests.get(list_url, headers=headers)
        list_data = res_list.json()
        
        target_article_nos = []
        for article in list_data.get("list", []):
            if "식단표" in article.get("title", ""):
                target_article_nos.append(article.get("articleNo"))
                if len(target_article_nos) == 2:  # 상위 2개까지만 수집
                    break
                
        if not target_article_nos:
            print("❌ 식단표 게시물을 찾을 수 없습니다.")
            return

        merged_daily_menus = {}
        model = genai.GenerativeModel(MODEL_NAME, generation_config={"response_mime_type": "application/json"})

        # 상위 2개 게시물 파싱 후 데이터 병합
        for article_no in target_article_nos:
            detail_url = f"https://medicine.korea.ac.kr/api/article/157/{article_no}?instNo=4&boardNo=157&articleNo={article_no}"
            res_detail = requests.get(detail_url, headers=headers)
            content_html = res_detail.json().get("content", "")
            
            img_match = re.search(r'<img[^>]+src="([^">]+)"', content_html)
            if not img_match:
                continue
                
            img_path = img_match.group(1) if img_match.group(1).startswith("/") else "/" + img_match.group(1)
            download_url = f"https://medicine.korea.ac.kr{img_path}"
            
            img_res = requests.get(download_url, headers=headers)
            temp_img_name = f"temp_menu_{article_no}.jpg"
            with open(temp_img_name, 'wb') as f:
                f.write(img_res.content)

            img = Image.open(temp_img_name)
            prompt = """
            당신은 데이터 추출 전문가입니다. 주간 식단표 이미지에서 데이터를 추출하세요.
            1. 정중앙의 '1페이지' 워터마크 무시
            2. 파란색 칼로리(kcal) 수치 추출
            3. 아래 JSON 구조로 출력:
            {
              "week_key": "0413_0417",
              "daily_menus": {
                "04월 13일 월요일": {
                  "lunch_korean": {"items": ["메뉴1", "메뉴2"], "calories": 989, "price": 6000},
                  "lunch_international": {"items": ["메뉴"], "calories": 1000, "price": 7500},
                  "dinner_korean": {"items": ["메뉴"], "calories": 800, "price": 6000}
                }
              }
            }
            빈 식단은 items에 ["미운영"] 삽입, calories는 null 처리. week_key는 이미지의 주간 기간을 숫자만 사용해 추출(예: 0413_0417).
            """
            response = model.generate_content([img, prompt])
            
            try:
                menu_data = json.loads(response.text)
                # 추출한 일별 메뉴를 병합 딕셔너리에 추가
                merged_daily_menus.update(menu_data.get("daily_menus", {}))
            except Exception as e:
                print(f"❌ JSON 파싱 에러 (articleNo {article_no}): {e}")
            
            os.remove(temp_img_name)

        # 병합된 데이터로 최종 파일 저장
        final_data = {
            "daily_menus": merged_daily_menus
        }

        with open(JSON_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(final_data, f, indent=2, ensure_ascii=False)
            
        print(f"[{datetime.datetime.now()}] ✅ 파싱 및 병합 저장 완료! (게시물 {len(target_article_nos)}개 반영)")
        
    except Exception as e:
        print(f"❌ 파싱 중 오류 발생: {e}")


# --- 공통 카카오톡 응답 생성기 ---
def generate_kakao_response(target_key: str, background_tasks: BackgroundTasks):
    global is_updating
    needs_update = True
    menu_data = {}
    
    # JSON 파일을 읽고, 요청한 날짜(target_key)가 있는지 확인
    if os.path.exists(JSON_FILE_PATH):
        try:
            with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
                menu_data = json.load(f)
            if target_key in menu_data.get("daily_menus", {}):
                needs_update = False
        except Exception:
            pass

    # 요청한 날짜의 식단이 없으면 파싱 백그라운드 작업 추가
    if needs_update:
        with update_lock:
            if not is_updating:
                is_updating = True
                background_tasks.add_task(safe_update_menu_data)

    today_menu = menu_data.get("daily_menus", {}).get(target_key)
    
    if not menu_data:
         response_text = "🔄 최신 식단표를 불러오고 분석하는 중입니다.\n1~2분 뒤에 다시 요청해 주세요!"
    elif not today_menu:
         response_text = f"❌ {target_key}의 식단 정보가 없습니다.\n(주말이거나 아직 업로드되지 않은 날짜입니다.)"
    else:
        response_text = f"🍽️ [{target_key} 학식]\n\n"
        
        if "lunch_korean" in today_menu:
            items = ", ".join(today_menu["lunch_korean"].get("items", []))
            cal = today_menu["lunch_korean"].get("calories", "표기없음")
            response_text += f"🍚 점심(한식)\n{items}\n({cal} kcal)\n\n"
            
        if "lunch_international" in today_menu:
            items = ", ".join(today_menu["lunch_international"].get("items", []))
            cal = today_menu["lunch_international"].get("calories", "표기없음")
            response_text += f"🍝 점심(인터)\n{items}\n({cal} kcal)\n\n"
            
        if "dinner_korean" in today_menu:
            items = ", ".join(today_menu["dinner_korean"].get("items", []))
            cal = today_menu["dinner_korean"].get("calories", "표기없음")
            response_text += f"🥘 저녁\n{items}\n({cal} kcal)"

    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": response_text.strip()
                    }
                }
            ]
        }
    }


# --- 카카오톡 챗봇 API 엔드포인트 ---
@app.post("/api/menu")
async def get_menu_chatbot(request: Request, background_tasks: BackgroundTasks):
    """오늘 식단"""
    return generate_kakao_response(get_date_key(0), background_tasks)

@app.post("/api/menu_tm1")
async def get_menu_tm1_chatbot(request: Request, background_tasks: BackgroundTasks):
    """내일 식단"""
    return generate_kakao_response(get_date_key(1), background_tasks)

@app.post("/api/menu_tm2")
async def get_menu_tm2_chatbot(request: Request, background_tasks: BackgroundTasks):
    """내일 모레 식단"""
    return generate_kakao_response(get_date_key(2), background_tasks)

@app.get("/api/showjson")
async def get_show_json(request: Request):
    """디버깅용 json 전체 보여주기"""
    menu_data = {}
    if os.path.exists(JSON_FILE_PATH):
        try:
            with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
                menu_data = json.load(f)
                return menu_data
        except Exception:
            return {"status": "error", "message": f"파일 읽기/파싱 실패: {e}"}
    
    {"status": "error", "message": f"식단 파일이 생성되지 않았습니다."}

if __name__ == "__main__":
    import uvicorn
    if not os.path.exists(JSON_FILE_PATH):
        update_menu_data()
    uvicorn.run(app, host="0.0.0.0", port=8000)
