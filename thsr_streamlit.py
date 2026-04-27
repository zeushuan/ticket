"""台灣高鐵訂票 - Streamlit Web App.

部署在 Synology NAS 容器，手機瀏覽器即可訂票。
- 支援 Profile (記住車站/日期/乘客) 與本地加密信用卡保險庫
- ddddocr 自動辨識驗證碼，失敗時頁面顯示圖片讓使用者輸入
- 自動刷票：時間範圍內無票時定時重新查詢
- 訂位成功後給付款連結，使用者在手機瀏覽器手動填寫信用卡完成付款
"""
from __future__ import annotations

import time
from datetime import date
from typing import Optional

import requests
import streamlit as st

from thsr_book import (
    CAPTCHA_LEN,
    STATIONS,
    CaptchaSolver,
    THSRBooker,
    _hhmm_to_min,
    _is_captcha_error,
    filter_trains,
)
import thsr_storage as store


STATION_OPTIONS = [(code, f"{code} {name}") for code, name in STATIONS.items()]


# ----- session state init -----

DEFAULTS = {
    "step": "form",          # form | querying | captcha | trains | submitting | done | error
    "booker": None,
    "captcha_bytes": None,
    "captcha_hint": "",
    "form_data": {},
    "trains": [],
    "available_trains": [],
    "chosen_train": None,
    "poll_count": 0,
    "result": None,
    "error_msg": "",
    "vault": None,
    "show_card": None,
    "stop_requested": False,
}


def init_state() -> None:
    for k, v in DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v


def reset_booking() -> None:
    for k in ("step", "booker", "captcha_bytes", "captcha_hint", "trains",
              "available_trains", "chosen_train", "poll_count", "result",
              "error_msg", "stop_requested"):
        st.session_state[k] = DEFAULTS[k]


# ----- sidebar: profiles + cards -----

def render_sidebar() -> None:
    with st.sidebar:
        st.header("Profile")
        names = store.list_profiles()
        active = store.active_profile_name() or ""
        idx = (names.index(active) + 1) if active in names else 0
        sel = st.selectbox("已存設定", [""] + names, index=idx)
        if sel and st.button("套用此 Profile", use_container_width=True):
            apply_profile(sel)
            st.rerun()

        new_name = st.text_input("Profile 名稱", value=sel)
        col1, col2 = st.columns(2)
        if col1.button("儲存", use_container_width=True):
            if new_name.strip():
                store.upsert_profile(new_name.strip(), collect_form_for_save())
                st.success(f"已存 {new_name}")
                st.rerun()
            else:
                st.warning("請輸入名稱")
        if col2.button("刪除", use_container_width=True, disabled=not sel):
            store.delete_profile(sel)
            st.rerun()

        st.divider()
        st.header("信用卡保險庫")
        render_card_vault()


def collect_form_for_save() -> dict:
    f = st.session_state.form_data
    return {k: f.get(k, "") for k in store.PROFILE_FIELDS}


def apply_profile(name: str) -> None:
    p = store.get_profile(name) or {}
    f = st.session_state.form_data
    for k in store.PROFILE_FIELDS:
        if k in p:
            f[k] = p[k]
    # propagate to widgets
    for k in store.PROFILE_FIELDS:
        st.session_state[f"w_{k}"] = p.get(k, f.get(k, ""))


