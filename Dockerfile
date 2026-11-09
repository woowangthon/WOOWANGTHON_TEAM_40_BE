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
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=web /web/dist ./frontend-dist
ENV FRONTEND_DIST=/app/frontend-dist DATABASE_URL=sqlite:////app/data/app.db MEDIA_DIR=/app/data/media
RUN mkdir -p /app/data/media
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
