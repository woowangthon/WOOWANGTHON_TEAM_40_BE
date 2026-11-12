#!/bin/sh
# 자리 4 보고서 편집기 ONLYOFFICE Document Server 를 띄운다. backend/.env 의 ONLYOFFICE_JWT_SECRET 을 그대로 쓴다.
#   ./scripts/onlyoffice.sh          켜기
#   ./scripts/onlyoffice.sh stop     끄기
set -e
cd "$(dirname "$0")/.."
NAME=woowang-onlyoffice
IMAGE=${ONLYOFFICE_IMAGE:-onlyoffice/documentserver:9.1}
if [ "$1" = "stop" ]; then docker rm -f "$NAME"; exit 0; fi
SECRET=$(grep '^ONLYOFFICE_JWT_SECRET=' .env | cut -d= -f2-)
if [ -z "$SECRET" ]; then echo ".env 에 ONLYOFFICE_JWT_SECRET 을 먼저 넣는다"; exit 1; fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
# ALLOW_PRIVATE_IP_ADDRESS: 같은 컴퓨터의 백엔드에서 문서를 받고 콜백을 보내야 하므로 사설 주소를 허용한다
docker run -d --name "$NAME" --restart unless-stopped -p 8080:80 \
  -e JWT_ENABLED=true -e JWT_SECRET="$SECRET" -e JWT_IN_BODY=true \
  -e ALLOW_PRIVATE_IP_ADDRESS=true -e ALLOW_META_IP_ADDRESS=true \
  --add-host host.docker.internal:host-gateway \
  "$IMAGE"
echo "ONLYOFFICE 시작 중. 1~2분 뒤 http://localhost:8080/healthcheck 가 true 를 돌려주면 준비된 것이다"
