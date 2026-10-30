FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /app/data/downloads /app/data/zips
COPY app.py catalog.jsonl ./
ENV PYTHONDONTWRITEBYTECODE=1 PORT=3000 PYTHONUNBUFFERED=1
EXPOSE 3000
CMD ["python3","app.py"]
