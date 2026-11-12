# 학습 도우미 백엔드

AI 우왕톤 출품작의 서버다. 주제는 "AI는 동료가 될 수 있는가"이고, 대학 수업에서 AI가 답을 대신 주는 대신 산파술로 질문해 학생이 스스로 과제를 해결하게 돕는 학습 도우미를 만든다. 프런트엔드 저장소는 별도의 FE 저장소에 있고, 이 저장소에는 FastAPI 서버와 자리 3 보조 프로그램이 들어 있다.

## 1. 서비스 개요

학생은 교수가 만든 과제에 참여 코드로 들어와 네 단계를 거친다.

1. 사전평가 단계. 교수가 역량마다 낸 문항을 풀어 스펙, 즉 역량별 강점과 약점을 잰다.
2. 학습 단계. 통과 점수에 못 미친 역량이 학습 목표가 되고, AI가 산파술로 되물으며 확인 문항으로 점수를 갱신한다. 모든 역량이 통과 점수를 넘으면 과제 단계가 열린다.
3. 과제 단계. 도구로 결과물을 만들며 저장할 때마다 변경 이유와 함께 버전이 남는다. AI는 같은 방식으로 돕되 답을 쓰지 않는다.
4. 사후평가 단계. 같은 역량의 다른 문항으로 늘어난 역량을 잰다. 첫 제출 뒤에는 마감까지 3단계만 다시 해 재제출할 수 있다.

교수는 다섯 가지 결과물, 즉 사전평가 점수, 항목별 AI 학습과정 요약, 결과물 변경 이력, 사후평가 점수, 전체 대화를 보고 점수와 코멘트를 입력한다. AI는 결과물 수정 이력과 대화에서 위반 의심 신호만 표시하고 최종 판단은 교수가 한다.

## 2. 기술 선택 이유

| 선택 | 이유 |
|---|---|
| Python FastAPI와 SQLAlchemy와 SQLite | 팀이 Python에 익숙하고, Google과 Anthropic의 공식 AI SDK와 파일 텍스트 추출 라이브러리를 같은 언어로 쓸 수 있다. SQLite는 설치 없이 돌고 심사 규모의 데이터에 충분하다. DATABASE_URL을 바꾸면 PostgreSQL로 옮길 수 있다. |
| React와 Vite | 과제 단계의 Monaco 편집기와 sql.js, Document Picture-in-Picture 같은 브라우저 기능을 다루기 쉽다. 빌드 결과를 FastAPI가 같이 서비스해 배포 URL이 하나다. |
| Gemini API 무료 등급, 모델 gemini-3.5-flash-lite | 유료 API를 쓸 예산이 없어 무료 등급이 있는 Gemini를 기본으로 쓴다. 상위 Flash 모델은 측정 당시 무료 등급 응답이 지연되거나 실패해 Flash-Lite를 고른다. 자체 모델 없이 시스템 프롬프트와 구조화 출력으로 산파술 응답을 만들고 파인튜닝은 하지 않는다. 환경 변수 AI_PROVIDER를 anthropic으로 바꾸면 같은 코드로 Claude API, 모델 claude-sonnet-5-5를 쓴다. |
| 구조화 출력과 판정 재작성 2단계 | 응답을 JSON 스키마에 강제해 질문 유형과 응답 모드를 받고, 과제 단계에서는 별도 호출로 답 노출을 판정해 노출이면 다시 쓰게 한다. |
| 검색 증강 생성, BM25 | 교수가 올린 학습 자료를 조각으로 나눠 색인하고 질문과 관련된 조각만 프롬프트에 넣는다. 벡터 DB 없이 rank-bm25로 처리한다. |
| 자리 3 보조 프로그램, Python과 pywebview와 PyInstaller | 브라우저는 다른 프로그램 창을 캡처하거나 로컬 폴더를 감시할 수 없다. 작은 exe가 창 캡처, 폴더 감시, AI 창을 맡는다. |

