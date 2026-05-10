FROM python:3.10-slim
RUN apt-get update && apt-get install -y ffmpeg git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements_bot.txt .
RUN pip install --no-cache-dir -r requirements_bot.txt
COPY bot_telegram.py .
CMD ["python", "bot_telegram.py"]
