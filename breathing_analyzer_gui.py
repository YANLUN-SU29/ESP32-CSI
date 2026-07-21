import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pandas as pd
import numpy as np
import os
from datetime import datetime
from scipy.signal import butter, filtfilt, welch
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei']  # 讓圖表可以顯示微軟正黑體中文
plt.rcParams['axes.unicode_minus'] = False
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

class BreathingAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CSI 呼吸頻率分析器 (Desktop GUI版)")
        self.root.geometry("1100x800")
        
        # 綁定視窗關閉事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # 建立上方控制面板
        control_frame = ttk.Frame(root, padding="10")
        control_frame.pack(side=tk.TOP, fill=tk.X)
        
        ttk.Label(control_frame, text="請選擇 CSI CSV 檔案進行分析:").pack(side=tk.LEFT, padx=5)
        
        self.btn_load = ttk.Button(control_frame, text="📁 載入 CSV 檔案", command=self.load_file)
        self.btn_load.pack(side=tk.LEFT, padx=5)
        
        self.label_status = ttk.Label(control_frame, text="等待載入...", foreground="blue")
        self.label_status.pack(side=tk.LEFT, padx=15)
        
        # 建立資料表格區域
        table_frame = ttk.LabelFrame(root, text="分析結果與數據", padding="10")
        table_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)
        
        columns = ("檔案名稱", "資料筆數", "持續時間 (秒)", "採樣率 (Hz)", "峰值頻率 (Hz)", "呼吸次數 (Welch)", "呼吸次數 (零交叉)")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=1)
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=130, anchor="center")
        self.tree.column("檔案名稱", width=220, anchor="w")
        self.tree.pack(fill=tk.X)
        
        # 建立圖表區域
        chart_frame = ttk.Frame(root)
        chart_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.fig, self.axs = plt.subplots(3, 1, figsize=(10, 8))
        self.fig.tight_layout(pad=4.0)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.draw()
        
        # 加入 matplotlib 工具列 (可縮放/存圖)
        toolbar = NavigationToolbar2Tk(self.canvas, chart_frame)
        toolbar.update()
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def on_closing(self):
        self.root.quit()
        self.root.destroy()
        import sys
        sys.exit(0)

    def load_file(self):
        filepath = filedialog.askopenfilename(
            title="選擇 CSI CSV 檔案",
            filetypes=(("CSV Files", "*.csv"), ("All Files", "*.*")),
            initialdir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        )
        
        if not filepath:
            return
            
        try:
            self.label_status.config(text=f"正在分析 {os.path.basename(filepath)}...", foreground="blue")
            self.root.update()
            
            self.analyze_and_plot(filepath)
            
            self.label_status.config(text="✅ 分析完成", foreground="green")
        except Exception as e:
            messagebox.showerror("錯誤", f"分析時發生錯誤:\n{str(e)}")
            self.label_status.config(text="❌ 分析失敗", foreground="red")

    def analyze_and_plot(self, filepath):
        filename = os.path.basename(filepath)
        
        # 讀取 CSV
        df = pd.read_csv(filepath)
        if len(df) < 10:
            raise ValueError("資料筆數不足")
            
        # 計算採樣率 fs
        t1 = datetime.strptime(df.iloc[0, 0].strip(), "%H:%M:%S.%f")
        t2 = datetime.strptime(df.iloc[-1, 0].strip(), "%H:%M:%S.%f")
        duration = (t2 - t1).total_seconds()
        n_samples = len(df)
        fs = n_samples / duration
        
        if duration <= 0:
            raise ValueError("無效的時間範圍")
            
        # Step 1: 52 子載波取平均
        sub_cols = [f'Sub_{i}' for i in range(52)]
        mean_signal = df[sub_cols].mean(axis=1).values
        
        # Step 2: 帶通濾波 0.1~0.6 Hz
        nyq = fs / 2.0
        low = 0.1 / nyq
        high = 0.6 / nyq
        b, a = butter(4, [low, high], btype="band")
        filtered_signal = filtfilt(b, a, mean_signal)
        
        # Step 3: 頻譜分析 (Welch's method)
        f, Pxx = welch(filtered_signal, fs, nperseg=min(n_samples, 256))
        
        # Step 4: 找呼吸頻段 (0.1~0.6 Hz) 的峰值
        mask = (f >= 0.1) & (f <= 0.6)
        f_breath = f[mask]
        Pxx_breath = Pxx[mask]
        
        if len(Pxx_breath) == 0:
            raise ValueError("找不到呼吸頻率範圍的頻譜資料")
            
        peak_idx = np.argmax(Pxx_breath)
        peak_freq = f_breath[peak_idx]
        bpm_welch = peak_freq * 60
        
        # 時域驗證：上升零交叉計數
        rising_crossings = np.where(np.diff(np.sign(filtered_signal)) > 0)[0]
        bpm_time = len(rising_crossings) / duration * 60
        
        # 更新表格
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        self.tree.insert("", "end", values=(
            filename,
            n_samples,
            f"{duration:.1f}",
            f"{fs:.2f}",
            f"{peak_freq:.3f}",
            f"{bpm_welch:.1f} bpm",
            f"{bpm_time:.1f} bpm"
        ))
        
        # 更新圖表
        t = np.arange(n_samples) / fs
        
        self.axs[0].clear()
        self.axs[0].plot(t, mean_signal, color='gray', alpha=0.7)
        self.axs[0].set_title(f"第一步: 原始 CSI 平均振幅 ({filename})")
        self.axs[0].set_ylabel("振幅")
        
        self.axs[1].clear()
        self.axs[1].plot(t, filtered_signal, color='blue', linewidth=1.5)
        self.axs[1].set_title("第二步: 濾波後呼吸訊號 (帶通濾波 0.1-0.6 Hz)")
        self.axs[1].set_ylabel("振幅")
        
        self.axs[2].clear()
        self.axs[2].plot(f, Pxx, color='red', linewidth=1.5)
        self.axs[2].set_title(f"第三至四步: 頻譜分析 (呼吸次數 = {bpm_welch:.1f} 次/分)")
        self.axs[2].set_xlabel("頻率 (Hz)")
        self.axs[2].set_ylabel("功率")
        self.axs[2].set_xlim(0, 1.5) # 只顯示 0~1.5Hz
        
        # 標記峰值點
        self.axs[2].plot(peak_freq, Pxx_breath[peak_idx], "go", markersize=8, label=f"峰值頻率: {peak_freq:.3f} Hz")
        self.axs[2].legend()
        
        self.fig.tight_layout(pad=3.0)
        self.canvas.draw()

if __name__ == "__main__":
    root = tk.Tk()
    app = BreathingAnalyzerApp(root)
    root.mainloop()
