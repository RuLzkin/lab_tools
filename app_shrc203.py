"""SHRC-203 ステージコントローラ GUI

機能
- 十字キーによる相対移動 (←→: X, ↑↓: Y, PageUp/PageDown: Z, Shift で ×10)
- マップ上のマウスクリックによる XY 絶対値移動 / 数値入力による XYZ 絶対値移動
- 停止 (Space, 減速停止 L:W) / 緊急停止 (Esc, 即停止 L:E)

module_shrc203.Shrc203 は変更せずにそのまま利用する。
ただし Shrc203 には停止・相対移動・移動中の位置取得が無いため、以下で補っている。
- Shrc203.device を排他制御付きのプロキシで包み、移動中 (move_abs の wait ループ中) でも
  別スレッドから位置取得や停止コマンドを安全に送れるようにする。
- 相対移動は「現在位置 + 移動量」を move_abs に渡して実現する。
- 停止は device 経由で L:W / L:E を直接送る。
"""
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from module_shrc203 import Shrc203

AXES = ("X", "Y", "Z")
UNIT = "U"  # move_abs に渡す単位 (U: um)


DEFAULT_RESOLUTION_UM = 2.0  # 分解能が読めなかった場合の値


def quantize(target, resolution):
    """目標値を各軸の分解能 [um] の整数倍に丸める

    SHRC-203 は U 単位で分解能の倍数でない値を受け付けない (ビープ音でエラー) ため。
    """
    out = []
    for v, r in zip(target, resolution):
        r = r if r > 0 else 1.0
        out.append(round(round(v / r) * r, 6))
    return out


def read_resolution(shrc: Shrc203):
    """分解能 [um/pulse] と分割数を読み込む

    ?:PW (TravelPerPulse) は 1 パルスあたりの移動量を nm 単位で返す
    (実機で 2000,2000,2000 → 2 um 刻みのみ受け付け) ので 1/1000 して um にする。
    """
    pw = shrc.TravelPerPulse()
    res = [v / 1000.0 for v in pw[:3]]
    try:
        div = [int(v) for v in shrc.NumberOfDivisions()[:3]]
    except Exception:
        div = None
    return res, div


# ---------------------------------------------------------------------------
# デバイス周り
# ---------------------------------------------------------------------------
class LockedDevice:
    """VISA リソースを包み、query をスレッド間で排他する。

    停止要求 (abort) が立っている間は G: (駆動開始) を送らない。
    move_abs の A: と G: の間に停止が割り込んだ場合に、停止後に動き出すのを防ぐため。
    """

    def __init__(self, device):
        self._dev = device
        self._lock = threading.RLock()
        self.abort = threading.Event()

    def query(self, cmd: str) -> str:
        if cmd.startswith("G:") and self.abort.is_set():
            return "OK"
        with self._lock:
            return self._dev.query(cmd)

    def write(self, cmd: str):
        with self._lock:
            return self._dev.write(cmd)

    def __getattr__(self, name):
        return getattr(self._dev, name)


class SimDevice:
    """SHRC-203 の簡易シミュレータ (実機なしでの動作確認用)"""

    def __init__(self, speed_um_s=20000.0):
        self.speed = speed_um_s
        self._start = [0.0, 0.0, 0.0]
        self._target = [0.0, 0.0, 0.0]
        self._pending = [0.0, 0.0, 0.0]
        self._t0 = time.time()

    def _pos(self):
        dt = time.time() - self._t0
        pos = []
        for s, t in zip(self._start, self._target):
            d = t - s
            step = min(abs(d), self.speed * dt)
            pos.append(s + step if d >= 0 else s - step)
        return pos

    def _busy(self):
        return any(abs(p - t) > 1e-9 for p, t in zip(self._pos(), self._target))

    def _freeze(self):
        p = self._pos()
        self._start, self._target, self._t0 = p, list(p), time.time()

    def _go(self, target):
        self._start, self._target, self._t0 = self._pos(), list(target), time.time()

    def query(self, cmd: str) -> str:
        time.sleep(0.002)
        if cmd.startswith("A:"):
            vals = re.findall(r"([+-])[A-Z]([0-9.]+)", cmd)
            vals = [float(v) * (-1 if s == "-" else 1) for s, v in vals[:3]]
            if any(abs(v / 2.0 - round(v / 2.0)) > 1e-9 for v in vals):
                return "NG"  # 実機同様、分解能 (2 um) の倍数以外は受け付けない
            self._pending = vals
            return "OK"
        if cmd.startswith("?:PW"):
            return "2000,2000,2000"
        if cmd.startswith("?:SW"):
            return "2,2,2"
        if cmd.startswith("G:"):
            self._go(self._pending)
            return "OK"
        if cmd.startswith("!:"):
            return "B" if self._busy() else "R"
        if cmd.startswith("Q:"):
            p = self._pos()
            st = "B" if self._busy() else "R"
            return "U{:+.0f},U{:+.0f},U{:+.0f},K,K,{}".format(*p, st)
        if cmd.startswith("L:"):
            self._freeze()
            return "OK"
        if cmd.startswith("H:"):
            self._go([0.0, 0.0, 0.0])
            return "OK"
        if cmd.startswith("*IDN?"):
            return "SIM,SHRC-203"
        return "OK"

    def write(self, cmd):
        self.query(cmd)

    def close(self):
        pass


