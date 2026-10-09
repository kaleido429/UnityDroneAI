# Blimp Bridge 메시지 계약 초안

원래 설계 초안. Python v0.1에서 구현한 정확한 필드·UUID·ACK는 [FIRMWARE_CONTRACT.md](FIRMWARE_CONTRACT.md)를 따른다. 실기체 펌웨어에 아직 적용되지 않음. BLE와 Classic SPP는 서로 다른 transport이며 여기서는 BLE를 가정한다.

## GATT

프로젝트 전용 128-bit service UUID와 아래 characteristic UUID를 구현 시 생성·고정하고 Unity/Python/펌웨어 공통 명세로 버전 관리한다. 검색 표시 이름은 식별·인증 수단이 아니다.

| Characteristic | 방향 / 속성 | 내용 |
|---|---|---|
| Identity | ESP32→Python / Read | protocol_version, drone_id, boot_id, firmware_version, capabilities, profile_hash |
| Control | Python→ESP32 / Write with response | hello/session 준비, arm/disarm, setpoint, session 폐기 |
| Telemetry | ESP32→Python / Notify | 배터리, 연결 상태, armed, last_applied_seq, 센서 상태 |
| Acknowledgement | ESP32→Python / Indicate 또는 Notify+앱 확인 | op_id, control_session, command_seq, accepted/applied/rejected 및 이유 |

첫 연결은 disarmed. 읽은 Identity를 등록 정보와 대조한 뒤 handshake를 수행한다. PID 프로파일 변경은 disarmed 상태의 별도 운영 절차로 제한한다. 페어링/인증된 연결을 사용하고 암호·키를 discovery advertisement에 넣지 않는다.

## BLE 프레이밍

Unity↔Python은 JSON, BLE는 같은 의미의 정수 키 CBOR를 사용한다. 배터리 mV/mA, 시간 ms 등 단위를 명시한 고정 스케일 정수를 사용하고 부동소수 NaN/Infinity는 허용하지 않는다. Python/ESP32 공통 codec 테스트 벡터를 구현 단계에 추가한다.

MTU가 커질 것으로 가정하지 않는다. 기본 20바이트 attribute payload에서도 동작하도록 각 fragment에 `message_id:uint16`, `fragment_index:uint8`, `fragment_count:uint8` 헤더를 두고 최대 16바이트 본문을 담는다. 다중 바이트 정수는 little-endian. 협상된 한도가 커도 초기 버전은 20바이트 프레임으로 통일한다. MTU 최적화는 별도 벤치마크 후 추가한다.

논리 메시지는 최대 1024바이트/64 fragments. 연결·characteristic별 재조립, 총 크기/인덱스 검증, 짧은 재조립 timeout, 동일 message_id 중복 내용 불일치 시 폐기. 연결 종료 시 버퍼 폐기. 수신이 완성되고 CBOR schema·세션·만료 검증을 통과하기 전에는 절대로 명령을 적용하지 않는다. message_id는 미완성/최근 중복 감지 창 내에서 재사용하지 않는다. ACK로 메시지 적용 상태를 식별하므로 GATT write 응답만으로 성공 처리하지 않는다.

setpoint 대기는 최신값 하나만 유지한다. 이미 일부 fragment를 보낸 메시지는 제한 시간 안에 끝내거나 연결 오류로 처리하고, 후속 최신값을 보낸다. disarm은 대기 setpoint보다 우선하되 진행 중인 GATT I/O를 무한정 기다리지 않게 timeout을 둔다. 무선 disarm 전달 실패를 대비한 독립적인 기체 watchdog이 필요하다.

## 세션과 만료

ESP32 uptime 기반 단조 시간을 handshake 왕복 측정으로 서버와 맞추고 보수적 오차 상한을 추적한다. 벽시계 날짜나 Python time.time()을 명령 만료 판정 기준으로 쓰지 않는다. RTT/시간 오차가 프로파일 한계를 넘으면 제어 시작을 거부하거나 DEGRADED 처리한다.

