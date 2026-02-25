#!/bin/bash

# Pre-create instance directory for SQLite fallback
mkdir -p /app/instance/uploads

# Function to wait for a hostname with a timeout
wait_for_dns() {
    local host=$1
    local timeout=15 # Shorter timeout since app has its own fallback now
    local elapsed=0
    
    echo "Diagnostic: Checking DNS for $host..."
    
    until python3 -c "import socket; socket.gethostbyname('$host')" &>/dev/null; do
        if [ $elapsed -ge $timeout ]; then
            echo "DNS WARNING: $host not resolvable. Flask will use SQLite fallback."
            return 1
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    
    echo "DNS SUCCESS: $host is ready."
    return 0
}

# Extract hostnames
DB_HOST=$(python3 -c "from urllib.parse import urlparse; import os; print(urlparse(os.getenv('DATABASE_URL', '')).hostname or 'None')")
REDIS_HOST=$(python3 -c "from urllib.parse import urlparse; import os; print(urlparse(os.getenv('REDIS_URL', '')).hostname or 'None')")

if [[ $DB_HOST != "None" && $DB_HOST != "localhost" ]]; then wait_for_dns "$DB_HOST"; fi
if [[ $REDIS_HOST != "None" && $REDIS_HOST != "localhost" ]]; then wait_for_dns "$REDIS_HOST"; fi

echo "Launching Gunicorn with process monitoring..."
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
