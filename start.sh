#!/bin/bash

# Start Gunicorn in the background
# We use GeventWebSocketWorker for proper SocketIO support with gevent
gunicorn -w 1 -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker --worker-connections 1000 --bind 0.0.0.0:5000 app:app &

# Wait a few seconds for Gunicorn to bind to the port
sleep 3

# Start Nginx in the foreground
nginx -g "daemon off;"
