import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from scipy.signal import detrend, butter, filtfilt
from sklearn.decomposition import PCA

# 強制設定微軟正黑體與負號顯示
plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei'] 
plt.rcParams['axes.unicode_minus'] = False               

class CSISpecificFeatureViewer_V3:
    def __init__(self, root):
        self.root = root
        self.root.title("CSI 物理特徵與 52 載波全景觀測器")
        self.root.geometry("1200x850")
        
        self.csi_matrix = None
        self.time_axis = None
        self.fps = 60.9  
        self.pc1_signal = None
        
        self.fig, (self.ax1, self.ax2, self.ax3) = plt.subplots(3, 1, figsize=(10, 8))
        self.fig.tight_layout(pad=4.0)

        self.create_widgets()

    def create_widgets(self):
        control_frame = ttk.LabelFrame(self.root, text="操作面板", padding=(10, 10))
        control_frame.pack(fill=tk.X, padx=10, pady=5)

        # 1. 載入檔案
        file_frame = ttk.Frame(control_frame)
        file_frame.pack(fill=tk.X, pady=5)
        ttk.Button(file_frame, text="1. 載入 CSV 檔案", command=self.load_file).pack(side=tk.LEFT, padx=5)
        self.lbl_file = ttk.Label(file_frame, text="尚未載入檔案...", foreground="blue")
        self.lbl_file.pack(side=tk.LEFT, padx=10)

        # 2. 選擇特徵按鈕
        filter_frame = ttk.Frame(control_frame)
        filter_frame.pack(fill=tk.X, pady=10)
        ttk.Label(filter_frame, text="2. 選擇你想驗證的目標特徵：", font=('Microsoft JhengHei', 10, 'bold')).pack(side=tk.LEFT, padx=5)
        
        self.var_mode = tk.StringVar(value="breath")
        
        rb1 = ttk.Radiobutton(filter_frame, text="大動作 (0.05~5.0 Hz)", variable=self.var_mode, value="walk", command=self.update_plot)
        rb2 = ttk.Radiobutton(filter_frame, text="呼吸 (0.15~0.4 Hz)", variable=self.var_mode, value="breath", command=self.update_plot)
        rb3 = ttk.Radiobutton(filter_frame, text="心跳 (0.8~2.0 Hz)", variable=self.var_mode, value="heart", command=self.update_plot)
        rb4 = ttk.Radiobutton(filter_frame, text="空房間基準 (寬頻底噪)", variable=self.var_mode, value="empty", command=self.update_plot)
        
        rb1.pack(side=tk.LEFT, padx=10)
        rb2.pack(side=tk.LEFT, padx=10)
        rb3.pack(side=tk.LEFT, padx=10)
        rb4.pack(side=tk.LEFT, padx=10)

        # 圖表區
        plot_frame = ttk.Frame(self.root)
        plot_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def load_file(self):
        filepath = filedialog.askopenfilename(title="選擇 CSI 錄製檔", filetypes=(("CSV Files", "*.csv"), ("All Files", "*.*")))
        if not filepath: return
        try:
            df = pd.read_csv(filepath)
            if 'Timestamp' in df.columns or df.columns[0].lower() == 'timestamp':
                self.csi_matrix = df.iloc[:, 1:].values
            else:
                self.csi_matrix = df.values
                
            self.time_axis = np.arange(self.csi_matrix.shape[0]) / self.fps
            self.lbl_file.config(text=f"已載入: {os.path.basename(filepath)} (共 {self.csi_matrix.shape[0]} 幀)")
            
            pca = PCA(n_components=1)
            self.pc1_signal = pca.fit_transform(self.csi_matrix)[:, 0]
            
            if np.corrcoef(self.pc1_signal, self.csi_matrix[:, 20])[0, 1] < 0:
                self.pc1_signal = -self.pc1_signal
                
            self.update_plot()
        except Exception as e:
            messagebox.showerror("錯誤", f"無法載入檔案:\n{e}")

    def basic_clean(self, data):
        s = pd.Series(data)
        rmed = s.rolling(31, center=True).median()
        rstd = s.rolling(31, center=True).std()
        outliers = np.abs(s - rmed) > (3 * rstd)
        s[outliers] = rmed[outliers]
        clean_data = s.bfill().ffill().values
        return detrend(clean_data, type='linear')

    def update_plot(self):
        if self.csi_matrix is None: return

        base_signal = self.basic_clean(self.pc1_signal)
        
        mode = self.var_mode.get()
        if mode == "walk":
            lowcut, highcut = 0.05, 5.0
            color, title_target = 'red', "大動作 (走動/起立坐下)"
        elif mode == "breath":
            lowcut, highcut = 0.15, 0.4
            color, title_target = 'blue', "微體徵 (呼吸)"
        elif mode == "heart":
            lowcut, highcut = 0.8, 2.0
            color, title_target = 'darkorange', "極微體徵 (心跳)"
        elif mode == "empty":
            # 空房間測試寬頻，確認沒有任何動作頻率的殘留
            lowcut, highcut = 0.05, 5.0 
            color, title_target = 'green', "空房間基準 (確認底噪平坦)"

        b, a = butter(4, [lowcut / (self.fps/2), highcut / (self.fps/2)], btype='band')
        target_signal = filtfilt(b, a, base_signal)

        self.ax1.clear(); self.ax2.clear(); self.ax3.clear()

        # 1. 繪製 52 條全景子載波
        for i in range(self.csi_matrix.shape[1]):
            self.ax1.plot(self.time_axis, self.csi_matrix[:, i], color='gray', alpha=0.3, linewidth=0.5)
        # 用粗黑線突顯 Sub_20，方便對照
        self.ax1.plot(self.time_axis, self.csi_matrix[:, 20], color='black', alpha=0.8, linewidth=1.5, label='Sub_20')
        self.ax1.set_title("1. 全景 52 條子載波 (疊加) - 觀察空間多徑衰落與整體變異", fontsize=10)
        self.ax1.set_ylabel("Amplitude")
        self.ax1.set_xlim(0, self.time_axis[-1])
        self.ax1.legend(loc='upper right')

        # 2. PCA 融合
        self.ax2.plot(self.time_axis, base_signal, color='gray', linewidth=1)
        self.ax2.set_title("2. PCA 融合 + 基礎清理 (無濾除特定頻率，此為送入 AI 之真實訊號)", fontsize=10)
        self.ax2.set_ylabel("PC1 Cleaned")
        self.ax2.set_xlim(0, self.time_axis[-1])

        # 3. 目標特徵萃取
        self.ax3.plot(self.time_axis, target_signal, color=color, linewidth=2)
        self.ax3.set_title(f"3. 專屬特徵萃取：{title_target} ({lowcut} ~ {highcut} Hz)", fontsize=12, fontweight='bold')
        self.ax3.set_xlabel("Time (Seconds)")
        self.ax3.set_ylabel("Filtered Value")
        self.ax3.set_xlim(0, self.time_axis[-1])

        for ax in [self.ax1, self.ax2, self.ax3]:
            ax.grid(True, linestyle='--', alpha=0.6)

        self.fig.tight_layout(pad=4.0)
        self.canvas.draw()

if __name__ == "__main__":
    root = tk.Tk()
    app = CSISpecificFeatureViewer_V3(root)
    root.mainloop()
