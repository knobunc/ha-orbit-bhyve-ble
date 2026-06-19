# Orbit B-Hyve Gen 2 Smart Hose Timer — BLE Protocol Specification

Reverse-engineered from decompilation of the Orbit B-Hyve Android APK (v3.0.53)
and the `ljmerza/orbit-bhyve-ble` Home Assistant integration. Verified against
captured BLE frames from HT25-0000 (fw0041, fw0085) and HT34A-0001 (fw0107).

---

## 1. Architecture Overview

```
                                    ┌───────────────────────┐
                                    │  Orbit Cloud          │
                                    │  api.orbitbhyve.com   │
                                    └──────┬────────────────┘
                                           │ HTTPS/WSS (protobuf)
                                    ┌──────┴────────────────┐
                                    │  BH1 Wi-Fi Hub        │
                                    │  (ESP32 + nRF BLE)    │
                                    └──────┬────────────────┘
                                           │ BLE (proprietary mesh)
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
   ┌──────────┴──────────┐    ┌───────────┴──────────┐    ┌───────────┴──────────┐
   │ HT25 Hose Timer     │    │ HT34A XD Timer       │    │ Flood Sensor         │
   │ (1 station)         │    │ (4 stations)          │    │ (FS1)                │
   └─────────────────────┘    └──────────────────────┘    └──────────────────────┘
```

For **local BLE control** (no hub required), a BLE client (phone, Raspberry Pi,
Home Assistant) connects directly to the timer. The cloud is contacted **once**
at setup to retrieve the AES-128 network encryption key. After that, all
communication is purely local BLE.

---

## 2. Obtaining the Encryption Key (Cloud API — One-Time)

### 2.1 Authentication

```
POST https://api.orbitbhyve.com/v1/session
Headers:
  orbit-app-id: Bhyve-App
  Content-Type: application/json

Body:
  {"session": {"email": "<email>", "password": "<password>"}}

Response:
  {"orbit_api_key": "<token>", "user_id": "<uid>", ...}
```

Use `orbit_api_key` in all subsequent requests as header `orbit-api-key`.

### 2.2 List Devices

```
GET https://api.orbitbhyve.com/v1/devices
Headers:
  orbit-app-id: Bhyve-App
  orbit-api-key: <token>
```

Returns a JSON array. Each device has:

| Field               | Type   | Description                                    |
|---------------------|--------|------------------------------------------------|
| `id`                | string | Cloud device ID                                |
| `name`              | string | User-assigned name                             |
| `type`              | string | `"sprinkler_timer"`, `"bridge"`, `"flood_sensor"` |
| `mac_address`       | string | 12 hex chars, no separators (e.g. `"44675522dc60"`) |
| `hardware_version`  | string | e.g. `"HT25-0000"`, `"HT34A-0001"`            |
| `firmware_version`  | string | e.g. `"85"`, `"41"`, `"107"`                   |
| `num_stations`      | int    | Number of valve stations (1 for hose timer)    |
| `mesh_id`           | string | MongoDB ObjectId linking devices in a BLE mesh |
| `reference`         | string | `"<bridge_mac>-<device_serial>"` for BLE devices |
| `battery`           | object | `{"percent": N, "mv": N}`                     |

### 2.3 Fetch Mesh / Network Key

Try these paths in order (stop at the first 200 response):

```
GET https://api.orbitbhyve.com/v1/meshes/<mesh_id>
GET https://api.orbitbhyve.com/v1/network_topologies/<mesh_id>
GET https://api.orbitbhyve.com/v1/networks/<mesh_id>
```

The response contains the key in one of these fields:
- `ble_network_key` — base64-encoded 16 bytes
- `network_key` — base64-encoded 16 bytes

Decode from base64 to get 16 raw bytes (32 hex chars). This is the **AES-128
key** used for all BLE communication with devices on this mesh.

The response also contains:
- `devices[]` — array with `device_id` and `ble_device_id` (the mesh address)
- `bridge_device_id` — the cloud ID of the hub in this mesh

### 2.4 Deriving mesh_device_id

Each device on the mesh has a `mesh_device_id` (uint16, used as the 2-byte
frame address prefix). Obtain it from:
1. The mesh response: `devices[].ble_device_id` matched by `devices[].device_id`
2. Fallback: parse the device `reference` field: split on `-`, take the second
   part as an integer

---

## 3. BLE GATT Service & Characteristics

### Service UUID: `0000fe32-0000-1000-8000-00805f9b34fb`

