FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir pyyaml==6.0.3

COPY app.py /app/app.py
COPY config.yaml /app/config.yaml

# Unprivilegierter Benutzer
RUN useradd --system --no-create-home rapla
USER rapla

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "/app/app.py"]
