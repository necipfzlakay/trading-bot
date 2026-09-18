FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .

COPY scripts ./scripts
COPY config ./config

# Non-root: nothing here needs privileges.
RUN useradd -m tbot && mkdir -p /app/runtime && chown -R tbot:tbot /app
USER tbot

CMD ["python", "scripts/run.py", "--config", "config/paper.yaml"]
