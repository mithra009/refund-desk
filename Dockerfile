# One image, two services: the command decides which one runs (see docker-compose.yml).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /srv

# Dependencies first so code edits do not bust the layer cache.
COPY pyproject.toml ./
RUN uv pip install --system ".[risk]"

COPY backend_sim ./backend_sim
COPY data ./data
COPY evals ./evals
COPY services ./services
COPY scripts ./scripts

ENV PYTHONPATH=/srv:/srv/services/refund_desk:/srv/services \
    PYTHONUNBUFFERED=1
RUN useradd --create-home app && chown -R app /srv
USER app

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
