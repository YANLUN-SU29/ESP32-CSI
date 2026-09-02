#include <WiFi.h>
#include <WiFiClient.h>
#include "esp_wifi.h"


const char* ssid = "WIFI CSI";
const char* password = "Louis229";


WiFiClient client;
IPAddress gateway; // 用來自動儲存路由器的 IP
bool tcp_connected = false; // TCP 連線狀態旗標


// CSI 中斷回呼函式 (sprintf 整行輸出版)
void _wifi_csi_cb(void *ctx, wifi_csi_info_t *data) {
  int8_t *csi_buf = data->buf;
  uint16_t len = data->len;


  // 【修改 1：加大紙箱】把原本的 1024 加大到 4096，避免被路由器的大封包塞爆
  static char line_buf[4096];

  // 【修改 2：安全氣囊】如果封包長度異常大，直接丟棄保護晶片不當機
  if (len > 512) return;


  // 【修改 3】輸出中繼資料：
  //   len      = 原始 CSI 位元組數，PC 端才能確認子載波佈局、並剔除長度不一致的封包
  //              （長度若在 128/256/384 之間跳動，同一個陣列索引在不同幀會代表不同的實體子載波）
  //   rssi     = 訊號強度，用於診斷連線品質
  //   sig_mode = 0:非HT(11b/g) 1:HT(11n)，決定 CSI 內容是 LLTF 還是含 HT-LTF
  //   行首改用 CSI_V2 以便 PC 端明確區分新舊格式
  int pos = 0;
  pos += sprintf(line_buf + pos, "CSI_V2,%u,%d,%u",
                 (unsigned)len, (int)data->rx_ctrl.rssi, (unsigned)data->rx_ctrl.sig_mode);
  for (int i = 0; i < len; i++) {
    pos += sprintf(line_buf + pos, ",%d", csi_buf[i]);
  }


  Serial.println(line_buf);
}


void setup() {
  Serial.begin(921600); // 高速鮑率，大幅提升傳輸吞吐量
  Serial.println("\n[ESP32] CSI Radar (TCP Keep-Alive / 921600 baud)");


  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  // 【關閉 Wi-Fi 省電】STA 模式預設會開啟 modem sleep，在信標間隔之間關掉射頻，
  // 封包被路由器緩衝後才一次灌下來，造成「一陣密集 + 一段靜默」的不均勻取樣。
  // 實測舊韌體的樣本間隔呈雙峰分布（約 8.5ms 與 50ms，中間完全空白），
  // 而 Welch PSD 與 Butterworth 濾波都假設均勻取樣，故必須關閉。
  WiFi.setSleep(false);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n[OK] Wi-Fi Connected");
  Serial.println("[Power] Wi-Fi modem sleep disabled (WIFI_PS_NONE)");


  // 自動取得 N300 路由器的 IP
  gateway = WiFi.gatewayIP();
  Serial.print("[Target] Gateway IP: ");
  Serial.println(gateway);


  // 啟動 CSI 底層引擎
  ESP_ERROR_CHECK(esp_wifi_set_csi(1));
 
  wifi_csi_config_t csi_config = {
      .lltf_en           = true,
      .htltf_en          = true,
      .stbc_htltf2_en    = true,
      .ltf_merge_en      = true,
      .channel_filter_en = true,
      .manu_scale        = false,
      .shift             = false,
  };
  ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(&_wifi_csi_cb, NULL));
 
  Serial.println("[Start] CSI capture active, TCP keep-alive mode...");
}


void loop() {
  // TCP 常駐連線：只在斷線時才重新建立
  if (!client.connected()) {
    if (client.connect(gateway, 80)) {
      // 【新增這行神功】：關閉 TCP 緩衝延遲，封包隨發隨至，減少卡頓！
      client.setNoDelay(true);
      tcp_connected = true;
    } else {
      tcp_connected = false;
    }
  }


  // 送一個最小封包保活，觸發路由器回傳單播封包
  if (tcp_connected && client.connected()) {
    // 稍微改一下，送一個空白鍵加換行，稍微騙一下路由器這是一個正常的文字
    client.print(" \r\n");
  } else {
    // 連線失敗，重置旗標，下一輪重連
    tcp_connected = false;
  }
 
  delay(10);
}
