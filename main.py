# pip install fastapi uvicorn requests pillow google-generativeai python-multipart
import os
import json
import re
import datetime
import requests
from fastapi import FastAPI, Request, BackgroundTasks
import google.generativeai as genai
from PIL import Image

app = FastAPI()

# --- 설정 및 초기화 ---
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "여기에_발급받은_API_KEY를_붙여넣으세요")
genai.configure(api_key=GOOGLE_API_KEY)
MODEL_NAME = 'gemini-2.5-flash'
JSON_FILE_PATH = "current_menu.json"

# --- 유틸 함수 ---
def get_this_week_key():
    """현재 날짜를 기준으로 해당 주의 월요일~금요일 문자열 반환 (캐시 검사용)"""
    now = datetime.datetime.now()
    monday = now - datetime.timedelta(days=now.weekday())
    friday = monday + datetime.timedelta(days=4)
    return f"{monday.strftime('%m%d')}_{friday.strftime('%m%d')}"

def get_todays_date_key():
    """오늘 날짜를 JSON 키 형식에 맞게 반환 (예: '04월 14일 화요일')"""
    now = datetime.datetime.now()
    month = f"{now.month:02d}"
    day = f"{now.day:02d}"
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    weekday_str = weekdays[now.weekday()]
    return f"{month}월 {day}일 {weekday_str}요일"

# --- 코어 로직: 파싱 및 캐싱 ---
def update_menu_data():
    """크롤링 및 Gemini API를 통해 최신 식단을 파싱하여 JSON으로 저장하는 함수"""
    print(f"[{datetime.datetime.now()}] 🔄 식단표 파싱 업데이트 시작...")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    list_url = "https://medicine.korea.ac.kr/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=10"
    
    try:
        res_list = requests.get(list_url, headers=headers)
        list_data = res_list.json()
        
        target_article_no = None
        for article in list_data.get("list", []):
            if "식단표" in article.get("title", ""):
                target_article_no = article.get("articleNo")
                break
                
        if not target_article_no:
            print("❌ 식단표 게시물을 찾을 수 없습니다.")
            return

        detail_url = f"https://medicine.korea.ac.kr/api/article/157/{target_article_no}?instNo=4&boardNo=157&articleNo={target_article_no}"
        res_detail = requests.get(detail_url, headers=headers)
        content_html = res_detail.json().get("content", "")
        
        img_match = re.search(r'<img[^>]+src="([^">]+)"', content_html)
        if not img_match:
            return
            
        img_path = img_match.group(1) if img_match.group(1).startswith("/") else "/" + img_match.group(1)
        download_url = f"https://medicine.korea.ac.kr{img_path}"
        
        img_res = requests.get(download_url, headers=headers)
        temp_img_name = "temp_menu.jpg"
        with open(temp_img_name, 'wb') as f:
            f.write(img_res.content)

        model = genai.GenerativeModel(MODEL_NAME, generation_config={"response_mime_type": "application/json"})
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
        빈 식단은 items에 ["운영 없음"] 삽입, calories는 null 처리. week_key는 이미지의 주간 기간을 숫자만 사용해 추출(예: 0413_0417).
        """
        response = model.generate_content([img, prompt])
        menu_data = json.loads(response.text)
        
        # 파일 저장
        with open(JSON_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(menu_data, f, indent=2, ensure_ascii=False)
            
        os.remove(temp_img_name)
        print(f"[{datetime.datetime.now()}] ✅ 파싱 및 저장 완료!")
        
    except Exception as e:
        print(f"❌ 파싱 중 오류 발생: {e}")


# --- 카카오톡 챗봇 API 엔드포인트 ---
@app.post("/api/menu")
async def get_menu_chatbot(request: Request, background_tasks: BackgroundTasks):
    """카카오톡 스킬 요청을 처리하는 엔드포인트"""
    # 1. 현재 이번 주 식단이 캐싱되어 있는지 확인
    this_week_key = get_this_week_key()
    needs_update = True
    menu_data = {}
    
    if os.path.exists(JSON_FILE_PATH):
        try:
            with open(JSON_FILE_PATH, 'r', encoding='utf-8') as f:
                menu_data = json.load(f)
            # JSON 파일 안의 week_key와 이번 주 실제 날짜가 일치하면 업데이트 불필요
            if menu_data.get("week_key") == this_week_key:
                needs_update = False
        except Exception:
            pass # 파싱 에러 등으로 파일이 깨졌으면 다시 업데이트

    # 2. 이번 주 월요일인데 아직 파싱이 안 되었다면 백그라운드로 파싱 지시
    if needs_update:
        # background_tasks를 사용하면 응답을 5초 이내에 먼저 카카오로 보내고,
        # 파싱은 서버 뒷단에서 조용히 실행됩니다. (타임아웃 방지)
        background_tasks.add_task(update_menu_data)

    # 3. 사용자에게 보낼 텍스트 조립
    today_key = get_todays_date_key()
    today_menu = menu_data.get("daily_menus", {}).get(today_key)
    
    if not menu_data:
         response_text = "🔄 최신 식단표를 불러오고 분석하는 중입니다.\n1~2분 뒤에 다시 '식단'을 입력해 주세요!"
    elif not today_menu:
         response_text = f"❌ {today_key}의 식단 정보가 없습니다.\n(주말이거나 식단표에 없는 날짜입니다.)"
    else:
        # JSON을 카카오톡에서 읽기 좋게 변환
        response_text = f"🍽️ [{today_key} 학식]\n\n"
        
        if "lunch_korean" in today_menu:
            items = ", ".join(today_menu["lunch_korean"].get("items", []))
            cal = today_menu["lunch_korean"].get("calories", "표기없음")
            response_text += f"🍚 점심(한식)\n{items}\n({cal} kcal)\n\n"
            
        if "lunch_international" in today_menu:
            items = ", ".join(today_menu["lunch_international"].get("items", []))
            cal = today_menu["lunch_international"].get("calories", "표기없음")
            response_text += f"🍝 점심(일품)\n{items}\n({cal} kcal)\n\n"
            
        if "dinner_korean" in today_menu:
            items = ", ".join(today_menu["dinner_korean"].get("items", []))
            cal = today_menu["dinner_korean"].get("calories", "표기없음")
            response_text += f"🥘 저녁\n{items}\n({cal} kcal)"

    # 4. 카카오톡 스킬 응답 포맷으로 리턴
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

if __name__ == "__main__":
    import uvicorn
    # 서버 실행 시 로컬에 파일이 없다면 최초 1회 업데이트 실행
    if not os.path.exists(JSON_FILE_PATH):
        update_menu_data()
    uvicorn.run(app, host="0.0.0.0", port=8000)