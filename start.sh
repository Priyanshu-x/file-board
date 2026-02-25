#!/bin/bash

# Function to wait for a hostname to be resolvable
wait_for_dns() {
    local host=$1
    echo "Waiting for DNS resolution of $host..."
    # Python-based check is more reliable across different Linux distros
    until python3 -c "import socket; socket.gethostbyname('$host')" &>/dev/null; do
        echo "DNS $host not ready yet. Retrying in 2s..."
        sleep 2
    done
    echo "DNS $host is resolvable."
}

# Extract hostnames from environment variables (defaults for local)
DB_HOST=$(python3 -c "from urllib.parse import urlparse; import os; print(urlparse(os.getenv('DATABASE_URL', 'postgresql://snapdoc:password@db:5432/snapdoc')).hostname)")
REDIS_HOST=$(python3 -c "from urllib.parse import urlparse; import os; print(urlparse(os.getenv('REDIS_URL', 'redis://redis:6379')).hostname)")

# Check DNS before starting (only if not local sqlite/memory)
if [[ $DB_HOST != "localhost" && $DB_HOST != "None" ]]; then wait_for_dns $DB_HOST; fi
if [[ $REDIS_HOST != "localhost" && $REDIS_HOST != "None" ]]; then wait_for_dns $REDIS_HOST; fi

# Start Gunicorn in the background
gunicorn -w 1 -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
         --worker-connections 1000 \
         --bind 127.0.0.1:5000 \
         app:app &
GUNICORN_PID=$!

# Wait for Gunicorn to bind
sleep 3

# Start Nginx in the background
nginx -g "daemon off;" &
NGINX_PID=$!

# Monitor processes
wait -n

# Fail-safe: if one dies, kill the other so the container restarts
kill $GUNICORN_PID $NGINX_PID 2>/dev/null
exit 1
