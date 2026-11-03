# Section 4.10 fault tolerance demo: this image is run as several
# independent GymFlow instances behind an nginx load balancer (see
# docker-compose.yml, nginx/nginx.conf, docs/FAULT_TOLERANCE_DEMO.md).
# It is NOT how the coursework demo deployment (PythonAnywhere) is built --
# that one process is fine on its own for grading the CRUD/algorithm/ML/AI
# agent features; this image exists specifically to demonstrate
# infrastructure-level redundancy locally.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# gunicorn, not the Flask dev server: multiple worker processes per
# instance, and it's what a real deployment would run. run.py already
# exposes `app = create_app()` at module level, so it doubles as the WSGI
# entry point (gunicorn run:app) with no separate wsgi.py needed.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--access-logfile", "-", "run:app"]
