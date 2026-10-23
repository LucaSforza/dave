FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ARG DAVE_UID=1000
ARG DAVE_GID=1000
ARG DAVE_HOME=/home/dave
RUN groupadd --gid "${DAVE_GID}" dave && useradd --uid "${DAVE_UID}" --gid "${DAVE_GID}" --create-home --home-dir "${DAVE_HOME}" --shell /usr/sbin/nologin dave
COPY pyproject.toml uv.lock ./
COPY dave_backend ./dave_backend
RUN uv sync --locked --no-dev --no-editable
ENV HOME=${DAVE_HOME}
USER dave
EXPOSE 8000
CMD ["uvicorn", "dave_backend.api:app", "--host", "0.0.0.0", "--port", "8000"]