| Characteristic | UUID                                     | Properties        | Purpose                |
|---------------|------------------------------------------|-------------------|------------------------|
| AES_CHAR      | `00006c71-fe32-4f58-8b78-98e42b2c047f`   | Read, Write       | AES handshake exchange |
| WRITE_CHAR    | `00006c72-fe32-4f58-8b78-98e42b2c047f`   | Write (with resp) | Send encrypted commands |
| READ_CHAR     | `00006c73-fe32-4f58-8b78-98e42b2c047f`   | Notify            | Receive encrypted responses |
| NETWORK_CHAR  | `00006c76-fe32-4f58-8b78-98e42b2c047f`   | Write (locked)    | Unused; firmware-locked |

**Important**: `WRITE_CHAR` requires `WRITE_REQ` (write-with-response). Using
`WRITE_CMD` (write-without-response) is silently dropped by the device.

---

## 4. AES Handshake (Per-Connection)

Performed on every new BLE connection before any command can be sent.

### 4.1 Procedure

1. **Subscribe** to notifications on `READ_CHAR` first (device may stay silent otherwise)
2. **Generate** 20 random bytes (`init_tx`), force byte 11 to `0x00`
3. **Write** `init_tx` to `AES_CHAR`
4. **Read** 20 bytes from `AES_CHAR` → `init_rx`
5. **Validate**: `init_rx[0:4]` must be non-zero, `init_rx[4:20]` must be all zeros

### 4.2 Derive Cipher State

```
combined = init_rx[0:4] || init_tx[4:20]   (20 bytes)

IV          = combined[0:12]                (12 bytes)
TX counter  = uint32_LE(combined[12:16])    (initial transmit counter)
RX counter  = uint32_LE(combined[16:20])    (initial receive counter)
```

The IV and counters are used for the CTR-mode cipher for the lifetime of
this BLE connection.

---

## 5. Encryption — AES-128-CTR (Custom Implementation)

### 5.1 Keystream Generation

Uses **AES-128-ECB** as a CTR-mode keystream generator:

```
For each 16-byte block:
  input_block = IV(12 bytes) || counter_LE(4 bytes)
  keystream_block = AES-128-ECB-Encrypt(network_key, input_block)
  counter += 1
```

### 5.2 Encrypt (Plaintext → Frame)

```python
# XOR plaintext with keystream
ciphertext = plaintext XOR keystream[:len(plaintext)]

# Compute trailer
trailer = (sum(plaintext_bytes) + trailer_const + len(plaintext)) & 0xFFFF

# Assemble frame
frame = [frame_magic] [len(ciphertext)] [ciphertext] [trailer_LE (2 bytes)]
```

TX counter advances by `ceil(len(plaintext) / 16)` blocks.

### 5.3 Decrypt (Frame → Plaintext)

```python
# Parse frame
assert frame[0] == frame_magic
ct_len = frame[1]
ciphertext = frame[2 : 2 + ct_len]

# XOR ciphertext with keystream using RX counter
plaintext = ciphertext XOR keystream[:len(ciphertext)]
```

RX counter advances by `ceil(len(ciphertext) / 16)` blocks.

### 5.4 Per-Model Constants

| Model       | `frame_magic` | `trailer_const` |
|-------------|---------------|-----------------|
| HT25-0000   | `0x10`        | `0x10`           |
| HT34A-0001  | `0x11`        | `0x11`           |

---

## 6. HT25 Inner Protocol (Single-Station Hose Timer)

The HT25 uses a proprietary binary protocol inside the encrypted frames.
This is the "d7-47 protocol family" (named after the mesh address of the
first device analyzed).

### 6.1 Frame Structure

```
[mesh_addr_LE (2B)] [type (1B)] [seq (1B)] [routing (1B)] [payload (N bytes)]
```

| Field      | Size  | Description                                        |
|------------|-------|----------------------------------------------------|
| mesh_addr  | 2     | Device's own `mesh_device_id`, little-endian        |
| type       | 1     | Command type (see below)                            |
| seq        | 1     | Sequence/command ID                                 |
| routing    | 1     | Always `0x40`                                       |
| payload    | 0-N   | Command-specific data                               |

### 6.2 Sequence IDs

| Seq    | Hex    | Name            | Description                    |
|--------|--------|-----------------|--------------------------------|
| 0      | `0x00` | MAGIC_CHECK     | Mesh identity announcement     |
| 1      | `0x01` | SUBSYSTEM       | Subsystem init query           |
| 2      | `0x02` | STATUS          | Status request                 |
| 3      | `0x03` | INFO            | Device info request            |
| 5      | `0x05` | BIND            | Bind/session init              |
| 9      | `0x09` | HEARTBEAT       | Keep-alive                     |
| 13     | `0x0D` | WATER_CTRL      | Watering start/stop            |

