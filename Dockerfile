FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install "python-telegram-bot[job-queue]"
CMD ["python", "-u", "src/bot.py"]
