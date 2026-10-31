# Meeting captions (server.py): faster-whisper on the CPU. Built + started by setup_services.ps1 as the "whisper" container,
# published on 127.0.0.1:9100 only. The whisper-models volume keeps the downloaded model (large-v3-turbo, about 1.6 GB).
FROM python:3.12-slim
# requests: faster-whisper still imports it. Its own audio decoder doesn't work with today's PyAV, so server.py decodes the audio itself
RUN pip install --no-cache-dir "faster-whisper==1.2.1" requests &&useradd --create-home --uid 10001 whisper && mkdir /models && chown whisper /models
COPY server.py /app/server.py
USER whisper
ENV HF_HOME=/models WHISPER_MODEL=large-v3-turbo PYTHONUNBUFFERED=1
EXPOSE 9000
CMD ["python", "/app/server.py"]
