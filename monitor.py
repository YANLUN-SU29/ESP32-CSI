import serial
import serial.tools.list_ports
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import csv
import datetime
import os
import sys

# ----------------- [動態標籤輸入] -----------------
VALID_LABELS = ["Empty_Room", "Walking", "Waving"]

if len(sys.argv) > 1:
    ACTION_LABEL = sys.argv[1]
else:
    print("[選擇] 請選擇本次錄製的動作標籤：")
    for i, label in enumerate(VALID_LABELS):
        print(f"  [{i}] {label}")
    print(f"  或直接輸入自訂名稱")
    while True:
        choice = input("\n請輸入編號或名稱: ").strip()
        if choice.isdigit() and 0 <= int(choice) < len(VALID_LABELS):
            ACTION_LABEL = VALID_LABELS[int(choice)]
            break
        elif choice:
            ACTION_LABEL = choice
            break
        print("[錯誤] 輸入不能為空，請重新輸入。")

BAUD_RATE = 921600
SUBCARRIERS = 52
WINDOW_SIZE = 100

print(f"\n[啟動] Wi-Fi 雷達 (CSV 錄製模式：{ACTION_LABEL} / 動態 COM 版)...")

# ----------------- [動態 COM Port 掃描] -----------------
def get_com_port():
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("[錯誤] 找不到任何 COM Port，請檢查 ESP32 是否已接上 USB。")
        exit()
    if len(ports) == 1:
        print(f"[自動] 鎖定唯一設備：{ports[0].device} ({ports[0].description})")
        return ports[0].device
    print("\n[掃描] 偵測到多個 COM Port，請選擇你的 ESP32：")
    for i, port in enumerate(ports):
        print(f"  [{i}] {port.device} - {port.description}")
    while True:
        try:
            choice = input("\n請輸入設備號碼 (例如 0): ").strip()
            idx = int(choice)
            if 0 <= idx < len(ports):
                selected_port = ports[idx].device
                print(f"[選擇] 已選擇：{selected_port}")
                return selected_port
            else:
                print("[錯誤] 號碼超出範圍，請重新輸入。")
        except ValueError:
            print("[錯誤] 請輸入有效的數字號碼。")

SERIAL_PORT = get_com_port()

# ----------------- [CSV 檔案常駐開啟] -----------------
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

filename = os.path.join(DATA_DIR, f"CSI_{ACTION_LABEL}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
csv_file = open(filename, 'w', newline='')
csv_writer = csv.writer(csv_file)
csv_writer.writerow(['Timestamp'] + [f'Sub_{i}' for i in range(SUBCARRIERS)])

print(f"[錄製] CSV 輸出路徑：{filename}")

# ----------------- [初始化矩陣與硬體] -----------------
data_matrix = np.zeros((SUBCARRIERS, WINDOW_SIZE))
record_count = 0
error_count = 0

try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
except Exception as e:
    print(f"[錯誤] 無法連線至 {SERIAL_PORT}: {e}")
    csv_file.close()
    exit()

# ----------------- [建立雙層視覺化畫布] -----------------
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), gridspec_kw={'height_ratios': [1, 1.5]})
fig.tight_layout(pad=4.0)

ax1.set_title(f"52 Subcarriers True Amplitude (Recording: {ACTION_LABEL})")
ax1.set_xlim(0, WINDOW_SIZE)
ax1.set_ylim(0, 100)
ax1.set_ylabel("Amplitude")
ax1.set_facecolor('#111111')
colors = plt.cm.jet(np.linspace(0, 1, SUBCARRIERS))
lines = []
for i in range(SUBCARRIERS):
    line, = ax1.plot(np.zeros(WINDOW_SIZE), color=colors[i], linewidth=1.5, alpha=0.7)
    lines.append(line)

cax = ax2.imshow(data_matrix, aspect='auto', cmap='jet', vmin=-15, vmax=15)
ax2.set_title("2D CSI Spectrogram (Background Subtracted)")
ax2.set_ylabel("Subcarrier (0-51)")
ax2.set_xlabel("Time (Frames)")
fig.colorbar(cax, ax=ax2, label="Amplitude Fluctuation")

# ----------------- [資料更新邏輯] -----------------
def update(frame):
    global data_matrix, record_count, error_count
    updated = False

    while ser.in_waiting > 0:
        try:
            raw_data = ser.readline().decode('utf-8').strip()
            if raw_data.startswith("CSI_DATA"):
                parts = raw_data.split(',')[1:]
                csi_numbers = [int(x) for x in parts if x.strip() != ""]

                amplitudes = []
                for i in range(0, len(csi_numbers)-1, 2):
                    amp = np.sqrt(csi_numbers[i]**2 + csi_numbers[i+1]**2)
                    amplitudes.append(amp)

                OFFSET = 5
                if len(amplitudes) >= (SUBCARRIERS + OFFSET):
                    clean_52_subcarriers = amplitudes[OFFSET : OFFSET+SUBCARRIERS]

                    # CSV 寫入（常駐 file handle，定期 flush）
                    timestamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
                    csv_writer.writerow([timestamp] + clean_52_subcarriers)
                    record_count += 1
                    if record_count % 50 == 0:
                        csv_file.flush()

                    # 環形緩衝區更新（避免 hstack 重新分配記憶體）
                    data_matrix = np.roll(data_matrix, -1, axis=1)
                    data_matrix[:, -1] = clean_52_subcarriers
                    updated = True
        except Exception as e:
            error_count += 1
            if error_count <= 10:
                print(f"[警告] 第 {error_count} 筆解析錯誤: {e}")
            elif error_count == 11:
                print("[警告] 後續錯誤將不再逐筆顯示，結束時統一報告。")

    if updated:
        # 更新上方折線圖
        for i, line in enumerate(lines):
            line.set_ydata(data_matrix[i, :])

        # 動態 Y 軸（根據最近資料自動伸縮）
        current_max = np.max(data_matrix)
        if current_max > 0:
            ax1.set_ylim(0, current_max * 1.2)

        # 更新下方熱力圖（動態去背）
        baseline = np.mean(data_matrix, axis=1, keepdims=True)
        cax.set_array(data_matrix - baseline)

        # 即時計數器顯示於視窗標題
        fig.canvas.manager.set_window_title(
            f"CSI Monitor - {ACTION_LABEL} | 已錄製 {record_count} 筆 | 丟棄 {error_count} 筆"
        )

    return lines + [cax]

ani = animation.FuncAnimation(fig, update, interval=20, blit=False, cache_frame_data=False)

try:
    plt.show()
except KeyboardInterrupt:
    pass
finally:
    csv_file.flush()
    csv_file.close()
    if 'ser' in locals() and ser.is_open:
        ser.close()
    print(f"\n[結束] 觀測結束。")
    print(f"  - 有效錄製：{record_count} 筆")
    print(f"  - 解析丟棄：{error_count} 筆")
    print(f"  - 儲存位置：{filename}")
