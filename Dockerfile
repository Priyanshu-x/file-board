FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    nginx \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Setup Nginx
COPY nginx.conf /etc/nginx/sites-available/default
RUN ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default

# Create uploads directory (if not exists) and set permissions
RUN mkdir -p instance/uploads && chmod 777 instance/uploads

# Make start script executable
RUN chmod +x start.sh

EXPOSE 80

CMD ["./start.sh"]
