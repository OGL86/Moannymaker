FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m bot && mkdir -p /app/data && chown -R bot /app
USER bot
ENTRYPOINT ["python", "-m", "memebot"]
CMD ["run"]
