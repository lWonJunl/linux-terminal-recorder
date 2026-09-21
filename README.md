# 🖥️ Linux Terminal Recorder

WSL 실습 화면을 처음부터 끝까지 **파노라마 PNG 한 장과 PDF**로 저장하는 Windows용 Python 프로그램이다.
실제 터미널을 캡처하므로 명령을 재실행하거나 출력 텍스트를 새로 그리지 않는다.

> 현재 버전: **v1.3.2**

<br>

## 📂 Overview

| Item | Description |
| :-- | :-- |
| **Version** | v1.3.2 |
| **Platform** | Windows, Windows Terminal, WSL Bash |
| **Runtime** | Python 3.14.2 검증 |
| **Output** | 파노라마 PNG 한 장과 PDF |
| **Permission** | 관리자 권한 불필요 |

<br>

## ✨ Key Features

- 실제 터미널 화면을 자동 스크롤·연결해 실습 흐름을 한 장의 PNG와 PDF로 저장한다.
- 최종 제출물과 세션·캡처·텍스트 백업을 분리해 보관한다.
- `clear`, `reset`, `Ctrl+L`로 기록이 사라지는 실수를 방지한다.
- 캡처 조각이 남아 있으면 결과 생성을 다시 시도할 수 있다.
- 상태 파일과 경로를 검사하고 기존 결과를 덮어쓰지 않는다.

<br>

## 🗂️ Project Structure

```text
linux-terminal-recorder/
├── linux_recorder.py       # CLI, 세션 관리, WSL 실행과 화면 기록
├── terminal_history.py     # 터미널 스크롤·화면 조각 연결
├── panorama_export.py      # PNG/PDF 생성과 검증
├── recording.bashrc        # 기록 중 Bash 보호 설정
├── tests/                  # 자동 회귀 테스트와 선택 GUI 검증 도구
├── requirements.txt        # 실행 의존성
├── requirements-dev.txt    # 테스트·검증 의존성
└── TEST_REPORT.md          # 검증 결과와 지원 한계
```

<br>

## 🚀 Quick Start

필요 환경: Windows Python, Windows Terminal, WSL의 Bash.
실제 검증 환경은 Python 3.14.2 / Ubuntu-26.04다. 관리자 권한은 필요하지 않다.

**시작·종료 명령은 WSL 내부가 아닌 Windows PowerShell에서 실행한다.**

### 1. 최초 설치

다운로드하거나 복제한 프로젝트 폴더에서 Windows PowerShell을 열고 실행한다.
아래 명령은 `linux_recorder.py`가 있는 폴더를 기준으로 한다.

~~~powershell
python -X utf8 -m pip install -r requirements.txt
wsl --list --quiet
~~~

### 2. 기록 시작

~~~powershell
python -X utf8 linux_recorder.py start "Week01-Day02" --distro Ubuntu-26.04
~~~

과제명은 원하는 이름으로 바꾸고, `Ubuntu-26.04`는 위에서 확인한 설치 배포판 이름으로 바꾼다.
배포판 옵션을 생략하면 기본 WSL 배포판을 사용한다.
새로 열린 WSL 창에서 실습하면 명령 결과가 실시간으로 표시된다.
처음 실행한 PowerShell과 WSL 창은 모두 열어 둔다.

### 3. 종료 및 저장

명령 실행이 끝나 프롬프트로 돌아오면 **다른 PowerShell 창에서도 같은 프로젝트 폴더를 열고** 실행한다.

~~~powershell
python -X utf8 linux_recorder.py stop
~~~

프로그램이 WSL 창을 자동 스크롤하며 캡처한다. 결과 경로가 표시될 때까지 입력하거나 창을 닫지 않는다.
창을 클릭하라는 안내가 나오면 10초 안에 기록 중인 WSL 창을 클릭한다.

## 📁 Output and Configuration

최종 PDF·PNG는 **바탕화면에 바로**, 중간 기록도 **바탕화면의 별도 폴더 안에** 저장한다.

~~~text
바탕화면/
├── Week01-Day02.png
├── Week01-Day02.pdf
└── linux-recordings/
    ├── .active-session.json       # 완료 시 제거되는 활성 기록 위치
    ├── .control.lock
    ├── .recorder.lock
    └── Week01-Day02/
        ├── session.json          # 상태와 최종 결과 경로
        ├── history.txt           # 최신 텍스트 백업
        └── captures/
            └── panorama-고유번호/
                ├── manifest.json
                └── tile-000001.png ...
~~~

