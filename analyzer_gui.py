import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, NullFormatter
import matplotlib as mpl
from scipy.signal import butter, detrend, filtfilt, welch
from sklearn.decomposition import PCA

# 強制設定微軟正黑體與負號顯示
mpl.rcParams['font.sans-serif'] = ['Microsoft JhengHei']
mpl.rcParams['axes.unicode_minus'] = False
# 微軟正黑體缺 U+2212，對數軸的指數由 mathtext 繪製，需指定有該字元的字型集
mpl.rcParams['mathtext.fontset'] = 'dejavusans'

# 各分析模式的目標頻段。calibrated 表示該頻段是否有實測 ground truth 可供判定，
# 目前只有呼吸頻段有（20 筆呼吸 / 5 筆閉氣 / 5 筆空房間）；突出度會隨頻段寬度
# 系統性改變，未校準的頻段不可套用同一組門檻。
MODES = {
    'walk':   {'band': (0.05, 5.00), 'color': 'red',        'label': '大動作 (走動/起立坐下)',   'calibrated': False},
    'breath': {'band': (0.15, 0.40), 'color': 'blue',       'label': '微體徵 (呼吸)',            'calibrated': True},
    'heart':  {'band': (0.80, 2.00), 'color': 'darkorange', 'label': '極微體徵 (心跳)',          'calibrated': False},
    'empty':  {'band': (0.05, 5.00), 'color': 'green',      'label': '空房間基準 (確認底噪平坦)', 'calibrated': False},
}

PLOT_MAX_POINTS = 4000   # 繪圖抽樣上限，避免長檔案拖慢畫面
TOP_K = 10               # 計算載波共識度時取前幾強的子載波
CONSENSUS_TOL = 0.02     # Hz；峰值頻率差距在此範圍內視為「同意」

# 判定門檻。以 20 筆呼吸 / 5 筆閉氣 / 5 筆空房間實測資料校準。
# 主指標採「最佳單一子載波突出度」而非融合訊號突出度：實測前者對
# 呼吸 vs (無人+閉氣) 的 AUC=1.00（排除 P0 盲區），門檻 20 命中 14/15 且零誤報；
# 換成融合訊號突出度只命中 9/15。共識度僅作輔助條件，門檻拉到 0.8 會誤殺真呼吸。
TH_HIGH_PROM, TH_HIGH_CONS = 20.0, 0.70
TH_MID_PROM = 10.0


