"""台灣高鐵訂票 - Streamlit Web App."""
from __future__ import annotations

import time
import traceback
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
    hhmm_to_thsr_table,
)
import thsr_auth as auth
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
    "logged_in": False,
    "username": "",
    "activity": [],          # list of (ts, level, msg)
    "last_traceback": "",
}


def log_event(level: str, msg: str) -> None:
    """Append to in-page activity log; level in {info, warn, error, success}."""
    ts = time.strftime("%H:%M:%S")
    st.session_state.activity.append((ts, level, msg))
    if len(st.session_state.activity) > 200:
        st.session_state.activity = st.session_state.activity[-200:]


def init_state() -> None:
    for k, v in DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v


def reset_booking() -> None:
    for k in ("step", "booker", "captcha_bytes", "captcha_hint", "trains",
              "available_trains", "chosen_train", "poll_count", "result",
              "error_msg", "stop_requested", "last_traceback"):
        st.session_state[k] = DEFAULTS[k]


# ----- auth gate -----

def render_auth_gate() -> bool:
    """Return True if the user is authenticated; otherwise render
    setup/login form and return False."""
    if st.session_state.get("logged_in"):
        return True

    use_env = auth.env_credentials() is not None
    has_file = auth.auth_exists()

    if not use_env and not has_file:
        st.title("🔐 初次設定")
        st.info("請建立管理員帳號 (PBKDF2-SHA256 雜湊保存於本機)")
        u = st.text_input("使用者名稱", key="setup_user")
        p1 = st.text_input("密碼", type="password", key="setup_pw1")
        p2 = st.text_input("確認密碼", type="password", key="setup_pw2")
        if st.button("建立並登入", type="primary", use_container_width=True):
            if not u.strip() or not p1:
                st.error("帳號密碼不可為空")
            elif p1 != p2:
                st.error("兩次密碼不符")
            elif len(p1) < 6:
                st.error("密碼至少 6 字元")
            else:
                auth.save_auth(u.strip(), p1)
                st.session_state.logged_in = True
                st.session_state.username = u.strip()
                st.rerun()
        return False

    st.title("🔐 登入")
    if use_env:
        st.caption("帳密由環境變數 THSR_AUTH_USER / THSR_AUTH_PASS 提供")
    u = st.text_input("使用者", key="login_user")
    p = st.text_input("密碼", type="password", key="login_pw")
    if st.button("登入", type="primary", use_container_width=True):
        if auth.authenticate(u, p):
            st.session_state.logged_in = True
            st.session_state.username = u
            st.rerun()
        else:
            st.error("帳號或密碼錯誤")
    return False


def render_logout() -> None:
    with st.sidebar:
        st.caption(f"已登入：{st.session_state.username}")
        if st.button("登出", use_container_width=True):
            for k in ("logged_in", "username", "vault"):
                st.session_state[k] = DEFAULTS[k]
            st.rerun()
        st.divider()


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

DEFAULT_START = "4"        # 桃園
DEFAULT_DEST = "12"        # 左營
DEFAULT_TIME_HHMM = "18:40"
DEFAULT_ID = "E122973276"