## 3. 아키텍처

```
브라우저 (React)  ──/api──▶  FastAPI ──▶ SQLite (data/app.db), 파일 (data/media)
       │                        │
       │ Google 문서 탭          ├──▶ Gemini API (gemini-3.5-flash-lite)
       │                        └──▶ Google Drive API (자리 2, revisions와 export)
보조 프로그램 (exe) ──/api/companion──▶ FastAPI   창 캡처, 저장 폴더 감시, AI 창(pywebview)
```

입력에서 처리로 출력까지의 흐름은 다음과 같다.

- 교수 입력: 역량과 통과 점수와 채점 기준, 사전 사후 문항, 과제 설명과 첨부, 학습 자료, AI 응답 규칙, 도구 연동. `app/routers/assignments.py`
- 학생 입력: 시험 답안, 학습 대화와 확인 문항 답, 결과물 버전과 변경 이유, 과제 대화. `app/routers/tests.py`, `learning.py`, `tasks.py`
- AI 처리: `app/ai/services.py`가 시스템 프롬프트(`prompts.py`)와 구조화 출력 스키마(`schemas.py`)로 Gemini를 부른다. 공급자별 호출은 `client.py`가 맡는다. 산파술 대화, 확인 문항 생성, 채점, 요약, 의심 신호 생성을 맡는다. 자료 검색은 `retrieval.py`다.
- 출력: 교수 화면 API `app/routers/professor.py`가 다섯 가지 결과물과 의심 신호를 돌려준다.

### 직접 구현과 외부 기술의 범위

- 직접 구현: 네 단계 흐름과 역량 기반 스펙 판정, 채점 파이프라인, 산파술 프롬프트와 출력 스키마, 답 노출 판정과 재작성, BM25 자료 검색, 버전 기록, 교수 화면, 보조 프로그램의 창 캡처와 폴더 감시.
- 외부 기술: Google Gemini API(대화, 채점, 요약, 의심 신호 생성 모델), Google Drive API(문서 수정 이력), Google과 GitHub의 OAuth 2.0 로그인, Monaco 편집기와 sql.js(브라우저 안 SQL 실행), pywebview와 PyInstaller와 watchdog와 pywin32(보조 프로그램).
- 모킹: 없다. API 키가 없으면 AI 기능은 503으로 안내하고 고정 결과를 보여 주지 않는다. 텍스트와 이미지 비교 화면은 별도 담당자가 구현 중이며 그 전까지 버전 원문과 캡처만 보인다.

## 4. 실행 방법

```bash
cd backend
py -3.14 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
copy env.example .env      # GEMINI_API_KEY 등을 채운다
.venv/Scripts/python -m uvicorn app.main:app --port 8000 --reload
```

프런트엔드를 빌드해 `frontend/dist`가 있으면 같은 서버가 화면도 서비스한다. 개발 중에는 FE 저장소에서 `npm run dev`로 5173 포트를 띄우고 `/api`를 8000으로 프록시한다.

검사와 테스트:

```bash
.venv/Scripts/python -m pyright app tests
.venv/Scripts/python -m pytest
```

심사용 계정과 시연 과제 생성:

```bash
set DEMO_PASSWORD=비밀번호
.venv/Scripts/python scripts/seed_demo.py --server http://localhost:8000
```

### 소셜 로그인

Google 계정과 GitHub 계정으로 로그인하고 가입할 수 있다. 구현은 `app/routers/social.py`에 있고 OAuth 2.0 인가 코드 방식이다. 클라이언트 ID와 비밀 값은 `.env`에서만 읽으며 저장소에 넣지 않는다. 값이 없는 공급자는 로그인 화면에 버튼이 나오지 않고 시작 요청은 503으로 안내한다.

