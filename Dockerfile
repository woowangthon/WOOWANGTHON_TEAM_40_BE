# 1단계: React 빌드
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# 2단계: FastAPI 실행. 빌드된 정적 파일을 같이 서비스한다
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# 자리 4 작업공간이 학생 코드를 호스트의 Docker 로 실행할 때 쓰는 명령줄 도구. 데몬은 넣지 않는다
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
# 보조 프로그램 설치 파일. /downloads/companion.exe 로 받는다
COPY backend/downloads ./downloads
COPY --from=web /web/dist ./frontend-dist
# 운영 환경에서는 SECRET_KEY가 32자 이상의 무작위 값이 아니면 서버가 시작하지 않는다
ENV APP_ENV=production FRONTEND_DIST=/app/frontend-dist DATABASE_URL=sqlite:////app/data/app.db MEDIA_DIR=/app/data/media
RUN mkdir -p /app/data/media
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
