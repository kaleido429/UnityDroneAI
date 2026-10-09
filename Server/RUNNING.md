# Blimp Bridge v0.1 사용 방법

Python 3.13/Windows에서 설치·테스트했다. `.venv`와 의존 패키지는 이 프로젝트에 설치되어 있다. 기본 실행은 드론 없이 통신을 시험하는 **mock 모드**다. 물리/비행 시뮬레이션은 없다.

## 실행

PowerShell:

```powershell
Set-Location 'F:\Unity\UnityDroneAI\Server'
.\.venv\Scripts\python.exe -m blimp_bridge --mock
```

또는 `./Start-Bridge.ps1 -Mode mock`. 서버 주소는 `http://127.0.0.1:8765`, API 문서는 `http://127.0.0.1:8765/docs`. 종료는 Ctrl+C. 이번 개발 테스트용 서버는 종료해 두었다.

서버 시작 때 생성하는 `data/mock/api-token.txt`를 읽어 API/이벤트 WebSocket 요청에 `Authorization: Bearer <token>` 헤더를 넣는다. 토큰은 재시작 때 바뀐다. 고정 토큰이 필요하면 프로세스 환경변수 `BLIMP_API_TOKEN`으로 주입한다. 토큰을 Unity 빌드나 Git에 포함하지 않는다.

다른 PowerShell 창에서:

```powershell
Set-Location 'F:\Unity\UnityDroneAI\Server'
.\.venv\Scripts\python.exe demo_swap.py
```

이 데모는 테스트 기체 7대를 등록하고 4대를 연결·배정한 뒤, 2번 기체 배터리 경고→회수 확인→5번 예비 기체로 교체한다. 나머지 세 기체에는 총 90개 명령을 보내 적용 ACK를 확인한다. 완료 후 테스트 기체를 모두 disarm한다. 데모 재실행 전 서버를 재시작한다. **데모의 3.7/3.5/3.2V 등은 시험용 수치이며 실제 배터리 기준이 아니다.**

## 실제 BLE 연결 준비

```powershell
.\.venv\Scripts\python.exe -m blimp_bridge --config settings.example.json
```

BLE 모드는 자동 검색·연결·arm하지 않는다. mock과 BLE의 DB/토큰은 별도(`data/mock`, `data/ble`)이며 테스트 프로파일이 실제 모드에 자동 적용되지 않는다. `/v1/discovery`를 요청하면 서비스 UUID에 맞는 BLE 기체만 검색한다. 여기서 받은 address와 운영자가 등록한 drone_id로 `/v1/drones/{id}/connect`를 호출하고, GATT Identity의 영구 ID와 다시 대조한다.

ESP32에는 [펌웨어 계약](FIRMWARE_CONTRACT.md)의 GATT 서비스/CBOR 프레임/ACK/만료 watchdog을 구현해야 한다. Classic Bluetooth SPP 펌웨어에는 연결되지 않는다. 정확한 ESP32 모델과 BLE 지원은 확인 필요하다.

실제 arm은 아래 준비가 모두 갖춰져야 한다.

- `settings.example.json`을 복사한 별도 설정에서 `hardware_arming_enabled=true`.
- 해당 기체의 `vehicle.validated=true`, 추력·회전율 한도·expiry_behavior를 설정하고 펌웨어 Identity의 profile_hash와 일치.
- 기체별로 보정한 `battery.validated=true`와 세 전압 임계값. 값 관계는 critical < replace < warning.
- 최신 유효 텔레메트리·NORMAL 배터리·필수 firmware capability·정상 이벤트 저장소.

프로파일은 최초 등록 요청에 포함한다. 수정은 **연결 및 역할 해제 후** `PUT /v1/drones/{id}/profile`로 전체 등록 모델을 제출한다. 원래 설계의 `config.example.json`은 설명용이며 실행 설정으로 사용하지 않는다. 실행 설정은 `settings.example.json`이다.

배터리 센서가 없으면 UNKNOWN으로 표시하고 새 arm을 거부한다. 현재는 전압 기반 필터/지속 시간/히스테리시스/CRITICAL 래치만 구현했다. SOC는 표시할 수 있으나 SOC 기준 경고·남은 비행 시간 예측은 아직 하지 않는다. 배터리 세션 ID를 바꾸기 전까지 CRITICAL 래치는 유지한다.

## Unity 호출 순서

