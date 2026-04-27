#!/usr/bin/env python3
"""台灣高鐵訂票 GUI (Tkinter)。

- 表單輸入 / 一鍵帶入儲存的 Profile
- 已存信用卡 (主密碼加密) 可選；訂位完成後可顯示卡片資訊供複製到付款頁
- 自動辨識驗證碼 (ddddocr)，必要時跳出視窗讓你手動輸入
- 自動刷票：時間範圍內查無票時定時重試
- 訂位成功後在瀏覽器開啟付款頁，由你手動填寫信用卡完成付款
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import webbrowser
from io import BytesIO
from tkinter import messagebox, simpledialog, ttk
from typing import Optional

import requests

from thsr_book import (
    CAPTCHA_LEN,
    STATIONS,
    CaptchaSolver,
    THSRBooker,
    _hhmm_to_min,
    _is_captcha_error,
    filter_trains,
    resolve_station,
)
import thsr_storage as store


STATION_OPTIONS = [f"{code} {name}" for code, name in STATIONS.items()]
STATION_LABEL_TO_CODE = {f"{code} {name}": code for code, name in STATIONS.items()}


def _try_pil():
    try:
        from PIL import Image, ImageTk  # type: ignore
        return Image, ImageTk
    except ImportError:
        return None, None


# ---------- captcha dialog ----------

class CaptchaDialog(tk.Toplevel):
    def __init__(self, master, image_bytes: bytes, hint: str = ""):
        super().__init__(master)
        self.title("輸入驗證碼")
        self.resizable(False, False)
        self.result: Optional[str] = None
        self._photo = None

        Image, ImageTk = _try_pil()
        if Image and ImageTk:
            img = Image.open(BytesIO(image_bytes))
            img = img.resize((img.width * 2, img.height * 2))
            self._photo = ImageTk.PhotoImage(img)
            tk.Label(self, image=self._photo).pack(padx=12, pady=8)
        else:
            tk.Label(self, text="(請安裝 Pillow 以顯示驗證碼圖)",
                     fg="red").pack(padx=12, pady=8)

        frame = ttk.Frame(self)
        frame.pack(padx=12, pady=4, fill="x")
        ttk.Label(frame, text="驗證碼:").pack(side="left")
        self.var = tk.StringVar(value=hint)
        entry = ttk.Entry(frame, textvariable=self.var, width=12)
        entry.pack(side="left", padx=4)
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self._ok())

        btns = ttk.Frame(self)
        btns.pack(padx=12, pady=8, fill="x")
        ttk.Button(btns, text="確定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right")

        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _ok(self):
        self.result = self.var.get().strip().upper()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ---------- card manager ----------

class CardManager(tk.Toplevel):
    def __init__(self, master, vault: store.CardVault):
        super().__init__(master)
        self.title("信用卡管理")
        self.geometry("520x320")
        self.vault = vault

        self.tree = ttk.Treeview(self, columns=("alias", "holder", "number", "expiry"),
                                 show="headings", height=8)
        for col, head, w in [
            ("alias", "別名", 110),
            ("holder", "持卡人", 110),
            ("number", "卡號", 180),
            ("expiry", "到期", 60),
        ]:
            self.tree.heading(col, text=head)
            self.tree.column(col, width=w)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=4)
        ttk.Button(btns, text="新增", command=self._add).pack(side="left")
        ttk.Button(btns, text="編輯", command=self._edit).pack(side="left", padx=4)
        ttk.Button(btns, text="刪除", command=self._delete).pack(side="left")
        ttk.Button(btns, text="關閉", command=self.destroy).pack(side="right")

        self._refresh()

    def _refresh(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for i, c in enumerate(self.vault.cards):
            self.tree.insert(
                "", "end", iid=str(i),
                values=(
                    c.get("alias", ""),
                    c.get("holder", ""),
                    store.mask_card_number(c.get("number", "")),
                    c.get("expiry", ""),
                ),
            )

    def _add(self):
        card = CardEditor(self).result
        if card:
            cards = list(self.vault.cards) + [card]
            self.vault.save(cards)
            self._refresh()

    def _edit(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        existing = self.vault.cards[idx]
        card = CardEditor(self, existing).result
        if card:
            cards = list(self.vault.cards)
            cards[idx] = card
            self.vault.save(cards)
            self._refresh()

    def _delete(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if not messagebox.askyesno("確認", "刪除此卡片？", parent=self):
            return
        cards = list(self.vault.cards)
        cards.pop(idx)
        self.vault.save(cards)
        self._refresh()


class CardEditor(tk.Toplevel):
    FIELDS = [
        ("alias", "別名 (例: 中信白金)"),
        ("holder", "持卡人姓名"),
        ("number", "卡號"),
        ("expiry", "到期 MM/YY"),
        ("cvc", "CVC (選填)"),
        ("note", "備註 (選填)"),
    ]

    def __init__(self, master, existing: Optional[dict] = None):
        super().__init__(master)
        self.title("編輯卡片" if existing else "新增卡片")
        self.resizable(False, False)
        self.result: Optional[dict] = None
        self.vars: dict[str, tk.StringVar] = {}

        body = ttk.Frame(self)
        body.pack(padx=12, pady=8, fill="x")
        for i, (key, label) in enumerate(self.FIELDS):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=(existing or {}).get(key, ""))
            self.vars[key] = v
            show = "*" if key == "cvc" else None
            ttk.Entry(body, textvariable=v, width=30, show=show).grid(
                row=i, column=1, sticky="ew", padx=4, pady=2
            )
        body.columnconfigure(1, weight=1)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=8)
        ttk.Button(btns, text="儲存", command=self._save).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")

        self.transient(master)
        self.grab_set()
        self.wait_window()

    def _save(self):
        data = {k: v.get().strip() for k, v in self.vars.items()}
        if not data["alias"] or not data["number"]:
            messagebox.showerror("錯誤", "別名與卡號必填", parent=self)
            return
        self.result = data
        self.destroy()


# ---------- main app ----------

class BookingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("台灣高鐵訂票")
        self.geometry("760x720")

        self._stop = threading.Event()
        self._captcha_event = threading.Event()
        self._captcha_holder: dict = {}
        self._log_queue: "queue.Queue[str]" = queue.Queue()

        self._vault: Optional[store.CardVault] = None
        self._cards: list[dict] = []

        self._build_ui()
        self._load_active_profile()
        self.after(100, self._drain_log)

    # ----- UI construction -----

    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True)

        # ---- profile bar ----
        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Label(bar, text="Profile:").pack(side="left")
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(bar, textvariable=self.profile_var,
                                          values=store.list_profiles(), width=24)
        self.profile_combo.pack(side="left", padx=4)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_selected_profile())
        ttk.Button(bar, text="儲存", command=self._save_profile).pack(side="left")
        ttk.Button(bar, text="刪除", command=self._delete_profile).pack(side="left", padx=4)

        # ---- trip + passenger ----
        body = ttk.LabelFrame(root, text="訂票資料")
        body.pack(fill="x", padx=8, pady=6)

        self.start_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        self.date_var = tk.StringVar()
        self.time_var = tk.StringVar(value="1230P")
        self.from_var = tk.StringVar()
        self.until_var = tk.StringVar()
        self.adults_var = tk.IntVar(value=1)

        self.id_var = tk.StringVar()
        self.phone_var = tk.StringVar()
        self.email_var = tk.StringVar()

        rows = [
            ("出發站", self._station_combo(body, self.start_var), None, None),
            ("到達站", self._station_combo(body, self.dest_var), None, None),
            ("出發日期 yyyy/mm/dd", ttk.Entry(body, textvariable=self.date_var, width=14),
             "查詢起點時間 (例 1230P)", ttk.Entry(body, textvariable=self.time_var, width=10)),
            ("最早可接受 HH:MM", ttk.Entry(body, textvariable=self.from_var, width=8),
             "最晚可接受 HH:MM", ttk.Entry(body, textvariable=self.until_var, width=8)),
            ("全票人數", ttk.Spinbox(body, from_=1, to=10, textvariable=self.adults_var, width=5),
             None, None),
            ("身分證/護照", ttk.Entry(body, textvariable=self.id_var, width=20),
             "手機 (選填)", ttk.Entry(body, textvariable=self.phone_var, width=14)),
            ("Email (選填)", ttk.Entry(body, textvariable=self.email_var, width=24), None, None),
        ]
        for r, (l1, w1, l2, w2) in enumerate(rows):
            ttk.Label(body, text=l1).grid(row=r, column=0, sticky="w", **pad)
            w1.grid(row=r, column=1, sticky="w", **pad)
            if l2 is not None:
                ttk.Label(body, text=l2).grid(row=r, column=2, sticky="w", **pad)
                w2.grid(row=r, column=3, sticky="w", **pad)

        # ---- options ----
        opts = ttk.LabelFrame(root, text="選項")
        opts.pack(fill="x", padx=8, pady=6)
        self.retry_var = tk.BooleanVar(value=False)
        self.retry_interval_var = tk.IntVar(value=10)
        self.retry_max_var = tk.IntVar(value=0)
        self.manual_capt_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="無票時自動刷票",
                        variable=self.retry_var).grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(opts, text="刷票間隔(秒)").grid(row=0, column=1, sticky="e", **pad)
        ttk.Spinbox(opts, from_=3, to=300, textvariable=self.retry_interval_var,
                    width=5).grid(row=0, column=2, sticky="w", **pad)
        ttk.Label(opts, text="最大次數 (0=無限)").grid(row=0, column=3, sticky="e", **pad)
        ttk.Spinbox(opts, from_=0, to=9999, textvariable=self.retry_max_var,
                    width=6).grid(row=0, column=4, sticky="w", **pad)
        ttk.Checkbutton(opts, text="關閉自動辨識，手動輸入驗證碼",
                        variable=self.manual_capt_var).grid(row=1, column=0, columnspan=3,
                                                            sticky="w", **pad)

        # ---- card section ----
        cardf = ttk.LabelFrame(root, text="信用卡 (本地加密保存；訂位後手動填到付款頁)")
        cardf.pack(fill="x", padx=8, pady=6)
        ttk.Button(cardf, text="解鎖/載入", command=self._unlock_vault).grid(row=0, column=0, **pad)
        ttk.Button(cardf, text="管理…", command=self._manage_cards).grid(row=0, column=1, **pad)
        ttk.Label(cardf, text="使用卡片:").grid(row=0, column=2, sticky="e", **pad)
        self.card_var = tk.StringVar()
        self.card_combo = ttk.Combobox(cardf, textvariable=self.card_var, width=28, state="readonly")
        self.card_combo.grid(row=0, column=3, sticky="w", **pad)
        ttk.Button(cardf, text="顯示卡號 (複製用)", command=self._reveal_card).grid(
            row=0, column=4, **pad)

        # ---- run / log ----
        run_bar = ttk.Frame(root)
        run_bar.pack(fill="x", padx=8, pady=6)
        self.run_btn = ttk.Button(run_bar, text="開始訂票", command=self._start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(run_bar, text="取消", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.payment_btn = ttk.Button(run_bar, text="開啟付款頁", command=self._open_payment,
                                      state="disabled")
        self.payment_btn.pack(side="left", padx=4)
        self._payment_url: Optional[str] = None

        log_frame = ttk.LabelFrame(root, text="訊息")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log = tk.Text(log_frame, height=14, wrap="word")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        self.log.configure(state="disabled")

    def _station_combo(self, parent, var):
        cb = ttk.Combobox(parent, textvariable=var, values=STATION_OPTIONS,
                          width=14, state="readonly")
        return cb

    # ----- profile handling -----

    def _profile_form(self) -> dict:
        return {
            "start": self._station_code(self.start_var.get()),
            "dest": self._station_code(self.dest_var.get()),
            "date": self.date_var.get().strip(),
            "time": self.time_var.get().strip(),
            "time_from": self.from_var.get().strip(),
            "time_until": self.until_var.get().strip(),
            "adults": int(self.adults_var.get()),
            "id_number": self.id_var.get().strip(),
            "phone": self.phone_var.get().strip(),
            "email": self.email_var.get().strip(),
            "retry": bool(self.retry_var.get()),
            "retry_interval": int(self.retry_interval_var.get()),
            "retry_max": int(self.retry_max_var.get()),
        }

    @staticmethod
    def _station_code(label: str) -> str:
        return STATION_LABEL_TO_CODE.get(label, label).strip() if label else ""

    @staticmethod
    def _station_label(code: str) -> str:
        for lbl, c in STATION_LABEL_TO_CODE.items():
            if c == code:
                return lbl
        return ""

    def _apply_profile(self, p: dict) -> None:
        self.start_var.set(self._station_label(p.get("start", "")) or p.get("start", ""))
        self.dest_var.set(self._station_label(p.get("dest", "")) or p.get("dest", ""))
        self.date_var.set(p.get("date", ""))
        self.time_var.set(p.get("time", "1230P"))
        self.from_var.set(p.get("time_from", ""))
        self.until_var.set(p.get("time_until", ""))
        self.adults_var.set(int(p.get("adults", 1) or 1))
        self.id_var.set(p.get("id_number", ""))
        self.phone_var.set(p.get("phone", ""))
        self.email_var.set(p.get("email", ""))
        self.retry_var.set(bool(p.get("retry", False)))
        self.retry_interval_var.set(int(p.get("retry_interval", 10) or 10))
        self.retry_max_var.set(int(p.get("retry_max", 0) or 0))

    def _load_active_profile(self):
        name = store.active_profile_name()
        if name:
            self.profile_var.set(name)
            p = store.get_profile(name)
            if p:
                self._apply_profile(p)

    def _load_selected_profile(self):
        name = self.profile_var.get().strip()
        if not name:
            return
        p = store.get_profile(name)
        if p:
            self._apply_profile(p)
            self._log_msg(f"已載入 profile: {name}")

    def _save_profile(self):
        name = self.profile_var.get().strip() or simpledialog.askstring(
            "Profile 名稱", "輸入要儲存的 profile 名稱:", parent=self
        )
        if not name:
            return
        store.upsert_profile(name, self._profile_form())
        self.profile_combo["values"] = store.list_profiles()
        self.profile_var.set(name)
        self._log_msg(f"已儲存 profile: {name}")

    def _delete_profile(self):
        name = self.profile_var.get().strip()
        if not name:
            return
        if not messagebox.askyesno("確認", f"刪除 profile {name!r}？", parent=self):
            return
        store.delete_profile(name)
        self.profile_combo["values"] = store.list_profiles()
        self.profile_var.set("")
        self._log_msg(f"已刪除 profile: {name}")

    # ----- card vault -----

    def _unlock_vault(self):
        existed = store.vault_exists()
        prompt = "輸入主密碼:" if existed else "建立新主密碼 (用於加密信用卡資料):"
        pw = simpledialog.askstring("信用卡主密碼", prompt, show="*", parent=self)
        if not pw:
            return
        try:
            v = store.CardVault(pw)
            v.load()
        except store.WrongPassword:
            messagebox.showerror("錯誤", "主密碼錯誤", parent=self)
            return
        except RuntimeError as e:
            messagebox.showerror("錯誤", str(e), parent=self)
            return
        self._vault = v
        self._refresh_card_combo()
        self._log_msg(f"信用卡保險庫已解鎖 ({len(v.cards)} 張卡)")

    def _manage_cards(self):
        if self._vault is None:
            self._unlock_vault()
        if self._vault is None:
            return
        CardManager(self, self._vault).wait_window()
        self._refresh_card_combo()

    def _refresh_card_combo(self):
        if self._vault is None:
            self.card_combo["values"] = ()
            return
        labels = [c.get("alias", "?") for c in self._vault.cards]
        self.card_combo["values"] = labels
        if labels and not self.card_var.get():
            self.card_var.set(labels[0])

    def _reveal_card(self):
        if self._vault is None or not self.card_var.get():
            messagebox.showinfo("提示", "請先解鎖並選擇卡片", parent=self)
            return
        for c in self._vault.cards:
            if c.get("alias") == self.card_var.get():
                msg = (
                    f"持卡人: {c.get('holder','')}\n"
                    f"卡號: {c.get('number','')}\n"
                    f"到期: {c.get('expiry','')}\n"
                    f"CVC: {c.get('cvc','') or '(未存)'}\n\n"
                    "請複製欄位內容到付款頁手動填寫。"
                )
                messagebox.showinfo(c.get("alias", "卡片"), msg, parent=self)
                return

    # ----- logging -----

    def _log_msg(self, msg: str):
        self._log_queue.put(msg)

    def _drain_log(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    # ----- start booking -----

    def _start(self):
        form = self._profile_form()
        if not form["start"] or not form["dest"]:
            messagebox.showerror("錯誤", "請選擇出發/到達站", parent=self)
            return
        if not form["date"] or not form["time"]:
            messagebox.showerror("錯誤", "請輸入日期與查詢起點時間", parent=self)
            return
        if not form["id_number"]:
            messagebox.showerror("錯誤", "請輸入身分證/護照號碼", parent=self)
            return
        try:
            from_min = _hhmm_to_min(form["time_from"]) if form["time_from"] else None
            until_min = _hhmm_to_min(form["time_until"]) if form["time_until"] else None
        except ValueError as e:
            messagebox.showerror("錯誤", str(e), parent=self)
            return

        # auto-save profile
        name = self.profile_var.get().strip()
        if name:
            store.upsert_profile(name, form)
            self.profile_combo["values"] = store.list_profiles()

        self._stop.clear()
        self.run_btn["state"] = "disabled"
        self.cancel_btn["state"] = "normal"
        self.payment_btn["state"] = "disabled"
        self._payment_url = None

        threading.Thread(
            target=self._worker,
            args=(form, from_min, until_min),
            daemon=True,
        ).start()

    def _cancel(self):
        self._stop.set()
        self._log_msg("[取消] 已要求停止…")

    def _open_payment(self):
        if self._payment_url:
            webbrowser.open(self._payment_url)

    # ----- captcha bridge (worker → UI) -----

    def _ask_captcha(self, image_bytes: bytes, hint: str = "") -> Optional[str]:
        self._captcha_event.clear()
        self._captcha_holder.clear()

        def show():
            dlg = CaptchaDialog(self, image_bytes, hint=hint)
            self.wait_window(dlg)
            self._captcha_holder["value"] = dlg.result
            self._captcha_event.set()

        self.after(0, show)
        self._captcha_event.wait()
        return self._captcha_holder.get("value")

    # ----- worker (background thread) -----

    def _worker(self, form: dict, from_min, until_min):
        try:
            booker = THSRBooker()
            if self.manual_capt_var.get():
                solver = None
            else:
                solver = CaptchaSolver()
                if not solver.available:
                    self._log_msg("[提示] ddddocr 未安裝；驗證碼將需手動輸入。")

            query_params = {
                "start": form["start"], "dest": form["dest"],
                "date": form["date"], "time": form["time"],
                "adults": form["adults"],
            }

            chosen = None
            poll = 0
            while not self._stop.is_set():
                poll += 1
                self._log_msg(f"\n=== 刷票 #{poll} ===")
                trains = self._captcha_query(booker, solver, query_params)
                if trains is None:
                    return
                self._log_msg(f"取得 {len(trains)} 班車。")
                avail = filter_trains(trains, from_min, until_min)
                if avail:
                    chosen = avail[0]
                    self._log_msg(
                        f"刷到票: {chosen['train_no']}  "
                        f"{chosen['depart']} → {chosen['arrive']}  ({chosen['duration']})"
                    )
                    break
                if not form["retry"]:
                    self._log_msg("時間範圍內無可訂票，且未啟用自動刷票。")
                    return
                if form["retry_max"] and poll >= form["retry_max"]:
                    self._log_msg(f"已達最大刷票次數 {form['retry_max']}。")
                    return
                self._log_msg(f"無可訂票，{form['retry_interval']}s 後重試…")
                self._interruptible_sleep(form["retry_interval"])

            if self._stop.is_set() or chosen is None:
                return

            self._log_msg("確認車次…")
            booker.step2_submit(chosen["value"])

            self._log_msg("送出乘客資料…")
            result = booker.step3_submit({
                "id_number": form["id_number"],
                "phone": form["phone"],
                "email": form["email"],
            })

            self._log_msg("\n=== 訂位成功 ===")
            if result.get("pnr"):
                self._log_msg(f"訂位代號 PNR: {result['pnr']}")
            self._log_msg(f"確認頁: {result['confirm_url']}")
            payment = result.get("payment_url") or result["confirm_url"]
            self._payment_url = payment
            self._log_msg(f"付款連結: {payment}")
            self._log_msg(
                "請點上方「開啟付款頁」按鈕，"
                "在瀏覽器中手動填寫信用卡資料完成付款。"
            )
            if self._vault and self.card_var.get():
                self._log_msg(
                    f"提示: 已選卡片 {self.card_var.get()!r}，"
                    "可按「顯示卡號 (複製用)」取出資料。"
                )
            self.after(0, lambda: self.payment_btn.configure(state="normal"))

        except requests.HTTPError as e:
            self._log_msg(f"[HTTP 錯誤] {e}")
        except RuntimeError as e:
            self._log_msg(f"[訂票失敗] {e}")
        except Exception as e:
            self._log_msg(f"[未預期錯誤] {type(e).__name__}: {e}")
        finally:
            self.after(0, lambda: (self.run_btn.configure(state="normal"),
                                   self.cancel_btn.configure(state="disabled")))

    def _captcha_query(self, booker: THSRBooker, solver: Optional[CaptchaSolver],
                        query_params: dict, max_attempts: int = 5):
        last_err = None
        for attempt in range(1, max_attempts + 1):
            if self._stop.is_set():
                return None
            self._log_msg(f"  載入驗證碼 (嘗試 {attempt}/{max_attempts})…")
            try:
                captcha_bytes = booker.step1_load()
            except Exception as e:
                self._log_msg(f"  載入頁面失敗: {e}")
                return None

            captcha = ""
            if solver and solver.available:
                captcha = solver.solve(captcha_bytes)
                if captcha:
                    self._log_msg(f"  自動辨識: {captcha}")

            if not captcha or len(captcha) != CAPTCHA_LEN:
                self._log_msg("  自動辨識失敗，請於彈出視窗輸入驗證碼。")
                captcha = self._ask_captcha(captcha_bytes, hint=captcha)
                if not captcha:
                    self._log_msg("  使用者取消輸入驗證碼。")
                    return None

            try:
                return booker.step1_submit(query_params, captcha)
            except RuntimeError as e:
                last_err = str(e)
                if _is_captcha_error(last_err) and attempt < max_attempts:
                    self._log_msg(f"  驗證碼錯誤，重試 ({last_err})")
                    continue
                self._log_msg(f"  查詢失敗: {last_err}")
                return None
        self._log_msg(f"  驗證碼重試達上限: {last_err}")
        return None

    def _interruptible_sleep(self, seconds: int):
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            time.sleep(0.2)


def main():
    app = BookingApp()
    app.mainloop()


if __name__ == "__main__":
    main()