### 6.3 Post-Handshake Init Sequence (Required)

After the AES handshake, an 8-step init sequence must be sent before the
device will accept watering commands. Sending a watering command without
this sequence results in a silent drop.

Each step is sent as a WRITE_REQ with ~150ms inter-step delay:

```
Step 1 — BIND:        [mesh_addr] [0x81] [0x05] [0x40] [sid(2B)] [f6 69 10 ff]
Step 2 — STATUS:      [mesh_addr] [0x02] [0x02] [0x40] [00]
Step 3 — INFO:        [mesh_addr] [0x03] [0x03] [0x40] [00 00 00 00 00 00 00]
Step 4 — SUBSYSTEM:   [mesh_addr] [0x04] [0x01] [0x40] [00 00 00]
Step 5 — MAGIC1:      [mesh_addr] [0x85] [0x00] [0x40] [01] [self_mesh_LE(2B)] [00 00 00 00]
Step 6 — MAGIC2:      [mesh_addr] [0x85] [0x00] [0x40] [00] [hub_mesh_LE(2B)]  [00 00 00 00]
Step 7 — HEARTBEAT:   [mesh_addr] [0x85] [0x09] [0x40] [00]
Step 8 — REBIND:      [mesh_addr] [0x86] [0x05] [0x40] [sid2(2B)] [f6 69 10 ff]
```

Where:
- `sid` = 2 random bytes (session ID)
- `sid2` = `(sid + 2) & 0xFFFF` (fw0041) or `(sid + 3) & 0xFFFF` (fw0085)
- `BIND_TAIL` = `f6 69 10 ff`
- `self_mesh_LE` = this device's `mesh_device_id` as little-endian uint16
- `hub_mesh_LE` = the hub's `mesh_device_id` as little-endian uint16

Wait ~300ms after the last step before sending commands.

### 6.4 Start Watering

```
[mesh_addr] [0xB6] [0x0D] [0x40] [04] [duration_sec_LE(2B)] [00 00 00 00]
```

- `0xB6` = start watering type byte
- `0x0D` = SEQ_WATER_CTRL
- Duration: 1–65535 seconds, little-endian uint16

### 6.5 Stop Watering

```
[mesh_addr] [0xB7] [0x0D] [0x40] [02 00 00 00]
```

- `0xB7` = stop watering type byte

### 6.6 Info Response Parsing (Battery)

The device-info response (reply to Step 3) contains battery voltage:

```
Response frame layout: [mesh(2B)] [type(1B)] [seq=0x03] [routing=0x40] [payload(7B)]

Type byte has bit 6 set (0x40) indicating a reply.
Payload bytes 4-5: battery voltage in mV, little-endian uint16

Battery % = clamp((mV - 2400) * 100 / 600, 0, 100)
  0% at 2400 mV, 100% at 3000 mV
```

---

## 7. HT34A Inner Protocol (4-Port XD Timer)

The HT34A uses protobuf-encoded messages inside the encrypted frames,
matching the `OrbitPbApi_Message` schema from the APK.

### 7.1 Frame Structure

```
[AA 77 5A 0F] [payload_len(1B)] [00] [protobuf_data] [CRC16_LE(2B)]
```

- Header: `AA 77 5A 0F` (constant)
- Payload length: `len(protobuf_data) + 2`
- CRC16-CCITT over entire message (header + length + 0x00 + protobuf), init=0

### 7.2 Start Watering (Protobuf)

```protobuf
// OrbitPbApi_Message.timerMode (field 14)
message TimerMode {
  Mode mode = 1;           // 2 = manualMode
  ManualModeParams manual_mode_params = 2;
}

message ManualModeParams {
  repeated StationInfo station_info = 3;
}

message StationInfo {
  uint32 station_id = 1;   // 0-indexed on the wire
  uint32 run_time_sec = 2; // duration in seconds
}
```

Wire bytes for station 0, 120 seconds:
```
72 04 08 02 12 00   →   field 14, varint mode=2, field 2 = ManualModeParams
                        containing field 3 = StationInfo(station_id=0, run_time_sec=120)
```

### 7.3 Stop Watering

```
Fixed bytes: 72 04 08 02 12 00
```

Wraps: `TimerMode { mode: manualMode, manual_mode_params: {} }` (empty
stations list = stop all).

---

## 8. Complete Protobuf Schema (OrbitPbApi_Message)

The root message `OrbitPbApi_Message` uses a oneof with 100+ message types.
Key fields for the hose timer:

