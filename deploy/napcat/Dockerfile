FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt requirements-onebot.txt ./
RUN pip install --no-cache-dir -r requirements-onebot.txt

COPY *.py ./

CMD ["uvicorn", "onebot_app:create_runtime_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