1. `GET /v1/fleet`로 server_epoch, slot별 binding_epoch 확인.
2. 기체 등록→검색→기체 연결→정상 배터리 확인 후 slot에 assign.
3. `POST /v1/slots/{slot}/arm`에 expected_binding_epoch 전송. 성공은 적용 ACK까지 확인한 응답이다.
4. `/actions`로 duration_ms(100~10000ms)를 지정해 action_id를 받고, 유효 기간 동안 `/commands`에 setpoint를 보내기. 초기 arm도 500ms 후 만료하므로 바로 action/command를 시작한다.
5. command에는 server_epoch, expected_binding_epoch, action_id, 증가하는 command_seq, surge_norm, yaw_rate_rad_s를 넣는다. HTTP 202는 **접수**이며, `/v1/operations/{operation_id}` 또는 WebSocket의 command.applied 이벤트로 실제 ACK를 확인한다.
6. `ws://127.0.0.1:8765/v1/events`로 배터리·연결·교체 이벤트 수신. 최초 snapshot을 받으며 resync_required/재접속 시 새 snapshot을 읽는다. 최근 500개 이벤트는 `/v1/events/recent`에서도 조회 가능.

surge_norm은 정규화된 전진 추력 요청이며 m/s가 아니다. yaw_rate_rad_s는 rad/s다. 역추진은 기체 프로파일에서 켰을 때만 허용한다. 서버는 3개 프로펠러의 PWM이나 PID를 계산하지 않는다.

서버는 최근 setpoint를 보낼 뿐, 만료된 행동을 스스로 반복 연장하거나 LLM의 목표를 자율 비행 계획으로 변환하지 않는다. Unity의 결정적 제어기가 유효 행동에 대해서만 새 명령을 생성해야 한다. 새 action_id를 발급하는 것 자체가 경로/충돌 안전성을 검증한 것은 아니다.

## 교체와 고장 처리

`POST /v1/slots/{slot}/swaps`에는 expected_binding_epoch 및 `Idempotency-Key` 헤더가 필요하다. 해당 slot의 새 행동/조종 명령이 차단된다. 이 v0.1에는 원격 회수용 별도 조종 UI가 없으므로 **교체 시작 전에 기체를 회수할 준비를 하고 운영자가 회수**한다. 자동 귀환은 구현하지 않았다.

`/v1/swaps/{id}/confirm-recovered`에 `{"recovered":true}`를 보내면 disarm ACK 후 기존 연결만 해제한다. ACK가 없으면 물리적으로 회수하고 전원을 끈 것을 확인한 뒤 `physically_powered_off:true`가 추가로 필요하다. 단절됐다는 이유만으로 회수 완료로 간주하지 않는다.

한 연결이 비면 예비 기체를 connect한 후 `/v1/swaps/{id}/replacement`에 새 drone_id를 전달한다. 새 binding_epoch로 교체되며 **READY_DISARMED**로 남는다. 운영자가 새로 arm해야 한다. 나머지 세 slot의 연결/세션에는 손대지 않는다.

역할 없는 연결은 `/v1/drones/{id}/disconnect`로 해제한다. 역할 없이 LOST가 된 기체는 물리적 회수·전원 차단 후 `/release-recovered`로 해제할 수 있다. 역할 있는 기체는 교체/회수 절차를 거친다.

서버/Unity 종료, 배터리 위험, 명령 만료 시 장치의 로컬 watchdog이 최종적으로 동작해야 한다. 서버는 무선 중단 상황에서 disarm 전달을 보장하지 못한다. 재시작 시 등록/프로파일/이벤트만 보존하고 연결·arm·slot을 자동 복원하지 않는다. 진행 중 교체는 이벤트 기록을 보고 운영자가 다시 확인한다.

## 검증 결과 / 한계

- 자동 테스트 11개 통과: 4연결 제한, 교체 중 다른 세 기체 명령 유지, 단절 격리, 늦은 명령/중복/만료 거부, 배터리 UNKNOWN·지속시간·히스테리시스·래치, 재시작, 인증, BLE 분할/ACK 검증 등.
- 실제 Uvicorn HTTP 서버 + 인증 WebSocket + 7대 등록/4대 연결/예비 교체 데모 통과. 다른 세 기체의 **90/90개 명령 적용**, 연결 ID 유지 확인.
- BLE 코드는 가짜 GATT로 패킷·ACK를 검사했다. 실제 노트북 무선 어댑터·ESP32·PID·배터리 센서·비행 성능은 미검증. 10Hz는 설정 목표이며 무선에서 측정한 보장값이 아니다.
- 테스트에 Starlette의 httpx 사용 중단 예정 경고 1개가 있으나 실패는 아니다.
- Unity C# 연결 컴포넌트와 ESP32 펌웨어는 이번 Python 구현 범위에 포함하지 않았다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests/live_smoke.py
```

새 환경 설치: `python -m venv .venv`, 이어서 `.venv\Scripts\python.exe -m pip install -r requirements.txt`. `requirements.lock.windows.txt`는 이번 Windows 검증 환경 전체 버전이다.
