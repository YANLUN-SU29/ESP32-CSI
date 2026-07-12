#include <WiFi.h>
#include <WiFiClient.h>
#include "esp_wifi.h" 

const char* ssid = "你的WIFI名稱";       // 例如："MERCUSYS_N300_CSI"
const char* password = "你的WIFI密碼";   // 例如："12345678"

WiFiClient client;
IPAddress gateway; // 用來自動儲存路由器的 IP

// CSI 中斷回呼函式
void _wifi_csi_cb(void *ctx, wifi_csi_info_t *data) {
  int8_t *csi_buf = data->buf;
  uint16_t len = data->len;

  Serial.print("CSI_DATA");
  for (int i = 0; i < len; i++) {
    Serial.print(",");
    Serial.print(csi_buf[i]);
  }
  Serial.println();
}

void setup() {
  Serial.begin(115200);
  Serial.println("\n📡 ESP32 終極獨立雷達 (TCP 敲門版) 啟動...");

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n✅ Wi-Fi 連線成功！");

  // 自動取得 N300 路由器的 IP
  gateway = WiFi.gatewayIP(); 
  Serial.print("🎯 鎖定路由器 IP: ");
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
  
  Serial.println("🎯 CSI 擷取啟動，開始進行高頻 TCP 敲門...");
}

void loop() {
  // 對路由器的 Web 後台 (Port 80) 發起連線
  // 這會強迫路由器發射一道專屬微波回傳給 ESP32，完美觸發 CSI！
  client.connect(gateway, 80);
  client.stop(); // 敲完門立刻跑，不佔用資源
  
  delay(30); // 決定你的更新率，30ms 大約等於 30 FPS 的滑順波形
}
