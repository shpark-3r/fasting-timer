import requests
from bs4 import BeautifulSoup
import json
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

def fetch_menu_for_date(date_str):
    """date_str: YYYY.MM.DD 형식. 제2학생회관 학생 식단을 가져온다."""
    url = (
        "https://mobileadmin.cnu.ac.kr/food/index.jsp"
        f"?searchYmd={date_str}"
        "&searchLang=OCL04.10"
        "&searchView=cafeteria"
        "&searchCafeteria=OCL03.02"
        "&Language_gb=OCL04.10"
    )
    resp = requests.get(url, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    menu = {"date": date_str, "breakfast": None, "lunch": None, "dinner": None}

    table = soup.find("table")
    if not table:
        return menu

    rows = table.find_all("tr")
    # 테이블 구조 (rowspan 때문에 열 위치가 밀림):
    # Row 0: 헤더 (th: 구분, 제1~제4, 생활과학대학)
    # Row 1: 조식 직원 — td[2]에 rowspan=100인 "메뉴운영내역"이 제1학생회관 열 차지
    # Row 2: 조식 학생 — td[0]=학생, td[1]=제2학생회관 (제1학생회관은 rowspan으로 이미 차지됨)
    # Row 3: 중식 직원 — td[0]=중식, td[1]=직원, td[2]=제1, td[3]=제2, ...
    # Row 4: 중식 학생 — td[0]=학생, td[1]=제1(or skip), td[2]=제2, ...
    # Row 5: 석식 직원
    # Row 6: 석식 학생

    def parse_menu_text(td):
        """td에서 메뉴 항목 리스트를 추출"""
        text = td.get_text(separator="|", strip=True)
        if not text or text == "운영안함":
            return None
        items = [item.strip() for item in text.split("|") if item.strip()]
        return items if items else None

    def find_menu_in_row(row, meal_type):
        """행에서 실제 메뉴가 있는 td를 찾아 반환"""
        tds = row.find_all("td")
        for td in tds:
            text = td.get_text(strip=True)
            # 구분 라벨(학생, 직원, 조식, 중식, 석식) 스킵
            if text in ("학생", "직원", "조식", "중식", "석식"):
                continue
            if text == "운영안함" or text == "메뉴운영내역":
                continue
            parsed = parse_menu_text(td)
            if parsed:
                return parsed
        return None

    # 학생 행: Row 2 (조식), Row 4 (중식), Row 6 (석식)
    student_rows = {
        "breakfast": 2,
        "lunch": 4,
        "dinner": 6,
    }

    for meal, row_idx in student_rows.items():
        if row_idx < len(rows):
            result = find_menu_in_row(rows[row_idx], meal)
            if result:
                menu[meal] = result

    return menu


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    kst_now = datetime.now(KST)
    result = {}

    # 오늘부터 7일간 메뉴 가져오기
    for i in range(7):
        date = kst_now + timedelta(days=i)
        date_str = date.strftime("%Y.%m.%d")
        day_key = date.strftime("%Y-%m-%d")
        weekday = date.weekday()

        if weekday >= 5:
            result[day_key] = {
                "date": date_str,
                "breakfast": None,
                "lunch": None,
                "dinner": None,
                "weekday": weekday,
            }
            continue

        try:
            menu = fetch_menu_for_date(date_str)
            menu["weekday"] = weekday
            result[day_key] = menu
        except Exception as e:
            result[day_key] = {
                "date": date_str,
                "breakfast": None,
                "lunch": None,
                "dinner": None,
                "weekday": weekday,
                "error": str(e),
            }

    with open("menu.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Menu fetched for {len(result)} days")
    for key, val in result.items():
        bf = val.get("breakfast")
        lch = val.get("lunch")
        print(f"  {key}: breakfast={bf if bf else 'None'}")


if __name__ == "__main__":
    main()
