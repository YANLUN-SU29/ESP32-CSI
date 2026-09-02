#include <WiFi.h>
#include <WiFiClient.h>
#include "esp_wifi.h"
#include "ping/ping_sock.h"
#include "lwip/ip_addr.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"


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


// ===== CSI 事件佇列 =====
// 原本在 CSI 回呼裡直接 Serial.println。該回呼跑在 Wi-Fi driver task 的 context，
// 阻塞式序列埠寫入（一行約 490 bytes、@921600 baud 需 ~5ms）會卡住該任務，
// 導致 Wi-Fi 驅動不再把後續 CSI 事件送進來——而且這種「事件根本沒進到回呼」的
// 損失在 PC 端完全偵測不到，連計數器都測不出來。
//
// 改成：回呼只負責把資料丟進佇列（非阻塞），由 loop() 排空並輸出。
// 佇列滿代表序列埠追不上，此時明確計數為 queue_full_drop——損失變成可量測的數字。
#define CSI_MAX_LEN   384   // 目前設定下 HT20 為 256 bytes，留些餘裕
#define CSI_QUEUE_LEN 24    // 24 * ~392 bytes ≈ 9.4KB

typedef struct {
  uint32_t seq;       // 單調遞增序號，PC 端可據此偵測傳輸中遺失的行
  uint16_t len;
  int8_t   rssi;
  uint8_t  sig_mode;
  int8_t   buf[CSI_MAX_LEN];
} csi_item_t;

QueueHandle_t csi_queue = NULL;

// 這些計數器由回呼（Wi-Fi task）寫入、loop 讀取，故標為 volatile
volatile uint32_t csi_seq          = 0;  // 進入回呼且通過長度檢查的事件總數
volatile uint32_t queue_full_drop  = 0;  // 因佇列滿而丟棄（序列埠追不上）
volatile uint32_t oversize_drop    = 0;  // 因長度異常而丟棄


// CSI 回呼：只做最小工作量，絕不阻塞
void _wifi_csi_cb(void *ctx, wifi_csi_info_t *data) {
  uint16_t len = data->len;

  // 長度異常直接丟棄，保護晶片不當機
  if (len == 0 || len > CSI_MAX_LEN) {
    oversize_drop++;
    return;
  }

  csi_item_t item;
  item.seq      = ++csi_seq;
  item.len      = len;
  item.rssi     = data->rx_ctrl.rssi;
  item.sig_mode = data->rx_ctrl.sig_mode;
  memcpy(item.buf, data->buf, len);

  // 逾時 0：佇列滿就立刻放棄並計數，絕不在此等待
  if (xQueueSend(csi_queue, &item, 0) != pdTRUE) {
    queue_full_drop++;
  }
}


// 由 loop() 呼叫，把佇列內的 CSI 格式化後輸出
// 行格式：CSI_V3,<seq>,<len>,<rssi>,<sig_mode>,<dropped>,<csi...>
//   seq     = 事件序號，PC 端比對跳號即知傳輸中遺失了幾行
//   dropped = ESP32 端累計丟棄數（佇列滿 + 長度異常），讓 PC 端直接看到丟失率
void drain_csi_queue() {
  static char line_buf[4096];
  csi_item_t item;

  while (xQueueReceive(csi_queue, &item, 0) == pdTRUE) {
    uint32_t dropped = queue_full_drop + oversize_drop;
    int pos = 0;
    pos += sprintf(line_buf + pos, "CSI_V3,%lu,%u,%d,%u,%lu",
                   (unsigned long)item.seq, (unsigned)item.len,
                   (int)item.rssi, (unsigned)item.sig_mode,
                   (unsigned long)dropped);
    for (int i = 0; i < item.len; i++) {
      pos += sprintf(line_buf + pos, ",%d", item.buf[i]);
    }
    Serial.println(line_buf);
  }
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

  // 佇列必須在註冊回呼之前建好，否則第一個事件就會寫到 NULL
  csi_queue = xQueueCreate(CSI_QUEUE_LEN, sizeof(csi_item_t));
  if (csi_queue == NULL) {
    Serial.println("[Error] CSI 佇列建立失敗（記憶體不足）");
    while (true) delay(1000);
  }
  Serial.printf("[Queue] CSI 佇列 %d 筆 x %u bytes = %u bytes\n",
                CSI_QUEUE_LEN, (unsigned)sizeof(csi_item_t),
                (unsigned)(CSI_QUEUE_LEN * sizeof(csi_item_t)));

  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(&_wifi_csi_cb, NULL));

#if USE_ICMP_PING
  start_ping_session();
  Serial.println("[Start] CSI capture active, ICMP ping mode...");
#else
  Serial.println("[Start] CSI capture active, TCP keep-alive mode...");
#endif
}


void loop() {
  // 每輪先盡量排空 CSI 佇列。這是 loop 最重要的工作——排得不夠快佇列就會滿，
  // 進而累加 queue_full_drop。原本的 delay(10) 會讓 loop 有 10ms 完全不排空，
  // 故改用 millis() 排程敲門，不再阻塞。
  drain_csi_queue();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] 連線中斷，重新連線...");
    WiFi.reconnect();
    return;
  }

#if !USE_ICMP_PING
  // TCP 常駐連線：只在斷線時才重新建立
  // 第三個參數是連線逾時 (ms)。預設逾時可長達數秒，期間 loop() 被卡住、
  // 敲門封包停發、CSI 就斷流——實測 201 秒錄製出現 9 次中斷（最長 613ms）。
  // 壓成 500ms 可把單次卡頓上限鎖住。
  if (!client.connected()) {
    if (client.connect(gateway, 80, 500)) {
      // 關閉 TCP 緩衝延遲，封包隨發隨至，減少卡頓
      client.setNoDelay(true);
      tcp_connected = true;
    } else {
      tcp_connected = false;
    }
  }

  // 每 10ms 送一個最小封包保活，觸發路由器回傳單播封包
  static uint32_t last_knock = 0;
  if (millis() - last_knock >= 10) {
    last_knock = millis();
    if (tcp_connected && client.connected()) {
      client.print(" \r\n");
    } else {
      tcp_connected = false;
    }
  }
#endif

  // 讓出 1 tick 給其他任務（含 Wi-Fi 任務），但不長時間阻塞排空工作
  vTaskDelay(1);
}
