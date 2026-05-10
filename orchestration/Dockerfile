FROM python:3.11-slim
RUN pip install --no-cache-dir kubernetes
WORKDIR /app
COPY main.py .
CMD ["python", "main.py"]
