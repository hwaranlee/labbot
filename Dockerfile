FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
VOLUME /data
ENV DB_PATH=/data/labbot.db
EXPOSE 8787
CMD ["python", "-m", "app.main"]
