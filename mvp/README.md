# Session Hub — 개발자 문서

Hermes Desktop 0.21.x·0.22.x용 unified plugin입니다(0.21.7 실기, 0.22.1 소스 기준 검증, 버전 0.7.1). 프로젝트 포트폴리오·오늘의 실행목록·논리 작업·정확한 원본 세션/Kanban 상세를 한 화면에 제공합니다. 설치는 저장소 루트 `README.md`(install.ps1)를 따릅니다.

## 구조

```text
plugins/session-hub/
├── plugin.yaml
├── __init__.py
├── dashboard/
│   ├── manifest.json        # api=plugin_api.py
│   ├── plugin_api.py        # /health, /config, /sessions, /kanban, /revalidate, /projects, /registry
│   ├── hub_config.py        # Hermes 홈·프로필 자동 감지·session-hub.config.json
│   ├── project_hub.py       # local registry, read-only Kanban adapter, audit/export/restore
│   ├── skills_index.py      # Skills 탭 로컬 스킬 색인
│   ├── ai_sessions.py       # 코덱스·클로드 탭 수집기/리더
│   ├── external_ai_status.py # (구) 외부 AI 작업 리더, API만 유지
│   └── sessionhub_backend.py
└── desktop/
    └── plugin.js
```

- 프로젝트 포트폴리오 + 오늘의 실행목록
- 프로필 자동 감지: `hub_config.py`가 `default` + `profiles/<이름>/state.db`를 찾고, 데스크톱 화면은 `GET /config`로 체크박스를 그림(설정 파일로 명시·별칭·지사 지정 가능)
- 코덱스·클로드 탭: 이 PC의 코덱스 데스크톱 세션과 클로드 데스크톱(로컬 코워크)·Claude Code 작업을 최근 활동순으로 모아 제목·요청·결론 3줄·상태를 표시 (`ai_sessions.py` 수집기가 30분마다 `%LOCALAPPDATA%\hermes\session-hub\ai-sessions.json`에 갱신, 탭의 Refresh는 즉시 수집). 클라우드(원격)에서 돈 클로드·코덱스 작업은 동기화 폴더에 남긴 요약 파일로 '클로드(원격)'·'코덱스(원격)'으로 함께 표시
- Desktop/Telegram 세션과 Kanban 카드의 독립 탭(합성 세션 없음)
- History window 7일/14일/30일/전체와 custom date 유지, 전환 시 paging reset
- 세션 제목·내용 및 카드 ID·제목·본문 검색, profile/assignee/date/status/Telegram-type 필터
- 저장형 10/20/50/100/all. `all`은 keyset cursor로 전체 결과를 100개 window씩 탐색하며 DOM은 항상 최대 100개 카드만 렌더링
- 세션 시간·profile·source·최근 메시지 3개와 Kanban board/status/assignee/priority/timestamps/project/current+latest run/blocked 신호
- Kanban 카드 클릭 시 정확한 board/task detail을 가져와 우측 drawer를 먼저 열며, 상위 세션과 최신 worker 실행 세션을 서로 다른 exact link로 표시
- 상위 세션 owner는 `tasks.session_id`의 로컬 profile DB 정확 일치로만 결정하고, worker 세션은 최신 run의 `metadata.worker_session_id`와 run profile 쌍으로만 결정
- accessible loading/error/empty/refresh 상태
- 30초 자동 갱신과 owner-path 재검증 후 last-wins in-place open
- 로컬 profile/Kanban SQLite `mode=ro`, `query_only=ON`, SELECT/PRAGMA authorizer allowlist, schema feature-detect 및 범위 밖 fail-closed

## 설치

`python build.py`로 `dist/`를 만든 뒤 `dist/install.ps1`을 실행합니다(루트 `README.md` 참고). `dist/`에는 플러그인 패키지, `install.ps1`·`uninstall.ps1`, 설정 견본만 들어갑니다.

## 클라우드 세션 요약 형식 (`ai-session-report-v1`)

`reportsDir` 폴더의 `<agent>-<sessionId>.json` 파일 하나가 세션 하나입니다(같은 세션은 같은 파일을 덮어씀, 64KB 이하).

```json
{
  "contract": "ai-session-report-v1",
  "agent": "claude",
  "sessionId": "session_abc",
  "title": "작업 제목",
  "request": "사용자 요청 1줄",
  "conclusion": ["결론 1", "결론 2", "결론 3"],
  "status": "done",
  "project": "선택",
  "model": "선택",
  "link": "선택",
  "createdAt": "2026-10-14T00:00:00Z",
  "updatedAt": "2026-10-14T01:00:00Z"
}
```

