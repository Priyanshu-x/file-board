#!/bin/bash

# Start Gunicorn in the background
# We use GeventWebSocketWorker for proper SocketIO support with gevent
gunicorn -w 1 -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
         --worker-connections 1000 \
         --bind 127.0.0.1:5000 \
         app:app &
GUNICORN_PID=$!

# Wait a few seconds for Gunicorn to bind to the port
sleep 3

# Start Nginx in the background
nginx -g "daemon off;" &
NGINX_PID=$!

# Process supervision: Exit if either process dies
# wait -n returns the status of the first child process that exits
wait -n

# Kill others if one died
kill $GUNICORN_PID $NGINX_PID 2>/dev/null

exit 1