각 제어 명령은 protocol_version, boot_id, control_session, command_seq, **ESP32 기준 유효 기한**을 포함한다. ESP32는 이미 지난 기한, 너무 먼 미래 기한, 잘못된 세션, 역순/중복 seq를 거부한다. 수신 시점부터 TTL을 새로 세는 방식만 사용하면 BLE 버퍼의 오래된 명령이 되살아날 수 있으므로 금지한다. 긴 메시지 전송 중 기한이 지나도 적용하지 않는다.

Unity 명령에는 server_epoch, slot_id, expected_binding_epoch, command_id, action_id가 필요하다. 서버는 사전에 등록/검증한 action의 원래 만료시간 안에서만 setpoint를 허용한다. 같은 action을 반복 전송해 임무 유효 기한을 계속 늘리지 않는다. 서버 재시작/기체 재부팅/역할 변경에는 새 세션이 필요하며 과거 command queue는 폐기한다.

## JSON 예시 — Unity가 Python에 보내는 setpoint

```json
{
  "protocol_version": 1,
  "server_epoch": "server-run-uuid",
  "slot_id": "slot-2",
  "expected_binding_epoch": "binding-uuid",
  "command_id": "command-uuid",
  "action_id": "validated-action-uuid",
  "setpoint": {"surge_norm": 0.15, "yaw_rate_rad_s": 0.1}
}
```

이 수치는 예시이며 허용 최대값이 아니다. 서버는 해당 역할과 기체 capability·설정된 범위·armed 상태·배터리 상태를 검사한다. 서버가 기체 세션/sequence/최종 deadline을 넣어 BLE 메시지를 만든다. API 응답 `accepted`와 기체 `applied`는 서로 다른 단계다.

## JSON 예시 — 기체 텔레메트리의 의미

```json
{
  "protocol_version": 1,
  "drone_id": "drone-persistent-uuid",
  "boot_id": "boot-uuid",
  "telemetry_seq": 128,
  "uptime_ms": 15230,
  "armed": false,
  "last_applied_seq": null,
  "battery": {
    "battery_session_id": "battery-installation-uuid",
    "pack_voltage_v": null,
    "current_a": null,
    "soc_pct": null,
    "remaining_seconds": null,
    "measurement_valid": false,
    "estimate_source": null
  },
  "faults": []
}
```

잔량을 추측하지 않고 미측정 값을 명시한다. 서버는 수신 시각을 별도로 기록한다.

## 알림 계약

`event_id`, `server_epoch`, `type`, `drone_id`, `slot_id`(배정되지 않았으면 null), `binding_epoch`, `severity`, `reason`, `measurement`, `measurement_age_ms`, `swap_id`, `requires_operator_action`를 포함한다. 이벤트 type은 `battery.warning`, `battery.replace_required`, `battery.critical`, `battery.unknown`, `drone.lost`, `slot.changed`, `swap.progress`, `command.rejected` 등이다.

배터리 알림 예: '역할 2 / 기체 B: 교체 준비 — 전압 기준 지속 하회, 1초 전 측정'. 남은 시간을 계산할 근거가 없으면 분 단위 카운트다운을 표시하지 않는다. 알림 확인 후에도 상태 표시는 남는다. 상태 심각도 상승 시 다시 알린다.

## 주기 후보 (실측 전, 보장 아님)

| 항목 | 초기 bench 시험 후보 |
|---|---|
| Unity→Python setpoint | 10Hz, 유효 action이 있을 때만 |
| Python→ESP32 setpoint | 최대 10Hz/기체, latest-only, 네 연결 부하로 검증 |
| ESP32 telemetry | 2Hz부터 시작, applied seq·배터리·상태 묶음 |
| 배터리 UI 갱신 | 1~2Hz면 충분한지 확인, 심각도 변경은 즉시 이벤트 |
| 링크 telemetry stale | 2초, 실측 후 조정 |
| setpoint 유효기간 | 500ms, PID/동특성·BLE 실제 지연 측정 후 확정 |
| PID | ESP32 로컬에서 기체 설계자가 결정 |

500ms 유효기간은 LLM에게 500ms마다 판단하라는 뜻이 아니다. Unity의 결정적 제어기가 아직 유효한 행동을 갱신하는 동안만 명령이 유지된다. TTL·주기는 프로파일 검증 전 실제 arm에 사용하지 않는다.