def render_card_vault() -> None:
    if st.session_state.vault is None:
        existed = store.vault_exists()
        st.caption("解鎖以使用已存信用卡" if existed else "首次使用：建立主密碼以加密信用卡")
        pw = st.text_input("主密碼", type="password", key="vault_pw")
        if st.button("解鎖 / 建立", use_container_width=True):
            if not pw:
                st.warning("請輸入主密碼")
                return
            try:
                v = store.CardVault(pw)
                v.load()
                st.session_state.vault = v
                st.rerun()
            except store.WrongPassword:
                st.error("主密碼錯誤")
            except RuntimeError as e:
                st.error(str(e))
        return

    v: store.CardVault = st.session_state.vault
    st.success(f"已解鎖 ({len(v.cards)} 張卡)")
    if st.button("鎖回保險庫", use_container_width=True):
        st.session_state.vault = None
        st.rerun()

    if v.cards:
        labels = [c.get("alias", f"#{i}") for i, c in enumerate(v.cards)]
        st.caption("管理卡片")
        sel = st.selectbox("選擇卡片", labels, key="card_select")
        idx = labels.index(sel) if sel in labels else 0
        c1, c2 = st.columns(2)
        if c1.button("顯示卡號", use_container_width=True):
            st.session_state.show_card = idx
            st.rerun()
        if c2.button("刪除卡片", use_container_width=True):
            cards = list(v.cards)
            cards.pop(idx)
            v.save(cards)
            st.session_state.show_card = None
            st.rerun()

    st.caption("新增 / 編輯")
    edit_idx: Optional[int] = None
    if v.cards:
        opts = ["(新增)"] + [c.get("alias", f"#{i}") for i, c in enumerate(v.cards)]
        choice = st.selectbox("操作", opts, key="card_edit_select")
        edit_idx = None if choice == "(新增)" else opts.index(choice) - 1
    existing = v.cards[edit_idx] if edit_idx is not None else {}

    alias = st.text_input("別名", value=existing.get("alias", ""), key="card_alias")
    holder = st.text_input("持卡人", value=existing.get("holder", ""), key="card_holder")
    number = st.text_input("卡號", value=existing.get("number", ""), key="card_number")
    expiry = st.text_input("到期 MM/YY", value=existing.get("expiry", ""), key="card_expiry")
    cvc = st.text_input("CVC (選填)", type="password",
                        value=existing.get("cvc", ""), key="card_cvc")
    if st.button("儲存卡片", use_container_width=True):
        if not alias or not number:
            st.warning("別名與卡號必填")
        else:
            entry = {"alias": alias, "holder": holder, "number": number,
                     "expiry": expiry, "cvc": cvc}
            cards = list(v.cards)
            if edit_idx is None:
                cards.append(entry)
            else:
                cards[edit_idx] = entry
            v.save(cards)
            st.success("已儲存")
            st.rerun()


# ----- main form -----

def render_form() -> None:
    st.subheader("行程")
    f = st.session_state.form_data

    cols = st.columns(2)
    start_default = _station_index(f.get("start", "2"))
    dest_default = _station_index(f.get("dest", "12"))
    with cols[0]:
        start = st.selectbox("出發站", STATION_OPTIONS, index=start_default,
                              format_func=lambda x: x[1], key="w_start")
    with cols[1]:
        dest = st.selectbox("到達站", STATION_OPTIONS, index=dest_default,
                             format_func=lambda x: x[1], key="w_dest")

    cols = st.columns(3)
    with cols[0]:
        d_str = f.get("date") or date.today().strftime("%Y/%m/%d")
        date_str = st.text_input("日期 yyyy/mm/dd", value=d_str, key="w_date")
    with cols[1]:
        time_str = st.text_input("查詢起點時間 (例 1230P)",
                                 value=f.get("time") or "1230P", key="w_time")
    with cols[2]:
        adults = st.number_input("全票人數", min_value=1, max_value=10,
                                  value=int(f.get("adults") or 1), key="w_adults")

    cols = st.columns(2)
    with cols[0]:
        time_from = st.text_input("最早可接受 HH:MM (選填)",
                                   value=f.get("time_from", ""), key="w_time_from")
    with cols[1]:
        time_until = st.text_input("最晚可接受 HH:MM (選填)",
                                    value=f.get("time_until", ""), key="w_time_until")

    st.subheader("乘客")
    cols = st.columns(3)
    with cols[0]:
        id_number = st.text_input("身分證/護照", value=f.get("id_number", ""),
                                   key="w_id_number")
    with cols[1]:
        phone = st.text_input("手機 (選填)", value=f.get("phone", ""), key="w_phone")
    with cols[2]:
        email = st.text_input("Email (選填)", value=f.get("email", ""), key="w_email")

    st.subheader("選項")
    cols = st.columns(3)
    with cols[0]:
        retry = st.checkbox("無票時自動刷票", value=bool(f.get("retry", False)),
                             key="w_retry")
    with cols[1]:
        retry_interval = st.number_input("刷票間隔(秒)", min_value=3, max_value=300,
                                          value=int(f.get("retry_interval") or 10),
                                          key="w_retry_interval")
    with cols[2]:
        retry_max = st.number_input("最大次數 (0=無限)", min_value=0, max_value=9999,
                                     value=int(f.get("retry_max") or 0),
                                     key="w_retry_max")

    manual_captcha = st.checkbox("關閉自動辨識，手動輸入驗證碼",
                                  value=False, key="w_manual_captcha")

    st.divider()
    if st.button("🚄 開始訂票", type="primary", use_container_width=True):
        # capture values
        st.session_state.form_data = {
            "start": start[0], "dest": dest[0],
            "date": date_str.strip(), "time": time_str.strip(),
            "time_from": time_from.strip(), "time_until": time_until.strip(),
            "adults": int(adults),
            "id_number": id_number.strip(), "phone": phone.strip(),
            "email": email.strip(),
            "retry": retry, "retry_interval": int(retry_interval),
            "retry_max": int(retry_max),
            "manual_captcha": manual_captcha,
        }
        if not id_number.strip():
            st.error("請輸入身分證/護照號碼")
            return
        try:
            _hhmm_to_min(time_from) if time_from.strip() else None
            _hhmm_to_min(time_until) if time_until.strip() else None
        except ValueError as e:
            st.error(str(e))
            return
        st.session_state.booker = THSRBooker()
        st.session_state.poll_count = 0
        st.session_state.step = "querying"
        st.rerun()