class CSIFeatureViewer:
    def __init__(self, root):
        self.root = root
        self.root.title("CSI 物理特徵與 52 載波全景觀測器")
        self.root.geometry("1280x950")
        self.root.minsize(1000, 700)

        self.csi_matrix = None      # 原始振幅矩陣（含全零載波）
        self.clean_matrix = None    # 逐條清理後的有效子載波
        self.sub_index = None       # clean_matrix 各欄對應的原始 Sub_n 編號
        self.sub_freqs = None       # 各子載波 PSD 的頻率軸（載入時算一次）
        self.sub_psd = None         # 各子載波的 PSD（載入時算一次）
        self.time_axis = None
        self.fps = 60.9
        self.filepath = None

        self._worker = None
        self._result = None
        self._error = None
        self._progress = 0
        self._stage = ""

        self.fig = Figure(figsize=(10, 8))
        self.ax1, self.ax2, self.ax3 = self.fig.subplots(3, 1)

        self.create_widgets()

    # ------------------------------------------------------------------ UI --
    def create_widgets(self):
        control_frame = ttk.LabelFrame(self.root, text="操作面板", padding=(10, 8))
        control_frame.pack(fill=tk.X, padx=10, pady=(8, 4))

        # 1. 載入檔案
        file_frame = ttk.Frame(control_frame)
        file_frame.pack(fill=tk.X, pady=3)
        self.btn_load = ttk.Button(file_frame, text="1. 載入 CSV 檔案", command=self.load_file)
        self.btn_load.pack(side=tk.LEFT, padx=5)
        self.lbl_file = ttk.Label(file_frame, text="尚未載入檔案...", foreground="blue")
        self.lbl_file.pack(side=tk.LEFT, padx=10)

        # 2. 選擇特徵按鈕
        filter_frame = ttk.Frame(control_frame)
        filter_frame.pack(fill=tk.X, pady=(8, 3))
        ttk.Label(filter_frame, text="2. 選擇你想驗證的目標特徵：",
                  font=('Microsoft JhengHei', 10, 'bold')).pack(side=tk.LEFT, padx=5)

        self.var_mode = tk.StringVar(value="breath")
        self.radios = []
        for key in ('walk', 'breath', 'heart', 'empty'):
            lo, hi = MODES[key]['band']
            name = MODES[key]['label'].split(' ')[0]
            rb = ttk.Radiobutton(filter_frame, text=f"{name} ({lo}~{hi} Hz)",
                                 variable=self.var_mode, value=key, command=self.update_plot)
            rb.pack(side=tk.LEFT, padx=10)
            self.radios.append(rb)

        # 分析結果面板
        result_frame = ttk.LabelFrame(self.root, text="分析結果", padding=(10, 8))
        result_frame.pack(fill=tk.X, padx=10, pady=4)

        grid = ttk.Frame(result_frame)
        grid.pack(fill=tk.X)
        self.lbl_rate = self._metric(grid, 0, "推估頻率", "—")
        self.lbl_prom = self._metric(grid, 1, "峰值突出度", "—")
        self.lbl_cons = self._metric(grid, 2, "載波共識度", "—")

        self.lbl_verdict = ttk.Label(result_frame, text="請先載入檔案",
                                     font=('Microsoft JhengHei', 11, 'bold'), foreground="gray")
        self.lbl_verdict.pack(anchor=tk.W, pady=(8, 0))
        self.lbl_hint = ttk.Label(result_frame, text="", foreground="gray50",
                                  font=('Microsoft JhengHei', 9))
        self.lbl_hint.pack(anchor=tk.W)

        # 圖表區（含工具列，可縮放/平移/存檔）
        plot_frame = ttk.Frame(self.root)
        plot_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        toolbar = NavigationToolbar2Tk(self.canvas, plot_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side=tk.TOP, fill=tk.X)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # 狀態列
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, padx=10, pady=(0, 8))
        self.lbl_status = ttk.Label(status_frame, text="就緒", foreground="gray30")
        self.lbl_status.pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(status_frame, mode='determinate', length=220)
        self.progress.pack(side=tk.RIGHT)

    def _metric(self, parent, col, title, value):
        box = ttk.Frame(parent)
        box.grid(row=0, column=col, padx=(0, 40), sticky=tk.W)
        ttk.Label(box, text=title, foreground="gray40",
                  font=('Microsoft JhengHei', 9)).pack(anchor=tk.W)
        lbl = ttk.Label(box, text=value, font=('Microsoft JhengHei', 14, 'bold'))
        lbl.pack(anchor=tk.W)
        return lbl

    def _set_busy(self, busy):
        state = tk.DISABLED if busy else tk.NORMAL
        self.btn_load.config(state=state)
        for rb in self.radios:
            rb.config(state=state)
        self.root.config(cursor="watch" if busy else "")

    # -------------------------------------------------------------- 載入流程 --
    def load_file(self):
        filepath = filedialog.askopenfilename(
            title="選擇 CSI 錄製檔",
            filetypes=(("CSV Files", "*.csv"), ("All Files", "*.*")))
        if not filepath:
            return

        self.filepath = filepath
        self._result = self._error = None
        self._progress, self._stage = 0, "讀取檔案..."
        self._set_busy(True)
        self.progress.config(value=0)
        self.lbl_status.config(text=self._stage)

        # 清理 43 條子載波在長檔案上要數秒，丟到工作執行緒避免 UI 凍結
        self._worker = threading.Thread(target=self._load_worker, args=(filepath,), daemon=True)
        self._worker.start()
        self.root.after(80, self._poll_load)

    def _load_worker(self, filepath):
        try:
            df = pd.read_csv(filepath)
            has_ts = 'Timestamp' in df.columns or df.columns[0].lower() == 'timestamp'
            if has_ts:
                csi = df.iloc[:, 1:].values.astype(float)
                # 舊版錄製為毫秒、新版為微秒，%f 兩者皆可解析
                ts = pd.to_datetime(df.iloc[:, 0], format='%H:%M:%S.%f')
                time_axis = (ts - ts.iloc[0]).dt.total_seconds().values
                # 用整段的「總幀數/總時長」估平均 fps；不用逐筆間隔中位數，
                # 因為 monitor.py 是一次把序列埠緩衝區讀乾，樣本到達本來就一陣一陣的
                duration = time_axis[-1]
                fps = (len(time_axis) - 1) / duration if duration > 0 else self.fps
            else:
                csi = df.values.astype(float)
                fps = self.fps
                time_axis = np.arange(csi.shape[0]) / fps

            # 剔除全程為 0 的子載波（ESP32 HT20 CSI 的 null/guard 通道）
            keep = ~np.all(csi == 0, axis=0)
            sub_index = np.where(keep)[0]
            raw = csi[:, keep]

            self._stage = "清理子載波..."
            clean = np.empty_like(raw)
            for j in range(raw.shape[1]):
                clean[:, j] = self._clean(raw[:, j])
                self._progress = 10 + int(70 * (j + 1) / raw.shape[1])

            # 各子載波的 PSD 與頻段無關，載入時算一次即可，切換模式就不必重算
            self._stage = "計算頻譜..."
            nper = int(min(len(clean), fps * 60))
            freqs, _ = welch(clean[:, 0], fs=fps, nperseg=nper, nfft=8192)
            psd = np.empty((len(freqs), clean.shape[1]))
            for j in range(clean.shape[1]):
                _, psd[:, j] = welch(clean[:, j], fs=fps, nperseg=nper, nfft=8192)
                self._progress = 80 + int(20 * (j + 1) / clean.shape[1])

            self._result = dict(csi=csi, clean=clean, sub_index=sub_index,
                                time_axis=time_axis, fps=fps,
                                sub_freqs=freqs, sub_psd=psd)
            self._progress = 100
        except Exception as e:
            self._error = e

    def _poll_load(self):
        if self._worker is not None and self._worker.is_alive():
            self.progress.config(value=self._progress)
            self.lbl_status.config(text=self._stage)
            self.root.after(80, self._poll_load)
            return

        self._worker = None
        self._set_busy(False)
        self.progress.config(value=0)

        if self._error is not None:
            self.lbl_status.config(text="載入失敗")
            messagebox.showerror("錯誤", f"無法載入檔案:\n{self._error}")
            self._error = None
            return

        r = self._result
        self.csi_matrix, self.clean_matrix = r['csi'], r['clean']
        self.sub_index, self.time_axis, self.fps = r['sub_index'], r['time_axis'], r['fps']
        self.sub_freqs, self.sub_psd = r['sub_freqs'], r['sub_psd']

        dropped = self.csi_matrix.shape[1] - self.clean_matrix.shape[1]
        self.lbl_file.config(
            text=f"已載入: {os.path.basename(self.filepath)} "
                 f"({self.csi_matrix.shape[0]} 幀 / {self.time_axis[-1]:.1f}s / "
                 f"實測 {self.fps:.1f} fps / 有效載波 {self.clean_matrix.shape[1]}"
                 f"{f'，剔除全零 {dropped} 條' if dropped else ''})")
        self.lbl_status.config(text="就緒")
        self.update_plot()

    # ---------------------------------------------------------------- 演算法 --
    @staticmethod
    def _clean(data):
        """Rolling MAD 離群值抑制 + 線性去趨勢。與目標頻段無關，故只需算一次。"""
        s = pd.Series(data)
        rmed = s.rolling(31, center=True).median()
        rstd = s.rolling(31, center=True).std()
        outliers = np.abs(s - rmed) > (3 * rstd)
        s[outliers] = rmed[outliers]
        return detrend(s.bfill().ffill().values, type='linear')

    def _band_peak(self, freqs, psd, band):
        """回傳頻段內的 (峰值頻率, 突出度=峰值/帶內中位數)。"""
        m = (freqs >= band[0]) & (freqs <= band[1])
        if m.sum() < 3:
            return np.nan, np.nan
        i = np.argmax(psd[m])
        med = np.median(psd[m])
        return freqs[m][i], (psd[m][i] / med if med > 0 else np.nan)

    def _fuse(self, band):
        """先帶通濾波、再 PCA 融合。

        順序很重要：原始振幅的變異量由寬頻雜訊與 AGC 漂移主導，遠大於胸腔起伏
        造成的微弱調變，若先做 PCA，PC1 會鎖定雜訊主軸而不是生理訊號。實測
        (20 筆呼吸 / 5 筆空房間) 顯示先融合再濾波完全分不出有無人員，改為先
        濾波再融合後兩者才明顯分離。
        """
        nyq = self.fps / 2
        lo, hi = band[0] / nyq, min(band[1] / nyq, 0.99)
        b, a = butter(4, [lo, hi], btype='band')
        filtered = filtfilt(b, a, self.clean_matrix, axis=0)

        pca = PCA(n_components=1)
        pc1 = pca.fit_transform(filtered)[:, 0]
        # 讓權重最大的子載波為正向，確保波形符號穩定
        loading = pca.components_[0]
        if loading[np.argmax(np.abs(loading))] < 0:
            pc1 = -pc1
        return pc1

    def _subcarrier_stats(self, band):
        """逐條子載波在目標頻段的峰值分析，判定與頻率推估都以此為準。

        不用融合訊號來判定，是因為實測 30 筆資料顯示「最佳單一子載波突出度」
        的判別力遠勝融合訊號（AUC 1.00 vs 0.74）。融合訊號仍保留作波形顯示。

        共識度＝前 K 強子載波的峰值頻率一致程度：真實週期性生理訊號會讓多條
        子載波獨立收斂到同一頻率，純雜訊則各自亂跳（實測呼吸多為 100%、空房間 0~100%）。
        推估頻率取前 K 強的中位數，比單看融合峰值穩定得多
        （實測各位置 ±0.1~0.6 次/分，融合峰值為 ±0.5~3.8）。
        """
        peaks = [self._band_peak(self.sub_freqs, self.sub_psd[:, j], band)
                 for j in range(self.sub_psd.shape[1])]
        freqs = np.array([f for f, _ in peaks])
        proms = np.array([p for _, p in peaks])
        valid = ~np.isnan(proms)
        if valid.sum() < 3:
            return None
        order = np.where(valid)[0][np.argsort(-proms[valid])]
        top = order[:TOP_K]
        cons_freq = float(np.median(freqs[top]))
        return {
            'best': int(order[0]),
            'prom': float(proms[order[0]]),
            'consensus': float(np.mean(np.abs(freqs[top] - cons_freq) < CONSENSUS_TOL)),
            'freq': cons_freq,
            'top': top,
        }

    @staticmethod
    def _normalise(psd, freqs, band):
        """把 PSD 除以帶內中位數，讓縱軸 1.0 = 雜訊底、峰值高度 = 突出度。"""
        m = (freqs >= band[0]) & (freqs <= band[1])
        med = np.median(psd[m]) if m.any() else 0
        return psd / med if med > 0 else psd

    # ---------------------------------------------------------------- 繪圖 --
    def _decimate(self, y):
        n = len(y)
        if n <= PLOT_MAX_POINTS:
            return self.time_axis, y
        step = n // PLOT_MAX_POINTS + 1
        return self.time_axis[::step], y[::step]

    def update_plot(self):
        if self.clean_matrix is None:
            return

        mode = MODES[self.var_mode.get()]
        band, color, title = mode['band'], mode['color'], mode['label']

        fused = self._fuse(band)
        nper = int(min(len(fused), self.fps * 60))
        f_fused, p_fused = welch(fused, fs=self.fps, nperseg=nper, nfft=8192)
        stats = self._subcarrier_stats(band)

        self._update_metrics(stats)

        for ax in (self.ax1, self.ax2, self.ax3):
            ax.clear()

        # 1. 全景子載波（抽樣後繪製，避免長檔案卡頓）
        for j in range(self.clean_matrix.shape[1]):
            t, y = self._decimate(self.csi_matrix[:, self.sub_index[j]])
            self.ax1.plot(t, y, color='gray', alpha=0.3, linewidth=0.5)
        if stats is not None:
            best = self.sub_index[stats['best']]
            t, y = self._decimate(self.csi_matrix[:, best])
            self.ax1.plot(t, y, color='black', alpha=0.85, linewidth=1.4,
                          label=f'Sub_{best}（該頻段最強）')
            self.ax1.legend(loc='upper right', fontsize=8)
        self.ax1.set_title(f"1. 全景 {self.clean_matrix.shape[1]} 條有效子載波 — 觀察多徑衰落與整體變異",
                           fontsize=10)
        self.ax1.set_ylabel("Amplitude")
        self.ax1.set_xlim(0, self.time_axis[-1])

        # 2. 先帶通濾波 → 再 PCA 融合
        t, y = self._decimate(fused)
        self.ax2.plot(t, y, color=color, linewidth=1.2)
        self.ax2.set_title(f"2. 目標頻段融合波形：{title}（先帶通 {band[0]}~{band[1]} Hz，再 PCA 融合）",
                           fontsize=10)
        self.ax2.set_ylabel("Fused PC1")
        self.ax2.set_xlabel("Time (Seconds)")
        self.ax2.set_xlim(0, self.time_axis[-1])

        # 3. 功率頻譜 — 峰值是否真的存在要看這張，不能只看波形
        #    縱軸已除以帶內中位數，所以峰值高度就是突出度，可直接對照判定門檻
        xmax = min(band[1] * 2.5, self.fps / 2)
        self.ax3.axvspan(band[0], band[1], color=color, alpha=0.12, label='目標頻段')
        self.ax3.semilogy(f_fused, self._normalise(p_fused, f_fused, band),
                          color='gray', linewidth=0.9, alpha=0.75, label='融合訊號')
        if stats is not None:
            best = self.sub_index[stats['best']]
            self.ax3.semilogy(self.sub_freqs,
                              self._normalise(self.sub_psd[:, stats['best']], self.sub_freqs, band),
                              color=color, linewidth=1.3, label=f'Sub_{best}（判定依據）')
            self.ax3.axvline(stats['freq'], color=color, linestyle='--', linewidth=1.4,
                             label=f"共識峰值 {stats['freq']:.3f} Hz ({stats['freq']*60:.1f}/分)")
        self.ax3.axhline(1.0, color='gray', linewidth=0.8, alpha=0.6)
        if mode['calibrated']:
            self.ax3.axhline(TH_HIGH_PROM, color='black', linestyle=':', linewidth=1.2,
                             label=f'高信心門檻 ({TH_HIGH_PROM:.0f}×)')
        self.ax3.set_title("3. 功率頻譜 (Welch PSD，已正規化為突出度倍率) — 確認峰值是真實週期，而非帶通造成的漣漪假象",
                           fontsize=11, fontweight='bold')
        self.ax3.set_xlabel("Frequency (Hz)")
        self.ax3.set_ylabel("相對帶內雜訊底 (倍)")
        self.ax3.set_xlim(0, xmax)
        # 融合訊號在帶外已被濾到趨近 0，若不設限對數軸會被拉到 20 個數量級而壓平峰值
        top = max(TH_HIGH_PROM * 1.5, (stats['prom'] if stats else 1) * 2.5)
        self.ax3.set_ylim(0.05, top)
        # 直接標「幾倍」比 10 的次方好讀，也避開 mathtext 指數的負號字型問題
        self.ax3.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f'{v:g}'))
        self.ax3.yaxis.set_minor_formatter(NullFormatter())
        self.ax3.legend(loc='upper right', fontsize=8)

        for ax in (self.ax1, self.ax2, self.ax3):
            ax.grid(True, linestyle='--', alpha=0.6)

        self.fig.tight_layout(pad=2.5)
        self.canvas.draw_idle()

    def _update_metrics(self, stats):
        if stats is None:
            for lbl in (self.lbl_rate, self.lbl_prom, self.lbl_cons):
                lbl.config(text="—")
            self.lbl_verdict.config(text="頻段內樣本不足，無法判定", foreground="gray")
            self.lbl_hint.config(text="")
            return

        f, prom, cons = stats['freq'], stats['prom'], stats['consensus']
        self.lbl_rate.config(text=f"{f*60:.1f} 次/分  ({f:.3f} Hz)")
        self.lbl_prom.config(text=f"{prom:.1f}")
        self.lbl_cons.config(text=f"{cons*100:.0f} %")

        mode = MODES[self.var_mode.get()]
        if not mode['calibrated']:
            # 突出度會隨頻段寬度系統性改變，套用呼吸頻段的門檻會嚴重高估
            self.lbl_verdict.config(
                text="◆ 此頻段尚無實測校準 — 數值僅供不同檔案間比較，不代表偵測結論",
                foreground="#6b7280")
            self.lbl_hint.config(
                text=f"目前只有呼吸頻段 ({MODES['breath']['band'][0]}~{MODES['breath']['band'][1]} Hz) "
                     f"有 ground truth 可校準；此頻段缺對照組資料，門檻無法直接套用。")
            return

        if prom >= TH_HIGH_PROM and cons >= TH_HIGH_CONS:
            verdict, fg = "● 高信心 — 偵測到穩定的週期性訊號", "#1a7f37"
        elif prom >= TH_MID_PROM:
            verdict, fg = "● 中等信心 — 有週期性跡象，但不夠明確", "#b8860b"
        else:
            verdict, fg = "● 低信心 — 與雜訊難以區分，不宜視為偵測到", "#b42318"
        self.lbl_verdict.config(text=verdict, foreground=fg)
        self.lbl_hint.config(
            text=f"判定依最強子載波：突出度 ≥{TH_HIGH_PROM:.0f} 且共識度 ≥{TH_HIGH_CONS*100:.0f}% 為高信心　"
                 f"｜實測校準：呼吸 18~165 / 多為 100%，空房間 5.7~15.8，閉氣 5.6~11.8")


if __name__ == "__main__":
    root = tk.Tk()
    app = CSIFeatureViewer(root)
    root.mainloop()