1. Google은 Google Cloud 콘솔에서 OAuth 클라이언트를 만들고 `GOOGLE_CLIENT_ID`와 `GOOGLE_CLIENT_SECRET`에 넣는다. 자리 2 Google 문서 연동과 같은 값을 쓴다. 승인된 리디렉션 URI에 `OAUTH_REDIRECT_BASE` 뒤에 `/api/auth/social/google/callback`을 붙인 주소를 더한다.
2. GitHub는 Settings의 Developer settings에서 OAuth App을 만들고 `GITHUB_CLIENT_ID`와 `GITHUB_CLIENT_SECRET`에 넣는다. 콜백 주소는 `OAUTH_REDIRECT_BASE` 뒤에 `/api/auth/social/github/callback`을 붙인 주소다.
3. `OAUTH_REDIRECT_BASE`는 이 서버를 외부에서 여는 주소다. 개발 중에는 `http://localhost:8000`이고, 프런트엔드를 5173 포트로 따로 띄우면 `PUBLIC_URL`을 `http://localhost:5173`으로 둔다.

공급자가 확인한 이메일만 받는다. 그 이메일로 소셜 가입한 계정이 있으면 그 계정으로 들어가고, 계정이 없으면 회원가입 화면에서 고른 역할로 계정을 만든다. 로그인 화면에서 소셜 계정을 눌렀는데 계정이 없으면 가입 화면으로 안내한다. 비밀번호로 가입한 계정에는 소셜 로그인으로 들어가지 않는다. 비밀번호 가입은 이메일 주인인지 확인하지 않으므로, 남이 먼저 만들어 둔 계정에 이메일 주인이 들어가는 일을 막기 위해서다.

서버는 60초 동안 한 번만 쓸 수 있는 표를 프런트엔드에 넘기고, 프런트엔드가 그 표를 로그인 토큰으로 바꾼다. 로그인 토큰을 주소에 싣지 않기 위한 단계다. 표와 state는 로그인 토큰과 종류가 달라 로그인 토큰 자리에 넣어도 통하지 않는다. state는 로그인을 시작한 브라우저에 쿠키로 묶으므로, 로그인 화면을 여는 주소와 `OAUTH_REDIRECT_BASE`의 호스트 이름이 같아야 한다. 한쪽을 `localhost`로, 다른 쪽을 `127.0.0.1`로 열면 요청이 만료되었다는 안내가 나온다.

2026-11-12 기준으로 공급자 서버를 가짜로 바꾼 자동 테스트만 통과했고 실제 Google과 GitHub 계정으로는 확인하지 않았다.

### Docker 배포

FE 저장소와 BE 저장소를 같은 폴더 아래 `frontend`, `backend` 이름으로 두고 `backend/.env`를 채운 뒤 실행한다.

```bash
cd backend
docker compose up -d --build
```

컨테이너 하나가 8000 포트로 API와 화면을 함께 서비스한다. 데이터는 `app-data` 볼륨에 남는다.

## 5. 보조 프로그램

`companion/`에 있다. 학생이 자리 3 과제에서 연결 토큰을 발급해 프로그램에 넣으면 지정한 프로그램 창을 일정 간격으로 캡처하고, 저장 폴더에 결과물이 저장될 때마다 버전으로 올리며, 우리 웹의 AI 패널을 항상 위에 띄운다. 기록 범위 동의를 먼저 받는다.

```bash
cd backend/companion
py -3.14 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python companion.py --server http://localhost:8000
.venv/Scripts/pyinstaller companion.spec     # dist/companion.exe
```

만든 exe를 `backend/downloads/companion.exe`에 두면 서버가 `/downloads/companion.exe`로 내려준다. 서명이 없어 Windows 경고가 뜰 수 있다.

## 6. AI 검증 기록