def _station_index(code: str) -> int:
    for i, (c, _) in enumerate(STATION_OPTIONS):
        if c == code:
            return i
    return 0


# ----- query / captcha / polling -----

def render_querying() -> None:
    f = st.session_state.form_data
    booker: THSRBooker = st.session_state.booker

    st.info(f"刷票 #{st.session_state.poll_count + 1}: 載入查詢頁與驗證碼…")
    st.button("⏹ 取消", on_click=_cancel)

    try:
        captcha_bytes = booker.step1_load()
    except (requests.HTTPError, requests.ConnectionError) as e:
        _to_error(f"載入頁面失敗: {e}")
        return

    captcha = ""
    if not f.get("manual_captcha"):
        solver = CaptchaSolver()
        if solver.available:
            captcha = solver.solve(captcha_bytes)

    if captcha and len(captcha) == CAPTCHA_LEN:
        try:
            trains = booker.step1_submit(_query_params(f), captcha)
            _post_query(trains)
            return
        except RuntimeError as e:
            if not _is_captcha_error(str(e)):
                _to_error(f"查詢失敗: {e}")
                return
            # captcha was wrong, fall through to manual

    # need user input
    st.session_state.captcha_bytes = captcha_bytes
    st.session_state.captcha_hint = captcha
    st.session_state.step = "captcha"
    st.rerun()


def render_captcha() -> None:
    st.subheader("輸入驗證碼")
    if st.session_state.captcha_bytes:
        st.image(st.session_state.captcha_bytes, width=240)
    val = st.text_input("驗證碼", value=st.session_state.captcha_hint,
                        max_chars=8, key="captcha_input").strip().upper()
    cols = st.columns(2)
    if cols[0].button("送出", type="primary", use_container_width=True):
        if not val:
            st.warning("請輸入")
            return
        try:
            booker: THSRBooker = st.session_state.booker
            trains = booker.step1_submit(_query_params(st.session_state.form_data), val)
            _post_query(trains)
        except RuntimeError as e:
            if _is_captcha_error(str(e)):
                st.warning("驗證碼錯誤，重新載入…")
                st.session_state.step = "querying"
                st.rerun()
            else:
                _to_error(f"查詢失敗: {e}")
    if cols[1].button("取消", use_container_width=True):
        _cancel()
        st.rerun()


def _query_params(f: dict) -> dict:
    return {
        "start": f["start"], "dest": f["dest"],
        "date": f["date"], "time": f["time"],
        "adults": f["adults"],
    }


def _post_query(trains: list) -> None:
    f = st.session_state.form_data
    st.session_state.trains = trains
    from_min = _hhmm_to_min(f["time_from"]) if f.get("time_from") else None
    until_min = _hhmm_to_min(f["time_until"]) if f.get("time_until") else None
    avail = filter_trains(trains, from_min, until_min)
    st.session_state.available_trains = avail

    if avail:
        st.session_state.step = "trains"
        st.rerun()
        return

    st.session_state.poll_count += 1
    if not f.get("retry"):
        _to_error(f"時間範圍內無可訂票 (查到 {len(trains)} 班、售完 "
                  f"{sum(1 for t in trains if t.get('sold_out'))} 班)。")
        return

    if f.get("retry_max") and st.session_state.poll_count >= f["retry_max"]:
        _to_error(f"已達最大刷票次數 {f['retry_max']}。")
        return

    interval = int(f.get("retry_interval") or 10)
    placeholder = st.empty()
    for i in range(interval, 0, -1):
        if st.session_state.stop_requested:
            break
        placeholder.warning(f"無可訂票 (#{st.session_state.poll_count})，"
                            f"{i}s 後重試… 點頁面上方「取消」可停止")
        time.sleep(1)
    placeholder.empty()

    if st.session_state.stop_requested:
        _to_error("已取消")
        return

    st.session_state.step = "querying"
    st.rerun()


