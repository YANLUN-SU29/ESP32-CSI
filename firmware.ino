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


  int pos = 0;
  pos += sprintf(line_buf + pos, "CSI_DATA");
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
 
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n[OK] Wi-Fi Connected");


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
