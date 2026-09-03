import io
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import holidays
import requests
from fastapi import BackgroundTasks, FastAPI
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

# -latest/-preview 별칭은 피하고 고정된 stable 모델 체인을 사용.
MODEL_FALLBACK_CHAIN = (
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

BASE_URL = "https://medicine.korea.ac.kr"
LIST_URL = f"{BASE_URL}/api/article/157?instNo=4&boardNo=157&startIndex=1&pageRow=4&title=식단"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
JSON_FILE_PATH = "current_menu.json"
DATE_KEY_FORMAT = "%Y-%m-%d"

KST = timezone(timedelta(hours=9))
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
MEAL_SECTIONS = (
    ("lunch_korean", "🍚 점심(한식)"),
    ("lunch_international", "🍝 점심(인터)"),
    ("dinner_korean", "🥘 저녁"),
)
DATE_RANGE_PATTERN = re.compile(r"\((\d{4})-(\d{4})\)")
IMAGE_SRC_PATTERN = re.compile(r'<img[^>]+src="([^">]+)"')

is_updating = False
update_lock = threading.Lock()


# --- 유틸리티 함수 ---
def load_menu_data() -> dict[str, Any]:
    with open(JSON_FILE_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def parse_date_range_from_title(title: str):
    match = DATE_RANGE_PATTERN.search(title)
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


def format_date_to_korean(date_str: str) -> str:
    try:
        date_obj = datetime.strptime(date_str, DATE_KEY_FORMAT).date()
        return f"{date_obj.month:02d}월 {date_obj.day:02d}일 {WEEKDAYS[date_obj.weekday()]}요일"
    except:
        return date_str


def unix_timestamp_to_date_str(timestamp_ms: int):
    try:
        date_obj = datetime.fromtimestamp(timestamp_ms / 1000, tz=KST).date()
        return date_obj.strftime(DATE_KEY_FORMAT)
    except:
        return None


def check_date_exists_in_notices(target_date: datetime) -> bool:
    try:
        res = requests.get(LIST_URL, headers=REQUEST_HEADERS, timeout=5)
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
                if count == 2:
                    break
    except Exception as e:
        print(f"리스트 확인 중 오류: {e}")
    return False


def fetch_menu_list():
    res_list = requests.get(LIST_URL, headers=REQUEST_HEADERS, timeout=10)
    if res_list.status_code != 200:
        print(f"⚠️ 목록 불러오기 실패 (상태 코드: {res_list.status_code})")
        return None

    try:
        list_data = res_list.json()
        print(f"[{datetime.now()}] 목록 JSON 파싱 성공, 총 항목: {len(list_data.get('list', []))}")
        return list_data
    except Exception:
        print("⚠️ API 응답이 JSON 형식이 아닙니다. 사이트가 HTML 에러 페이지를 반환했을 수 있습니다.")
        print(f"응답 내용 일부: {res_list.text[:300]}")
        return None


def save_merged_menu_data(new_menus: dict[str, Any]) -> None:
    existing_data = {"daily_menus": {}}
    if os.path.exists(JSON_FILE_PATH):
        try:
            existing_data = load_menu_data()
            print(f"[{datetime.now()}] 기존 JSON 읽기 성공, 메뉴 항목 수: {len(existing_data.get('daily_menus', {}))}")
        except Exception:
            print(f"⚠️ 기존 JSON 파싱 실패, 빈 데이터로 초기화합니다: {JSON_FILE_PATH}")

    all_menus = existing_data.get("daily_menus", {})
    all_menus.update(new_menus)
    print(f"[{datetime.now()}] 전체 메뉴 병합 완료, 총 항목 수: {len(all_menus)}")

    today = datetime.now(KST).date()
    cleaned_menus = {}
    for key, value in all_menus.items():
        try:
            dt = datetime.strptime(key, DATE_KEY_FORMAT).date()
            if dt >= today:
                cleaned_menus[key] = value
        except Exception as e:
            cleaned_menus[key] = value
            print(f"[{datetime.now()}] 키 파싱 실패({key}) - 유지: {e}")

    print(f"[{datetime.now()}] 필터링된 메뉴 수: {len(cleaned_menus)}")
    temp_json_path = JSON_FILE_PATH + ".tmp"
    with open(temp_json_path, "w", encoding="utf-8") as file:
        json.dump({"daily_menus": cleaned_menus}, file, indent=2, ensure_ascii=False)
    os.replace(temp_json_path, JSON_FILE_PATH)
    print(f"[{datetime.now()}] JSON 파일 저장 완료: {JSON_FILE_PATH}")


# --- Gemini 호출 (모델 폴백 체인 + 재시도) ---
def call_gemini_with_retry(
    img: Image.Image, prompt: str, max_retries_per_model: int = 2, base_delay: int = 6
):
    """
    MODEL_FALLBACK_CHAIN을 순서대로 시도.
    한 모델에서 429/503/overloaded(용량 문제)가 나면 짧게 재시도하고,
    그래도 안 되면 다음(다른 세대) 모델로 넘어간다.
    용량 문제가 아닌 에러(400 등)는 폴백해봐야 의미 없으니 바로 올린다.
    """
    last_err = None
    for i, model_name in enumerate(MODEL_FALLBACK_CHAIN):
        for attempt in range(1, max_retries_per_model + 1):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[img, prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                if i > 0:
                    print(f"[{datetime.now()}] ℹ️ 폴백 모델 사용됨: {model_name}")
                return response
            except Exception as e:
                err_str = str(e)
                last_err = e
                is_capacity_error = (
                    "429" in err_str
                    or "503" in err_str
                    or "RESOURCE_EXHAUSTED" in err_str
                    or "UNAVAILABLE" in err_str
                    or "overloaded" in err_str.lower()
                )
                if not is_capacity_error:
                    print(f"[{datetime.now()}] ❌ {model_name} 호출 실패(용량 문제 아님, 폴백 안 함): {err_str[:300]}")
                    raise
                if attempt < max_retries_per_model:
                    delay = base_delay * (2 ** (attempt - 1))
                    print(f"[{datetime.now()}] ⏳ {model_name} 과부하, {delay}초 후 재시도 ({attempt}/{max_retries_per_model})")
                    time.sleep(delay)
                    continue
                print(f"[{datetime.now()}] ⚠️ {model_name} 계속 과부하 → 다음 모델로 폴백: {err_str[:150]}")
    print(f"[{datetime.now()}] ❌ 모든 폴백 모델 실패")
    raise last_err


# --- 코어 로직 ---
def update_menu_data() -> None:
    global is_updating
    print(f"[{datetime.now()}] 🔄 백그라운드 업데이트 시작...")

    try:
        print(f"[{datetime.now()}] 목록 API 요청 시작: {LIST_URL}")
        list_data = fetch_menu_list()
        if list_data is None:
            return

        target_article_nos = [
            article.get("articleNo")
            for article in list_data.get("list", [])
            if "식단표" in article.get("title", "")
        ][:2]
        print(f"[{datetime.now()}] 대상 식단표 articleNo: {target_article_nos}")

        new_menus = {}
        for article_no in target_article_nos:
            try:
                print(f"[{datetime.now()}] articleNo {article_no} 처리 시작")
                detail_url = (
                    f"{BASE_URL}/api/article/157/{article_no}"
                    f"?instNo=4&boardNo=157&articleNo={article_no}"
                )
                res_detail = requests.get(detail_url, headers=REQUEST_HEADERS)
                if res_detail.status_code != 200:
                    print(f"⚠️ 상세 조회 실패 articleNo={article_no}, 상태 코드={res_detail.status_code}")
                    continue

                try:
                    content_html = res_detail.json().get("content", "")
                    print(f"[{datetime.now()}] 상세 JSON 파싱 성공 articleNo={article_no}, content 길이={len(content_html)}")
                except Exception as e:
                    print(f"⚠️ 상세 JSON 파싱 실패 articleNo={article_no}: {e}")
                    continue

                img_match = IMAGE_SRC_PATTERN.search(content_html)
                if not img_match:
                    print(f"⚠️ 이미지 태그를 찾을 수 없음 articleNo={article_no}")
                    continue

                img_src = img_match.group(1)
                img_path = img_src if img_src.startswith("/") else "/" + img_src
                print(f"[{datetime.now()}] 이미지 경로 발견 articleNo={article_no}: {img_path}")
                img_res = requests.get(f"{BASE_URL}{img_path}", headers=REQUEST_HEADERS)
                print(f"[{datetime.now()}] 이미지 다운로드 상태(articleNo={article_no}): {img_res.status_code}")

                img = Image.open(io.BytesIO(img_res.content))

                # 게시물 업로드 날짜 추출
                article = next(
                    (a for a in list_data.get("list", []) if a.get("articleNo") == article_no),
                    {},
                )
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
                response = call_gemini_with_retry(img, prompt)
                print(f"[{datetime.now()}] Gemini 응답 수신 articleNo={article_no}, 길이={len(response.text)}")
                try:
                    extracted = json.loads(response.text)
                    extracted_menus = extracted.get("daily_menus", {})
                    new_menus.update(extracted_menus)
                    print(f"[{datetime.now()}] 추출 item 수: {len(extracted_menus)} (articleNo={article_no})")
                except Exception as e:
                    print(f"⚠️ JSON 파싱 실패 articleNo={article_no}: {e}")
                    print(f"응답 원문: {response.text[:500]}")
            except Exception as e:
                # 한 articleNo에서 실패해도 전체를 죽이지 않고 다음으로 넘어감
                print(f"⚠️ articleNo={article_no} 처리 중 오류로 스킵: {e}")

        if not new_menus:
            print(f"[{datetime.now()}] 새로 추출된 메뉴가 없습니다.")
        save_merged_menu_data(new_menus)
    finally:
        with update_lock:
            is_updating = False


def generate_kakao_response(days_offset: int, background_tasks: BackgroundTasks) -> dict[str, Any]:
    global is_updating, last_holiday_check, kr_holidays

    # holiday check (연도 변경시)
    current_year = datetime.now(KST).year
    if last_holiday_check != current_year:
        kr_holidays = holidays.KR(years=[current_year, current_year + 1])
        last_holiday_check = current_year

    # response 형성부
    target_date = datetime.now(KST) + timedelta(days=days_offset)
    target_key = target_date.strftime(DATE_KEY_FORMAT)

    if target_date.weekday() >= 5 or target_date.date() in kr_holidays:
        return simple_text_response("❗ 주말이거나 공휴일이에요.")

    menu_data = {}
    if os.path.exists(JSON_FILE_PATH):
        try:
            menu_data = load_menu_data()
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

    return simple_text_response(f"❌ {target_key} 식단은 아직 업로드되지 않았어요.")


def simple_text_response(text: str) -> dict[str, Any]:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}


def format_menu_text(date_key: str, menu: dict[str, Any]) -> dict[str, Any]:
    korean_date = format_date_to_korean(date_key)
    res = f"🍽️  {korean_date} 학식\n\n"
    for key, label in MEAL_SECTIONS:
        if key in menu:
            meal = menu[key]
            items = ", ".join(meal.get("items", []))
            res += f"{label}\n{items}\n"
            if "미운영" not in items and meal.get("calories"):
                res += f"({meal['calories']} kcal)\n\n"
            else:
                res += "\n"
    return simple_text_response(res.strip())


# --- 주말 alive 핑 기반 자동 업데이트 ---
def get_next_monday(now: datetime):
    """오늘(주말) 기준 다음주 월요일 date를 반환"""
    days_until_monday = 7 - now.weekday()  # 토(5)->2, 일(6)->1
    return (now + timedelta(days=days_until_monday)).date()


def check_and_trigger_weekend_update(background_tasks: BackgroundTasks) -> None:
    """
    주말에 /api/alive 핑이 들어오면:
      1. 평일이면 그냥 리턴 (주말에만 동작)
      2. 다음주 월요일 메뉴가 이미 캐시에 있으면 리턴
      3. 없으면 공지사항에 다음주 식단표가 올라왔는지 확인
      4. 올라왔으면 백그라운드로 update_menu_data 실행 (금요일에 보통 업로드됨)
    """
    global is_updating
    now = datetime.now(KST)

    if now.weekday() < 5:  # 월~금(0~4)이면 스킵
        return

    next_monday = get_next_monday(now)
    next_monday_key = next_monday.strftime(DATE_KEY_FORMAT)

    menu_data = {}
    if os.path.exists(JSON_FILE_PATH):
        try:
            menu_data = load_menu_data()
        except Exception:
            menu_data = {}

    if next_monday_key in menu_data.get("daily_menus", {}):
        return  # 이미 다음주 식단 있음

    target_dt = datetime.combine(next_monday, datetime.min.time()).replace(tzinfo=KST)
    if not check_date_exists_in_notices(target_dt):
        return  # 아직 공지 자체가 안 올라옴

    with update_lock:
        if is_updating:
            return
        is_updating = True
        background_tasks.add_task(update_menu_data)
        print(f"[{datetime.now()}] 🔔 alive 핑 - 다음주({next_monday_key}) 식단표 감지, 업데이트 시작")


# --- API 엔드포인트 유지 ---
@app.post("/api/menu")
async def get_menu_chatbot(background_tasks: BackgroundTasks):
    return generate_kakao_response(0, background_tasks)


@app.post("/api/menu_tm1")
async def get_menu_tm1_chatbot(background_tasks: BackgroundTasks):
    return generate_kakao_response(1, background_tasks)


@app.post("/api/menu_tm2")
async def get_menu_tm2_chatbot(background_tasks: BackgroundTasks):
    return generate_kakao_response(2, background_tasks)


@app.get("/api/menu_dbg")
async def get_menu_dbg_chatbot(background_tasks: BackgroundTasks, offset: int = 0):
    return generate_kakao_response(offset, background_tasks)


@app.get("/api/showjson")
async def get_show_json():
    if os.path.exists(JSON_FILE_PATH):
        try:
            return load_menu_data()
        except Exception as e:
            return {"status": "error", "message": f"파일 읽기/파싱 실패: {e}"}
    return {"status": "error", "message": "식단 파일이 생성되지 않았습니다."}


@app.head("/api/alive")
async def for_uptime(background_tasks: BackgroundTasks):
    check_and_trigger_weekend_update(background_tasks)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    if not os.path.exists(JSON_FILE_PATH):
        update_menu_data()
    uvicorn.run(app, host="0.0.0.0", port=8000)
