#!/bin/sh
# 자리 4 작업공간의 코드 실행 이미지를 미리 받는다. 첫 실행이 느리거나 시연장 네트워크가 막혀 있을 때를 대비한다.
set -e
for image in python:3.12-slim gcc:14 eclipse-temurin:21-jdk; do
  docker pull "$image"
done
