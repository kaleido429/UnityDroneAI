# Python v0.1과 ESP32 BLE 계약

이 문서는 구현된 Python codec 기준이며 이전 PROTOCOL.md의 초안보다 우선한다. 아직 ESP32 펌웨어를 제공하는 것은 아니다.

## 서비스

| 용도 | UUID | GATT 속성 |
|---|---|---|
| Service | 74b70000-5401-4fba-8984-fddff08b0100 | 광고에 포함 |
| Identity | 74b70001-5401-4fba-8984-fddff08b0100 | Read |
| Control | 74b70002-5401-4fba-8984-fddff08b0100 | Write with response |
| Telemetry | 74b70003-5401-4fba-8984-fddff08b0100 | Notify |
| ACK | 74b70004-5401-4fba-8984-fddff08b0100 | Notify 또는 Indicate |

Python은 페어링을 요청한다. 영구 drone_id는 NVS에 저장하고, 매 부팅 boot_id를 갱신한다. 광고 이름·MAC 주소를 영구 식별자 또는 인증으로 취급하지 않는다.

## Identity

Read 값은 **분할 헤더 없는 CBOR map**, 최대 512바이트. 20바이트로 조각낸 특성 읽기가 아니라 GATT long read를 지원해야 한다.

```json
{"protocol_version":1,"drone_id":"blimp-A","boot_id":"boot-uuid","profile_hash":"vehicle-calibration-v1","capabilities":["surge_yaw_rate","expiry_watchdog"]}
```

필드 추가는 가능하다. profile_hash는 서버에 등록된 vehicle.profile_hash와 일치해야 arm할 수 있다.

## Control / Telemetry / ACK 프레임

각 BLE payload는 최대 20바이트. 앞 4바이트는 little-endian uint16 message_id, uint8 fragment_index(0부터), uint8 fragment_count. 나머지는 최대 16바이트의 CBOR 본문. 논리 메시지 최대 1024바이트/64조각, 재조립 유효기간 2초, 채널별 미완성 메시지 최대 4개. 같은 ID는 2초 안에 재사용하지 않는다. 완성된 메시지 중복은 2초 동안 무시한다.

CBOR **문자열 키**를 사용한다(초기 설계의 정수 키 방식에서 변경). bool/int/string/null 사용; 아래 JSON은 CBOR의 의미를 표현한다. 모든 메시지의 protocol_version은 1. 누락/불일치/기한 만료/세션 오류가 있는 명령은 적용하지 않는다. CRC는 BLE 링크 계층에 맡기며 앱 ACK로 적용을 확인한다.

## Control 종류

모든 요청에 Python이 op_id(UUID)를 추가한다.

| type | 추가 필드 | 동작 |
|---|---|---|
| sync | 없음 | 현재 uptime_ms를 ACK로 반환. 제어 권한 변경 없음 |
| arm | control_session, expires_at_uptime_ms | disarmed 상태에서 새 세션 시작. 시작 추력은 중립이며 기한 내 setpoint 없으면 만료 동작 |
| setpoint | control_session, command_seq, expires_at_uptime_ms, surge_milli, yaw_millirad_s | 세션·증가 seq·범위·기한을 검사한 뒤 로컬 제어기에 요청 전달 |
| disarm | 없음 | 현재 세션/요청을 폐기하고 모터 출력 정지 확인 후 ACK |

surge_milli=surge_norm×1000, yaw_millirad_s=yaw_rate_rad_s×1000. 예: surge_milli=150은 전진 추력 요청 0.15. 속도가 아니며, actual PWM은 기체별 믹서에서 결정한다. 정수 반올림은 Python round 결과를 사용한다.

control_session은 난수 UUID. command_seq는 세션 내 단조 증가. ESP32가 마지막 seq를 기억하고 과거/중복 값은 거부한다. 연결 해제·부팅·disarm 때 이전 세션은 무효화한다. 펌웨어는 현재 BLE central의 인증된 세션만 수락한다.

시간은 ESP32의 64-bit 단조 uptime_ms 사용. 32-bit millis wrap을 그대로 사용하지 않는다. sync ACK의 uptime_ms는 ACK 생성 시각이다. Python은 최대 RTT 150ms, 5초마다 필요 시 재동기화, 10초 넘은 기준 거부, RTT+10ms 여유를 빼서 명령 deadline을 만든다. 이 여유가 실제 BLE 지연/클록 드리프트에 충분한지는 장치에서 검증해야 한다. 펌웨어도 지나간 기한과 허용 범위보다 먼 기한을 거부한다. 수신 후 TTL을 다시 시작하면 안 된다.

## ACK

```json
{"protocol_version":1,"op_id":"request-uuid","status":"applied","boot_id":"boot-uuid","uptime_ms":12000,"armed":true,"control_session":"session-uuid","command_seq":12}
```

sync는 uptime_ms가 필수, arm/disarm은 armed가 필수. 요청에 control_session 또는 command_seq가 있으면 ACK에 **동일 값**을 넣는다. 적용 불가 시 status="rejected" 및 reason을 포함한다. 늦은 ACK는 op_id가 없어 무시된다. GATT Write Response는 이 ACK를 대신하지 못한다.

## Telemetry

```json
{"protocol_version":1,"drone_id":"blimp-A","boot_id":"boot-uuid","telemetry_seq":42,"uptime_ms":12345,"armed":false,"battery_session_id":"pack-installation-uuid","pack_voltage_mv":null,"soc_pct":null,"measurement_valid":false,"control_session":null,"last_applied_seq":null}
```

이 필드만 전송한다. 현재 서버는 알 수 없는 telemetry 필드를 거부한다. voltage는 mV 정수 또는 null. soc_pct는 알고 있을 때만 0~100. 배터리 정보가 없으면 measurement_valid=false와 null. 배터리 교체를 확인할 때 battery_session_id 갱신; 비행 중 갑자기 바뀌면 서버는 재확인 대상으로 분류한다. telemetry_seq와 uptime은 부팅 내 증가. boot_id 변경은 기존 연결/제어 무효화로 취급한다.

2Hz부터 실측한다. telemetry 값이 2초 이상 오래되면 서버는 STALE로 표시한다. 기체의 저전압 보호는 이 전송 주기나 Python 경고에 의존하면 안 된다.

## 기체 측 필수 동작

- PID·모터 믹서는 BLE 콜백과 분리해 동작.
- 제어 요청·arm 만료, BLE 연결 단절, 저전압 시 기체별로 검증한 expiry_behavior 적용.
- 재연결/재부팅 때 disarmed이며 예전 setpoint 재생 금지.
- 잘못된 packet/JSON/CBOR, 지원하지 않는 capability를 이유로 PID 작업이 중단되지 않도록 처리.
- Python mock의 neutral_disarm은 통신 시험을 위한 구현일 뿐 비행선 회수/착지 제어기가 아님.
