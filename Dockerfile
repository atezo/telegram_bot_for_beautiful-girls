FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
RUN useradd --uid 10001 --create-home bot && mkdir -p /data && chown bot:bot /data
USER bot
CMD ["python", "-m", "app.main"]