- 같은 이름이 있으면 날짜·고유 번호를 붙여 기존 결과를 보존한다.
- 일반적인 길이는 PDF도 긴 한 페이지다. 매우 긴 기록은 PNG는 한 장을 유지하고 PDF만 줄 경계에서 나눈다.
- 원본 캡처와 백업은 완료 후에도 보존한다. 이전 기록을 자동 삭제하지 않는다.
- 일반적인 저장 오류에서는 이번 복사 중 만든 불완전한 결과만 정리한다. 강제 종료·정전까지 보장하지는 않는다.

저장 위치를 바꾸려면 시작할 PowerShell에서 설정한다.

~~~powershell
$env:LINUX_RECORDER_HOME = "D:\linux-recordings"  # 중간 기록
$env:LINUX_RECORDER_OUTPUT = "D:\submissions"    # 최종 PDF/PNG
~~~

중간 기록 경로는 종료·복구 PowerShell에도 같은 값으로 설정한다.
최종 저장 위치는 시작 시 세션에 기록되며 이후 환경 변수 변경은 소급 적용되지 않는다.

## ⚠️ Recording Scope and Limits

- 기록용 Bash에서 `clear`, `clear -x`, `reset`은 차단 안내와 종료 상태 1을 반환한다.
- `Ctrl+L`은 Emacs·Vi 입력 모드에서 무시하고 입력 중인 명령은 유지한다.
- 사용자의 `~/.bashrc`를 읽은 뒤 보호 설정을 적용한다. 원래 설정 파일은 수정하지 않는다.
- `command clear`, `/usr/bin/clear`, 다른 셸·터미널 메뉴 등으로 우회하는 것은 막지 않는다.

시작 안내에는 이 프로젝트 작성자가 직접 제작한 기록 도구라는 설명이 포함돼 있다.
다른 사람이 사용하는 경우 `recording.bashrc` 끝의 안내 문구를 본인의 사용 상황에 맞게 바꾼다.

현재 지원 범위는 일반 셸에서 출력이 위에서 아래로 누적되는 실습이다.

