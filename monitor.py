import serial
import serial.tools.list_ports
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import csv
import datetime

ACTION_LABEL = "Empty_Room"
BAUD_RATE = 115200 
SUBCARRIERS = 52
WINDOW_SIZE = 100  

print(f"[啟動] Wi-Fi 雷達 (CSV 錄製模式：{ACTION_LABEL} / 動態 COM 版)...")

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

filename = f"CSI_{ACTION_LABEL}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
with open(filename, 'w', newline='') as f:
    writer = csv.writer(f)
    header = ['Timestamp'] + [f'Sub_{i}' for i in range(SUBCARRIERS)]
    writer.writerow(header)

data_matrix = np.zeros((SUBCARRIERS, WINDOW_SIZE))
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
except Exception as e:
    print(f"[錯誤] 無法連線至 {SERIAL_PORT}: {e}")
    exit()

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

def update(frame):
    global data_matrix
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
                    
                    with open(filename, 'a', newline='') as f:
                        writer = csv.writer(f)
                        timestamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        writer.writerow([timestamp] + clean_52_subcarriers)
                    
                    new_col = np.array(clean_52_subcarriers).reshape(SUBCARRIERS, 1)
                    data_matrix = np.hstack((data_matrix[:, 1:], new_col))
                    updated = True
        except:
            pass
            
    if updated:
        for i, line in enumerate(lines):
            line.set_ydata(data_matrix[i, :])
        current_max = np.max(data_matrix)
        if current_max > ax1.get_ylim()[1]:
            ax1.set_ylim(0, current_max * 1.2)
        baseline = np.mean(data_matrix, axis=1, keepdims=True)
        cax.set_array(data_matrix - baseline)
    return lines + [cax]

ani = animation.FuncAnimation(fig, update, interval=20, blit=False, cache_frame_data=False) 
try:
    plt.show()
except KeyboardInterrupt:
    pass
finally:
    if 'ser' in locals() and ser.is_open:
        ser.close()
    print(f"\n[結束] 觀測結束。資料已儲存至：{filename}")
