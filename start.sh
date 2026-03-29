#!/bin/bash

cd "$(dirname "$0")"

# Start ngrok in background
ngrok http 8001 --log=stdout --log-format=json > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!

echo "Starting ngrok..."

# Wait for ngrok to come up
for i in {1..15}; do
    NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    tunnels = data.get('tunnels', [])
    for t in tunnels:
        if t.get('proto') == 'https':
            print(t['public_url'])
            break
except: pass
" 2>/dev/null)
    if [ -n "$NGROK_URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$NGROK_URL" ]; then
    echo "Error: ngrok failed to start. Check /tmp/ngrok.log"
    kill $NGROK_PID 2>/dev/null
    exit 1
fi

WEBHOOK_URL="${NGROK_URL}/webhook"
echo "ngrok tunnel: $WEBHOOK_URL"

# Update WEBHOOK_URL in .env
if grep -q "^WEBHOOK_URL=" .env; then
    sed -i '' "s|^WEBHOOK_URL=.*|WEBHOOK_URL=${WEBHOOK_URL}|" .env
else
    echo "WEBHOOK_URL=${WEBHOOK_URL}" >> .env
fi

echo "Updated .env with new webhook URL"
echo ""
echo "Set this URL in Twilio sandbox settings:"
echo "  $WEBHOOK_URL"
echo ""

# Trap Ctrl+C to kill ngrok when server stops
trap "kill $NGROK_PID 2>/dev/null; echo 'Stopped.'" EXIT

# Start the server
uvicorn main:app --host 0.0.0.0 --port 8001
