# 📁 File Board

**Live at: [https://file-board.onrender.com/](https://file-board.onrender.com/)**

A production-hardened, minimalist file sharing platform built with Flask. Designed for high performance and resilience, File Board allows for large file sharing with automated cleanup and robust backend architecture.

---

## 🚀 Key Features

*   **⚡ Chunked Uploads**: Support for files up to 1GB via Resumable/Chunked transmission.
*   **🛡️ Atomic Assembly**: Files are assembled on the server using atomic renames to prevent corruption.
*   **⏲️ Auto-Expiry**: Public files are automatically deleted after 15 minutes to preserve storage.
*   **🔒 Admin Control**: Secure admin panel to manage files, monitor disk usage, and mark files as "Permanent".
*   **🌐 Infrastructure Resilience**: 
    *   **DNS Hardening**: Handles intermittent cloud networking issues via gevent-patched resolvers.
    *   **Database Fallback**: Automatically switches to local SQLite if the primary Postgres DB is unreachable.
    *   **Redis Fallback**: Seamlessly falls back to in-memory rate limiting if Redis is down.

## 🛠️ Technology Stack

*   **Backend**: Flask, SQLAlchemy (Postgres/SQLite)
*   **Concurrency**: Gevent (Monkey-patched for async IO)
*   **Task Management**: APScheduler (Background cleanup)
*   **Real-time**: Socket.IO
*   **Reverse Proxy**: Nginx (Optimized for `X-Accel-Redirect`)
*   **Deployment**: Docker & Render

---

## 💻 Local Quickstart

### 🐳 Using Docker (Recommended)
```bash
docker-compose up --build
```
Access the app at `http://localhost:5000`.

### 🐍 Standard Setup
1. **Install Requirements**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Environment Variables**:
   - `ADMIN_USER` / `ADMIN_PASS`: Set your credentials.
   - `DATABASE_URL`: Set to Postgres or leave empty for SQLite fallback.
3. **Run**:
   ```bash
   python app.py
   ```

---

## 🔒 Security & Performance

*   **Rate Limiting**: Integrated `Flask-Limiter` with global and route-specific caps.
*   **Server-Side IDs**: Upload IDs are server-controlled to prevent enumeration attacks.
*   **Zero-Copy Serving**: Uses Nginx's `X-Accel-Redirect` to serve files efficiently without blocking Python workers.
*   **CSRF Protection**: All upload and management actions are secured via `Flask-WTF`.

---
*Built for performance. Hardened for production.*
