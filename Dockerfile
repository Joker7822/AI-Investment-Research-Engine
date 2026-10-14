FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY app /app/app
COPY config /app/config
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