모델: Google Gemini API 무료 등급, `gemini-3.5-flash-lite`, 파인튜닝 없음. 호출마다 `ai_call_logs` 테이블에 용도와 모델과 토큰 수와 성공 여부를 기록한다. 사고 수준은 대화 medium, 답 노출 판정 low, 채점 high로 넘긴다. 무료 등급은 모델별, 프로젝트 단위의 하루 호출 한도가 있고 입력 내용이 Google 제품 개선에 쓰이므로 시연에는 샘플 데이터만 넣는다. 호출 한 번은 60초 제한에 재시도 한 번이고, 한도를 넘거나 서버가 혼잡하면 503으로 안내한다.

2026-11-10 같은 키로 측정한 무료 등급 응답:

| 모델 | 결과 |
|---|---|
| gemini-3.8-flash | 하루 20회, 분당 5회 한도. 산파술 대화 1회가 242초 걸려 성공했고, 이후 단순 요청은 118초 뒤 504 DEADLINE_EXCEEDED |
| gemini-3.6-flash | 503 UNAVAILABLE, 수요 급증 안내 |
| gemini-3.5-flash-lite | 구조화 출력 산파술 응답 4.0초, 정상 해석 |

출력 품질과 오류와 한계의 검증 결과는 시연 과제로 질문 20개를 돌린 뒤 이 절에 기록한다. 아직 실행 전이므로 미검증이다.

2026-11-12에 채점 출력에 학생에게 보여 줄 근거를 더했다. AI는 점수와 함께 채점 기준의 어느 부분으로 판정했는지, 학습 자료의 어느 내용이 근거인지, 답안의 어느 부분이 틀렸는지를 따로 돌려주고, 서버는 틀린 부분 가운데 답안에 실제로 있는 문장만 남긴다. 화면은 근거를 교수 채점 기준, 학습 자료, AI 판단으로 색을 나눠 보여 준다. 자동 테스트는 통과했고, 2026-11-12에 실제 모델로 서술형 답 하나를 채점해 채점 기준 근거와 틀린 부분 세 곳이 돌아오는 것을 한 번 확인했다. 여러 답안에서 이 항목이 얼마나 정확한지는 재지 않아 미검증이다.

알려진 한계:

- 서술형과 논술형 채점은 AI가 교수의 채점 기준으로 하되 교수가 확인하고 고칠 수 있다.
- 답 노출 판정은 과제 단계 응답에만 적용한다. 학습 단계는 비용을 줄이려고 자기 점검만 한다.
- Google 문서는 Google이 수정 이력을 시간이 지나면 합치므로 중간 버전이 사라질 수 있다.
- 보조 프로그램은 Windows 전용이고 최소화된 창은 캡처하지 않는다.

## 7. 데이터와 오픈소스 출처

| 항목 | 출처 | 사용 범위 |
|---|---|---|
| FastAPI, SQLAlchemy, pydantic, uvicorn | https://pypi.org | 서버 프레임워크, MIT 또는 BSD |
| google-genai Python SDK 3.0.0 | https://github.com/googleapis/python-genai | Gemini API 호출, Apache 2.0 |
| anthropic Python SDK | https://github.com/anthropics/anthropic-sdk-python | AI_PROVIDER=anthropic 일 때 Claude API 호출, MIT |
| rank-bm25 | https://github.com/dorianbrown/rank_bm25 | 자료 검색, Apache 2.0 |
| pypdf, python-docx, openpyxl | https://pypi.org | 파일 텍스트 추출 |
| pywebview, PyInstaller, watchdog, pywin32, Pillow | https://pypi.org | 보조 프로그램 |
| Google Drive API | https://developers.google.com/drive | 자리 2 수정 이력과 내보내기 |
| Google OAuth 2.0과 OpenID Connect | https://developers.google.com/identity/protocols/oauth2 | 소셜 로그인, 확인된 이메일과 이름만 읽음, 확인일 2026-11-12 |
| GitHub OAuth Apps | https://docs.github.com/en/apps/oauth-apps | 소셜 로그인, 확인된 기본 이메일과 이름만 읽음, 확인일 2026-11-12 |
| httpx 0.28.1 | https://www.python-httpx.org | 공급자 서버 호출, BSD |