| Field ID | Name                 | Type                              | Purpose                          |
|----------|----------------------|-----------------------------------|----------------------------------|
| 1        | id                   | bytes                             | Message ID                       |
| 2        | timestampIso8601     | string                            | ISO-8601 timestamp               |
| 7        | timestampSecEpochUTC | uint32                            | Unix timestamp                   |
| 9        | keepAlive            | KeepAlive                         | Connection keep-alive            |
| 10       | syncRequest          | SyncRequest                       | Request full state sync          |
| 11       | closeConnection      | CloseConnection                   | Graceful disconnect              |
| 14       | timerMode            | TimerMode                         | Set mode (off/auto/manual)       |
| 15       | getDeviceStatusInfo  | GetDeviceStatusInfo               | Request status                   |
| 16       | deviceStatusInfo     | DeviceStatusInfo                  | Status response                  |
| 17       | setRainDelay         | SetRainDelay                      | Set rain delay (minutes)         |
| 19       | setProgramSchedule   | SetProgramSchedule                | Set watering schedule            |
| 22       | getDeviceInfo        | GetDeviceInfo                     | Request device info              |
| 23       | deviceInfo           | DeviceInfo                        | Device info response             |
| 28       | getSettings          | GetSettings                       | Request settings                 |
| 29       | setSettings          | SetSettings                       | Update settings                  |
| 30       | wateringStatus       | WateringStatus                    | Watering status notification     |
| 45       | getBatteryStatus     | GetBatteryStatus                  | Request battery status           |
| 46       | batteryStatus        | BatteryStatus                     | Battery status response          |
| 47       | identifyDevice       | IdentifyDevice                    | Flash LED for identification     |
| 54       | setNetworkEncKey     | SetNetworkEncKey                  | Update encryption key            |
| 75       | setEpochTime         | SetEpochTime                      | Set device clock                 |
| 100      | ack                  | Ack                               | Command acknowledgment           |

### Key Enumerations

**TimerMode.Mode**: `offMode=0`, `autoMode=1`, `manualMode=2`

**WateringStatus.Status**: `wateringComplete=1`, `wateringInProgress=2`,
`pumpDelay=3`, `stationComplete=4`, `stationDelay=5`, `programPreDelay=6`,
`programPostDelay=7`

**DeviceInfo.DeviceType**: `hosetapTimer=0`, `undergroundTimer=1`,
`rainSensor=2`, `moistureSensor=3`

**BleDeviceId** (hardware model identifiers):
```
id_ht25    = 6    (HT25 single-station hose timer)
id_ht31    = 21   (HT31)
id_ht32    = 22   (HT32)
id_ht34    = 23   (HT34 4-port XD timer)
id_ht25G2  = 24   (HT25 Gen 2)
id_ht31A   = 38   (HT31A)
id_ht32A   = 39   (HT32A)
id_ht34A   = 40   (HT34A)
id_ht25A   = 41   (HT25A)
```

The full 311-type protobuf schema is in `protobuf_schema.json`.

---

## 9. BLE Connection Flow — Complete Sequence

```
1. CLOUD (one-time):
   POST /v1/session          → orbit_api_key
   GET  /v1/devices          → mac_address, mesh_id, hardware_version, firmware_version
   GET  /v1/meshes/{mesh_id} → network_key (base64 → 16 bytes)

2. BLE SCAN:
   Scan for service UUID 0000fe32-0000-1000-8000-00805f9b34fb
   Match by mac_address

3. BLE CONNECT:
   Connect to device (BLE GATT)

4. SUBSCRIBE:
   Start notifications on READ_CHAR (0x6c73)

5. AES HANDSHAKE:
   Generate init_tx (20 random bytes, byte[11] = 0x00)
   Write init_tx to AES_CHAR (0x6c71)
   Read init_rx from AES_CHAR (0x6c71)
   Validate: init_rx[0:4] != 0x00000000 AND init_rx[4:20] == all zeros
   Derive: IV = init_rx[0:4] || init_tx[4:12]
           TX counter = uint32_LE(init_tx[12:16])
           RX counter = uint32_LE(init_tx[16:20])

6. POST-HANDSHAKE INIT (HT25):
   Send 8-step sequence: bind, status, info, subsystem, magic1, magic2, heartbeat, rebind
   Wait 300ms

7. COMMAND:
   Encrypt plaintext → frame
   Write frame to WRITE_CHAR (0x6c72) with response=True
   Wait ~1500ms for notification responses on READ_CHAR

8. IDLE DISCONNECT:
   Disconnect after 60s of inactivity to conserve battery
```

---

## 10. Device Models & Compatibility