- 완료 전에 WSL 창을 닫지 않는다. 기록 중 창·글꼴 크기를 유지한다.
- 캡처 중 다른 창으로 가리거나 화면을 잠그지 않는다.
- `vim`, `less`, `top` 같은 전체 화면 프로그램의 모든 과정은 보장하지 않는다.
- 스크롤백 한도 초과로 사라진 화면은 복원할 수 없다. 긴 실습 전에는
  [터미널 스크롤 기록 설정](https://learn.microsoft.com/en-us/windows/terminal/customize-settings/profile-advanced)을 확인한다.
- PDF는 스크린샷 기반이라 텍스트 선택·검색을 지원하지 않는다.

기록 삭제·변경을 감지하면 불완전한 기록을 완성본으로 내보내지 않는다.
변경 직전 백업은 처음 한 번 보존하고, 이후에는 최신 텍스트만 갱신해 동일 백업의 반복 생성을 막는다.
백업 사이에 나타났다 사라진 모든 화면을 보존하는 동영상 기록기는 아니다.

## 🛠️ Commands and Recovery

프로젝트 폴더의 PowerShell에서 실행한다.

~~~powershell
python -X utf8 linux_recorder.py status
python -X utf8 linux_recorder.py doctor
python -X utf8 linux_recorder.py recover "Week01-Day02"
~~~

- `status`: 현재 세션과 기록 프로세스 상태.
- `doctor`: 중간·최종 저장 경로와 화면 접근 검사. 이미지는 저장하지 않는다.
- `recover`: 과제 표시명이 아니라 **실제 세션 폴더 이름**으로 결과 생성 재시도.
- `start ... --interval 1`: 텍스트 백업 간격(초). 촬영 간격이 아니다.

캡처 실패 시 WSL 창을 유지하고 원인을 해소한 뒤 `stop`을 다시 실행한다.
완전한 캡처 조각이 이미 있으면 창 없이도 복구할 수 있다.
창을 닫아 기록이 사라졌다면 텍스트 백업만으로 실제 스크린샷을 복원할 수는 없다.

상태 파일의 필수 항목·자료형·실제 경로를 검사하며, 허용된 폴더 밖을 가리키거나 손상된 상태는 안내 후 중단한다.
파일을 임의로 편집·삭제하지 말고 원본을 보존한다.
구버전 세션의 복구도 유지하며, 최종 경로 정보가 없는 구버전 결과는 기존 세션 폴더에 저장한다.

`BitBlt` / `screen grab failed`는 화면 잠금·원격 세션 단절·제한된 실행 환경에서도 발생한다.
로그인된 Windows의 일반 PowerShell에서 실행한다.

## 🔒 Privacy and Safety

- 자체 업로드 기능은 없다. 사용자가 WSL에서 실행하는 명령의 통신은 별개다.
- 화면의 비밀번호·토큰·개인정보와 겹친 알림은 이미지·텍스트에 남을 수 있다. 자동 마스킹·암호화는 제공하지 않는다.
- 바탕화면이 클라우드 동기화 대상이면 기록도 동기화될 수 있다.
- 본인이 만든 기록만 복구한다. 경로 검사만으로 로컬 악성 프로그램의 동시 변조나 자료 위조까지 방지하는 것은 아니다.
- 프로그램·중간 기록·원본 캡처는 제출 전에 구분하고, 제출물의 내용을 직접 확인한다.

## 🧪 Test

~~~powershell
python -X utf8 -m pip install -r requirements-dev.txt
python -X utf8 -m unittest discover -s tests -v
~~~

자동 테스트는 경로·자료형 검사, 중복 백업 방지, 기존 파일 보존, 실패 후 복구,
줄 누락·중복 탐지와 PNG/PDF의 픽셀·배치를 검증한다. 세부 결과는 [TEST_REPORT.md](TEST_REPORT.md)에 정리한다.
Git이 설치돼 있으면 개인 파일 제외·공개 파일 포함·Bash 줄바꿈 검사도 실행한다.
이 검사는 임시 저장소를 사용하며 프로젝트의 Git 상태를 바꾸지 않는다. Git이 없으면 해당 3개 검사만 생략한다.

실제 WSL 테스트는 키보드와 창을 제어한다. 완료될 때까지 다른 작업을 하지 않는다.

~~~powershell
python -X utf8 tests/manual_smoke.py
python -X utf8 tests/verify_export.py "세션 폴더 경로"
~~~

GUI 테스트는 600줄 출력·한글·색상·긴 줄·화면 지우기 방지·최종 파일 분리를 확인한다.
기본 WSL 배포판을 사용하며, 다른 배포판은 `manual_smoke.py --distro "배포판 이름"`으로 선택한다.
테스트 PDF·PNG와 중간 기록은 바탕화면에 남는다.
선택 기능인 `verify_export.py --render`는 `pypdfium2` 설치가 필요하며 미리보기는 `tmp/pdfs`에 만든다.

## 🎯 Design and Implementation Scope

목표는 **시작 한 번 → 평소처럼 실습 → 종료 한 번**으로 실제 화면이 이어진 제출물을 얻는 것이다.

1. 실습 중에는 텍스트 백업만 갱신한다. 명령어마다 화면을 캡처하지 않는다.
2. 종료 시 Windows UI Automation으로 실제 줄 위치를 확인하고 스크롤하며 캡처한다.
3. 공통 한 줄의 픽셀을 비교한 뒤 연결용 중복만 제외한다. 실제 반복 출력은 유지한다.
4. 줄 순서와 이미지 크기를 확인해 긴 PNG와 PDF를 만든다.

구현 범위는 일반 셸 실습, 화면 지우기 실수 방지, 원본 보존·재변환, 안전한 상태 파일 처리다.
터미널의 보관 한도를 넘어서 사라진 출력, 전체 화면 앱의 전 과정, 창 크기 변경에 따른 재배치는 지원 범위에 포함하지 않는다.

## 📌 Public Files and Personal Records

- 소스 코드, `recording.bashrc`, 의존성 목록, 테스트, README와 검증 보고서를 공개 대상으로 둔다.
- 개인용 실행 메모 `LOCAL_USAGE.md`, 환경 변수 파일, 캡처·PDF·텍스트 백업·로그·캐시는 `.gitignore`에서 제외한다.
- `.gitattributes`는 WSL에서 읽는 Bash 설정을 포함한 소스 파일을 LF 줄바꿈으로 유지한다.
- `.gitignore`는 이미 추적한 파일이나 웹 페이지에서 직접 선택해 업로드하는 파일을 숨기지 않는다.
  업로드 전 `git status --short`와 `git diff --cached`로 실제 포함 파일을 확인한다.
- 라이선스 파일은 아직 지정하지 않았다. 배포 라이선스 선택은 별도로 진행한다.

## 🧰 Tech Stack

| Category | Stack |
| :-- | :-- |
| **Language** | Python |
| **Terminal & Linux** | Windows Terminal, WSL, Bash |
| **Screen Control** | Windows UI Automation, MSS, Pillow, PyGetWindow |
| **Export** | Pillow, ReportLab |
| **Test** | unittest, pypdf, pypdfium2 (선택) |

## 🔗 References

- [Windows Terminal 실행 옵션](https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments)
- [Windows Terminal UI Automation](https://github.com/microsoft/terminal/blob/main/doc/terminal-a11y-2023.md)
- [Bash 시작 파일](https://www.gnu.org/software/bash/manual/html_node/Bash-Startup-Files)