def connect_shrc203(simulate=False, num_device=None) -> Shrc203:
    if simulate:
        # pyvisa のバックエンドが無くても動くよう __init__ を通さない
        shrc = Shrc203.__new__(Shrc203)
        shrc.debug = False
        shrc.res_man = None
        shrc._device = SimDevice()
    else:
        shrc = Shrc203(num_device=num_device)
    shrc._device = LockedDevice(shrc._device)
    return shrc


def parse_position(resp: str):
    """Q:Su の応答 → ([x, y, z] (um), busy)"""
    for c in ("\x00", "\r", "\n", " "):
        resp = resp.replace(c, "")
    items = resp.split(",")
    pos = [float(re.sub(r"[A-Za-z]", "", v)) for v in items[:3]]
    busy = "B" in items[-1] if len(items) > 3 else False
    return pos, busy


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class Shrc203GUI:
    MAP_SIZE = 420
    POLL_MS = 100

    def __init__(self, root):
        self.root = root
        self.root.title("SHRC-203 Stage Control")

        self.shrc = None
        self.busy = False
        self.pos = [0.0, 0.0, 0.0]
        self.target = None
        self.stop_msg = None
        self.jobs = queue.Queue()
        self.events = queue.Queue()  # ワーカー/ポーラー → GUI
        self.alive = True

        self.simulate = tk.BooleanVar(value=False)
        self.device_index = tk.StringVar(value="")
        self.step = [tk.DoubleVar(value=v) for v in (100.0, 100.0, 100.0)]
        self.resolution = [DEFAULT_RESOLUTION_UM] * 3
        self.res_text = [tk.StringVar(value=f"{DEFAULT_RESOLUTION_UM:g}") for _ in AXES]
        self.invert = [tk.BooleanVar(value=False) for _ in AXES]
        self.abs_entry = [tk.DoubleVar(value=0.0) for _ in AXES]
        self.range_min = [tk.DoubleVar(value=-50000.0), tk.DoubleVar(value=-50000.0)]
        self.range_max = [tk.DoubleVar(value=50000.0), tk.DoubleVar(value=50000.0)]
        self.click_enabled = tk.BooleanVar(value=True)
        self.pos_text = [tk.StringVar(value="---") for _ in AXES]
        self.state_text = tk.StringVar(value="未接続")

        self._build()
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()
        self.root.after(self.POLL_MS, self._process_events)

    # ---------------- UI 構築 ----------------
    def _build(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(main)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 接続
        f = ttk.LabelFrame(left, text="接続", padding=6)
        f.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(f, text="リソース番号 (空欄で自動):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(f, textvariable=self.device_index, width=6).grid(row=0, column=1, sticky=tk.W)
        ttk.Checkbutton(f, text="シミュレーション", variable=self.simulate).grid(
            row=1, column=0, columnspan=2, sticky=tk.W)
        self.btn_connect = ttk.Button(f, text="接続", command=self.toggle_connect)
        self.btn_connect.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(4, 0))

        # 現在位置
        f = ttk.LabelFrame(left, text="現在位置 [um]", padding=6)
        f.pack(fill=tk.X, pady=(0, 6))
        for i, ax in enumerate(AXES):
            ttk.Label(f, text=ax + ":").grid(row=i, column=0, sticky=tk.W)
            ttk.Label(f, textvariable=self.pos_text[i], width=14, anchor=tk.E,
                      font=("Consolas", 12)).grid(row=i, column=1, sticky=tk.E)
        ttk.Label(f, textvariable=self.state_text).grid(row=3, column=0, columnspan=2, sticky=tk.W)

        # 停止
        f = ttk.LabelFrame(left, text="停止", padding=6)
        f.pack(fill=tk.X, pady=(0, 6))
        tk.Button(f, text="緊急停止 (Esc)", bg="#d32f2f", fg="white",
                  activebackground="#b71c1c", activeforeground="white",
                  font=("", 14, "bold"), height=2,
                  command=self.emergency_stop).pack(fill=tk.X)
        ttk.Button(f, text="減速停止 (Space)", command=self.stop).pack(fill=tk.X, pady=(4, 0))

        # 相対移動
        f = ttk.LabelFrame(left, text="相対移動 (十字キー / PgUp・PgDn, Shift で×10)", padding=6)
        f.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(f, text="軸").grid(row=0, column=0)
        ttk.Label(f, text="ステップ [um]").grid(row=0, column=1)
        ttk.Label(f, text="反転").grid(row=0, column=4)
        ttk.Label(f, text="分解能 [um]").grid(row=0, column=5)
        for i, ax in enumerate(AXES):
            ttk.Label(f, text=ax).grid(row=i + 1, column=0)
            ttk.Entry(f, textvariable=self.step[i], width=10).grid(row=i + 1, column=1)
            ttk.Button(f, text="−", width=3,
                       command=lambda i=i: self.move_rel(i, -1)).grid(row=i + 1, column=2)
            ttk.Button(f, text="+", width=3,
                       command=lambda i=i: self.move_rel(i, +1)).grid(row=i + 1, column=3)
            ttk.Checkbutton(f, variable=self.invert[i]).grid(row=i + 1, column=4)
            ttk.Label(f, textvariable=self.res_text[i]).grid(row=i + 1, column=5)

        # 絶対値移動
        f = ttk.LabelFrame(left, text="絶対値移動 [um]", padding=6)
        f.pack(fill=tk.X, pady=(0, 6))
        for i, ax in enumerate(AXES):
            ttk.Label(f, text=ax).grid(row=0, column=2 * i)
            ttk.Entry(f, textvariable=self.abs_entry[i], width=10).grid(row=0, column=2 * i + 1)
        bf = ttk.Frame(f)
        bf.grid(row=1, column=0, columnspan=6, sticky=tk.EW, pady=(4, 0))
        ttk.Button(bf, text="移動", command=self.move_abs_entry).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(bf, text="現在値を取得", command=self.copy_current).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(bf, text="原点復帰", command=self.home).pack(side=tk.LEFT, expand=True, fill=tk.X)

        # マップ
        f = ttk.LabelFrame(right, text="XY マップ (クリックで絶対値移動)", padding=6)
        f.pack(fill=tk.BOTH, expand=True)
        rf = ttk.Frame(f)
        rf.pack(fill=tk.X)
        for i, ax in enumerate(AXES[:2]):
            ttk.Label(rf, text=f"{ax} 範囲").grid(row=i, column=0, sticky=tk.W)
            ttk.Entry(rf, textvariable=self.range_min[i], width=10).grid(row=i, column=1)
            ttk.Label(rf, text="〜").grid(row=i, column=2)
            ttk.Entry(rf, textvariable=self.range_max[i], width=10).grid(row=i, column=3)
        ttk.Button(rf, text="再描画", command=self.redraw_map).grid(row=0, column=4, rowspan=2, padx=4)
        ttk.Checkbutton(rf, text="クリック移動を有効", variable=self.click_enabled).grid(
            row=0, column=5, rowspan=2, padx=4)
        self.canvas = tk.Canvas(f, width=self.MAP_SIZE, height=self.MAP_SIZE, bg="white",
                                highlightthickness=1, highlightbackground="#888")
        self.canvas.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.canvas.bind("<Button-1>", self.on_map_click)
        self.canvas.bind("<Motion>", self.on_map_motion)
        self.canvas.bind("<Configure>", lambda e: self.redraw_map())
        self.cursor_text = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.cursor_text).pack(anchor=tk.W)

    def _bind_keys(self):
        for key, (ax, sign) in {
            "Left": (0, -1), "Right": (0, +1),
            "Up": (1, +1), "Down": (1, -1),
            "Prior": (2, +1), "Next": (2, -1),
        }.items():
            self.root.bind_all(f"<{key}>", lambda e, a=ax, s=sign: self._on_arrow(e, a, s, 1))
            self.root.bind_all(f"<Shift-{key}>", lambda e, a=ax, s=sign: self._on_arrow(e, a, s, 10))
        self.root.bind_all("<Escape>", lambda e: (self.emergency_stop(), "break")[1])
        self.root.bind_all("<space>", self._on_space)
        # フォーカス中のボタンが Space で押されてしまうのを防ぐ
        for cls in ("TButton", "Button", "TCheckbutton", "Checkbutton"):
            self.root.bind_class(cls, "<Key-space>", self._on_space)

    @staticmethod
    def _is_text_widget(widget):
        return isinstance(widget, (tk.Entry, ttk.Entry, tk.Spinbox, ttk.Spinbox, ttk.Combobox))

    def _on_arrow(self, event, axis, sign, factor):
        if self._is_text_widget(event.widget):
            return None  # 入力欄ではカーソル移動を優先
        self.move_rel(axis, sign, factor)
        return "break"

    def _on_space(self, event):
        if self._is_text_widget(event.widget):
            return None
        self.stop()
        return "break"

    # ---------------- 接続 ----------------
    def toggle_connect(self):
        if self.shrc is not None:
            self.disconnect()
            return
        self.btn_connect.config(state=tk.DISABLED)
        self.state_text.set("接続中...")
        idx = self.device_index.get().strip()
        num = int(idx) if idx else None
        sim = self.simulate.get()

        def task():
            try:
                shrc = connect_shrc203(simulate=sim, num_device=num)
                try:
                    res = read_resolution(shrc)
                except Exception as e:
                    res = e
                self.events.put(("connected", (shrc, res)))
            except Exception as e:
                self.events.put(("connect_error", e))
        threading.Thread(target=task, daemon=True).start()

    def disconnect(self):
        shrc, self.shrc = self.shrc, None
        if shrc is not None:
            try:
                shrc.close()
            except Exception:
                pass
        self.busy = False
        self.btn_connect.config(text="接続")
        self.state_text.set("未接続")
        for v in self.pos_text:
            v.set("---")

    # ---------------- 移動コマンド ----------------
    def _submit(self, kind, *args):
        if self.shrc is None:
            self.state_text.set("未接続です")
            return False
        if self.busy:
            return False  # 移動中は新しい移動を受け付けない (キーリピート対策)
        self.busy = True
        self.stop_msg = None
        self.shrc.device.abort.clear()
        self.jobs.put((kind, args))
        return True

    def move_rel(self, axis, sign, factor=1):
        try:
            step = float(self.step[axis].get())
        except (tk.TclError, ValueError):
            self.state_text.set("ステップ値が不正です")
            return
        if self.invert[axis].get():
            sign = -sign
        delta = [0.0, 0.0, 0.0]
        delta[axis] = sign * step * factor
        self._submit("rel", delta)

    def move_abs(self, target):
        target = quantize(target, self.resolution)
        if self._submit("abs", target):
            self.target = target
            self.redraw_map()

    def move_abs_entry(self):
        try:
            target = [float(v.get()) for v in self.abs_entry]
        except (tk.TclError, ValueError):
            self.state_text.set("目標値が不正です")
            return
        self.move_abs(target)

    def copy_current(self):
        for v, p in zip(self.abs_entry, self.pos):
            v.set(round(p))

    def home(self):
        if self.shrc is None:
            return
        if messagebox.askyesno("原点復帰", "全軸を機械原点へ復帰させます。よろしいですか？"):
            self._submit("home")

    def _send_stop(self, cmd):
        if self.shrc is None:
            return
        dev = self.shrc.device
        dev.abort.set()
        # 未実行のジョブは破棄
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        try:
            dev.query(cmd)
        except Exception as e:
            messagebox.showerror("停止エラー", f"{cmd} の送信に失敗しました:\n{e}")
            return
        self.target = None
        self.stop_msg = "緊急停止しました" if cmd == "L:E" else "停止しました"
        self.state_text.set(self.stop_msg)

    def emergency_stop(self):
        self._send_stop("L:E")

    def stop(self):
        self._send_stop("L:W")

    # ---------------- バックグラウンド ----------------
    def _worker(self):
        while self.alive:
            try:
                kind, args = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            shrc = self.shrc
            try:
                if shrc is None:
                    raise RuntimeError("未接続")
                if kind == "rel":
                    pos, _ = parse_position(shrc.device.query("Q:Su"))
                    target = quantize([p + d for p, d in zip(pos, args[0])], self.resolution)
                    self.events.put(("target", target))
                    shrc.move_abs(target, [UNIT] * 3)
                elif kind == "abs":
                    shrc.move_abs(quantize(args[0], self.resolution), [UNIT] * 3)
                elif kind == "home":
                    shrc.homeposition()
                self.events.put(("done", None))
            except Exception as e:
                self.events.put(("error", e))

    def _poller(self):
        while self.alive:
            shrc = self.shrc
            if shrc is not None:
                try:
                    pos, busy = parse_position(shrc.device.query("Q:Su"))
                    self.events.put(("pos", (pos, busy)))
                except Exception as e:
                    self.events.put(("poll_error", e))
                    time.sleep(1.0)
            time.sleep(self.POLL_MS / 1000)

    def _process_events(self):
        latest_pos = None
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "pos":
                latest_pos = data
            elif kind == "connected":
                self.shrc, res = data
                self.btn_connect.config(text="切断", state=tk.NORMAL)
                self.state_text.set("接続しました (シミュレーション)" if self.simulate.get() else "接続しました")
                if isinstance(res, Exception):
                    self.resolution = [DEFAULT_RESOLUTION_UM] * 3
                    messagebox.showwarning(
                        "分解能", f"分解能の読み込みに失敗しました。{DEFAULT_RESOLUTION_UM:g} um として扱います。\n{res}")
                else:
                    self.resolution, div = res
                for i, r in enumerate(self.resolution):
                    self.res_text[i].set(f"{r:g}" + (f" (分割{res[1][i]})" if not isinstance(res, Exception) and res[1] else ""))
            elif kind == "connect_error":
                self.btn_connect.config(state=tk.NORMAL)
                self.state_text.set("接続失敗")
                messagebox.showerror("接続エラー", str(data))
            elif kind == "target":
                self.target = data
            elif kind == "done":
                self.busy = False
                self.target = None
            elif kind == "error":
                self.busy = False
                self.target = None
                self.state_text.set("エラー")
                messagebox.showerror("移動エラー", str(data))
            elif kind == "poll_error" and self.shrc is not None:
                self.state_text.set(f"位置取得エラー: {data}")
        if latest_pos is not None and self.shrc is not None:
            self.pos, dev_busy = latest_pos
            for v, p in zip(self.pos_text, self.pos):
                v.set(f"{p:,.2f}")
            if self.stop_msg is not None:
                if not (self.busy or dev_busy):
                    self.stop_msg = None  # 停止完了まで停止メッセージを表示し続ける
            elif self.busy or dev_busy:
                self.state_text.set("移動中...")
            elif self.state_text.get() == "移動中...":
                self.state_text.set("停止中")
            self.redraw_map()
        if self.alive:
            self.root.after(self.POLL_MS, self._process_events)

    # ---------------- マップ ----------------
    def _map_geometry(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        m = 20
        try:
            xmin, ymin = float(self.range_min[0].get()), float(self.range_min[1].get())
            xmax, ymax = float(self.range_max[0].get()), float(self.range_max[1].get())
        except (tk.TclError, ValueError):
            return None
        if xmax <= xmin or ymax <= ymin or w < 2 * m + 10 or h < 2 * m + 10:
            return None
        # 縦横比を保つ
        scale = min((w - 2 * m) / (xmax - xmin), (h - 2 * m) / (ymax - ymin))
        ox = (w - scale * (xmax - xmin)) / 2
        oy = (h - scale * (ymax - ymin)) / 2
        return xmin, xmax, ymin, ymax, scale, ox, oy

    def _to_canvas(self, x, y, g):
        xmin, xmax, ymin, ymax, s, ox, oy = g
        if self.invert[0].get():
            x = xmin + xmax - x
        if self.invert[1].get():
            y = ymin + ymax - y
        return ox + (x - xmin) * s, oy + (ymax - y) * s

    def _from_canvas(self, cx, cy, g):
        xmin, xmax, ymin, ymax, s, ox, oy = g
        x = xmin + (cx - ox) / s
        y = ymax - (cy - oy) / s
        if self.invert[0].get():
            x = xmin + xmax - x
        if self.invert[1].get():
            y = ymin + ymax - y
        return x, y

    def redraw_map(self):
        c = self.canvas
        c.delete("all")
        g = self._map_geometry()
        if g is None:
            c.create_text(10, 10, anchor=tk.NW, text="範囲設定が不正です", fill="red")
            return
        xmin, xmax, ymin, ymax, s, ox, oy = g
        x0, y0 = ox, oy
        x1, y1 = ox + (xmax - xmin) * s, oy + (ymax - ymin) * s
        c.create_rectangle(x0, y0, x1, y1, outline="#444")
        for k in range(1, 10):
            gx = x0 + (x1 - x0) * k / 10
            gy = y0 + (y1 - y0) * k / 10
            c.create_line(gx, y0, gx, y1, fill="#e0e0e0")
            c.create_line(x0, gy, x1, gy, fill="#e0e0e0")
        if xmin < 0 < xmax and ymin < 0 < ymax:
            zx, zy = self._to_canvas(0, 0, g)
            c.create_line(zx, y0, zx, y1, fill="#9e9e9e", dash=(3, 3))
            c.create_line(x0, zy, x1, zy, fill="#9e9e9e", dash=(3, 3))
        lx, ly = self._to_canvas(xmin, ymin, g)
        hx, hy = self._to_canvas(xmax, ymax, g)
        c.create_text(lx, ly + 2, anchor=tk.N, text=f"{xmin:g}", fill="#666", font=("", 8))
        c.create_text(hx, ly + 2, anchor=tk.N, text=f"{xmax:g}", fill="#666", font=("", 8))
        c.create_text(x0 - 2, ly, anchor=tk.E, text=f"{ymin:g}", fill="#666", font=("", 8))
        c.create_text(x0 - 2, hy, anchor=tk.E, text=f"{ymax:g}", fill="#666", font=("", 8))

        if self.target is not None:
            tx, ty = self._to_canvas(self.target[0], self.target[1], g)
            c.create_line(tx - 8, ty, tx + 8, ty, fill="#1976d2", width=2)
            c.create_line(tx, ty - 8, tx, ty + 8, fill="#1976d2", width=2)
        if self.shrc is not None:
            px, py = self._to_canvas(self.pos[0], self.pos[1], g)
            px = min(max(px, x0), x1)
            py = min(max(py, y0), y1)
            c.create_oval(px - 6, py - 6, px + 6, py + 6, fill="#e53935", outline="")

    def on_map_motion(self, event):
        g = self._map_geometry()
        if g is None:
            return
        x, y = self._from_canvas(event.x, event.y, g)
        self.cursor_text.set(f"cursor: X={x:,.1f} um, Y={y:,.1f} um")

    def on_map_click(self, event):
        if not self.click_enabled.get():
            return
        g = self._map_geometry()
        if g is None:
            return
        xmin, xmax, ymin, ymax = g[:4]
        x, y = self._from_canvas(event.x, event.y, g)
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            return
        self.move_abs([x, y, self.pos[2]])

    # ---------------- 終了 ----------------
    def on_close(self):
        if self.busy and not messagebox.askyesno("終了", "移動中です。停止して終了しますか？"):
            return
        if self.busy:
            self.stop()
        self.alive = False
        self.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    Shrc203GUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