| Model      | Hardware     | Stations | Protocol Family | `frame_magic` | Status      |
|------------|-------------|----------|-----------------|---------------|-------------|
| HT25       | HT25-0000   | 1        | d7-47 binary    | `0x10`        | Verified    |
| HT25 Gen 2 | HT25-????  | 1        | d7-47 binary    | `0x10`        | Likely same |
| HT34A XD   | HT34A-0001  | 4        | Protobuf        | `0x11`        | Verified    |
| HT31/32    | HT31/32-*   | 2        | Unknown         | Unknown       | Untested    |

The Gen 2 Smart Hose Timer (your device) is an HT25-series device. The BLE
GATT service and encryption are identical across all models; only the inner
plaintext protocol and magic byte differ.

---

## 11. Security Considerations

- The AES-128 network key is shared across all devices in a mesh
- The key is generated server-side by Orbit and never changes unless explicitly rotated
- BLE communication range is ~10-30m (typical BLE range)
- The AES handshake provides per-session uniqueness (fresh IV and counters)
- The cloud API password is stored in plaintext in the HA config entry (standard HA pattern)
- After initial key retrieval, no cloud connectivity is required

---

## Appendix A: Protobuf .proto Reconstruction (Key Messages)

```protobuf
syntax = "proto2";
package orbit;

enum TimerModeEnum {
  offMode = 0;
  autoMode = 1;
  manualMode = 2;
}

message StationInfo {
  optional uint32 station_id = 1;
  optional uint32 mesh_device_id = 2;
}

message ManualModeParams {
  optional string start_time_iso8601 = 1;
  optional uint32 active_program_flags = 2;
  repeated StationInfo station_info = 3;
  optional uint32 start_time_sec_epoch_utc = 4;
  optional uint32 group_watering_pre_delay_sec = 5;
  optional uint32 group_watering_post_delay_sec = 6;
}

message TimerMode {
  required TimerModeEnum mode = 1;
  optional ManualModeParams manual_mode_params = 2;
}

message SetNetworkEncKey {
  required bytes network_enc_key = 1;
}

message BleInitMsg {
  optional bytes bd_address = 1;
  optional bytes network_key = 2;
  optional uint32 advert_type = 3;
  optional uint32 device_sn = 4;
  optional bool network_provisioned = 5;
}

message BleEventData_DataBlock {
  optional uint32 msg_type = 1;
  optional bool encrypt = 2;
  optional bytes data = 3;
}

message BleBridgedDevices {
  optional bytes network_encryption_key = 1;
  repeated BleBridgedDeviceEntry ble_bridged_dev_entry = 2;
}

message Message {
  optional bytes id = 1;
  optional string timestamp_iso8601 = 2;
  optional uint32 timestamp_sec_epoch_utc = 7;
  optional uint32 message_id = 95;
  optional uint32 ack_message_id = 96;

  oneof message {
    KeepAlive keep_alive = 9;
    SyncRequest sync_request = 10;
    CloseConnection close_connection = 11;
    TimerMode timer_mode = 14;
    GetDeviceStatusInfo get_device_status_info = 15;
    DeviceStatusInfo device_status_info = 16;
    SetRainDelay set_rain_delay = 17;
    SetProgramSchedule set_program_schedule = 19;
    GetDeviceInfo get_device_info = 22;
    DeviceInfo device_info = 23;
    GetSettings get_settings = 28;
    SetSettings set_settings = 29;
    WateringStatus watering_status = 30;
    GetBatteryStatus get_battery_status = 45;
    BatteryStatus battery_status = 46;
    IdentifyDevice identify_device = 47;
    SetNetworkEncKey set_network_enc_key = 54;
    SetEpochTime set_epoch_time = 75;
    Ack ack = 100;
    // ... 100+ more message types (see protobuf_schema.json)
  }
}
```

---

## Appendix B: References

- **APK**: `com.orbit.orbitsmarthome` v3.0.53 (React Native + ClojureScript + Hermes bytecode)
- **Existing integration**: https://github.com/ljmerza/orbit-bhyve-ble
- **Cloud API integration**: https://github.com/sebr/bhyve-home-assistant
- **Cloud API library**: https://github.com/sebr/pybhyve
- **Node.js API**: https://github.com/billchurch/bhyve-api
- **FCC filing**: https://fccid.io/ML6-HT34BT (HT34 XD Timer)
- **HA Community thread**: https://community.home-assistant.io/t/integration-with-orbit-b-hyve-irrigation-system/39688
- **Full protobuf schema**: `protobuf_schema.json` (311 message types, extracted from APK)