def render_trains() -> None:
    st.success(f"刷到 {len(st.session_state.available_trains)} 班可訂車次")
    f = st.session_state.form_data
    auto = bool(f.get("retry")) and st.session_state.poll_count > 1

    for i, t in enumerate(st.session_state.available_trains):
        label = (f"{t['train_no']:>6}  {t['depart']} → {t['arrive']}  "
                 f"({t['duration']})")
        if st.button(label, key=f"train_{i}", use_container_width=True,
                     type=("primary" if i == 0 and auto else "secondary")):
            st.session_state.chosen_train = t
            st.session_state.step = "submitting"
            st.rerun()

    if auto and st.session_state.available_trains:
        st.session_state.chosen_train = st.session_state.available_trains[0]
        st.session_state.step = "submitting"
        st.rerun()


def render_submitting() -> None:
    f = st.session_state.form_data
    booker: THSRBooker = st.session_state.booker
    chosen = st.session_state.chosen_train
    st.info(f"確認車次 {chosen['train_no']}…")
    try:
        booker.step2_submit(chosen["value"])
        st.info("送出乘客資料…")
        result = booker.step3_submit({
            "id_number": f["id_number"],
            "phone": f["phone"],
            "email": f["email"],
        })
        st.session_state.result = result
        st.session_state.step = "done"
        st.rerun()
    except RuntimeError as e:
        _to_error(f"訂位失敗: {e}")


def render_done() -> None:
    r = st.session_state.result or {}
    st.balloons()
    st.success("=== 訂位成功 ===")
    if r.get("pnr"):
        st.markdown(f"### 訂位代號 PNR: `{r['pnr']}`")
    payment = r.get("payment_url") or r.get("confirm_url")
    if payment:
        st.markdown(f"**付款連結**：[{payment}]({payment})")
        st.link_button("🔗 開啟付款頁 (在新分頁手動填寫信用卡)", payment,
                       use_container_width=True)

    v: Optional[store.CardVault] = st.session_state.vault
    if v and v.cards:
        st.divider()
        st.subheader("已存信用卡 (複製到付款頁)")
        labels = [c.get("alias", f"#{i}") for i, c in enumerate(v.cards)]
        sel = st.selectbox("選擇卡片", labels)
        idx = labels.index(sel)
        c = v.cards[idx]
        st.code(f"持卡人: {c.get('holder','')}\n"
                f"卡號:   {c.get('number','')}\n"
                f"到期:   {c.get('expiry','')}\n"
                f"CVC:    {c.get('cvc','') or '(未存)'}",
                language="text")

    st.divider()
    if st.button("🔄 再訂一張", use_container_width=True):
        reset_booking()
        st.rerun()


def render_error() -> None:
    st.error(st.session_state.error_msg)
    if st.button("⬅ 回表單", use_container_width=True):
        reset_booking()
        st.rerun()


def render_show_card() -> None:
    v: store.CardVault = st.session_state.vault
    if v is None or st.session_state.show_card is None:
        return
    idx = st.session_state.show_card
    if idx >= len(v.cards):
        st.session_state.show_card = None
        return
    c = v.cards[idx]
    with st.expander(f"卡片：{c.get('alias','')}", expanded=True):
        st.code(f"持卡人: {c.get('holder','')}\n"
                f"卡號:   {c.get('number','')}\n"
                f"到期:   {c.get('expiry','')}\n"
                f"CVC:    {c.get('cvc','') or '(未存)'}",
                language="text")
        if st.button("關閉"):
            st.session_state.show_card = None
            st.rerun()


# ----- helpers -----

def _to_error(msg: str) -> None:
    st.session_state.error_msg = msg
    st.session_state.step = "error"
    st.rerun()


def _cancel() -> None:
    st.session_state.stop_requested = True


# ----- entry -----

def main() -> None:
    st.set_page_config(page_title="台灣高鐵訂票", page_icon="🚄",
                       layout="centered",
                       initial_sidebar_state="auto",
                       menu_items={"About": "Taiwan HSR booking helper"})
    init_state()
    st.title("🚄 台灣高鐵訂票")
    render_sidebar()
    render_show_card()

    step = st.session_state.step
    if step == "form":
        render_form()
    elif step == "querying":
        render_querying()
    elif step == "captcha":
        render_captcha()
    elif step == "trains":
        render_trains()
    elif step == "submitting":
        render_submitting()
    elif step == "done":
        render_done()
    elif step == "error":
        render_error()


main()