대화 전문·비밀값은 넣지 않습니다. 수집기가 다시 마스킹·길이 제한을 적용합니다.

## 실제 컴포넌트 preview

```text
npm run preview
```

명령은 먼저 official plugin ESM을 loader-compatible local SDK/React harness에서 실제 실행해 `evidence/ui-harness.html`을 만든 뒤, fixture-backed loopback 서버를 엽니다.

- UI: `http://127.0.0.1:8787/`
- fixture health: `http://127.0.0.1:8787/health`
- 8787이 사용 중이면 `SESSION_HUB_PREVIEW_PORT=0 npm run preview`로 안전한 임시 loopback 포트를 자동 선택할 수 있습니다. 출력된 URL을 사용합니다.

Preview는 live Desktop이나 live profile DB를 사용하지 않습니다. 종료는 `Ctrl+C`입니다.

## 검증

```text
npm run test:python
npm run test:node
npm run test:frontend
```

Node integration은 source regex 변환 없이 custom ESM loader로 실제 `desktop/plugin.js`를 import·render·조작합니다.

## validated build

```text
npm run build
```

Build는 모든 필수 source를 먼저 검증하고, project 내부의 실제 `dist/`만 clean/recreate한 뒤 전체 비-manifest 파일 집합과 `dist/manifest.json` 집합이 정확히 같은지 검증합니다. Installable payload는 `dist/plugins/session-hub/`입니다.

`dist/README.md`는 installable artifact 계약만 설명합니다. Dist에는 npm preview/build 도구가 포함되지 않으며 해당 명령을 제공한다고 주장하지 않습니다.

## 한계와 승인 경계

- live Desktop plugin load/reload/current-window E2E는 실행하지 않았습니다.
- 코덱스·클로드 수집기는 각 앱의 로컬 기록(`.codex/state_*.sqlite`·rollout, `%APPDATA%/Claude/local-agent-mode-sessions`, `.claude/projects`)을 읽기만 하며, 세션별 제목·첫 요청 1줄·마지막 답변 앞 3줄·상태·폴더·시각만 저장합니다(비밀값·메일 마스킹, 시스템 프롬프트·계정 정보·전체 대화 미저장). 클라우드 원격 세션은 PC에 대화 기록이 없으므로, 작업을 마칠 때 요약 파일(`ai-session-report-v1`: agent·sessionId·title·request·conclusion 3줄·status·project·model·createdAt·updatedAt)을 동기화 폴더에 남기면 수집기가 다시 정제해 읽습니다. 폴더 위치는 환경변수 `HERMES_SESSION_HUB_REPORTS` 또는 `%LOCALAPPDATA%\hermes\session-hub\ai-sessions.config.json`의 `reportsDir`로 지정하며, 64KB 초과·형식 불일치 파일은 건너뜁니다. 이전 외부 AI 작업 API(`/external-ai/jobs`)는 남아 있지만 탭에서는 더 이상 쓰지 않습니다.
- 지사 PC의 원본 `state.db`·secret·cron/Gateway 상태는 직접 열거나 복제하지 않습니다. 지사 PC가 `tools/session_snapshot.py`로 만든 **축약 사본**(`sessions` 전체 + 최근 30일 user/assistant `messages`, `kanban.db` 전체)을 `<syncRoot>/snapshots/<ID>/`에서 읽습니다. 사본은 manifest sha256 검증 후 `%LOCALAPPDATA%\hermes\session-hub\snapshot-cache\`에 복사해서 엽니다(동기화 중 부분 파일 방지). 신선도는 `fresh / stale / unknown / damaged / unsupported`로 표시하며 0건으로 가장하지 않습니다.
- 지사 세션 행은 표시 전용입니다(`이 PC에서는 열 수 없음 — 지사 A 세션`). Desktop SDK `openSession`은 이 PC의 프로필만 열고, `/revalidate`는 지사 프로필에 `remote_instance`를 반환합니다. 설정의 `instances.syncRoot` 또는 `HERMES_SYNC_ROOT`로 동기화 폴더를, `HERMES_SESSION_HUB_NO_REMOTE=1`로 지사 읽기를 끌 수 있습니다.
- Python backend와 Desktop half는 각각 별도 enable gate가 있으며, backend가 꺼졌거나 누락되면 UI가 명시적 error state로 fail closed합니다.
- Telegram Mini App 인증, Cloudflare/DNS/Bot 메뉴, 외부 공개는 범위 밖입니다.
- live active DB의 begin/end hash drift는 외부 drift이므로 hash 불변성 판정은 inconclusive로 기록합니다.
