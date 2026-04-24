#!/bin/bash
# ezadmin 재기동: 포트 9900 점유 프로세스를 종료하고 백그라운드로 재실행
# conda env `ez` 의 python 을 직접 호출하여 비대화형 쉘에서도 안정적으로 실행.
set -e
cd "$(dirname "$0")"

PORT=9900
PYTHON="${PYTHON:-/Users/bmchae/opt/anaconda3/envs/ez/bin/python}"

PIDS=$(lsof -ti:${PORT} 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    echo "기존 프로세스 종료: $PIDS"
    kill $PIDS 2>/dev/null || true
    sleep 1
    # graceful 실패 시 강제 종료
    LEFT=$(lsof -ti:${PORT} 2>/dev/null || true)
    if [ -n "$LEFT" ]; then
        kill -9 $LEFT 2>/dev/null || true
    fi
fi

mkdir -p logs
nohup "$PYTHON" src/app.py > logs/app.log 2>&1 &
echo "시작: PID=$!  로그: logs/app.log  포트: ${PORT}"
