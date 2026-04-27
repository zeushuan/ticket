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


CAPTCHA_RETRY_MAX = 5
CAPTCHA_LEN = 4

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
                "sold_out": _is_sold_out(label, radio),
            })

        if not trains:
            for radio in soup.select("input[name='TrainQueryDataViewPanel:TrainGroup']"):
                trains.append({
                    "value": radio.get("value", ""),
                    "train_no": radio.get("value", ""),
                    "depart": "", "arrive": "", "duration": "",
                    "sold_out": radio.has_attr("disabled"),
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


def _is_captcha_error(msg: str) -> bool:
    keywords = ("驗證碼", "captcha", "CAPTCHA", "securityCode", "認證碼")
    return any(k in msg for k in keywords)


def _is_sold_out(label, radio) -> bool:
    cls = " ".join(label.get("class") or []).lower()
    if "disable" in cls or "soldout" in cls or "sold-out" in cls:
        return True
    if radio is not None and radio.has_attr("disabled"):
        return True
    text = label.get_text(" ", strip=True)
    for marker in ("已售完", "客滿", "售完", "Sold Out", "Sold out"):
        if marker in text:
            return True
    return False


def _hhmm_to_min(hhmm: str) -> int:
    s = hhmm.replace(":", "")
    if not re.fullmatch(r"\d{3,4}", s):
        raise ValueError(f"時間格式錯誤 (需 HH:MM 或 HHMM): {hhmm!r}")
    s = s.zfill(4)
    h, m = int(s[:2]), int(s[2:])
    if h > 23 or m > 59:
        raise ValueError(f"時間超出範圍: {hhmm!r}")
    return h * 60 + m


def _depart_minutes(t: dict) -> Optional[int]:
    s = (t.get("depart") or "").strip()
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def filter_trains(trains: list[dict], from_min: Optional[int],
                  until_min: Optional[int]) -> list[dict]:
    out = []
    for t in trains:
        if t.get("sold_out"):
            continue
        dm = _depart_minutes(t)
        if dm is None:
            out.append(t)
            continue
        if from_min is not None and dm < from_min:
            continue
        if until_min is not None and dm > until_min:
            continue
        out.append(t)
    return out


def _sleep_with_countdown(seconds: int, prefix: str = "  ") -> None:
    import time
    for remaining in range(seconds, 0, -1):
        sys.stdout.write(f"\r{prefix}{remaining:>3}s 後重新刷票…   ")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r" + " " * 40 + "\r")
    sys.stdout.flush()


class CaptchaSolver:
    """ddddocr 包裝；若未安裝則 available=False。"""

    def __init__(self) -> None:
        self.available = False
        self._ocr = None
        try:
            import logging
            logging.getLogger("ddddocr").setLevel(logging.ERROR)
            import ddddocr  # type: ignore
            self._ocr = ddddocr.DdddOcr(show_ad=False)
            self.available = True
        except ImportError:
            pass
        except Exception as e:
            print(f"[警告] ddddocr 載入失敗，將使用手動輸入: {e}", file=sys.stderr)

    def solve(self, image_bytes: bytes) -> str:
        if not self.available or self._ocr is None:
            return ""
        try:
            text = self._ocr.classification(image_bytes)
        except Exception as e:
            print(f"[警告] 驗證碼辨識失敗: {e}", file=sys.stderr)
            return ""
        text = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
        return text


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


def query_for_trains(booker: "THSRBooker", solver: Optional["CaptchaSolver"],
                     query_params: dict, args, silent: bool = False) -> list[dict]:
    """載入頁面 → (自動)解驗證碼 → 送出查詢；驗證碼錯誤時重試。

    silent=True 時用於背景刷票：永遠走自動辨識，不開圖、不阻塞使用者。
    """
    use_solver = bool(solver and solver.available and not args.manual_captcha)
    auto_only = silent and use_solver
    last_err: Optional[str] = None

    for attempt in range(1, max(1, args.captcha_retries) + 1):
        captcha_bytes = booker.step1_load()
        captcha = ""
        if use_solver:
            captcha = solver.solve(captcha_bytes)
            if not silent:
                if captcha:
                    extra = (f"  (長度 {len(captcha)} ≠ {CAPTCHA_LEN})"
                             if len(captcha) != CAPTCHA_LEN else "")
                    print(f"  自動辨識: {captcha}{extra}")
                else:
                    print("  自動辨識失敗。")

        valid = bool(captcha) and len(captcha) == CAPTCHA_LEN

        if not valid and not auto_only:
            show_captcha(captcha_bytes)
            hint = f" (Enter 接受 [{captcha}])" if captcha else ""
            user_in = input(f"請輸入驗證碼{hint}: ").strip()
            if user_in:
                captcha = user_in.upper()

        if args.confirm_captcha and not auto_only and captcha:
            user_in = input(f"確認驗證碼 [{captcha}] (Enter 接受): ").strip()
            if user_in:
                captcha = user_in.upper()

        if not captcha:
            if auto_only:
                continue
            raise RuntimeError("驗證碼不可為空")

        try:
            return booker.step1_submit(query_params, captcha)
        except RuntimeError as e:
            last_err = str(e)
            if _is_captcha_error(last_err) and attempt < args.captcha_retries:
                if not silent:
                    print(f"  驗證碼錯誤，重試中… ({last_err})")
                continue
            raise

    raise RuntimeError(f"驗證碼重試達上限: {last_err}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="台灣高鐵訂票 CLI",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=f"車站代碼: {stations_help()}",
    )
    parser.add_argument("--start", help="出發站 (代碼或站名，例: 2 或 台北)")
    parser.add_argument("--dest", help="到達站")
    parser.add_argument("--date", help="出發日期 YYYY/MM/DD")
    parser.add_argument("--time", help="出發時間 (查詢起點)，例: 1230P / 0830A")
    parser.add_argument("--from-time", dest="time_from",
                        help="可接受最早出發時間 HH:MM (篩選用，例 09:00)")
    parser.add_argument("--until", dest="time_until",
                        help="可接受最晚出發時間 HH:MM (篩選用，例 11:00)")
    parser.add_argument("--retry", action="store_true",
                        help="若無符合條件車次，每隔幾秒重新刷票直到搶到")
    parser.add_argument("--retry-interval", type=int, default=10,
                        help="刷票間隔秒數 (預設 10)")
    parser.add_argument("--retry-max", type=int, default=0,
                        help="最大刷票次數，0 = 無限 (預設 0)")
    parser.add_argument("--adults", type=int, default=1, help="全票人數 (預設 1)")
    parser.add_argument("--id", dest="id_number", help="身分證或護照號碼")
    parser.add_argument("--phone", help="手機 (選填)")
    parser.add_argument("--email", help="Email (選填)")
    parser.add_argument("--no-browser", action="store_true", help="完成後不自動開啟付款頁")
    parser.add_argument("--manual-captcha", action="store_true",
                        help="不使用自動辨識，全部手動輸入驗證碼")
    parser.add_argument("--confirm-captcha", action="store_true",
                        help="自動辨識後仍要求人工確認 (按 Enter 接受 / 輸入新值覆寫)")
    parser.add_argument("--captcha-retries", type=int, default=CAPTCHA_RETRY_MAX,
                        help=f"驗證碼錯誤時最多重試次數 (預設 {CAPTCHA_RETRY_MAX})")
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

    try:
        from_min = _hhmm_to_min(args.time_from) if args.time_from else None
        until_min = _hhmm_to_min(args.time_until) if args.time_until else None
    except ValueError as e:
        print(f"錯誤: {e}", file=sys.stderr)
        return 2

    booker = THSRBooker()
    solver = None if args.manual_captcha else CaptchaSolver()
    if solver and not solver.available and not args.manual_captcha:
        print("[提示] 未安裝 ddddocr，將改為手動輸入驗證碼。"
              " 安裝: pip install ddddocr")
    if args.retry and (not solver or not solver.available) and not args.manual_captcha:
        print("[警告] 自動刷票模式建議搭配 ddddocr 使用，否則每輪都會要求手動輸入驗證碼。")

    query_params = {
        "start": start, "dest": dest,
        "date": date_str, "time": time_str,
        "adults": adults,
    }

    try:
        chosen: Optional[dict] = None
        poll_count = 0
        while True:
            poll_count += 1
            silent = args.retry and poll_count > 1
            tag = f" (刷票 #{poll_count})" if args.retry else ""
            print(f"\n[1/3] 載入查詢頁與驗證碼{tag}…")

            try:
                trains = query_for_trains(booker, solver, query_params, args,
                                           silent=silent)
            except RuntimeError as e:
                if args.retry and poll_count > 1:
                    print(f"  本輪查詢失敗: {e}")
                    if args.retry_max and poll_count >= args.retry_max:
                        print("已達最大刷票次數。", file=sys.stderr)
                        return 1
                    _sleep_with_countdown(args.retry_interval)
                    continue
                raise

            print(f"[2/3] 共取得 {len(trains)} 筆車次。")
            available = filter_trains(trains, from_min, until_min)
            sold = [t for t in trains if t.get("sold_out")]
            if sold and not args.retry:
                print(f"  其中 {len(sold)} 班已售完。")

            if available:
                if args.retry and poll_count > 1:
                    print(f"\n刷到車票！(第 {poll_count} 次刷新)")
                print("\n可選車次:")
                for i, t in enumerate(available, 1):
                    print(f"  [{i}] {t['train_no']:>8}  "
                          f"{t['depart']} → {t['arrive']}  ({t['duration']})")
                if args.retry and poll_count > 1:
                    chosen = available[0]
                    print(f"自動選擇: {chosen['train_no']}  "
                          f"{chosen['depart']} → {chosen['arrive']}")
                else:
                    choice_str = prompt(f"請選擇車次 1-{len(available)}", "1")
                    try:
                        choice = int(choice_str)
                        chosen = available[choice - 1]
                    except (ValueError, IndexError):
                        print("無效的車次選擇。", file=sys.stderr)
                        return 2
                break

            if not args.retry:
                print("找不到符合條件的可訂車次 (可能已售完或不在時間範圍內)。",
                      file=sys.stderr)
                return 1

            if args.retry_max and poll_count >= args.retry_max:
                print(f"已達最大刷票次數 ({args.retry_max})，仍無票可訂。",
                      file=sys.stderr)
                return 1

            print(f"  時間範圍內無可訂車次 (售完 {len(sold)}/總 {len(trains)})，"
                  f"{args.retry_interval}s 後重試… (Ctrl+C 取消)")
            _sleep_with_countdown(args.retry_interval)

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
