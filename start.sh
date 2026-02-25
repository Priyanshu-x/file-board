#!/bin/bash

# Function to wait for a hostname with a timeout
wait_for_dns() {
    local host=$1
    local timeout=30
    local elapsed=0
    
    echo "Attempting to resolve DNS for $host (timeout: ${timeout}s)..."
    
    until python3 -c "import socket; socket.gethostbyname('$host')" &>/dev/null; do
        if [ $elapsed -ge $timeout ]; then
            echo "WARNING: DNS resolution for $host timed out after ${timeout}s. Proceeding anyway..."
            return 1
        fi
        echo "DNS $host not ready yet. Retrying in 2s... ($elapsed/${timeout}s)"
        sleep 2
        elapsed=$((elapsed + 2))
    done
    
    echo "SUCCESS: DNS $host is resolvable."
    return 0
}

# Log environment info (Sanitized)
echo "Starting application environment diagnostics..."
python3 -c "import os; from urllib.parse import urlparse; \
url = os.getenv('DATABASE_URL', ''); \
if url: \
    parsed = urlparse(url); \
    safe_url = f'{parsed.scheme}://{parsed.username}:****@{parsed.hostname}{parsed.path}'; \
    print(f'DATABASE_URL host: {parsed.hostname}'); \
else: \
    print('DATABASE_URL is not set.');"

# Extract hostnames
DB_HOST=$(python3 -c "from urllib.parse import urlparse; import os; print(urlparse(os.getenv('DATABASE_URL', '')).hostname or 'None')")
REDIS_HOST=$(python3 -c "from urllib.parse import urlparse; import os; print(urlparse(os.getenv('REDIS_URL', '')).hostname or 'None')")

# Check DNS with timeouts
if [[ $DB_HOST != "None" && $DB_HOST != "localhost" ]]; then wait_for_dns "$DB_HOST"; fi
if [[ $REDIS_HOST != "None" && $REDIS_HOST != "localhost" ]]; then wait_for_dns "$REDIS_HOST"; fi

echo "Launching Gunicorn..."
gunicorn -w 1 -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
         --worker-connections 1000 \
         --bind 127.0.0.1:5000 \
         app:app &
GUNICORN_PID=$!

sleep 3

echo "Launching Nginx..."
nginx -g "daemon off;" &
NGINX_PID=$!

# Monitor processes
wait -n

kill $GUNICORN_PID $NGINX_PID 2>/dev/null
exit 1
