import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import openai_register as registrar


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "gui_config.json")
LOG_PATH = os.path.join(BASE_DIR, "register.log")


def load_config() -> dict:
    default = {
        "proxy": "",
        "sleep_min": 5,
        "sleep_max": 10,
        "once_mode": True,
        "output_dir": BASE_DIR,
        "thread_count": 3,
    }
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            default.update(data or {})
    except Exception:
        pass
    # 校正数值
    try:
        default["sleep_min"] = max(1, int(default.get("sleep_min", 5)))
    except Exception:
        default["sleep_min"] = 5
    try:
        default["sleep_max"] = max(
            default["sleep_min"], int(default.get("sleep_max", default["sleep_min"]))
        )
    except Exception:
        default["sleep_max"] = default["sleep_min"]
    default["once_mode"] = bool(default.get("once_mode", True))
    try:
        default["thread_count"] = max(1, min(32, int(default.get("thread_count", 3))))
    except Exception:
        default["thread_count"] = 3
    out = str(default.get("output_dir") or BASE_DIR)
    if not os.path.isabs(out):
        out = os.path.join(BASE_DIR, out)
    default["output_dir"] = out
    return default


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        messagebox.showwarning("保存配置失败", f"保存配置文件出错：{e}")


class QueueWriter:
    """将 stdout/stderr 写入到 Tk 文本框的简单 writer。"""

    def __init__(self, callback):
        self.callback = callback

    def write(self, s: str) -> None:
        if not s:
            return
        # stdout 可能传入多次换行，这里简单拆分
        for part in str(s).splitlines(keepends=True):
            if part:
                self.callback(part)

    def flush(self) -> None:
        pass


class ThreadSafeLogWriter:
    """多线程下带线程 ID 前缀的 writer，供 sys.stdout 使用。"""

    def __init__(self, thread_ids_map: dict, schedule_callback):
        self._thread_ids = thread_ids_map
        self._schedule = schedule_callback  # (msg: str) -> None，在主线程追加日志
        self._lock = threading.Lock()

    def write(self, s: str) -> None:
        if not s:
            return
        with self._lock:
            tid = self._thread_ids.get(threading.current_thread(), "?")
            prefix = f"[线程{tid}] "
            for part in str(s).splitlines(keepends=True):
                if part:
                    self._schedule(prefix + part)

    def flush(self) -> None:
        pass


class RegistrarGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("OpenAI 注册机 GUI")
        self.root.geometry("1000x680")
        self.root.minsize(900, 600)

        # 使用 ttk 主题，简洁一些
        style = ttk.Style()
        # 根据系统可用主题选择一个较现代的
        for theme_name in ("clam", "vista", "xpnative", "default"):
            if theme_name in style.theme_names():
                style.theme_use(theme_name)
                break

        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Status.TLabel", font=("Consolas", 10))

        self.config = load_config()

        # 线程控制（多线程注册）
        self.worker_threads: list[threading.Thread] = []
        self.thread_ids_map: dict = {}  # current_thread() -> 线程编号 1..N
        self.stop_event = threading.Event()
        self.running_lock = threading.Lock()
        self.is_running = False
        self._workers_finished_lock = threading.Lock()
        self._workers_finished_count = 0

        # 日志缓冲，既写文件又展示
        self.log_file = None
        self._open_log_file()

        self._build_ui()
        self._load_initial_values()
        self.refresh_accounts()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ================= UI 构建 =================
    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.tab_register = ttk.Frame(notebook)
        self.tab_accounts = ttk.Frame(notebook)
        self.tab_config = ttk.Frame(notebook)

        notebook.add(self.tab_register, text="注册与日志")
        notebook.add(self.tab_accounts, text="账号 JSON 管理")
        notebook.add(self.tab_config, text="配置")

        self._build_register_tab()
        self._build_accounts_tab()
        self._build_config_tab()

    # ---------- 注册与日志 ----------
    def _build_register_tab(self) -> None:
        frame_top = ttk.Frame(self.tab_register)
        frame_top.pack(fill=tk.X, padx=4, pady=4)

        # 行 1：代理 & 模式
        row1 = ttk.Frame(frame_top)
        row1.pack(fill=tk.X, pady=2)

        ttk.Label(row1, text="代理地址:", width=10).pack(side=tk.LEFT)
        self.var_proxy = tk.StringVar()
        self.entry_proxy = ttk.Entry(row1, textvariable=self.var_proxy)
        self.entry_proxy.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.var_once_mode = tk.BooleanVar(value=True)
        chk_once = ttk.Checkbutton(
            row1, text="只注册一次（取消勾选则循环）", variable=self.var_once_mode
        )
        chk_once.pack(side=tk.LEFT)

        # 行 2：睡眠时间
        row2 = ttk.Frame(frame_top)
        row2.pack(fill=tk.X, pady=2)

        ttk.Label(row2, text="等待区间 (秒):", width=12).pack(side=tk.LEFT)
        self.var_sleep_min = tk.StringVar()
        self.var_sleep_max = tk.StringVar()
        spin_min = ttk.Spinbox(
            row2, textvariable=self.var_sleep_min, from_=1, to=86400, width=7
        )
        spin_max = ttk.Spinbox(
            row2, textvariable=self.var_sleep_max, from_=1, to=86400, width=7
        )
        spin_min.pack(side=tk.LEFT)
        ttk.Label(row2, text=" - ").pack(side=tk.LEFT)
        spin_max.pack(side=tk.LEFT)

        ttk.Label(row2, text="  并发线程数:", width=10).pack(side=tk.LEFT, padx=(12, 0))
        self.var_thread_count = tk.StringVar()
        ttk.Spinbox(
            row2, textvariable=self.var_thread_count, from_=1, to=32, width=5
        ).pack(side=tk.LEFT)

        ttk.Label(row2, text="   输出目录:", width=10).pack(side=tk.LEFT, padx=(12, 0))
        self.var_output_dir = tk.StringVar()
        entry_out = ttk.Entry(row2, textvariable=self.var_output_dir, width=40)
        entry_out.pack(side=tk.LEFT, fill=tk.X, expand=True)
        btn_browse = ttk.Button(row2, text="浏览...", command=self._choose_output_dir)
        btn_browse.pack(side=tk.LEFT, padx=(4, 0))

        # 行 3：控制按钮与状态
        row3 = ttk.Frame(frame_top)
        row3.pack(fill=tk.X, pady=4)

        self.btn_start = ttk.Button(row3, text="开始注册", command=self.start_worker)
        self.btn_stop = ttk.Button(
            row3, text="停止", command=self.stop_worker, state=tk.DISABLED
        )
        self.btn_start.pack(side=tk.LEFT)
        self.btn_stop.pack(side=tk.LEFT, padx=(6, 0))

        self.var_status = tk.StringVar(value="状态：空闲")
        lbl_status = ttk.Label(row3, textvariable=self.var_status, style="Status.TLabel")
        lbl_status.pack(side=tk.RIGHT)

        # 日志区
        frame_log = ttk.LabelFrame(self.tab_register, text="注册日志（实时）")
        frame_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        self.txt_log = tk.Text(
            frame_log,
            wrap=tk.WORD,
            font=("Consolas", 10),
            state=tk.DISABLED,
            bg="#111111",
            fg="#DDDDDD",
        )
        scroll_y = ttk.Scrollbar(frame_log, orient=tk.VERTICAL, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll_y.set)

        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

        row_log_btn = ttk.Frame(self.tab_register)
        row_log_btn.pack(fill=tk.X, padx=4, pady=(0, 4))

        ttk.Button(row_log_btn, text="清空日志显示", command=self.clear_log_display).pack(
            side=tk.LEFT
        )
        ttk.Button(row_log_btn, text="打开日志文件所在目录", command=self.open_log_dir).pack(
            side=tk.LEFT, padx=(6, 0)
        )

    # ---------- 账号 JSON 管理 ----------
    def _build_accounts_tab(self) -> None:
        frame_main = ttk.Frame(self.tab_accounts)
        frame_main.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # 左侧列表
        frame_left = ttk.Frame(frame_main)
        frame_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)

        lbl_title = ttk.Label(frame_left, text="已生成的 token_*.json", style="Title.TLabel")
        lbl_title.pack(anchor=tk.W, pady=(0, 4))

        columns = ("file", "email", "exported_at")
        self.tree_accounts = ttk.Treeview(
            frame_left,
            columns=columns,
            show="headings",
            height=18,
            selectmode="browse",
        )
        self.tree_accounts.heading("file", text="文件名")
        self.tree_accounts.heading("email", text="邮箱 / 名称")
        self.tree_accounts.heading("exported_at", text="导出时间")
        self.tree_accounts.column("file", width=260, anchor=tk.W)
        self.tree_accounts.column("email", width=200, anchor=tk.W)
        self.tree_accounts.column("exported_at", width=160, anchor=tk.W)

        scroll_y = ttk.Scrollbar(
            frame_left, orient=tk.VERTICAL, command=self.tree_accounts.yview
        )
        self.tree_accounts.configure(yscrollcommand=scroll_y.set)

        self.tree_accounts.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree_accounts.bind("<<TreeviewSelect>>", self.on_account_select)

        frame_left_btn = ttk.Frame(frame_left)
        frame_left_btn.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(frame_left_btn, text="刷新列表", command=self.refresh_accounts).pack(
            side=tk.LEFT
        )
        ttk.Button(
            frame_left_btn, text="删除所选 JSON", command=self.delete_selected_account
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(
            frame_left_btn,
            text="在资源管理器中打开目录",
            command=self.open_output_dir,
        ).pack(side=tk.LEFT, padx=(6, 0))

        # 右侧详细内容
        frame_right = ttk.LabelFrame(frame_main, text="JSON 详细内容（只读预览）")
        frame_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        self.var_selected_file = tk.StringVar()
        ttk.Label(frame_right, textvariable=self.var_selected_file).pack(
            anchor=tk.W, padx=4, pady=(2, 2)
        )

        self.txt_json_preview = tk.Text(
            frame_right,
            wrap=tk.NONE,
            font=("Consolas", 10),
            state=tk.DISABLED,
        )
        scroll_y2 = ttk.Scrollbar(
            frame_right, orient=tk.VERTICAL, command=self.txt_json_preview.yview
        )
        scroll_x2 = ttk.Scrollbar(
            frame_right, orient=tk.HORIZONTAL, command=self.txt_json_preview.xview
        )
        self.txt_json_preview.configure(
            yscrollcommand=scroll_y2.set, xscrollcommand=scroll_x2.set
        )

        self.txt_json_preview.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)
        scroll_y2.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_x2.pack(side=tk.BOTTOM, fill=tk.X)

        frame_right_btn = ttk.Frame(frame_right)
        frame_right_btn.pack(fill=tk.X, padx=4, pady=(0, 4))

        ttk.Button(
            frame_right_btn,
            text="用默认程序打开此文件",
            command=self.open_selected_file_external,
        ).pack(side=tk.LEFT)

    # ---------- 配置 ----------
    def _build_config_tab(self) -> None:
        frame = ttk.LabelFrame(self.tab_config, text="全局配置（下次启动仍然生效）")
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 代理
        row1 = ttk.Frame(frame)
        row1.pack(fill=tk.X, pady=4)
        ttk.Label(row1, text="默认代理地址:", width=14).pack(side=tk.LEFT)
        self.var_cfg_proxy = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_cfg_proxy).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        # 等待时间
        row2 = ttk.Frame(frame)
        row2.pack(fill=tk.X, pady=4)
        ttk.Label(row2, text="默认等待区间:", width=14).pack(side=tk.LEFT)
        self.var_cfg_sleep_min = tk.StringVar()
        self.var_cfg_sleep_max = tk.StringVar()
        ttk.Spinbox(
            row2, textvariable=self.var_cfg_sleep_min, from_=1, to=86400, width=7
        ).pack(side=tk.LEFT)
        ttk.Label(row2, text=" - ").pack(side=tk.LEFT)
        ttk.Spinbox(
            row2, textvariable=self.var_cfg_sleep_max, from_=1, to=86400, width=7
        ).pack(side=tk.LEFT)

        # 模式
        row3 = ttk.Frame(frame)
        row3.pack(fill=tk.X, pady=4)
        self.var_cfg_once_mode = tk.BooleanVar()
        ttk.Checkbutton(
            row3,
            text="默认只注册一次（取消则默认循环）",
            variable=self.var_cfg_once_mode,
        ).pack(side=tk.LEFT)

        # 并发线程数
        row4 = ttk.Frame(frame)
        row4.pack(fill=tk.X, pady=4)
        ttk.Label(row4, text="默认并发线程数:", width=14).pack(side=tk.LEFT)
        self.var_cfg_thread_count = tk.StringVar()
        ttk.Spinbox(
            row4, textvariable=self.var_cfg_thread_count, from_=1, to=32, width=5
        ).pack(side=tk.LEFT)
        ttk.Label(row4, text=" (1~32)").pack(side=tk.LEFT)

        # 输出目录
        row5 = ttk.Frame(frame)
        row5.pack(fill=tk.X, pady=4)
        ttk.Label(row5, text="默认输出目录:", width=14).pack(side=tk.LEFT)
        self.var_cfg_output_dir = tk.StringVar()
        ttk.Entry(row5, textvariable=self.var_cfg_output_dir).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(
            row5,
            text="浏览...",
            command=self._choose_cfg_output_dir,
        ).pack(side=tk.LEFT, padx=(4, 0))

        # 保存按钮
        row_btn = ttk.Frame(frame)
        row_btn.pack(fill=tk.X, pady=12)
        ttk.Button(row_btn, text="保存配置", command=self.on_save_config).pack(
            side=tk.LEFT
        )

    # ================= 配置加载/保存 =================
    def _load_initial_values(self) -> None:
        cfg = self.config
        self.var_proxy.set(cfg.get("proxy", ""))
        self.var_sleep_min.set(str(cfg.get("sleep_min", 5)))
        self.var_sleep_max.set(str(cfg.get("sleep_max", 10)))
        self.var_once_mode.set(bool(cfg.get("once_mode", True)))
        self.var_thread_count.set(str(cfg.get("thread_count", 3)))
        self.var_output_dir.set(cfg.get("output_dir", BASE_DIR))

        self.var_cfg_proxy.set(cfg.get("proxy", ""))
        self.var_cfg_sleep_min.set(str(cfg.get("sleep_min", 5)))
        self.var_cfg_sleep_max.set(str(cfg.get("sleep_max", 10)))
        self.var_cfg_once_mode.set(bool(cfg.get("once_mode", True)))
        self.var_cfg_thread_count.set(str(cfg.get("thread_count", 3)))
        self.var_cfg_output_dir.set(cfg.get("output_dir", BASE_DIR))

    def _choose_output_dir(self) -> None:
        cur = self.var_output_dir.get() or BASE_DIR
        directory = filedialog.askdirectory(
            title="选择 JSON 输出目录", initialdir=cur if os.path.isdir(cur) else BASE_DIR
        )
        if directory:
            self.var_output_dir.set(directory)

    def _choose_cfg_output_dir(self) -> None:
        cur = self.var_cfg_output_dir.get() or BASE_DIR
        directory = filedialog.askdirectory(
            title="选择默认输出目录", initialdir=cur if os.path.isdir(cur) else BASE_DIR
        )
        if directory:
            self.var_cfg_output_dir.set(directory)

    def on_save_config(self) -> None:
        try:
            sleep_min = max(1, int(self.var_cfg_sleep_min.get() or 5))
        except Exception:
            sleep_min = 5
        try:
            sleep_max = max(sleep_min, int(self.var_cfg_sleep_max.get() or sleep_min))
        except Exception:
            sleep_max = sleep_min

        try:
            thread_count = max(
                1, min(32, int(self.var_cfg_thread_count.get() or 3))
            )
        except Exception:
            thread_count = 3
        cfg = {
            "proxy": self.var_cfg_proxy.get().strip(),
            "sleep_min": sleep_min,
            "sleep_max": sleep_max,
            "once_mode": bool(self.var_cfg_once_mode.get()),
            "thread_count": thread_count,
            "output_dir": self.var_cfg_output_dir.get().strip() or BASE_DIR,
        }
        self.config = cfg
        save_config(cfg)
        messagebox.showinfo("配置已保存", "全局配置已成功保存。")

    # ================= 日志处理 =================
    def _open_log_file(self) -> None:
        try:
            self.log_file = open(LOG_PATH, "a", encoding="utf-8")
            # 简单写入启动时间
            self.log_file.write(
                f"\n\n===== GUI 启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====\n"
            )
            self.log_file.flush()
        except Exception:
            self.log_file = None

    def append_log(self, text: str) -> None:
        """在 UI 线程中追加日志到文本框，同时写入文件。"""
        ts = datetime.now().strftime("[%H:%M:%S] ")
        full = text
        # 若开头已有时间前缀，就不重复加
        if not (text.startswith("[") and "]" in text[:10]):
            full = ts + text

        # 写入 Text
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.insert(tk.END, full)
        self.txt_log.see(tk.END)
        self.txt_log.configure(state=tk.DISABLED)

        # 写入文件
        if self.log_file:
            try:
                self.log_file.write(full)
                if not full.endswith("\n"):
                    self.log_file.write("\n")
                self.log_file.flush()
            except Exception:
                pass

    def clear_log_display(self) -> None:
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.configure(state=tk.DISABLED)

    def open_log_dir(self) -> None:
        try:
            os.startfile(BASE_DIR)
        except Exception as e:
            messagebox.showwarning("打开目录失败", f"无法打开目录：{e}")

    # ================= 账号 JSON 管理 =================
    def get_output_dir(self) -> str:
        path = self.var_output_dir.get().strip() or BASE_DIR
        if not os.path.isabs(path):
            path = os.path.join(BASE_DIR, path)
        return path

    def refresh_accounts(self) -> None:
        out_dir = self.get_output_dir()
        for item in self.tree_accounts.get_children():
            self.tree_accounts.delete(item)

        if not os.path.isdir(out_dir):
            return

        files = [
            f
            for f in os.listdir(out_dir)
            if f.startswith("token_") and f.endswith(".json")
        ]
        files.sort(key=lambda x: os.path.getmtime(os.path.join(out_dir, x)), reverse=True)

        for fname in files:
            path = os.path.join(out_dir, fname)
            email = ""
            exported_at = ""
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                exported_at = str(data.get("exported_at", ""))
                accounts = data.get("accounts") or []
                if accounts:
                    email = str((accounts[0] or {}).get("name") or "")
            except Exception:
                pass
            self.tree_accounts.insert(
                "",
                tk.END,
                iid=fname,
                values=(fname, email, exported_at),
            )

    def on_account_select(self, event=None) -> None:
        sel = self.tree_accounts.selection()
        if not sel:
            self.var_selected_file.set("")
            self._set_json_preview_text("")
            return
        fname = sel[0]
        path = os.path.join(self.get_output_dir(), fname)
        self.var_selected_file.set(f"当前选择：{path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            text = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            text = f"读取文件失败：{e}"
        self._set_json_preview_text(text)

    def _set_json_preview_text(self, text: str) -> None:
        self.txt_json_preview.configure(state=tk.NORMAL)
        self.txt_json_preview.delete("1.0", tk.END)
        if text:
            self.txt_json_preview.insert(tk.END, text)
        self.txt_json_preview.configure(state=tk.DISABLED)

    def delete_selected_account(self) -> None:
        sel = self.tree_accounts.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在左侧列表中选择一个 JSON 文件。")
            return
        fname = sel[0]
        path = os.path.join(self.get_output_dir(), fname)
        if not os.path.exists(path):
            self.tree_accounts.delete(fname)
            self.var_selected_file.set("")
            self._set_json_preview_text("")
            return

        if not messagebox.askyesno("确认删除", f"确定要删除文件？\n\n{path}"):
            return
        try:
            os.remove(path)
        except Exception as e:
            messagebox.showwarning("删除失败", f"无法删除文件：{e}")
            return
        self.refresh_accounts()
        self.var_selected_file.set("")
        self._set_json_preview_text("")

    def open_output_dir(self) -> None:
        try:
            os.startfile(self.get_output_dir())
        except Exception as e:
            messagebox.showwarning("打开目录失败", f"无法打开输出目录：{e}")

    def open_selected_file_external(self) -> None:
        sel = self.tree_accounts.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在左侧列表中选择一个 JSON 文件。")
            return
        fname = sel[0]
        path = os.path.join(self.get_output_dir(), fname)
        if not os.path.exists(path):
            messagebox.showwarning("文件不存在", f"找不到文件：\n{path}")
            return
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showwarning("打开失败", f"无法打开文件：{e}")

    # ================= 注册线程（多线程） =================
    def start_worker(self) -> None:
        with self.running_lock:
            if self.is_running:
                return
            # 基本校验
            try:
                sleep_min = max(1, int(self.var_sleep_min.get() or 5))
            except Exception:
                sleep_min = 5
            try:
                sleep_max = max(sleep_min, int(self.var_sleep_max.get() or sleep_min))
            except Exception:
                sleep_max = sleep_min
            try:
                thread_count = max(
                    1, min(32, int(self.var_thread_count.get() or 3))
                )
            except Exception:
                thread_count = 3

            output_dir = self.get_output_dir()
            os.makedirs(output_dir, exist_ok=True)

            proxy = self.var_proxy.get().strip() or None
            once_mode = bool(self.var_once_mode.get())

            # 保存最新配置
            self.config.update(
                {
                    "proxy": proxy or "",
                    "sleep_min": sleep_min,
                    "sleep_max": sleep_max,
                    "once_mode": once_mode,
                    "thread_count": thread_count,
                    "output_dir": output_dir,
                }
            )
            save_config(self.config)

            self.stop_event.clear()
            self.is_running = True
            self.worker_threads = []
            self.thread_ids_map.clear()
            self._workers_finished_count = 0

            self.btn_start.configure(state=tk.DISABLED)
            self.btn_stop.configure(state=tk.NORMAL)
            self.var_status.set(f"状态：正在注册中（{thread_count} 个线程）...")

            # 多线程共用：将 print 重定向到带线程 ID 的日志
            def schedule_log(msg: str) -> None:
                self.root.after(0, self.append_log, msg)

            self._shared_log_writer = ThreadSafeLogWriter(
                self.thread_ids_map, schedule_log
            )
            self._saved_stdout, self._saved_stderr = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = self._shared_log_writer

            # 启动 N 个工作线程
            for i in range(thread_count):
                tid = i + 1
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(tid, proxy, once_mode, sleep_min, sleep_max, output_dir),
                    daemon=True,
                )
                self.worker_threads.append(t)
                t.start()

    def stop_worker(self) -> None:
        with self.running_lock:
            if not self.is_running:
                return
            self.stop_event.set()
        self.append_log("收到停止指令，正在安全退出当前循环...\n")

    def _worker_loop(
        self,
        thread_id: int,
        proxy: str | None,
        once_mode: bool,
        sleep_min: int,
        sleep_max: int,
        output_dir: str,
    ) -> None:
        # 注册当前线程到 thread_ids_map，以便 ThreadSafeLogWriter 打前缀
        self.thread_ids_map[threading.current_thread()] = thread_id

        count = 0
        try:
            print(f"[Info] 线程 {thread_id} 已启动。", flush=True)
            while not self.stop_event.is_set():
                count += 1
                print(
                    f"\n[{datetime.now().strftime('%H:%M:%S')}] >>> 线程{thread_id} 第 {count} 次注册 <<<",
                    flush=True,
                )

                try:
                    token_json = registrar.run(proxy)

                    if token_json:
                        try:
                            t_data = json.loads(token_json)
                            email = t_data.get("email", "unknown")
                            fname_email = email.replace("@", "_")
                        except Exception:
                            email = "unknown"
                            fname_email = "unknown"

                        exported_at = (
                            datetime.utcnow()
                            .replace(tzinfo=timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ")
                        )

                        expires_in = 0
                        expired_at_str = t_data.get("expired") or ""
                        last_refresh_str = t_data.get("last_refresh") or ""
                        try:
                            if expired_at_str and last_refresh_str:
                                expired_at = datetime.strptime(
                                    expired_at_str, "%Y-%m-%dT%H:%M:%SZ"
                                ).replace(tzinfo=timezone.utc)
                                last_refresh = datetime.strptime(
                                    last_refresh_str, "%Y-%m-%dT%H:%M:%SZ"
                                ).replace(tzinfo=timezone.utc)
                                expires_in = max(
                                    0, int((expired_at - last_refresh).total_seconds())
                                )
                        except Exception:
                            expires_in = 0

                        credentials = {
                            "access_token": t_data.get("access_token", ""),
                            "chatgpt_account_id": t_data.get("account_id", ""),
                            "chatgpt_user_id": "",
                            "client_id": registrar.CLIENT_ID,
                            "email": email,
                            "expires_at": expired_at_str,
                            "expires_in": expires_in,
                            "id_token": t_data.get("id_token", ""),
                            "organization_id": "",
                            "refresh_token": t_data.get("refresh_token", ""),
                        }

                        extra = {
                            "codex_5h_reset_after_seconds": 0,
                            "codex_5h_reset_at": exported_at,
                            "codex_5h_used_percent": 0,
                            "codex_5h_window_minutes": 0,
                            "codex_7d_reset_after_seconds": 0,
                            "codex_7d_reset_at": exported_at,
                            "codex_7d_used_percent": 0,
                            "codex_7d_window_minutes": 0,
                            "codex_primary_over_secondary_percent": 0,
                            "codex_primary_reset_after_seconds": 0,
                            "codex_primary_used_percent": 0,
                            "codex_primary_window_minutes": 0,
                            "codex_secondary_reset_after_seconds": 0,
                            "codex_secondary_used_percent": 0,
                            "codex_secondary_window_minutes": 0,
                            "codex_usage_updated_at": exported_at,
                            "email": email,
                            "openai_oauth_responses_websockets_v2_enabled": False,
                            "openai_oauth_responses_websockets_v2_mode": "off",
                        }

                        export_data = {
                            "exported_at": exported_at,
                            "proxies": [proxy] if proxy else [],
                            "accounts": [
                                {
                                    "name": email,
                                    "platform": "openai",
                                    "type": t_data.get("type", "oauth"),
                                    "credentials": credentials,
                                    "extra": extra,
                                    "concurrency": 10,
                                    "priority": 1,
                                    "rate_multiplier": 1,
                                    "auto_pause_on_expired": True,
                                }
                            ],
                        }

                        file_name = f"token_{fname_email}_{int(time.time())}.json"
                        out_path = os.path.join(output_dir, file_name)
                        try:
                            with open(out_path, "w", encoding="utf-8") as f:
                                json.dump(export_data, f, ensure_ascii=False, indent=2)
                            print(f"[*] 成功! Token 已保存至: {out_path}", flush=True)
                        except Exception as e:
                            print(f"[Error] 保存 JSON 文件失败: {e}", flush=True)

                        # 刷新账号列表
                        self.root.after(0, self.refresh_accounts)
                    else:
                        print("[-] 本次注册失败。", flush=True)

                except Exception as e:
                    print(f"[Error] 发生未捕获异常: {e}", flush=True)

                if once_mode:
                    break

                # 等待随机时间，同时响应停止事件
                wait_time = random.randint(sleep_min, sleep_max)
                print(f"[*] 休息 {wait_time} 秒...", flush=True)
                for _ in range(wait_time):
                    if self.stop_event.is_set():
                        break
                    time.sleep(1)
                if self.stop_event.is_set():
                    break

            print(f"[Info] 线程 {thread_id} 已结束。", flush=True)
        finally:
            # 仅当最后一个退出的线程负责恢复 stdout 并更新 UI
            with self._workers_finished_lock:
                self._workers_finished_count += 1
                total = len(self.worker_threads)
                if self._workers_finished_count >= total:
                    try:
                        sys.stdout = self._saved_stdout
                        sys.stderr = self._saved_stderr
                    except Exception:
                        pass
                    with self.running_lock:
                        self.is_running = False
                    self.root.after(0, self._on_worker_stopped)

    def _on_worker_stopped(self) -> None:
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.var_status.set("状态：空闲")

    # ================= 关闭处理 =================
    def on_close(self) -> None:
        if self.is_running:
            if not messagebox.askyesno(
                "确认退出", "注册线程仍在运行，确定要停止并退出吗？"
            ):
                return
            self.stop_event.set()
            for t in self.worker_threads:
                if t is not None and t.is_alive():
                    t.join(timeout=5)

        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = RegistrarGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

