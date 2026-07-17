import tkinter as tk
from tkinter import filedialog, messagebox
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

class CSIAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CSI Offline Analyzer")
        self.root.geometry("1200x900")

        # 建立頂部控制面板
        top_frame = tk.Frame(root, bg="#2c3e50", pady=10)
        top_frame.pack(side=tk.TOP, fill=tk.X)

        self.btn_load = tk.Button(top_frame, text="選擇 CSV 檔案", font=("Arial", 12, "bold"), 
                                  bg="#3498db", fg="white", command=self.load_csv)
        self.btn_load.pack(side=tk.LEFT, padx=20)

        self.lbl_file = tk.Label(top_frame, text="請選擇要分析的 CSI 數據檔案...", 
                                 font=("Arial", 12), bg="#2c3e50", fg="white")
        self.lbl_file.pack(side=tk.LEFT, padx=10)

        # 建立 Matplotlib 畫布區塊
        self.fig = plt.Figure(figsize=(12, 10))
        self.canvas = FigureCanvasTkAgg(self.fig, master=root)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # 加入工具列 (支援放大、縮小、存檔)
        self.toolbar = NavigationToolbar2Tk(self.canvas, root)
        self.toolbar.update()

    def load_csv(self):
        filepath = filedialog.askopenfilename(
            title="選擇 CSI 數據",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")]
        )
        if not filepath:
            return
        
        self.lbl_file.config(text=f"載入並分析中: {filepath.split('/')[-1]}...", fg="#f1c40f")
        self.root.update()

        try:
            self.process_and_plot(filepath)
            self.lbl_file.config(text=f"分析完成: {filepath.split('/')[-1]}", fg="#2ecc71")
        except Exception as e:
            messagebox.showerror("資料解析錯誤", f"無法讀取或繪製此檔案，請確認是否為標準 CSI CSV 格式。\n錯誤細節: {e}")
            self.lbl_file.config(text="分析失敗", fg="#e74c3c")

    def process_and_plot(self, filepath):
        # 1. 讀取資料
        df = pd.read_csv(filepath)
        sub_cols = [f'Sub_{i}' for i in range(52)]
        
        # 檢查欄位是否完整
        if not all(col in df.columns for col in sub_cols):
            raise ValueError("CSV 檔案缺少部分 Sub_X 欄位。")

        data = df[sub_cols]
        frames = df.index

        # 清除舊圖表，重新建立 4 個子圖 (共用 X 軸)
        self.fig.clf()
        self.axs = self.fig.subplots(4, 1, sharex=True)
        
        # -------------------------------------------------
        # 圖表 1：全域變異數 (Global Variance - 移動靈敏度)
        # -------------------------------------------------
        rolling_var = data.var(axis=1).rolling(window=20, min_periods=1).mean()
        self.axs[0].plot(frames, rolling_var, color='purple', linewidth=1.5)
        self.axs[0].set_title("1. Global Variance Over Time (Movement Activity Level)", fontsize=10, fontweight='bold')
        self.axs[0].set_ylabel("Variance")
        self.axs[0].grid(True, linestyle='--', alpha=0.5)

        # -------------------------------------------------
        # 圖表 2：2D 動態熱力圖 (Background Subtracted)
        # -------------------------------------------------
        data_matrix = data.values.T
        baseline = np.mean(data_matrix, axis=1, keepdims=True)
        dynamic_data = data_matrix - baseline
        
        im = self.axs[1].imshow(dynamic_data, aspect='auto', cmap='jet', vmin=-15, vmax=15, origin='lower')
        self.axs[1].set_title("2. 2D CSI Spectrogram (Background Subtracted)", fontsize=10, fontweight='bold')
        self.axs[1].set_ylabel("Subcarrier")
        cbar = self.fig.colorbar(im, ax=self.axs[1], fraction=0.046, pad=0.01)
        cbar.set_label('Fluctuation')

        # -------------------------------------------------
        # 圖表 3：前 5 條子載波 (First 5 Subcarriers)
        # -------------------------------------------------
        for col in sub_cols[:5]:
            self.axs[2].plot(frames, data[col], label=col, alpha=0.8)
        self.axs[2].set_title("3. Amplitude of First 5 Subcarriers (Detailed View)", fontsize=10, fontweight='bold')
        self.axs[2].set_ylabel("Amplitude")
        self.axs[2].legend(loc='upper right', fontsize='small')
        self.axs[2].grid(True, linestyle='--', alpha=0.5)

        # -------------------------------------------------
        # 圖表 4：全部 52 條子載波 (All 52 Subcarriers)
        # -------------------------------------------------
        for col in sub_cols:
            self.axs[3].plot(frames, data[col], alpha=0.2) # 使用低透明度避免線條互相掩蓋
        self.axs[3].set_title("4. Amplitude of All 52 Subcarriers (Macroscopic View)", fontsize=10, fontweight='bold')
        self.axs[3].set_ylabel("Amplitude")
        self.axs[3].set_xlabel("Frame (Time)")
        self.axs[3].grid(True, linestyle='--', alpha=0.5)

        # 調整版面與繪製
        self.fig.tight_layout()
        self.canvas.draw()

if __name__ == "__main__":
    root = tk.Tk()
    app = CSIAnalyzerApp(root)
    root.mainloop()
