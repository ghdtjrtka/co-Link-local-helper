# co-Link local installer and conversion helper

Public inspection source for the local Windows components distributed at
https://colink.namanavi.com/dongle. The desktop application and cloud service
are separate; their source is not included in this repository.

## Which code is being published?

- `setup/`: Standard 0.1.2 app installation, removal, optional signed USB/IP
  installer verification and launcher scripts.
- `helper/`: Conversion Assistant 0.1.5 loopback server, inspection, stable-device
  identity, signed-plan verifier, local driver backup/binding/restore operations,
  protected installation and administrator elevation scripts.
- `helper/radio/`: the actual packaged HCI initialization/probe Python source.
- `helper/native/inspect-radio.cpp`: source reference for the read-only Windows
  radio-address probe and its CMake build definition.

Scripts and Python modules are copied byte-for-byte from the released ZIPs,
not from a later development checkout. `SOURCE-MANIFEST.json` maps each file
to its original archive member and SHA-256. The native probe reference includes
its distributed executable hash; this is not a claim of reproducible C++ builds.
The Python runtime, third-party driver executables, external codec files, private
keys, pairing records and device reports are not copied into this repository.

## Why a helper?

A web page cannot install a Windows USB driver. The local helper reads the
selected dongle, creates an original-driver backup and executes a signed,
device-specific plan after administrator approval. It listens on 127.0.0.1:8765
and accepts the configured co-Link site origin. The helper is only needed for
inspection/conversion/recovery; ordinary app playback does not need it running.
Conversion can disconnect other devices on the selected dongle. Keep the backup.
No driver, boot/security or Bluetooth setting is changed merely by reading this
repository or running the offline source checks.

## What remains private?

Cloud compatibility/recipe selection, analysis prompts/models, plan generation
and signing, payment processing, conversion entitlements and recovery-key issuance
remain on the server. This repository contains the local consumer of signed
plans, not the server authority. It contains a PUBLIC verification key only.
The optional developer offline authority `local_policy.py` is not included.

## Inspect / verify

1. Download the actual public Setup and Assistant ZIPs from the official site.
2. Run `python tools/verify-release-source.py <setup.zip> <assistant.zip>`.
   It checks archive digests and all archive-derived source members without
   running installers, drivers, USB operations or the local server.
3. Read `setup/install-colink.ps1`, `setup/uninstall-colink.ps1`,
   `helper/native/manage-transmitter.ps1`, `helper/plan.py` and the protected-path
   checks before authorizing changes. The uninstaller removes its own installed
   app version; restoring a converted dongle is a separate explicit operation.
4. To build the read-only native probe: `cmake -S helper/native -B build -A x64`,
   then `cmake --build build --config Release` (Visual Studio C++ required).

Unsigned scripts still require your own trust decision. Publishing this subset
does not prove that the closed desktop EXE has no malicious functionality.
Checksums identify bytes, not their safety. Antivirus results are time-, engine-
and file-specific and are not a guarantee. Current evidence and limitations:
https://colink.namanavi.com/security.html

## Third-party tools

The released Assistant invokes pinned `wdi-simple.exe` (Raspberry Pi usbboot
commit 0fd2663f869b7f325f4481049c60d6394855260e, `win32/redist/`) and a separately
pinned libusb0 helper. Their hashes and original licences are retained. These
binaries are not authored by co-Link and are not re-licensed MIT by this repo.
Upstream library source: https://github.com/pbatard/libwdi
USB/IP optional driver: https://github.com/vadimgrn/usbip-win2
Pinned Python/runtime revisions: the requirements and original runtime notices
in the downloaded ZIP. This source subset alone is not a standalone installer
package; use the complete verified release for runtime dependencies.

## 한국어

설치·삭제 및 로컬 도우미의 장치 확인·백업·서명 확인·드라이버 변경·복구 코드를
공개합니다. 실제 배포 ZIP에서 추출한 코드이며 클라우드 분석·서명·결제·권한 발급
로직, 개인용 오프라인 변환 권한과 외부 인코더 원본은 포함하지 않습니다.
현재 배포 코드와 개발 중인 코드의 차이는 공개 배포 파일 기준으로 구분했습니다.
본체 EXE 전체 소스 공개나 무해성 인증을 뜻하지 않습니다.
