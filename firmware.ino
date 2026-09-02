#include <WiFi.h>
#include <WiFiClient.h>
#include "esp_wifi.h"
#include "ping/ping_sock.h"
#include "lwip/ip_addr.h"


const char* ssid = "WIFI CSI";
const char* password = "Louis229";


// ===== 觸發方式切換 =====
// 1 = ICMP ping：路由器對 echo request 是即時回應，不受 TCP delayed ACK 影響
// 0 = 原本的 TCP 敲門
//
// 改用 ping 的原因：實測 TCP 敲門雖每 10ms 送一次（預期 ~100 CSI/s），
// 實際只拿到 59/s，且封包成群到達、長間隔穩定在 47ms（變異係數僅 0.13），
// 指向路由器的 TCP delayed ACK 計時器。ICMP echo 不走該路徑。
// 註：ICMP 路徑尚未實機驗證，故預設維持已驗證的 TCP 模式。
//     要試 ping 請改為 1，並比對 monitor.py 結束統計的平均 fps。
#define USE_ICMP_PING 0


WiFiClient client;
IPAddress gateway; // 用來自動儲存路由器的 IP
bool tcp_connected = false; // TCP 連線狀態旗標
esp_ping_handle_t ping_handle = NULL;


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


// ===== ICMP ping 觸發 =====
// 回呼留空即可：我們不在意 ping 的統計結果，只要它持續產生單播往返，
// 讓路由器的 echo reply 觸發 CSI 回呼。
static void on_ping_success(esp_ping_handle_t hdl, void *args) {}
static void on_ping_timeout(esp_ping_handle_t hdl, void *args) {}
static void on_ping_end(esp_ping_handle_t hdl, void *args) {}

void start_ping_session() {
  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();

  ip_addr_t target;
  IP_ADDR4(&target, gateway[0], gateway[1], gateway[2], gateway[3]);
  cfg.target_addr = target;
  cfg.count = ESP_PING_COUNT_INFINITE; // 不停止
  cfg.interval_ms = 10;                // 與原本 TCP 敲門的 delay(10) 對齊，便於比較
  cfg.timeout_ms = 200;
  cfg.data_size = 32;                  // 小封包即可，目的只是觸發往返

  esp_ping_callbacks_t cbs;
  cbs.cb_args = NULL;
  cbs.on_ping_success = on_ping_success;
  cbs.on_ping_timeout = on_ping_timeout;
  cbs.on_ping_end = on_ping_end;

  if (esp_ping_new_session(&cfg, &cbs, &ping_handle) == ESP_OK) {
    esp_ping_start(ping_handle);
    Serial.printf("[Trigger] ICMP ping -> %s, interval %ums\n",
                  gateway.toString().c_str(), cfg.interval_ms);
  } else {
    Serial.println("[Error] 無法建立 ping session，請改用 TCP 模式 (USE_ICMP_PING 0)");
  }
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
 
  // stbc_htltf2_en 關閉：實測 STBC-HT-LTF2 與 HT-LTF 的逐條時間序列相關係數
  // 中位數達 0.902，屬冗餘資料，PC 端本來就已捨棄。關掉可讓 len 由 384 降到 256 bytes，
  // 每行序列埠傳輸時間約由 8ms 降到 5.3ms，使用率由 ~40% 降到 ~27%，
  // 減少序列埠壅塞導致漏掉 CSI 事件的風險。
  // （PC 端無需改動：擷取索引最大為 127，len=256 剛好提供 128 條振幅。）
  wifi_csi_config_t csi_config = {
      .lltf_en           = true,
      .htltf_en          = true,
      .stbc_htltf2_en    = false,
      .ltf_merge_en      = true,
      .channel_filter_en = true,
      .manu_scale        = false,
      .shift             = false,
  };
  ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(&_wifi_csi_cb, NULL));

#if USE_ICMP_PING
  start_ping_session();
  Serial.println("[Start] CSI capture active, ICMP ping mode...");
#else
  Serial.println("[Start] CSI capture active, TCP keep-alive mode...");
#endif
}


void loop() {
#if USE_ICMP_PING
  // ping 由 esp_ping 的背景任務持續發送，loop 只需監看 Wi-Fi 是否掉線
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] 連線中斷，重新連線...");
    WiFi.reconnect();
    delay(500);
  }
  delay(200);

#else
  // TCP 常駐連線：只在斷線時才重新建立
  // 第三個參數是連線逾時 (ms)。預設逾時可長達數秒，期間 loop() 被卡住、
  // 敲門封包停發、CSI 就斷流——實測 201 秒錄製出現 9 次中斷（最長 613ms）。
  // 壓成 500ms 可把單次卡頓上限鎖住。
  if (!client.connected()) {
    if (client.connect(gateway, 80, 500)) {
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
#endif
}
