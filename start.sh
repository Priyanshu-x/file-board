#!/bin/bash

# Start Gunicorn in the background
gunicorn -w 1 -k gevent --worker-connections 1000 --bind 127.0.0.1:5000 app:app &

# Start Nginx in the foreground
nginx -g "daemon off;"
