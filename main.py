"""Disk Warden - 主入口"""
import sys
import tkinter as tk
from tkinter import ttk


class WelcomeWindow:
    """启动欢迎页 - 深色科技风"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Disk Warden")
        self.root.geometry("420x520")
        self.root.resizable(False, False)
        self.root.configure(bg="#0f172a")  # 深蓝黑背景

        # 居中显示
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - 420) // 2
        y = (self.root.winfo_screenheight() - 520) // 2
        self.root.geometry(f"420x520+{x}+{y}")

        self._build_ui()

    def _build_ui(self):
        # 顶部留白
        tk.Frame(self.root, bg="#0f172a", height=80).pack()

        # Logo图标 - 磁盘样式
        logo_canvas = tk.Canvas(
            self.root,
            width=64, height=64,
            bg="#0f172a",
            highlightthickness=0
        )
        logo_canvas.pack(pady=(0, 20))
        logo_canvas.create_oval(12, 12, 52, 52, fill="#3b82f6", outline="")
        logo_canvas.create_text(32, 32, text="DW", fill="white", font=("Segoe UI", 16, "bold"))

        # 标题
        title = tk.Label(
            self.root,
            text="Disk Warden",
            font=("Segoe UI", 26, "bold"),
            fg="#f8fafc",
            bg="#0f172a"
        )
        title.pack(pady=(0, 40))

        # GitHub登录按钮 - 浅灰底+深灰字
        github_btn = tk.Button(
            self.root,
            text="  GitHub 登录",
            font=("Segoe UI", 11, "bold"),
            bg="#e2e8f0",
            fg="#1e293b",
            activebackground="#cbd5e1",
            activeforeground="#0f172a",
            relief="flat",
            width=24,
            height=2,
            cursor="hand2",
            borderwidth=0,
            command=self._on_github
        )
        github_btn.pack(pady=(0, 12))

        # Gitee登录按钮 - 同色系浅灰底
        gitee_btn = tk.Button(
            self.root,
            text="  Gitee 登录",
            font=("Segoe UI", 11, "bold"),
            bg="#e2e8f0",
            fg="#1e293b",
            activebackground="#cbd5e1",
            activeforeground="#0f172a",
            relief="flat",
            width=24,
            height=2,
            cursor="hand2",
            borderwidth=0,
            command=self._on_gitee
        )
        gitee_btn.pack(pady=(0, 30))

        # 跳过按钮 - 极小、浅灰、底部居中
        skip_btn = tk.Button(
            self.root,
            text="跳过登录",
            font=("Segoe UI", 9),
            fg="#64748b",
            bg="#0f172a",
            activebackground="#0f172a",
            activeforeground="#94a3b8",
            relief="flat",
            cursor="hand2",
            borderwidth=0,
            command=self._on_skip
        )
        skip_btn.pack(side=tk.BOTTOM, pady=20)

    def _on_github(self):
        """点击GitHub登录"""
        print("跳转到GitHub授权...")
        self._enter_main()

    def _on_gitee(self):
        """点击Gitee登录"""
        print("跳转到Gitee授权...")
        self._enter_main()

    def _on_skip(self):
        """点击跳过"""
        self._enter_main()

    def _enter_main(self):
        """进入主界面"""
        self.root.destroy()
        MainWindow().run()

    def run(self):
        self.root.mainloop()


class MainWindow:
    """主窗口 - 深色科技风"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Disk Warden")
        self.root.geometry("960x640")
        self.root.configure(bg="#0f172a")

        self._build_ui()

    def _build_ui(self):
        # 顶栏
        topbar = tk.Frame(self.root, bg="#1e293b", height=56)
        topbar.pack(fill=tk.X)
        topbar.pack_propagate(False)

        title = tk.Label(
            topbar,
            text="  Disk Warden",
            font=("Segoe UI", 14, "bold"),
            fg="#f8fafc",
            bg="#1e293b"
        )
        title.pack(side=tk.LEFT, padx=16)

        scan_btn = tk.Button(
            topbar,
            text="重新扫描",
            font=("Segoe UI", 10),
            bg="#3b82f6",
            fg="white",
            activebackground="#2563eb",
            relief="flat",
            cursor="hand2",
            borderwidth=0,
            padx=16,
            pady=6,
            command=self._scan
        )
        scan_btn.pack(side=tk.RIGHT, padx=16)

        # 概览卡片
        cards_frame = tk.Frame(self.root, bg="#0f172a")
        cards_frame.pack(fill=tk.X, padx=20, pady=20)

        cards = [
            ("C盘剩余", "-- GB", "#3b82f6"),
            ("目标盘可用", "-- GB", "#10b981"),
            ("可释放总量", "-- GB", "#f59e0b"),
            ("累计已释放", "0 GB", "#8b5cf6"),
        ]

        for i, (label, value, color) in enumerate(cards):
            card = tk.Frame(cards_frame, bg="#1e293b", padx=20, pady=16)
            card.grid(row=0, column=i, padx=8, sticky="nsew")
            cards_frame.grid_columnconfigure(i, weight=1)

            tk.Label(card, text=label, font=("Segoe UI", 10), fg="#94a3b8", bg="#1e293b").pack(anchor="w")
            tk.Label(card, text=value, font=("Segoe UI", 22, "bold"), fg=color, bg="#1e293b").pack(anchor="w", pady=(4, 0))

        # 左侧Tab
        left_frame = tk.Frame(self.root, bg="#0f172a")
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(20, 10))

        tabs = [
            ("IDE数据", True),
            ("项目目录", False),
            ("C盘全景", False),
            ("规则学习", False),
            ("操作历史", False),
        ]
        for tab_name, active in tabs:
            bg = "#334155" if active else "#0f172a"
            fg = "#f8fafc" if active else "#94a3b8"
            btn = tk.Button(
                left_frame,
                text=tab_name,
                font=("Segoe UI", 10),
                bg=bg,
                fg=fg,
                activebackground="#334155",
                activeforeground="#f8fafc",
                relief="flat",
                width=14,
                anchor="w",
                cursor="hand2",
                borderwidth=0,
                padx=12,
                pady=8
            )
            btn.pack(pady=2, fill=tk.X)

        # 右侧内容区
        right_frame = tk.Frame(self.root, bg="#1e293b")
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(10, 20), pady=(0, 20))

        tk.Label(
            right_frame,
            text="点击「重新扫描」开始检测本机IDE",
            font=("Segoe UI", 12),
            fg="#64748b",
            bg="#1e293b"
        ).pack(expand=True)

    def _scan(self):
        """开始扫描"""
        print("开始扫描...")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = WelcomeWindow()
    app.run()
