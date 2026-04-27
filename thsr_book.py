#!/usr/bin/env python3
"""台灣高鐵 (THSR) 線上訂票 CLI。

互動式詢問起訖站、日期、時間、票數與身分證字號，
自動送出訂位查詢、車次選擇、與乘客資料，
完成後印出訂位代號與付款連結，使用者可在瀏覽器中
手動填寫信用卡資料完成付款。

僅供個人購買自己車票使用，請遵守台灣高鐵網站使用條款。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import webbrowser
from datetime import date
from typing import Optional

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://irs.thsrc.com.tw"
BOOKING_PAGE = f"{BASE_URL}/IMINT/?locale=tw"

STATIONS: dict[str, str] = {
    "1": "南港", "2": "台北", "3": "板橋", "4": "桃園",
    "5": "新竹", "6": "苗栗", "7": "台中", "8": "彰化",
    "9": "雲林", "10": "嘉義", "11": "台南", "12": "左營",
}
NAME_TO_CODE = {v: k for k, v in STATIONS.items()}
EN_TO_CODE = {
    "nangang": "1", "taipei": "2", "banqiao": "3", "taoyuan": "4",
    "hsinchu": "5", "miaoli": "6", "taichung": "7", "changhua": "8",
    "yunlin": "9", "chiayi": "10", "tainan": "11", "zuoying": "12",
    "kaohsiung": "12",
}


def resolve_station(value: str) -> str:
    v = value.strip()
    if v in STATIONS:
        return v
    if v in NAME_TO_CODE:
        return NAME_TO_CODE[v]
    if v.lower() in EN_TO_CODE:
        return EN_TO_CODE[v.lower()]
    raise ValueError(f"未知車站 / Unknown station: {value!r}")


def stations_help() -> str:
    return ", ".join(f"{k}={v}" for k, v in STATIONS.items())


class THSRBooker:
    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
    )

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        })
        self.s1_action: Optional[str] = None
        self.s2_action: Optional[str] = None
        self.s3_action: Optional[str] = None

    def step1_load(self) -> bytes:
        r = self.session.get(BOOKING_PAGE)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        form = soup.find("form", id="BookingS1Form")
        if form is None:
            raise RuntimeError("找不到訂票表單；高鐵網站可能已改版。")
        self.s1_action = form.get("action")

        captcha_img = soup.find("img", id=re.compile("BookingS1Form.*[Cc]aptcha"))
        if captcha_img is None:
            captcha_img = soup.select_one("#BookingS1Form img[src*='captcha']")
        if captcha_img is None:
            raise RuntimeError("找不到驗證碼圖片。")

        captcha_url = captcha_img.get("src", "")
        if captcha_url.startswith("/"):
            captcha_url = BASE_URL + captcha_url
        elif captcha_url.startswith("//"):
            captcha_url = "https:" + captcha_url

        cr = self.session.get(captcha_url)
        cr.raise_for_status()
        return cr.content

    def step1_submit(self, params: dict, captcha_answer: str) -> list[dict]:
        if not self.s1_action:
            raise RuntimeError("請先呼叫 step1_load。")
        data = {
            "BookingS1Form:hf:0": "",
            "selectStartStation": params["start"],
            "selectDestinationStation": params["dest"],
            "bookingMethod": "radio33",
            "tripCon:typesoftrip": "0",
            "trainCon:trainRadioGroup": "0",
            "seatCon:seatRadioGroup": "radio17",
            "toTimeInputField": params["date"],
            "toTimeTable": params["time"],
            "toTrainIDInputField": "",
            "backTimeInputField": params["date"],
            "backTimeTable": "",
            "backTrainIDInputField": "",
            "ticketPanel:rows:0:ticketAmount": f"{params['adults']}F",
            "ticketPanel:rows:1:ticketAmount": "0H",
            "ticketPanel:rows:2:ticketAmount": "0W",
            "ticketPanel:rows:3:ticketAmount": "0E",
            "ticketPanel:rows:4:ticketAmount": "0P",
            "homeCaptcha:securityCode": captcha_answer,
            "SubmitButton": "開始查詢",
        }
        r = self.session.post(self.s1_action, data=data)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        self._raise_on_errors(soup, "Step 1")

        form = soup.find("form", id="BookingS2Form")
        if form is None:
            raise RuntimeError("找不到車次選擇表單；驗證碼或查詢條件可能有誤。")
        self.s2_action = form.get("action")

        trains: list[dict] = []
        for label in soup.select("label.result-item"):
            radio = label.find("input", attrs={"name": "TrainQueryDataViewPanel:TrainGroup"})
            if not radio:
                continue
            cols = label.find_all(class_=re.compile(r"column\d"))
            train_no = label.find(class_="column1")
            depart = label.find(class_="column3")
            arrive = label.find(class_="column4")
            duration = label.find(class_="column2")
            trains.append({
                "value": radio.get("value", ""),
                "train_no": _text(train_no),
                "depart": _text(depart),
                "arrive": _text(arrive),
                "duration": _text(duration),
            })

        if not trains:
            for radio in soup.select("input[name='TrainQueryDataViewPanel:TrainGroup']"):
                trains.append({
                    "value": radio.get("value", ""),
                    "train_no": radio.get("value", ""),
                    "depart": "", "arrive": "", "duration": "",
                })
        return trains

    def step2_submit(self, train_value: str) -> None:
        if not self.s2_action:
            raise RuntimeError("請先呼叫 step1_submit。")
        data = {
            "BookingS2Form:hf:0": "",
            "TrainQueryDataViewPanel:TrainGroup": train_value,
            "SubmitButton": "確認車次",
        }
        r = self.session.post(self.s2_action, data=data)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        self._raise_on_errors(soup, "Step 2")
        form = soup.find("form", id="BookingS3Form") or soup.find("form", id="BookingS3FormSP")
        if form is None:
            raise RuntimeError("找不到乘客資料表單。")
        self.s3_action = form.get("action")

    def step3_submit(self, passenger: dict) -> dict:
        if not self.s3_action:
            raise RuntimeError("請先呼叫 step2_submit。")
        data = {
            "BookingS3FormSP:hf:0": "",
            "diffOver": "1",
            "isSPromotion": "1",
            "passengerCount": "1",
            "isGoBackM": "",
            "backHome": "",
            "TicketMemberSystemInputPanel:TakerMemberSystemDataView:memberSystemRadioGroup": "radio44",
            "idInputRadio": "0",
            "dummyId": passenger["id_number"],
            "idInputRadio:idNumber": passenger["id_number"],
            "mobileInputRadio:mobilePhone": passenger.get("phone", ""),
            "dummyPhone": passenger.get("phone", ""),
            "email": passenger.get("email", ""),
            "agree": "on",
            "SubmitButton": "確認訂位",
        }
        r = self.session.post(self.s3_action, data=data)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        self._raise_on_errors(soup, "Step 3")

        text = soup.get_text(" ", strip=True)
        pnr = None
        for pat in (r"訂位代號[^\w]*([A-Z0-9]{8})", r"PNR[^\w]*([A-Z0-9]{8})"):
            m = re.search(pat, text)
            if m:
                pnr = m.group(1)
                break

        payment_url = None
        for a in soup.find_all("a"):
            href = a.get("href", "")
            txt = a.get_text(strip=True)
            if any(k in txt for k in ("付款", "信用卡", "繳費")) or "payment" in href.lower():
                payment_url = href if href.startswith("http") else (
                    BASE_URL + href if href.startswith("/") else None
                )
                if payment_url:
                    break

        return {
            "pnr": pnr,
            "payment_url": payment_url,
            "confirm_url": r.url,
        }

    @staticmethod
    def _raise_on_errors(soup: BeautifulSoup, stage: str) -> None:
        errs = soup.select(".feedbackPanelERROR, .error_message, span.feedbackPanelERROR")
        msgs = [e.get_text(strip=True) for e in errs if e.get_text(strip=True)]
        if msgs:
            raise RuntimeError(f"{stage} 錯誤: " + "; ".join(msgs))


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""


def show_captcha(image_bytes: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".png", prefix="thsr_captcha_")
    with os.fdopen(fd, "wb") as f:
        f.write(image_bytes)
    print(f"\n[驗證碼已存於 {path}]")
    try:
        if sys.platform == "darwin":
            os.system(f"open {path!r}")
        elif sys.platform.startswith("linux"):
            os.system(f"xdg-open {path!r} >/dev/null 2>&1 &")
        elif sys.platform == "win32":
            os.startfile(path)
    except Exception:
        pass
    return path


def prompt(msg: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val or (default or "")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="台灣高鐵訂票 CLI",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=f"車站代碼: {stations_help()}",
    )
    parser.add_argument("--start", help="出發站 (代碼或站名，例: 2 或 台北)")
    parser.add_argument("--dest", help="到達站")
    parser.add_argument("--date", help="出發日期 YYYY/MM/DD")
    parser.add_argument("--time", help="出發時間，例: 1230P / 0830A")
    parser.add_argument("--adults", type=int, default=1, help="全票人數 (預設 1)")
    parser.add_argument("--id", dest="id_number", help="身分證或護照號碼")
    parser.add_argument("--phone", help="手機 (選填)")
    parser.add_argument("--email", help="Email (選填)")
    parser.add_argument("--no-browser", action="store_true", help="完成後不自動開啟付款頁")
    args = parser.parse_args()

    print("=== 台灣高鐵訂票 / THSR Booking ===")
    print(f"車站代碼: {stations_help()}\n")

    try:
        start = resolve_station(args.start or prompt("出發站"))
        dest = resolve_station(args.dest or prompt("到達站"))
    except ValueError as e:
        print(f"錯誤: {e}", file=sys.stderr)
        return 2

    date_str = args.date or prompt("出發日期 yyyy/mm/dd",
                                   date.today().strftime("%Y/%m/%d"))
    time_str = args.time or prompt("出發時間 (e.g. 1230P, 0830A)", "1230P")
    adults = args.adults
    id_number = args.id_number or prompt("身分證 / 護照號碼")
    if not id_number:
        print("錯誤: 必須提供身分證/護照號碼。", file=sys.stderr)
        return 2
    phone = args.phone if args.phone is not None else prompt("手機 (選填)", "")
    email = args.email if args.email is not None else prompt("Email (選填)", "")

    booker = THSRBooker()

    try:
        print("\n[1/3] 載入查詢頁與驗證碼…")
        captcha_bytes = booker.step1_load()
        show_captcha(captcha_bytes)
        captcha = input("請輸入驗證碼 (CAPTCHA): ").strip()
        if not captcha:
            print("錯誤: 驗證碼不可為空。", file=sys.stderr)
            return 2

        print("\n[2/3] 查詢車次…")
        trains = booker.step1_submit({
            "start": start, "dest": dest,
            "date": date_str, "time": time_str,
            "adults": adults,
        }, captcha)
        if not trains:
            print("找不到任何符合條件的車次。", file=sys.stderr)
            return 1

        print("\n可選車次:")
        for i, t in enumerate(trains, 1):
            print(f"  [{i}] {t['train_no']:>8}  {t['depart']} → {t['arrive']}  ({t['duration']})")
        choice_str = prompt(f"請選擇車次 1-{len(trains)}", "1")
        try:
            choice = int(choice_str)
            chosen = trains[choice - 1]
        except (ValueError, IndexError):
            print("無效的車次選擇。", file=sys.stderr)
            return 2
        booker.step2_submit(chosen["value"])

        print("\n[3/3] 提交乘客資料…")
        result = booker.step3_submit({
            "id_number": id_number,
            "phone": phone,
            "email": email,
        })
    except requests.HTTPError as e:
        print(f"HTTP 錯誤: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"訂票失敗: {e}", file=sys.stderr)
        return 1

    print("\n=== 訂位成功 / Booking Successful ===")
    if result["pnr"]:
        print(f"訂位代號 PNR: {result['pnr']}")
    print(f"確認頁: {result['confirm_url']}")

    payment_url = result["payment_url"] or result["confirm_url"]
    print("\n--- 付款連結 / Payment URL ---")
    print(payment_url)
    print("\n請在瀏覽器中開啟上方連結，手動填寫信用卡資料完成付款。")
    print("（為了你的資料安全，本程式不會代為輸入信用卡。）")

    if not args.no_browser:
        ans = prompt("自動在瀏覽器開啟付款頁? (y/n)", "y")
        if ans.lower().startswith("y"):
            webbrowser.open(payment_url)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