def render_form() -> None:
    st.subheader("行程")
    f = st.session_state.form_data

    cols = st.columns(2)
    start_default = _station_index(f.get("start", DEFAULT_START))
    dest_default = _station_index(f.get("dest", DEFAULT_DEST))
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
        time_str = st.text_input(
            "出發時間 HH:MM (例 18:40)",
            value=f.get("time") or DEFAULT_TIME_HHMM,
            key="w_time",
            help="24h 格式；高鐵以 30 分鐘為一個時段，自動向下取整。也可直接填 0630P。",
        )
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
        id_number = st.text_input("身分證/護照",
                                   value=f.get("id_number") or DEFAULT_ID,
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
        try:
            thsr_time = hhmm_to_thsr_table(time_str)
        except ValueError as e:
            st.error(f"時間格式錯誤: {e}")
            return
        if thsr_time != time_str.strip().upper():
            st.caption(f"已將 {time_str} 轉成高鐵時段 `{thsr_time}`")
        st.session_state.form_data = {
            "start": start[0], "dest": dest[0],
            "date": date_str.strip(), "time": thsr_time,
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
        log_event(
            "info",
            f"開始訂票 {start[0]}→{dest[0]}  {date_str} {time_str}  "
            f"窗口 {time_from or '*'}~{time_until or '*'}  "
            f"retry={retry}",
        )
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

    poll_n = st.session_state.poll_count + 1
    st.info(f"刷票 #{poll_n}: 載入查詢頁與驗證碼…")
    st.button("⏹ 取消", on_click=_cancel)
    log_event("info", f"刷票 #{poll_n}: 載入查詢頁")

    try:
        with st.spinner("連線高鐵網站中…"):
            captcha_bytes = booker.step1_load()
        log_event("info", f"已取得驗證碼圖 ({len(captcha_bytes):,} bytes)")
    except Exception as e:
        log_event("error", f"載入頁面失敗: {type(e).__name__}: {e}")
        _to_error(f"載入頁面失敗: {e}", exc=e)
        return

    captcha = ""
    if not f.get("manual_captcha"):
        try:
            with st.spinner("自動辨識驗證碼中…"):
                solver = CaptchaSolver()
                if solver.available:
                    captcha = solver.solve(captcha_bytes)
                    log_event("info", f"自動辨識: {captcha or '(空)'} "
                                      f"(長度 {len(captcha)})")
                else:
                    log_event("warn", "ddddocr 不可用，需手動輸入驗證碼")
        except Exception as e:
            log_event("error", f"驗證碼辨識例外: {e}")

    if captcha and len(captcha) == CAPTCHA_LEN:
        try:
            with st.spinner("送出查詢…"):
                trains = booker.step1_submit(_query_params(f), captcha)
            log_event("success", f"查詢成功，回傳 {len(trains)} 班車")
            _post_query(trains)
            return
        except RuntimeError as e:
            if not _is_captcha_error(str(e)):
                log_event("error", f"查詢失敗: {e}")
                _to_error(f"查詢失敗: {e}", exc=e)
                return
            log_event("warn", f"驗證碼錯誤: {e}")
        except Exception as e:
            log_event("error", f"查詢例外: {type(e).__name__}: {e}")
            _to_error(f"查詢例外: {e}", exc=e)
            return

    log_event("info", "需要使用者輸入驗證碼")
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
        log_event("info", f"使用者輸入驗證碼: {val}")
        try:
            booker: THSRBooker = st.session_state.booker
            trains = booker.step1_submit(_query_params(st.session_state.form_data), val)
            log_event("success", f"查詢成功，回傳 {len(trains)} 班車")
            _post_query(trains)
        except RuntimeError as e:
            if _is_captcha_error(str(e)):
                log_event("warn", f"驗證碼錯誤: {e}")
                st.warning("驗證碼錯誤，重新載入…")
                st.session_state.step = "querying"
                st.rerun()
            else:
                log_event("error", f"查詢失敗: {e}")
                _to_error(f"查詢失敗: {e}", exc=e)
        except Exception as e:
            log_event("error", f"查詢例外: {e}")
            _to_error(f"查詢例外: {e}", exc=e)
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
    sold = sum(1 for t in trains if t.get("sold_out"))
    log_event("info", f"過濾後 {len(avail)} 班可訂 (總 {len(trains)}, 售完 {sold})")

    if avail:
        st.session_state.step = "trains"
        st.rerun()
        return

    st.session_state.poll_count += 1
    if not f.get("retry"):
        _to_error(f"時間範圍內無可訂票 (查到 {len(trains)} 班、售完 {sold} 班)。")
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
    log_event("info", f"確認車次 {chosen['train_no']} ({chosen['depart']} → {chosen['arrive']})")
    try:
        with st.spinner("確認車次…"):
            booker.step2_submit(chosen["value"])
        st.info("送出乘客資料…")
        log_event("info", "送出乘客資料")
        with st.spinner("送出乘客資料…"):
            result = booker.step3_submit({
                "id_number": f["id_number"],
                "phone": f["phone"],
                "email": f["email"],
            })
        log_event("success", f"訂位成功 PNR={result.get('pnr') or '?'}")
        st.session_state.result = result
        st.session_state.step = "done"
        st.rerun()
    except RuntimeError as e:
        log_event("error", f"訂位失敗: {e}")
        _to_error(f"訂位失敗: {e}", exc=e)
    except Exception as e:
        log_event("error", f"訂位例外: {e}")
        _to_error(f"訂位例外: {e}", exc=e)


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
    booker = st.session_state.get("booker")
    if booker is not None and getattr(booker, "last_response", None) is not None:
        r = booker.last_response
        with st.expander("最後一次 HTTP 回應"):
            st.code(
                f"{r.request.method} {r.url}\n→ HTTP {r.status_code}  "
                f"({len(r.content):,} bytes)",
                language="text",
            )
            preview = r.text[:1500] if r.text else ""
            if preview:
                st.code(preview, language="html")
    if st.session_state.last_traceback:
        with st.expander("Traceback"):
            st.code(st.session_state.last_traceback, language="python")
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

def _to_error(msg: str, exc: Optional[BaseException] = None) -> None:
    st.session_state.error_msg = msg
    if exc is not None:
        st.session_state.last_traceback = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    st.session_state.step = "error"
    st.rerun()


def _cancel() -> None:
    st.session_state.stop_requested = True


# ----- diagnostics + activity log -----

def render_diagnostics() -> None:
    with st.sidebar.expander("🔧 系統狀態 / 診斷"):
        try:
            import ddddocr  # noqa: F401
            st.write("✅ ddddocr 可用 (自動驗證碼)")
        except Exception as e:
            st.write(f"❌ ddddocr 不可用: {e}")

        try:
            import cryptography  # noqa: F401
            st.write("✅ cryptography 可用 (加密信用卡)")
        except Exception as e:
            st.write(f"❌ cryptography 不可用: {e}")

        try:
            from PIL import Image as _PILImage  # noqa: F401
            st.write("✅ Pillow 可用")
        except Exception as e:
            st.write(f"❌ Pillow 不可用: {e}")

        st.write(f"📁 資料目錄: `{store.CONFIG_DIR}`")
        st.write(f"📑 Profile 數: {len(store.list_profiles())}")
        st.write(f"💳 卡片庫: {'存在' if store.vault_exists() else '尚未建立'}")
        st.write(f"🔐 Auth 設定: "
                 f"{'env vars' if auth.env_credentials() else ('檔案' if auth.auth_exists() else '尚未建立')}")

        if st.button("測試 THSR 連線"):
            try:
                r = requests.get(
                    "https://irs.thsrc.com.tw/IMINT/?locale=tw",
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                st.write(f"HTTP {r.status_code} • {len(r.content):,} bytes")
                if r.status_code == 200:
                    st.success("✅ 連線正常")
                else:
                    st.warning(f"非預期狀態碼: {r.status_code}")
            except Exception as e:
                st.error(f"連線失敗: {type(e).__name__}: {e}")


def render_activity_log() -> None:
    n = len(st.session_state.activity)
    label = f"📜 活動記錄 ({n})"
    with st.expander(label, expanded=(n > 0 and st.session_state.step == "error")):
        if not st.session_state.activity:
            st.caption("(尚無記錄)")
            return
        icons = {"info": "ℹ️", "warn": "⚠️", "error": "❌", "success": "✅"}
        for ts, level, msg in reversed(st.session_state.activity[-100:]):
            st.write(f"`{ts}` {icons.get(level, '•')} {msg}")
        if st.button("清除記錄"):
            st.session_state.activity = []
            st.rerun()


# ----- entry -----

def main() -> None:
    st.set_page_config(page_title="台灣高鐵訂票", page_icon="🚄",
                       layout="centered",
                       initial_sidebar_state="auto",
                       menu_items={"About": "Taiwan HSR booking helper"})
    init_state()
    if not render_auth_gate():
        return
    st.title("🚄 台灣高鐵訂票")
    render_logout()
    render_sidebar()
    render_diagnostics()
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

    st.divider()
    render_activity_log()


main()
