# Hermes Session Hub

[Hermes Agent](https://hermes-agent.nousresearch.com) Desktop 0.21.x·0.22.x용 플러그인입니다(0.21.7 실기, 0.22.1·0.22.2 소스 기준 검증). 이 PC의 Hermes 세션·Kanban 카드·스킬, 그리고 코덱스·클로드 작업 기록을 한 화면에서 **읽기 전용으로** 찾고 바로 엽니다.

> English summary below.

## 무엇을 보여주나

| 탭 | 내용 |
|---|---|
| Project Hub | 프로젝트 포트폴리오 → 다음 행동 → 원본 세션/카드를 3클릭 안에 |
| Desktop / Telegram | 최근 활동순 세션 목록, 제목·내용 검색, 프로필·기간·유형 필터, 클릭하면 현재 창에서 그 세션을 엶 |
| Kanban | 여러 보드의 카드, 상태·담당자 필터, 카드 상세 drawer와 원본·worker 세션 링크 |
| 코덱스·클로드 | 이 PC의 Codex 데스크톱·Claude 데스크톱·Claude Code 세션의 제목/첫 요청/결론 3줄/상태 |
| Skills | 설치된 SKILL.md 색인 검색 (이 PC의 Hermes Desktop에서만 열림, 원격 대시보드에서는 차단) |

- **프로필 자동 감지**: `default`와 `profiles\<이름>\state.db`가 있는 모든 프로필이 체크박스로 나타납니다. 설정 파일이 없어도 동작합니다.
- **본사/지사(선택)**: 여러 PC를 쓰면 다른 PC(지사)의 세션 사본을 공유 폴더로 받아 함께 볼 수 있습니다. 기본으로 켜져 있지만, 지사를 등록하기 전에는 이 PC(본사) 프로필만 보입니다.
- **고장 난 프로필 격리**: 오래된 스키마 등으로 읽을 수 없는 프로필은 건너뛰고 화면 위에 "읽지 못한 프로필"로 알려줍니다(선택한 프로필이 전부 실패할 때만 오류).
- **읽기 전용**: Hermes의 `state.db`·Kanban DB는 SQLite `mode=ro` + `query_only` + SELECT 허용 목록으로만 엽니다. 쓰는 곳은 플러그인 전용 프로젝트 레지스트리(`session-hub\project-registry.sqlite3`)뿐입니다.

## 설치 (Windows)

1. 이 저장소를 받습니다: **Code → Download ZIP** 후 압축 해제(또는 `git clone`). 압축을 푼 폴더의 `mvp\installer\`로 이동합니다.
2. PowerShell에서 실행합니다.

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1 -EnableCollector
   ```

   - 플러그인을 `%LOCALAPPDATA%\hermes\plugins\session-hub\`에 복사합니다(기존 폴더는 `session-hub\backups\`에 백업).
   - `hermes plugins enable session-hub`로 Python 쪽을 켭니다(`-NoEnable`로 생략).
   - `-EnableCollector`: 코덱스·클로드 수집기를 작업 스케줄러에 30분 주기로 등록합니다(`-CollectorMinutes 15`처럼 변경, 관리자 권한 불필요). 등록하지 않아도 탭의 Refresh 버튼으로 즉시 수집됩니다.
   - `-WriteConfig`: 설정 파일 견본을 `%LOCALAPPDATA%\hermes\session-hub\session-hub.config.json`에 만듭니다.
   - `-UpdateProfileCopies`: `profiles\<이름>\plugins\session-hub`에 남은 이전 사본도 함께 갱신합니다.
   - `-HermesHome D:\hermes`: Hermes가 기본 위치가 아닐 때.
3. Hermes를 재시작하고, Desktop → Capabilities → Plugins에서 **Session Hub**를 켭니다.

제거: `.\uninstall.ps1` (플러그인 폴더는 삭제하지 않고 `session-hub\backups\`로 옮깁니다. `-RemoveData`는 설정·레지스트리 폴더도 옆으로 옮깁니다.)

> **Hermes 0.22 참고**: `config.yaml`의 `plugins.isolation`을 `host`로 바꾸면 Skills 검색과 코덱스·클로드 새로고침은 "이 PC 요청인지" 확인할 수 없어 `plugin_host_isolation` 오류로 막힙니다. 기본값(`in_process`)에서는 그대로 동작합니다.

## 설정 (선택)

`%LOCALAPPDATA%\hermes\session-hub\session-hub.config.json` — 없으면 모두 기본값입니다. 바꾼 뒤 Hermes를 재시작합니다.

```json
{
  "contract": "session-hub-config-v1",
  "profiles": "auto",
  "profileLabels": { "default": "기본", "writer": "작가" },
  "instances": {
    "enabled": true,
    "localLabel": "본사",
    "remoteGroupLabel": "지사",
    "syncRoot": "D:/Sync/hermes-hub",
    "remotes": { "A": { "label": "노트북", "ttlMinutes": 30 } }
  },
  "skillsRoot": "",
  "projectBoard": ""
}
```

| 키 | 의미 |
|---|---|
| `profiles` | `"auto"`(기본) 또는 `["default", "writer"]`처럼 직접 지정 |
| `profileLabels` | 체크박스에 보일 별칭 |
| `instances.enabled` | 본사/지사 구분 표시. `false`면 이 PC 프로필만 한 묶음으로 표시 |
| `instances.syncRoot` | 지사 사본이 쌓이는 공유 폴더(`<syncRoot>\snapshots\<ID>\`). 비우면 지사를 읽지 않음 |
| `instances.remotes` | 지사 ID(영문·숫자 16자 이내, `HQ` 제외)와 이름·신선도 기준(`ttlMinutes`) |
| `skillsRoot` | Hermes 밖에 둔 공용 스킬 폴더(선택) |
| `projectBoard` | Project Hub가 읽을 Kanban 보드 이름(비우면 `current` → 기본 보드) |
| `projects` | Project Hub 프로젝트 목록 `[{"id": "site", "name": "WebSite", "goal": "…", "aliases": ["web site"]}]`. 비우면 Kanban 카드의 `project_id`·작업 폴더 이름마다 프로젝트를 자동으로 만듦 |

환경변수로도 덮어쓸 수 있습니다: `HERMES_HOME`, `HERMES_SESSION_HUB_CONFIG`, `HERMES_SYNC_ROOT`, `HERMES_SESSION_HUB_NO_REMOTE=1`, `HERMES_KANBAN_DB`, `HERMES_SESSION_HUB_SKILLS_ROOT`.

### 지사 PC 연결

1. 지사 PC에 `tools\session_snapshot.py`와 `tools\branch\*.cmd`를 `<syncRoot>\shared\tools\`로 복사합니다.
2. 지사 PC에서 `register_snapshot_task.cmd A 15` — 15분마다 그 PC의 세션을 축약 사본(세션 목록 + 최근 30일 user/assistant 메시지, tool 출력 제외)으로 `<syncRoot>\snapshots\A\`에 씁니다.
3. 본사 PC 설정의 `instances.remotes`에 `"A"`를 추가합니다. 지사 세션은 표시 전용이며 이 PC에서 열 수 없습니다.

### 클라우드 세션 요약 (선택)

원격(클라우드)에서 실행된 Claude·Codex 작업은 PC에 기록이 남지 않습니다. 작업 끝에 `ai-session-report-v1` 형식 JSON 요약을 공유 폴더에 남기도록 하고, `%LOCALAPPDATA%\hermes\session-hub\ai-sessions.config.json`에 `{"reportsDir": "<폴더>"}`를 넣으면 '클로드(원격)·코덱스(원격)'으로 함께 보입니다. 형식은 `mvp/README.md`를 보세요.

## 개발

```bash
cd mvp
python tests/run_python_tests.py      # 또는: python -m pytest -q tests
npm run test:node
python build.py                       # dist/ 생성·검증
npm run preview                       # fixture 기반 로컬 미리보기
```

필요 패키지: Python 3.11+ (`fastapi`, `httpx`, `pyyaml` — 테스트용), Node 20+.

## 보안·개인정보

세션 원문·DB·로그·설정 파일은 저장소에 올리지 않습니다(`.gitignore`). 자세한 내용은 [SECURITY.md](SECURITY.md).

---

## English summary

Session Hub is a read-only Hermes Desktop plugin that lists Desktop/Telegram sessions, Kanban cards, skills, and local Codex/Claude sessions in one page and opens the exact session in place.

- **Install:** download the repository (Code → Download ZIP), open `mvp\installer\`, run `powershell -ExecutionPolicy Bypass -File .\install.ps1 [-EnableCollector] [-WriteConfig]`, restart Hermes, enable "Session Hub" in Desktop → Capabilities → Plugins. `uninstall.ps1` reverses it (moves folders to backups, never hard-deletes).
- **Profiles** are auto-detected (`default` + every `profiles/<name>/state.db`).
- **Optional settings** live in `%LOCALAPPDATA%\hermes\session-hub\session-hub.config.json` (labels, explicit profile list, multi-PC "branch" snapshots via a shared folder).
- Hermes databases are opened with SQLite `mode=ro`, `query_only=ON` and a SELECT-only authorizer.

## License

[MIT](LICENSE)
